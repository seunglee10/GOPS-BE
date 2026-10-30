"""투자 복기 월간 리포트 (D27).

현재 계좌 세대의 페이퍼 체결을 처음부터 다시 쌓아 매도 시점의 평균 단가를 구하고
(페이퍼 보유 종목과 같은 평균 단가 방식), 한 달 안에 체결된 주문을 주문 단위로 묶어
손익·습관·판 뒤 주가를 계산한다.

지표는 모두 돈과 이어진 숫자다. 체크리스트를 몇 개 채웠는지 같은 점수는 없다.
응답 문구에 앞으로의 주가 전망이나 사고팔라는 권유는 넣지 않는다. 과거 가격과 손익만 싣는다.

용어
- pnl: 판 주문의 확정 손익. (체결가 - 매도 직전 평균 단가) x 수량. 산 주문은 null.
- evalPnl: 주문 손익. 판 주문은 pnl, 산 주문은 (최근 종가 - 체결가) x 수량.
  목업의 "판 주문은 확정 손익, 들고 있는 주문은 오늘 가격 기준" 과 같다.
- missedProfit: 판 주문만. max(0, 판 뒤 5거래일 종가 최고값 - 판 가격) x 수량.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from app.review.market import ReviewMarketData, parse_datetime
from app.services.instrument_names import name_ko

NEW_YORK = ZoneInfo("America/New_York")
MONTH_PATTERN = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")

HABITS = ("plan", "exit", "news", "rush", "chase", "avg")
HABIT_LABELS_KO = {
    "plan": "계획한 주문",
    "exit": "수익 내고 판 주문",
    "news": "뉴스 보고 바로 낸 주문",
    "rush": "장 시작 직후 시장가 주문",
    "chase": "급등일 추격 매수",
    "avg": "손실 중 물타기",
}
MISTAKE_HABITS = frozenset({"chase", "avg"})

CHASE_DAY_CHANGE_PERCENT = 3.0
RUSH_WINDOW = timedelta(minutes=10)
NEWS_WINDOW = timedelta(hours=1)
MISSED_PROFIT_TRADING_DAYS = 5
MARKET_OPEN = time(9, 30)
BENCHMARK_SYMBOLS = ("^GSPC", "SPY")
# 판 뒤 5거래일, 직전 거래일 종가를 넉넉히 덮는 달력일 여유
CANDLE_LOOKBACK_DAYS = 10


class InvalidMonth(ValueError):
    pass


def parse_month(value: str | None, now: datetime) -> tuple[date, date]:
    """`YYYY-MM` 을 [그달 1일, 다음달 1일) 로. 비면 뉴욕 기준 이번 달."""
    if not value:
        local = now.astimezone(NEW_YORK)
        start = date(local.year, local.month, 1)
    else:
        match = MONTH_PATTERN.fullmatch(value.strip())
        if not match:
            raise InvalidMonth(value)
        start = date(int(match.group(1)), int(match.group(2)), 1)
    return start, _next_month(start)


def _next_month(day: date) -> date:
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def _previous_month(day: date) -> date:
    return date(day.year - (day.month == 1), (day.month - 2) % 12 + 1, 1)


@dataclass
class _Order:
    order_id: str
    symbol: str
    side: str
    order_type: str
    memo: str | None
    ordered_at: datetime
    first_at: datetime
    qty: Decimal = Decimal("0")
    notional: Decimal = Decimal("0")
    realized: Decimal | None = Decimal("0")
    avg_cost: Decimal | None = None
    held_qty_before: Decimal = Decimal("0")
    avg_before: Decimal = Decimal("0")
    executions: int = 0

    @property
    def price(self) -> Decimal:
        return self.notional / self.qty if self.qty else Decimal("0")


@dataclass
class _Position:
    qty: Decimal = Decimal("0")
    avg: Decimal = Decimal("0")


def replay_orders(executions: Iterable[dict[str, Any]]) -> list[_Order]:
    """체결을 오래된 순으로 쌓아 주문별 수량·평균가·확정 손익을 만든다.

    평균 단가는 페이퍼 보유 종목과 같은 이동평균 방식이다. 매수는 평균을 다시 내고,
    매도는 평균을 바꾸지 않은 채 (체결가 - 평균) x 수량 을 확정 손익으로 잡는다.
    보유 수량보다 많이 판 기록(예전 세대의 보유 등)은 평균 단가를 모르므로 손익을 비운다.
    """
    rows = sorted(
        (row for row in executions if isinstance(row, dict)),
        key=lambda row: (parse_datetime(row.get("executed_at")) or datetime.min.replace(tzinfo=timezone.utc), str(row.get("execution_id") or "")),
    )
    positions: dict[str, _Position] = {}
    orders: dict[str, _Order] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        side = str(row.get("side") or "").lower()
        qty = _decimal(row.get("quantity"))
        price = _decimal(row.get("price"))
        executed_at = parse_datetime(row.get("executed_at"))
        order_id = str(row.get("order_id") or "")
        if not symbol or side not in {"buy", "sell"} or not qty or qty <= 0 or price is None or executed_at is None or not order_id:
            continue
        position = positions.setdefault(symbol, _Position())
        order = orders.get(order_id)
        if order is None:
            order = _Order(
                order_id=order_id,
                symbol=symbol,
                side=side,
                order_type=str(row.get("order_type") or "limit").lower(),
                memo=row.get("memo") or None,
                ordered_at=parse_datetime(row.get("order_created_at")) or executed_at,
                first_at=executed_at,
                held_qty_before=position.qty,
                avg_before=position.avg,
            )
            orders[order_id] = order
        order.qty += qty
        order.notional += qty * price
        order.executions += 1
        if side == "buy":
            total = position.qty + qty
            position.avg = ((position.qty * position.avg) + qty * price) / total
            position.qty = total
            continue
        if position.qty < qty:
            order.realized = None
            position.qty = max(Decimal("0"), position.qty - qty)
            continue
        if order.avg_cost is None:
            order.avg_cost = position.avg
        if order.realized is not None:
            order.realized += (price - position.avg) * qty
        position.qty -= qty
        if position.qty == 0:
            position.avg = Decimal("0")
    return sorted(orders.values(), key=lambda order: (order.first_at, order.order_id))


class _Closes:
    """종목별 일봉 종가를 한 번만 읽어 두고 날짜로 찾는다."""

    def __init__(self, market: ReviewMarketData, start: date, end: date) -> None:
        self._market = market
        self._start = start
        self._end = end
        self._cache: dict[str, list[tuple[date, float]]] = {}

    def series(self, symbol: str) -> list[tuple[date, float]]:
        if symbol not in self._cache:
            try:
                self._cache[symbol] = list(self._market.daily_closes(symbol, self._start, self._end) or [])
            except Exception:
                self._cache[symbol] = []
        return self._cache[symbol]

    def on(self, symbol: str, day: date) -> float | None:
        for candle_day, close in self.series(symbol):
            if candle_day == day:
                return close
        return None

    def before(self, symbol: str, day: date) -> float | None:
        previous = [close for candle_day, close in self.series(symbol) if candle_day < day]
        return previous[-1] if previous else None

    def after(self, symbol: str, day: date, count: int) -> list[float]:
        return [close for candle_day, close in self.series(symbol) if candle_day > day][:count]

    def last_on_or_before(self, symbol: str, day: date) -> float | None:
        closes = [close for candle_day, close in self.series(symbol) if candle_day <= day]
        return closes[-1] if closes else None


@dataclass
class _Trade:
    order: _Order
    habit_auto: str
    habit: str
    habit_source: str
    memo: str | None
    pnl: float | None
    return_percent: float | None
    mark_price: float | None
    eval_pnl: float | None
    eval_return_percent: float | None
    missed_profit: float | None
    price_after: dict[str, float | None] = field(default_factory=dict)


def classify_habit(
    order: _Order,
    *,
    pnl: float | None,
    day_change_percent: float | None,
    news_times: list[datetime],
) -> str:
    """서버 습관 분류. 앞 규칙이 먼저 걸리면 그 값을 쓴다.

    rush: 장 시작 10분 안의 시장가 주문
    chase: 체결일 종가가 직전 거래일 종가보다 3% 이상 오른 날의 매수
    avg: 들고 있는 종목이 평균 단가보다 싼 가격(손실 중)일 때의 추가 매수
    news: 주문 전 1시간 안에 그 종목 관련 뉴스가 있었던 주문
    exit: 확정 손익이 플러스인 매도
    plan: 그 외
    """
    if order.order_type == "market" and _within_open_window(order.ordered_at):
        return "rush"
    if order.side == "buy":
        if day_change_percent is not None and day_change_percent >= CHASE_DAY_CHANGE_PERCENT:
            return "chase"
        if order.held_qty_before > 0 and order.price < order.avg_before:
            return "avg"
    if any(order.ordered_at - NEWS_WINDOW <= published <= order.ordered_at for published in news_times):
        return "news"
    if order.side == "sell" and pnl is not None and pnl > 0:
        return "exit"
    return "plan"


def _within_open_window(at: datetime) -> bool:
    local = at.astimezone(NEW_YORK)
    if local.weekday() >= 5:
        return False
    opened = datetime.combine(local.date(), MARKET_OPEN, tzinfo=NEW_YORK)
    return opened <= local < opened + RUSH_WINDOW


def build_monthly_review(
    *,
    month_start: date,
    executions: list[dict[str, Any]],
    notes: dict[str, dict[str, Any]],
    market: ReviewMarketData,
    starting_cash: Decimal | float | None,
    now: datetime,
) -> dict[str, Any]:
    month_end = _next_month(month_start)
    prev_start = _previous_month(month_start)
    orders = replay_orders(executions)
    current = [order for order in orders if _in_month(order.first_at, month_start, month_end)]
    previous = [order for order in orders if _in_month(order.first_at, prev_start, month_start)]
    today = now.astimezone(NEW_YORK).date()
    closes = _Closes(market, prev_start - timedelta(days=CANDLE_LOOKBACK_DAYS), today)

    current_trades = [_trade(order, notes.get(order.order_id), closes, market, today) for order in current]
    previous_trades = [_trade(order, notes.get(order.order_id), closes, market, today) for order in previous]
    cash = _float(starting_cash)

    return {
        "month": f"{month_start.year:04d}-{month_start.month:02d}",
        "kpi": _kpi(current_trades, cash),
        "prevMonth": _kpi(previous_trades, cash),
        "benchmark": {
            "spxReturnPercent": _benchmark_return(closes, month_start, month_end, today),
            "prevSpxReturnPercent": _benchmark_return(closes, prev_start, month_start, today),
        },
        "byHabit": _by_habit(current_trades),
        "bySymbol": _by_symbol(current_trades),
        "trades": [_trade_payload(trade) for trade in current_trades],
    }


def _in_month(at: datetime, start: date, end: date) -> bool:
    local_day = at.astimezone(NEW_YORK).date()
    return start <= local_day < end


def _trade(
    order: _Order,
    note: dict[str, Any] | None,
    closes: _Closes,
    market: ReviewMarketData,
    today: date,
) -> _Trade:
    trade_day = order.first_at.astimezone(NEW_YORK).date()
    price = float(order.price)
    qty = float(order.qty)
    pnl = float(order.realized) if order.side == "sell" and order.realized is not None and order.avg_cost is not None else None
    return_percent = (
        (price / float(order.avg_cost) - 1) * 100
        if pnl is not None and order.avg_cost and order.avg_cost > 0
        else None
    )
    day_close = closes.on(order.symbol, trade_day)
    prev_close = closes.before(order.symbol, trade_day)
    day_change = (day_close / prev_close - 1) * 100 if day_close and prev_close else None
    news_times: list[datetime] = []
    try:
        news_times = list(market.news_published_times(order.symbol, order.ordered_at, NEWS_WINDOW) or [])
    except Exception:
        news_times = []
    habit_auto = classify_habit(order, pnl=pnl, day_change_percent=day_change, news_times=news_times)
    user_habit = (note or {}).get("habit")
    if (note or {}).get("habit_source") == "user" and user_habit in HABIT_LABELS_KO:
        habit, habit_source = str(user_habit), "user"
    else:
        habit, habit_source = habit_auto, "auto"
    review_memo = (note or {}).get("memo")
    memo = review_memo if review_memo else order.memo

    after = closes.after(order.symbol, trade_day, MISSED_PROFIT_TRADING_DAYS)
    mark_price = closes.last_on_or_before(order.symbol, today)
    if order.side == "buy":
        eval_pnl = (mark_price - price) * qty if mark_price is not None else None
        eval_return = (mark_price / price - 1) * 100 if mark_price is not None and price > 0 else None
        missed = None
    else:
        eval_pnl = pnl
        eval_return = return_percent
        missed = max(0.0, max(after) - price) * qty if after else None
    return _Trade(
        order=order,
        habit_auto=habit_auto,
        habit=habit,
        habit_source=habit_source,
        memo=memo,
        pnl=pnl,
        return_percent=return_percent,
        mark_price=mark_price,
        eval_pnl=eval_pnl,
        eval_return_percent=eval_return,
        missed_profit=missed,
        price_after={
            "d1": after[0] if len(after) >= 1 else None,
            "d5": after[4] if len(after) >= MISSED_PROFIT_TRADING_DAYS else None,
        },
    )


def _kpi(trades: list[_Trade], starting_cash: float | None) -> dict[str, Any]:
    evaluated = [trade.eval_pnl for trade in trades if trade.eval_pnl is not None]
    wins = [value for value in evaluated if value > 0]
    losses = [value for value in evaluated if value < 0]
    eval_pnl = sum(evaluated)
    realized = sum(trade.pnl for trade in trades if trade.pnl is not None)
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = -sum(losses) / len(losses) if losses else None
    mistakes = [trade for trade in trades if trade.habit in MISTAKE_HABITS]
    return {
        "realizedPnl": _money(realized),
        "evalPnl": _money(eval_pnl),
        "returnPercent": _pct(eval_pnl / starting_cash * 100) if starting_cash else None,
        "winRate": _pct(len(wins) / len(evaluated) * 100) if evaluated else None,
        "winCount": len(wins),
        "tradeCount": len(trades),
        "avgWin": _money(avg_win) if avg_win is not None else None,
        "avgLoss": _money(avg_loss) if avg_loss is not None else None,
        "payoffRatio": round(avg_win / avg_loss, 2) if avg_win is not None and avg_loss else None,
        "mistakePnl": _money(sum(trade.eval_pnl or 0.0 for trade in mistakes)),
        "mistakeCount": len(mistakes),
        "missedProfit": _money(sum(trade.missed_profit or 0.0 for trade in trades)),
    }


def _by_habit(trades: list[_Trade]) -> list[dict[str, Any]]:
    rows = []
    for habit in HABITS:
        group = [trade for trade in trades if trade.habit == habit]
        rows.append({
            "habit": habit,
            "labelKo": HABIT_LABELS_KO[habit],
            "pnl": _money(sum(trade.eval_pnl or 0.0 for trade in group)),
            "count": len(group),
            "winCount": sum(1 for trade in group if (trade.eval_pnl or 0.0) > 0),
            "missedProfit": _money(sum(trade.missed_profit or 0.0 for trade in group)),
        })
    return rows


def _by_symbol(trades: list[_Trade]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for trade in trades:
        symbol = trade.order.symbol
        row = grouped.setdefault(symbol, {"symbol": symbol, "nameKo": name_ko(symbol), "pnl": 0.0, "count": 0})
        row["pnl"] += trade.eval_pnl or 0.0
        row["count"] += 1
    rows = [{**row, "pnl": _money(row["pnl"])} for row in grouped.values()]
    rows.sort(key=lambda row: (-row["pnl"], row["symbol"]))
    return rows


def _trade_payload(trade: _Trade) -> dict[str, Any]:
    order = trade.order
    return {
        "orderId": order.order_id,
        "at": _iso(order.first_at),
        "symbol": order.symbol,
        "nameKo": name_ko(order.symbol),
        "side": order.side,
        "orderType": order.order_type,
        "qty": float(order.qty),
        "price": _price(float(order.price)),
        "avgPriceAtSell": _price(float(order.avg_cost)) if order.side == "sell" and order.avg_cost is not None else None,
        "pnl": _money(trade.pnl) if trade.pnl is not None else None,
        "returnPercent": _pct(trade.return_percent) if trade.return_percent is not None else None,
        "markPrice": _price(trade.mark_price) if trade.mark_price is not None else None,
        "evalPnl": _money(trade.eval_pnl) if trade.eval_pnl is not None else None,
        "evalReturnPercent": _pct(trade.eval_return_percent) if trade.eval_return_percent is not None else None,
        "missedProfit": _money(trade.missed_profit) if trade.missed_profit is not None else None,
        "habit": trade.habit,
        "habitAuto": trade.habit_auto,
        "habitSource": trade.habit_source,
        "memo": trade.memo,
        "orderMemo": order.memo,
        "priceAfter": {key: _price(value) if value is not None else None for key, value in trade.price_after.items()},
    }


def _benchmark_return(closes: _Closes, start: date, end: date, today: date) -> float | None:
    """S&P 500 의 그달 수익률. 전달 마지막 종가 대비 그달 마지막(오늘까지) 종가."""
    for symbol in BENCHMARK_SYMBOLS:
        base = closes.before(symbol, start)
        in_month = [close for day, close in closes.series(symbol) if start <= day < end and day <= today]
        if base and in_month:
            return _pct((in_month[-1] / base - 1) * 100)
    return None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _float(value: Any) -> float | None:
    parsed = _decimal(value)
    return float(parsed) if parsed is not None and parsed > 0 else None


def _money(value: float) -> float:
    return round(float(value) + 0.0, 2)


def _price(value: float) -> float:
    return round(float(value), 4)


def _pct(value: float) -> float:
    return round(float(value) + 0.0, 2)
