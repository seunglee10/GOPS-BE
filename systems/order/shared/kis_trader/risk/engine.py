"""Deterministic pre-trade risk rule evaluation.

Every rule follows the same contract: given the order, context, and config it
returns a RuleResult (block / resize / warn / info) with the numbers used, or
None when it passes, or a skip marker when required data is missing. The LLM
Risk Agent only narrates these results — it can never change them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
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
    numbers: dict[str, str] = field(default_factory=dict)
    suggested_qty: Decimal | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ruleId": self.rule_id,
            "action": self.action,
            "explanation": self.explanation,
            "numbers": dict(self.numbers),
        }
        if self.suggested_qty is not None:
            payload["suggestedQty"] = str(self.suggested_qty)
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
        return RuleResult(
            rule_id="fat_finger",
            action=ACTION_BLOCK,
            explanation=(
                f"주문 금액 {notional}이 1회 상한 {config.max_order_notional}을 초과합니다. "
                "수량이나 가격 입력 실수가 아닌지 확인하세요."
            ),
            numbers={"notional": str(notional), "maxOrderNotional": str(config.max_order_notional)},
        )
    adv = context.metrics.average_daily_volume
    if adv is not None and adv > 0:
        participation = qty / adv
        if participation > config.max_adv_participation:
            return RuleResult(
                rule_id="fat_finger",
                action=ACTION_BLOCK,
                explanation=(
                    f"주문 수량이 일평균거래량의 {_pct(participation)}로 상한 "
                    f"{_pct(config.max_adv_participation)}을 초과합니다."
                ),
                numbers={
                    "qty": str(qty),
                    "averageDailyVolume": str(adv),
                    "participation": str(participation),
                    "maxAdvParticipation": str(config.max_adv_participation),
                },
            )
    last_price = context.metrics.last_price
    if last_price is not None and last_price > 0:
        deviation = abs(price - last_price) / last_price
        if deviation > config.price_band_pct:
            return RuleResult(
                rule_id="fat_finger",
                action=ACTION_BLOCK,
                explanation=(
                    f"지정가 {price}가 직전가 {last_price} 대비 {_pct(deviation)} 벗어났습니다 "
                    f"(허용 {_pct(config.price_band_pct)})."
                ),
                numbers={
                    "price": str(price),
                    "lastPrice": str(last_price),
                    "deviation": str(deviation),
                    "priceBandPct": str(config.price_band_pct),
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
            explanation=(
                f"오늘 손실 {context.daily_pnl}이 일일 한도 {limit}에 도달했습니다. "
                "손실 직후의 추가 매수는 판단이 흐려지기 쉬워 오늘은 신규 매수를 차단합니다."
            ),
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
        explanation=(
            f"이 주문 후 {symbol} 비중이 {_pct(post_trade_weight)}로 한도 "
            f"{_pct(config.single_name_max_weight)}을 초과합니다. {allowed_qty}주까지 권장합니다."
        ),
        numbers={
            "currentValue": str(current_value),
            "postTradeWeight": str(post_trade_weight),
            "singleNameMaxWeight": str(config.single_name_max_weight),
            "allowedQty": str(allowed_qty),
        },
        suggested_qty=allowed_qty,
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
        explanation=(
            f"이 주문 후 {sector} 섹터 비중이 {_pct(post_trade_exposure)}로 한도 "
            f"{_pct(config.sector_max_weight)}을 초과합니다. 같은 섹터 종목은 함께 떨어지는 경향이 있습니다."
        ),
        numbers={
            "sector": sector,
            "postTradeExposure": str(post_trade_exposure),
            "sectorMaxWeight": str(config.sector_max_weight),
        },
    )


def _pct(value: Decimal) -> str:
    return f"{(value * 100).quantize(Decimal('0.1'))}%"
