"""Streaming risk monitor — the always-on counterpart of the pre-trade engine.

Consumes closed candles (market.layer.candles.*.closed.v1) and order fills
(orders.fills.v1), keeps per-account position state in process memory, and
emits defensive risk events. Deterministic rules only; the LLM Risk Agent can
narrate these events but never produces them.

Data minimization: state holds account alias, symbol, quantity, and prices —
nothing else ever enters this module.

Known approximations (documented on purpose):
- Daily PnL = realized-today + sum(qty * (last - day anchor)), where the day
  anchor is the first price observed for the symbol that day (or the candle
  open). True prev-close anchoring arrives when 1d candles flow in.
- Account equity is optional configuration (RISK_MONITOR_EQUITY). Equity-based
  rules stay silent when it is unavailable instead of guessing a value.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..contracts import MarketEvent

CANDLE_TOPIC_PREFIX = "market.layer.candles."
DEFAULT_FILLS_TOPIC = "orders.fills.v1"

TOAST_SEVERITIES = {"watch", "alert", "critical"}


@dataclass
class RiskMonitorThresholds:
    daily_loss_limit_pct: float = 0.03
    single_name_max_weight: float = 0.20
    concentration_reset_ratio: float = 0.95
    adv_window: int = 20
    min_adv_samples: int = 5
    # correlation cluster
    correlation_threshold: float = 0.8
    cluster_max_weight: float = 0.40
    correlation_window: int = 20
    min_correlation_samples: int = 10
    # anomaly surge (세력 신호 — 경고 전용, 조작 단정 금지)
    surge_min_change: float = 0.08
    surge_volume_multiple: float = 5.0


@dataclass
class Position:
    qty: float = 0.0
    avg_price: float = 0.0


@dataclass
class _SymbolStats:
    prev_close: float | None = None
    volumes: deque = field(default_factory=deque)
    returns: deque = field(default_factory=deque)

    def adv(self, min_samples: int) -> float | None:
        if len(self.volumes) < min_samples:
            return None
        return sum(self.volumes) / len(self.volumes)


class RiskMonitor:
    def __init__(
        self,
        *,
        thresholds: RiskMonitorThresholds | None = None,
        account_equity: float | None = None,
        fills_topic: str = DEFAULT_FILLS_TOPIC,
    ) -> None:
        self.thresholds = thresholds or RiskMonitorThresholds()
        self.account_equity = account_equity
        self.fills_topic = fills_topic
        self.positions: dict[str, dict[str, Position]] = {}
        self.realized_pnl_today: dict[str, float] = {}
        self.last_price: dict[str, float] = {}
        self.day_anchor: dict[str, float] = {}
        self.current_day: str | None = None
        self._stats: dict[str, _SymbolStats] = {}
        self._fired: set[tuple[Any, ...]] = set()

    # --- entry point ---------------------------------------------------------

    def handle(self, payload: dict[str, Any], topic: str) -> list[dict[str, Any]]:
        if topic == self.fills_topic:
            return self._handle_fill(payload, topic)
        if topic.startswith(CANDLE_TOPIC_PREFIX):
            return self._handle_candle(payload, topic)
        return []

    # --- fills ---------------------------------------------------------------

    def _handle_fill(self, envelope: dict[str, Any], topic: str) -> list[dict[str, Any]]:
        fill = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else envelope
        account = str(envelope.get("account_alias") or fill.get("account_alias") or "").strip()
        symbol = str(fill.get("symbol") or "").strip().upper()
        side = str(fill.get("side") or "").strip().lower()
        qty = _as_float(fill.get("qty"))
        price = _as_float(fill.get("price"))
        if not account or not symbol or side not in {"buy", "sell"} or not qty or not price or qty <= 0 or price <= 0:
            return []

        book = self.positions.setdefault(account, {})
        position = book.setdefault(symbol, Position())

        if side == "buy":
            total_cost = position.avg_price * position.qty + price * qty
            position.qty += qty
            position.avg_price = total_cost / position.qty
        else:
            sold = min(qty, position.qty) if position.qty > 0 else qty
            if position.qty > 0:
                self.realized_pnl_today[account] = (
                    self.realized_pnl_today.get(account, 0.0) + (price - position.avg_price) * sold
                )
            position.qty -= qty
            if position.qty <= 0:
                book.pop(symbol, None)
                self._fired.discard(("concentration", account, symbol))
        return []

    # --- candles -------------------------------------------------------------

    def _handle_candle(self, payload: dict[str, Any], topic: str) -> list[dict[str, Any]]:
        symbol = str(payload.get("symbol") or "").strip().upper()
        close = _first_float(payload, "close", "price", "lastPrice")
        if not symbol or close is None or close <= 0:
            return []
        open_price = _first_float(payload, "open")
        volume = _first_float(payload, "volume", "size")
        timestamp = str(payload.get("timestamp") or payload.get("eventTime") or payload.get("updatedAt") or "")

        self._roll_day_if_needed(timestamp)
        self.last_price[symbol] = close
        self.day_anchor.setdefault(symbol, open_price if open_price and open_price > 0 else close)
        prior_adv = self._stats.get(symbol, _SymbolStats()).adv(self.thresholds.min_adv_samples)
        self._update_stats(symbol, close, volume)

        events: list[dict[str, Any]] = []
        events.extend(self._check_anomaly_surge(symbol, close, volume, prior_adv, topic))
        for account, book in self.positions.items():
            position = book.get(symbol)
            if position is None or position.qty <= 0:
                continue
            events.extend(self._check_concentration(account, symbol, position, close, topic))
            events.extend(self._check_correlation_cluster(account, book, symbol, topic))
            events.extend(self._check_daily_loss(account, topic))
        return events

    def _roll_day_if_needed(self, timestamp: str) -> None:
        day = timestamp[:10] if len(timestamp) >= 10 else None
        if day is None:
            return
        if self.current_day is None:
            self.current_day = day
            return
        if day != self.current_day:
            self.current_day = day
            self.realized_pnl_today.clear()
            self.day_anchor.clear()
            self._fired = {key for key in self._fired if key[0] != "daily_loss"}

    def _update_stats(self, symbol: str, close: float, volume: float | None) -> None:
        stats = self._stats.setdefault(symbol, _SymbolStats())
        if stats.volumes.maxlen != self.thresholds.adv_window:
            stats.volumes = deque(stats.volumes, maxlen=self.thresholds.adv_window)
            stats.returns = deque(stats.returns, maxlen=self.thresholds.correlation_window)
        if stats.prev_close is not None and stats.prev_close > 0:
            stats.returns.append(close / stats.prev_close - 1.0)
        stats.prev_close = close
        if volume is not None and volume > 0:
            stats.volumes.append(volume)

    # --- rules ---------------------------------------------------------------

    def _check_concentration(self, account: str, symbol: str, position: Position, close: float, topic: str) -> list[dict[str, Any]]:
        if not self.account_equity or self.account_equity <= 0:
            return []
        weight = position.qty * close / self.account_equity
        key = ("concentration", account, symbol)
        limit = self.thresholds.single_name_max_weight
        if weight <= limit * self.thresholds.concentration_reset_ratio:
            self._fired.discard(key)
            return []
        if weight <= limit or key in self._fired:
            return []
        self._fired.add(key)
        return [self._event(
            symbol=symbol,
            event_type="risk_concentration_drift",
            severity="alert",
            source_topic=topic,
            summary=(
                f"{symbol}이 계좌에서 차지하는 비율이 {weight * 100:.1f}%로, 설정한 한도 {limit * 100:.0f}%를 초과했습니다. "
                "한 종목의 가격이 크게 움직이면 계좌 전체 손익에 미치는 영향도 커질 수 있습니다. 보유 비중 확인이 필요합니다."
            ),
            metrics={"account": account, "weight": round(weight, 4), "limit": limit, "price": close, "qty": position.qty},
            ui_proposals=[{"panelType": "portfolioDiversification", "action": "open"}],
        )]

    def _check_daily_loss(self, account: str, topic: str) -> list[dict[str, Any]]:
        if not self.account_equity or self.account_equity <= 0:
            return []
        key = ("daily_loss", account, self.current_day)
        if key in self._fired:
            return []
        pnl = self.daily_pnl(account)
        limit = -self.account_equity * self.thresholds.daily_loss_limit_pct
        if pnl > limit:
            return []
        self._fired.add(key)
        return [self._event(
            symbol="PORTFOLIO",
            event_type="risk_daily_loss_limit",
            severity="critical",
            source_topic=topic,
            summary=(
                f"오늘 손실이 {abs(pnl):,.0f}로, 설정한 일일 손실 보호 한도 {abs(limit):,.0f}에 도달했습니다. "
                "오늘은 추가 매수만 제한되며 보유 종목 매도는 가능합니다."
            ),
            metrics={"account": account, "dailyPnl": round(pnl, 2), "dailyLossLimit": round(limit, 2)},
            ui_proposals=[{"panelType": "portfolioPerformance", "action": "open"}],
        )]

    def _check_anomaly_surge(
        self,
        symbol: str,
        close: float,
        volume: float | None,
        prior_adv: float | None,
        topic: str,
    ) -> list[dict[str, Any]]:
        """세력 신호 — 이상 급등 경고 (조작 단정 금지, 추격 매수 주의 프레임)."""
        anchor = self.day_anchor.get(symbol)
        if not anchor or anchor <= 0 or volume is None or prior_adv is None or prior_adv <= 0:
            return []
        change = close / anchor - 1.0
        volume_multiple = volume / prior_adv
        if change < self.thresholds.surge_min_change or volume_multiple < self.thresholds.surge_volume_multiple:
            return []
        key = ("anomaly_surge", symbol, self.current_day)
        if key in self._fired:
            return []
        self._fired.add(key)
        return [self._event(
            symbol=symbol,
            event_type="risk_anomaly_surge",
            severity="watch",
            source_topic=topic,
            summary=(
                f"{symbol}이(가) 오늘 {change * 100:.1f}% 급등 중이며 거래량은 평소의 {volume_multiple:.1f}배입니다. "
                "평소와 다른 움직임이므로 관련 뉴스와 공시를 확인하기 전에는 가격 변동 위험에 주의해야 합니다. "
                "이 알림만으로 시세조종이나 향후 주가 방향을 판단할 수는 없습니다."
            ),
            metrics={
                "changeFromAnchor": round(change, 4),
                "volumeMultiple": round(volume_multiple, 2),
                "price": close,
                "anchorPrice": anchor,
            },
            ui_proposals=[
                {"panelType": "chart", "action": "focus", "symbol": symbol},
                {"panelType": "newsFeed", "action": "open", "symbol": symbol},
            ],
        )]

    def _check_correlation_cluster(
        self,
        account: str,
        book: dict[str, Position],
        symbol: str,
        topic: str,
    ) -> list[dict[str, Any]]:
        if not self.account_equity or self.account_equity <= 0:
            return []
        base_returns = list(self._stats.get(symbol, _SymbolStats()).returns)
        if len(base_returns) < self.thresholds.min_correlation_samples:
            return []
        cluster = [symbol]
        for other, position in book.items():
            if other == symbol or position.qty <= 0:
                continue
            other_returns = list(self._stats.get(other, _SymbolStats()).returns)
            if len(other_returns) < self.thresholds.min_correlation_samples:
                continue
            correlation = _pearson(base_returns, other_returns)
            if correlation is not None and correlation >= self.thresholds.correlation_threshold:
                cluster.append(other)
        if len(cluster) < 2:
            return []
        cluster_value = sum(
            book[member].qty * self.last_price.get(member, 0.0)
            for member in cluster
            if member in book
        )
        weight = cluster_value / self.account_equity
        key = ("correlation", account, tuple(sorted(cluster)))
        limit = self.thresholds.cluster_max_weight
        if weight <= limit * self.thresholds.concentration_reset_ratio:
            self._fired.discard(key)
            return []
        if weight <= limit or key in self._fired:
            return []
        self._fired.add(key)
        names = ", ".join(sorted(cluster))
        return [self._event(
            symbol=symbol,
            event_type="risk_correlation_cluster",
            severity="alert",
            source_topic=topic,
            summary=(
                f"{names}이(가) 최근 비슷하게 움직이고 있습니다 (상관계수 {self.thresholds.correlation_threshold:.1f} 이상). "
                f"이 종목들을 합치면 계좌의 {weight * 100:.1f}%로, 설정한 묶음 한도 {limit * 100:.0f}%를 넘습니다. "
                "여러 종목을 보유해도 함께 움직이면 분산 효과가 줄어들 수 있습니다."
            ),
            metrics={
                "account": account,
                "cluster": sorted(cluster),
                "clusterWeight": round(weight, 4),
                "limit": limit,
                "correlationThreshold": self.thresholds.correlation_threshold,
            },
            ui_proposals=[{"panelType": "portfolioDiversification", "action": "open"}],
        )]

    # --- helpers -------------------------------------------------------------

    def daily_pnl(self, account: str) -> float:
        realized = self.realized_pnl_today.get(account, 0.0)
        unrealized = 0.0
        for symbol, position in self.positions.get(account, {}).items():
            last = self.last_price.get(symbol)
            anchor = self.day_anchor.get(symbol)
            if last is not None and anchor is not None:
                unrealized += position.qty * (last - anchor)
        return realized + unrealized

    def _event(
        self,
        *,
        symbol: str,
        event_type: str,
        severity: str,
        source_topic: str,
        summary: str,
        metrics: dict[str, Any],
        ui_proposals: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        event = MarketEvent.from_payload(
            symbol=symbol,
            event_type=event_type,
            severity=severity,
            source_topic=source_topic,
            summary=summary,
            metrics={**metrics, "uiProposals": ui_proposals or []},
        )
        payload = event.to_dict()
        payload["level"] = severity
        payload["showToast"] = severity in TOAST_SEVERITIES
        return payload


def _pearson(left: list[float], right: list[float]) -> float | None:
    """Pearson correlation over the trailing overlap of two return series.

    Series are aligned by position from the end (count alignment, not
    timestamp alignment) — a documented approximation that holds when both
    symbols stream the same candle cadence.
    """
    size = min(len(left), len(right))
    if size < 2:
        return None
    a = left[-size:]
    b = right[-size:]
    mean_a = sum(a) / size
    mean_b = sum(b) / size
    var_a = sum((value - mean_a) ** 2 for value in a)
    var_b = sum((value - mean_b) ** 2 for value in b)
    if var_a <= 0 or var_b <= 0:
        return None
    covariance = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(size))
    return covariance / (var_a ** 0.5 * var_b ** 0.5)


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_float(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in payload:
            parsed = _as_float(payload[key])
            if parsed is not None:
                return parsed
    return None
