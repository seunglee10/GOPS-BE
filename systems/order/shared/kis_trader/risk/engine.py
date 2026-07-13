"""Deterministic pre-trade risk rule evaluation.

Every rule follows the same contract: given the order, context, and config it
returns a RuleResult (block / resize / warn / info) with the numbers used, or
None when it passes, or a skip marker when required data is missing. The LLM
Risk Agent only narrates these results — it can never change them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from .config import RiskConfig
from .context import RiskContext
from .portfolio import position_market_value, sector_exposures

ACTION_BLOCK = "block"
ACTION_RESIZE = "resize"
ACTION_WARN = "warn"
ACTION_INFO = "info"

VERDICT_ALLOW = "allow"
VERDICT_RESIZE = "resize"
VERDICT_BLOCK = "block"


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    action: str
    explanation: str
    title: str | None = None
    guidance: str | None = None
    numbers: dict[str, str] = field(default_factory=dict)
    suggested_qty: Decimal | None = None
    suggested_price: Decimal | None = None
    suggested_action_label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ruleId": self.rule_id,
            "action": self.action,
            "explanation": self.explanation,
            "numbers": dict(self.numbers),
        }
        if self.title:
            payload["title"] = self.title
        if self.guidance:
            payload["guidance"] = self.guidance
        if self.suggested_qty is not None:
            payload["suggestedQty"] = str(self.suggested_qty)
        if self.suggested_price is not None:
            payload["suggestedPrice"] = str(self.suggested_price)
        if self.suggested_action_label:
            payload["suggestedActionLabel"] = self.suggested_action_label
        return payload


@dataclass(frozen=True)
class SkippedRule:
    rule_id: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"ruleId": self.rule_id, "reason": self.reason}


@dataclass(frozen=True)
class PretradeVerdict:
    verdict: str
    requested_qty: Decimal
    adjusted_qty: Decimal | None
    results: tuple[RuleResult, ...]
    skipped: tuple[SkippedRule, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "requestedQty": str(self.requested_qty),
            "adjustedQty": str(self.adjusted_qty) if self.adjusted_qty is not None else None,
            "triggeredRules": [result.to_dict() for result in self.results],
            "skippedRules": [item.to_dict() for item in self.skipped],
        }


def evaluate_pretrade(
    *,
    side: str,
    symbol: str,
    qty: Decimal,
    price: Decimal,
    context: RiskContext,
    config: RiskConfig | None = None,
) -> PretradeVerdict:
    config = config or RiskConfig()
    normalized_side = side.strip().lower()
    normalized_symbol = symbol.strip().upper()
    results: list[RuleResult] = []
    skipped: list[SkippedRule] = []

    checks = (
        _check_fat_finger,
        _check_daily_loss_cooldown,
        _check_daily_buy_budget,
        _check_single_name_limit,
        _check_sector_limit,
    )
    for check in checks:
        outcome = check(normalized_side, normalized_symbol, qty, price, context, config)
        if outcome is None:
            continue
        if isinstance(outcome, SkippedRule):
            skipped.append(outcome)
        else:
            results.append(outcome)

    return _aggregate(qty, results, skipped)


def _aggregate(
    qty: Decimal,
    results: list[RuleResult],
    skipped: list[SkippedRule],
) -> PretradeVerdict:
    if any(result.action == ACTION_BLOCK for result in results):
        return PretradeVerdict(VERDICT_BLOCK, qty, None, tuple(results), tuple(skipped))
    resize_suggestions = [
        result.suggested_qty
        for result in results
        if result.action == ACTION_RESIZE and result.suggested_qty is not None
    ]
    if resize_suggestions:
        adjusted = min(resize_suggestions)
        if adjusted <= 0:
            return PretradeVerdict(VERDICT_BLOCK, qty, None, tuple(results), tuple(skipped))
        return PretradeVerdict(VERDICT_RESIZE, qty, adjusted, tuple(results), tuple(skipped))
    return PretradeVerdict(VERDICT_ALLOW, qty, qty, tuple(results), tuple(skipped))


# --- individual rules -------------------------------------------------------


BUDGET_WARN_RATIO = Decimal("0.8")


def _check_daily_buy_budget(
    side: str,
    symbol: str,
    qty: Decimal,
    price: Decimal,
    context: RiskContext,
    config: RiskConfig,
) -> RuleResult | SkippedRule | None:
    """사용자 옵트인 자기구속 장치 — 오늘 매수 누적액이 예산을 넘지 않게.

    예산 미설정(None)이면 침묵. 손실 여부와 무관하게 '쓰기로 한 만큼만'을
    지켜주는 의도 기반 차단이라 daily_loss_cooldown(결과 기반)과 보완 관계.
    """
    if side != "buy":
        return None
    budget = config.daily_buy_budget
    if budget is None:
        return None
    spent = context.daily_buy_notional
    if spent is None:
        return SkippedRule("daily_buy_budget", "today's accumulated buy amount unavailable")
    post_trade = spent + qty * price
    if post_trade > budget:
        remaining = budget - spent
        allowed_qty = (
            (remaining / price).to_integral_value(rounding=ROUND_FLOOR)
            if remaining > 0 and price > 0
            else Decimal("0")
        )
        return RuleResult(
            rule_id="daily_buy_budget",
            action=ACTION_BLOCK,
            title="오늘 설정한 매수 예산을 초과합니다",
            explanation=(
                f"오늘 매수 예산은 {_money(budget)}이며, 이미 {_money(spent)}을 사용했습니다. "
                f"이번 주문 {_money(qty * price)}을 더하면 예산을 초과합니다."
            ),
            guidance=(
                f"남은 예산 {_money(remaining if remaining > 0 else Decimal('0'))} 안에서 수량을 줄이거나 "
                "다음 거래일에 다시 주문할 수 있습니다."
            ),
            numbers={
                "dailyBuyBudget": str(budget),
                "spentToday": str(spent),
                "orderNotional": str(qty * price),
                "remaining": str(remaining if remaining > 0 else Decimal("0")),
            },
            suggested_qty=allowed_qty if allowed_qty > 0 else None,
            suggested_action_label=(f"{allowed_qty}주로 줄이기" if allowed_qty > 0 else None),
        )
    if post_trade >= budget * BUDGET_WARN_RATIO:
        return RuleResult(
            rule_id="daily_buy_budget",
            action=ACTION_WARN,
            title="오늘 매수 예산의 대부분을 사용합니다",
            explanation=(
                f"이 주문까지 포함하면 오늘 예산 {_money(budget)}의 {_pct(post_trade / budget)}를 사용하게 됩니다."
            ),
            guidance=f"주문 후 남는 매수 예산은 {_money(budget - post_trade)}입니다. 주문은 그대로 진행할 수 있습니다.",
            numbers={
                "dailyBuyBudget": str(budget),
                "spentAfterOrder": str(post_trade),
                "usageRatio": str(post_trade / budget),
            },
        )
    return None


def _check_fat_finger(
    side: str,
    symbol: str,
    qty: Decimal,
    price: Decimal,
    context: RiskContext,
    config: RiskConfig,
) -> RuleResult | SkippedRule | None:
    notional = qty * price
    if notional > config.max_order_notional:
        allowed_qty = (config.max_order_notional / price).to_integral_value(rounding=ROUND_FLOOR)
        return RuleResult(
            rule_id="fat_finger",
            action=ACTION_BLOCK,
            title="1회 주문 금액 한도를 초과합니다",
            explanation=(
                f"이번 주문 금액은 {_money(notional)}으로, 입력 실수 방지를 위한 1회 주문 한도 "
                f"{_money(config.max_order_notional)}을 넘습니다."
            ),
            guidance=f"가격을 유지하려면 {allowed_qty}주 이하로 수량을 조정해야 합니다.",
            numbers={"notional": str(notional), "maxOrderNotional": str(config.max_order_notional)},
            suggested_qty=allowed_qty if allowed_qty > 0 else None,
            suggested_action_label=(f"{allowed_qty}주로 줄이기" if allowed_qty > 0 else None),
        )
    adv = context.metrics.average_daily_volume
    if adv is not None and adv > 0:
        participation = qty / adv
        if participation > config.max_adv_participation:
            allowed_qty = (adv * config.max_adv_participation).to_integral_value(rounding=ROUND_FLOOR)
            return RuleResult(
                rule_id="fat_finger",
                action=ACTION_BLOCK,
                title="평소 거래량에 비해 주문 수량이 많습니다",
                explanation=(
                    f"이번 주문 {qty}주는 최근 20거래일 하루 평균 거래량 {adv}주의 "
                    f"{_pct(participation)}에 해당합니다."
                ),
                guidance=(
                    "거래가 적은 종목에 큰 주문을 내면 가격이 크게 움직이거나 일부만 체결될 수 있습니다. "
                    f"{allowed_qty}주 이하로 줄이면 입력 실수 방지 기준 {_pct(config.max_adv_participation)} 안에 들어옵니다."
                ),
                numbers={
                    "qty": str(qty),
                    "averageDailyVolume": str(adv),
                    "participation": str(participation),
                    "maxAdvParticipation": str(config.max_adv_participation),
                },
                suggested_qty=allowed_qty if allowed_qty > 0 else None,
                suggested_action_label=(f"{allowed_qty}주로 줄이기" if allowed_qty > 0 else None),
            )
    last_price = context.metrics.last_price
    if last_price is not None and last_price > 0:
        signed_deviation = (price - last_price) / last_price
        deviation = abs(signed_deviation)
        is_aggressive = (
            (side == "buy" and signed_deviation > config.price_band_pct)
            or (side == "sell" and signed_deviation < -config.price_band_pct)
        )
        is_passive = (
            (side == "buy" and signed_deviation < -config.price_band_pct)
            or (side == "sell" and signed_deviation > config.price_band_pct)
        )
        reference_label = _reference_price_label(context)
        if is_aggressive:
            if side == "buy":
                safe_price = (last_price * (Decimal("1") + config.price_band_pct)).quantize(
                    Decimal("0.01"), rounding=ROUND_FLOOR
                )
                title = "매수 가격 확인이 필요합니다"
                risk_text = "가격 입력 오류가 있으면 예상보다 비싸게 매수될 수 있습니다."
            else:
                safe_price = (last_price * (Decimal("1") - config.price_band_pct)).quantize(
                    Decimal("0.01"), rounding=ROUND_CEILING
                )
                title = "매도 가격 확인이 필요합니다"
                risk_text = "가격 입력 오류가 있으면 예상보다 낮은 가격에 매도될 수 있습니다."
            return RuleResult(
                rule_id="fat_finger",
                action=ACTION_WARN,
                title=title,
                explanation=(
                    f"입력한 {'매수' if side == 'buy' else '매도'} 가격 {_money(price)}은 "
                    f"{reference_label} {_money(last_price)}보다 {_pct(deviation)} "
                    f"{'높습니다' if side == 'buy' else '낮습니다'}."
                ),
                guidance=(
                    f"{risk_text} {_money(safe_price)} "
                    f"{'이하' if side == 'buy' else '이상'}로 수정하면 가격 보호 기준 {_pct(config.price_band_pct)} 이내가 됩니다. "
                    "경고를 확인한 후 현재 입력한 가격으로 주문을 계속할 수도 있습니다."
                ),
                numbers={
                    "price": str(price),
                    "lastPrice": str(last_price),
                    "deviation": str(deviation),
                    "signedDeviation": str(signed_deviation),
                    "priceBandPct": str(config.price_band_pct),
                    "priceSource": str(context.metrics.price_source or ""),
                    "priceObservedAt": str(context.metrics.price_observed_at or ""),
                    "safePrice": str(safe_price),
                },
            )
        if is_passive:
            return RuleResult(
                rule_id="fat_finger",
                action=ACTION_WARN,
                title=f"{reference_label}보다 {'낮은 매수 가격입니다' if side == 'buy' else '높은 매도 가격입니다'}",
                explanation=(
                    f"입력한 {'매수' if side == 'buy' else '매도'} 가격 {_money(price)}은 "
                    f"{reference_label} {_money(last_price)}보다 {_pct(deviation)} "
                    f"{'낮습니다' if side == 'buy' else '높습니다'}."
                ),
                guidance=(
                    "원하는 가격이 될 때까지 기다리는 주문이라면 그대로 진행할 수 있습니다. "
                    "다만 시장가격이 이 가격에 도달하지 않으면 주문이 체결되지 않을 수 있습니다."
                ),
                numbers={
                    "price": str(price),
                    "lastPrice": str(last_price),
                    "deviation": str(deviation),
                    "signedDeviation": str(signed_deviation),
                    "priceBandPct": str(config.price_band_pct),
                    "priceSource": str(context.metrics.price_source or ""),
                    "priceObservedAt": str(context.metrics.price_observed_at or ""),
                },
            )
    return None


def _check_daily_loss_cooldown(
    side: str,
    symbol: str,
    qty: Decimal,
    price: Decimal,
    context: RiskContext,
    config: RiskConfig,
) -> RuleResult | SkippedRule | None:
    if side != "buy":
        return None
    if context.daily_pnl is None or context.account_equity is None or context.account_equity <= 0:
        return SkippedRule("daily_loss_cooldown", "daily pnl or account equity unavailable")
    limit = -(context.account_equity * config.daily_loss_limit_pct)
    if context.daily_pnl <= limit:
        return RuleResult(
            rule_id="daily_loss_cooldown",
            action=ACTION_BLOCK,
            title="오늘 신규 매수가 제한됩니다",
            explanation=(
                f"오늘 손실은 {_money(abs(context.daily_pnl))}으로, 설정한 일일 손실 보호 한도 "
                f"{_money(abs(limit))}에 도달했습니다."
            ),
            guidance="추가 매수만 오늘까지 제한됩니다. 보유 종목 매도는 가능하며, 제한은 다음 거래일에 다시 계산됩니다.",
            numbers={
                "dailyPnl": str(context.daily_pnl),
                "dailyLossLimit": str(limit),
                "dailyLossLimitPct": str(config.daily_loss_limit_pct),
            },
        )
    return None


def _check_single_name_limit(
    side: str,
    symbol: str,
    qty: Decimal,
    price: Decimal,
    context: RiskContext,
    config: RiskConfig,
) -> RuleResult | SkippedRule | None:
    if side != "buy":
        return None
    equity = context.account_equity
    if equity is None or equity <= 0:
        return SkippedRule("single_name_limit", "account equity unavailable")
    current_value = position_market_value(context.positions, symbol)
    post_trade_value = current_value + qty * price
    post_trade_weight = post_trade_value / equity
    if post_trade_weight <= config.single_name_max_weight:
        return None
    allowed_value = equity * config.single_name_max_weight - current_value
    allowed_qty = (allowed_value / price).to_integral_value(rounding=ROUND_FLOOR) if allowed_value > 0 else Decimal("0")
    return RuleResult(
        rule_id="single_name_limit",
        action=ACTION_RESIZE,
        title=f"{symbol} 한 종목의 투자 비중이 높습니다",
        explanation=(
            f"이 주문 후 {symbol}이 계좌에서 차지하는 비율은 {_pct(post_trade_weight)}가 되어, "
            f"설정한 한도 {_pct(config.single_name_max_weight)}을 넘습니다."
        ),
        guidance=(
            f"한 종목의 가격이 크게 움직이면 계좌 전체 손익에 미치는 영향도 커질 수 있습니다. "
            f"{allowed_qty}주까지 줄이면 설정한 한도 안에 들어옵니다."
        ),
        numbers={
            "currentValue": str(current_value),
            "postTradeWeight": str(post_trade_weight),
            "singleNameMaxWeight": str(config.single_name_max_weight),
            "allowedQty": str(allowed_qty),
        },
        suggested_qty=allowed_qty,
        suggested_action_label=(f"{allowed_qty}주로 줄이기" if allowed_qty > 0 else None),
    )


def _check_sector_limit(
    side: str,
    symbol: str,
    qty: Decimal,
    price: Decimal,
    context: RiskContext,
    config: RiskConfig,
) -> RuleResult | SkippedRule | None:
    if side != "buy":
        return None
    equity = context.account_equity
    if equity is None or equity <= 0:
        return SkippedRule("sector_limit", "account equity unavailable")
    sector = context.metrics.sector or config.sector_map.get(symbol)
    if not sector:
        return SkippedRule("sector_limit", "sector unknown for symbol")
    exposures = sector_exposures(context.positions, equity, config.sector_map)
    post_trade_exposure = exposures.get(sector, Decimal("0")) + (qty * price) / equity
    if post_trade_exposure <= config.sector_max_weight:
        return None
    return RuleResult(
        rule_id="sector_limit",
        action=ACTION_WARN,
        title=f"{sector} 업종의 투자 비중이 높습니다",
        explanation=(
            f"이 주문 후 {sector} 업종이 계좌에서 차지하는 비율은 {_pct(post_trade_exposure)}가 되어, "
            f"설정한 한도 {_pct(config.sector_max_weight)}을 넘습니다."
        ),
        guidance="같은 업종의 종목은 비슷한 원인으로 함께 움직일 수 있어 분산 효과가 줄어들 수 있습니다. 주문은 그대로 진행할 수 있습니다.",
        numbers={
            "sector": sector,
            "postTradeExposure": str(post_trade_exposure),
            "sectorMaxWeight": str(config.sector_max_weight),
        },
    )


def _pct(value: Decimal) -> str:
    return f"{(value * 100).quantize(Decimal('0.1'))}%"


def _money(value: Decimal) -> str:
    return f"${value.quantize(Decimal('0.01')):,.2f}"


def _reference_price_label(context: RiskContext) -> str:
    source = str(context.metrics.price_source or "").lower()
    if source in {"redis_live", "live", "redis", "live_quote"}:
        return "실시간 시장가격"
    if source == "daily_close":
        return "최근 종가"
    return "최근 기준가격"
