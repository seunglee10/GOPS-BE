from __future__ import annotations

import json
import math
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from gops_simul.config import PROJECT_ROOT


DEFAULT_SCENARIO_ID = "iran-ceasefire-collapse-2026-07-08"
SEMICONDUCTOR_SYMBOLS = ("NVDA", "AMD", "AVGO", "MU", "TSM")
ENERGY_SYMBOLS = ("XOM", "CVX", "COP")
ALL_DEMO_SYMBOLS = (*SEMICONDUCTOR_SYMBOLS, *ENERGY_SYMBOLS)
SEMICONDUCTOR_WEIGHTS = {
    "NVDA": 0.30,
    "AMD": 0.20,
    "AVGO": 0.20,
    "MU": 0.15,
    "TSM": 0.15,
}
ENERGY_WEIGHTS = {"XOM": 0.45, "CVX": 0.35, "COP": 0.20}
DEFAULT_SEED_PRICES = {
    "NVDA": 195.55,
    "AMD": 141.90,
    "AVGO": 340.00,
    "MU": 132.60,
    "TSM": 245.00,
    "XOM": 113.22,
    "CVX": 154.80,
    "COP": 94.20,
}


@dataclass(frozen=True)
class DemoScenarioEvent:
    at_seconds: float
    payload: dict[str, object]
    source_timestamp: str | None = None


@dataclass(frozen=True)
class DemoScenario:
    scenario_id: str
    title: str
    duration_seconds: float = 300.0
    breaking_news_at_seconds: float = 5.0
    seed_prices: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_SEED_PRICES))
    events: list[DemoScenarioEvent] = field(default_factory=list)
    breaking_news: dict[str, object] = field(default_factory=dict)


class DemoScenarioController:
    def __init__(
        self,
        scenario: DemoScenario,
        *,
        clock: Callable[[], float] = time.monotonic,
        starting_cash: float = 100_000.0,
    ) -> None:
        self.scenario = scenario
        self.clock = clock
        self.starting_cash = starting_cash
        self.mode = "live"
        self.run_id: str | None = None
        self.started_at: float | None = None
        self.paused_at: float | None = None
        self.paused_duration = 0.0
        self.latest_prices = dict(scenario.seed_prices)
        self._applied_event_index = 0
        self._accounts: dict[str, dict[str, object]] = {}
        self._lock = threading.RLock()

    def set_mode(self, mode: str) -> dict[str, object]:
        normalized = str(mode or "").strip().lower()
        if normalized in {"sim", "scenario"}:
            normalized = "simulation"
        if normalized not in {"live", "simulation"}:
            raise ValueError("mode must be live or simulation")
        with self._lock:
            if normalized == "simulation" and self.mode != "simulation":
                self.mode = normalized
                self._start_new_run()
            elif normalized == "live":
                self.mode = normalized
                self.started_at = None
                self.paused_at = None
                self.run_id = None
                self._accounts.clear()
            return self.status()

    def restart(self) -> dict[str, object]:
        with self._lock:
            if self.mode != "simulation":
                raise ValueError("simulation mode is not active")
            self._start_new_run()
            return self.status()

    def pause(self) -> dict[str, object]:
        with self._lock:
            if self.mode == "simulation" and self.paused_at is None and self._state() == "running":
                self.paused_at = self.clock()
            return self.status()

    def resume(self) -> dict[str, object]:
        with self._lock:
            if self.mode == "simulation" and self.paused_at is not None:
                self.paused_duration += max(0.0, self.clock() - self.paused_at)
                self.paused_at = None
            return self.status()

    def status(self) -> dict[str, object]:
        with self._lock:
            elapsed = self._elapsed_seconds()
            self._apply_events_until(elapsed)
            state = self._state(elapsed)
            return {
                "mode": self.mode,
                "state": state,
                "scenarioId": self.scenario.scenario_id if self.mode == "simulation" else None,
                "scenarioTitle": self.scenario.title if self.mode == "simulation" else None,
                "runId": self.run_id,
                "elapsedSeconds": round(elapsed, 3),
                "durationSeconds": self.scenario.duration_seconds,
                "breakingNewsAtSeconds": self.scenario.breaking_news_at_seconds,
                "phase": self._phase(elapsed),
                "breakingNewsReleased": self._news_released(elapsed),
                "eventCount": self._applied_event_index,
                "symbols": [
                    {
                        "symbol": symbol,
                        "price": self.latest_prices.get(symbol),
                        "seedPrice": self.scenario.seed_prices.get(symbol),
                        "changePercent": percentage_change(
                            self.latest_prices.get(symbol),
                            self.scenario.seed_prices.get(symbol),
                        ),
                    }
                    for symbol in ALL_DEMO_SYMBOLS
                ],
            }

    def news(self, symbols: Iterable[str] | None = None) -> dict[str, object]:
        with self._lock:
            elapsed = self._elapsed_seconds()
            if self.mode != "simulation" or not self._news_released(elapsed):
                return {"news": [], "next_page_token": None}
            requested = {str(symbol).strip().upper() for symbol in symbols or [] if str(symbol).strip()}
            article = deepcopy(self.scenario.breaking_news)
            article_symbols = [str(value).upper() for value in article.get("symbols", ALL_DEMO_SYMBOLS)]
            if requested and not requested.intersection(article_symbols):
                return {"news": [], "next_page_token": None}
            article["id"] = f"{self.run_id}:{article.get('id') or 'breaking-news'}"
            article.setdefault("source", "GOPS Simulator")
            article.setdefault("created_at", "2026-07-08T13:20:00Z")
            article.setdefault("updated_at", article["created_at"])
            article["symbols"] = article_symbols
            article["simulator"] = {
                "scenarioId": self.scenario.scenario_id,
                "runId": self.run_id,
                "breaking": True,
            }
            return {"news": [article], "next_page_token": None}

    def events_between(self, after_seconds: float, through_seconds: float | None = None) -> list[DemoScenarioEvent]:
        with self._lock:
            end = self._elapsed_seconds() if through_seconds is None else through_seconds
            return [
                event
                for event in self.scenario.events
                if after_seconds < event.at_seconds <= end
            ]

    def account(self, user_id: str) -> dict[str, object]:
        with self._lock:
            self._apply_events_until(self._elapsed_seconds())
            ledger = self._ledger(user_id)
            return self._account_payload(ledger)

    def submit_basket_order(self, user_id: str, *, basket: str, side: str) -> dict[str, object]:
        with self._lock:
            if self.mode != "simulation":
                raise ValueError("simulation mode is not active")
            normalized_basket = str(basket or "").strip().lower()
            normalized_side = str(side or "").strip().lower()
            ledger = self._ledger(user_id)
            if normalized_basket == "semiconductor":
                if normalized_side != "sell":
                    raise ValueError("semiconductor basket supports sell only")
                orders = self._sell_semiconductor_basket(ledger)
            elif normalized_basket == "energy":
                if normalized_side != "buy":
                    raise ValueError("energy basket supports buy only")
                orders = self._buy_energy_basket(ledger)
            else:
                raise ValueError("unsupported basket")
            return {"orders": deepcopy(orders), "account": self._account_payload(ledger)}

    def submit_order(
        self,
        user_id: str,
        *,
        symbol: str,
        side: str,
        quantity: int,
    ) -> dict[str, object]:
        with self._lock:
            if self.mode != "simulation":
                raise ValueError("simulation mode is not active")
            normalized_symbol = str(symbol or "").strip().upper()
            normalized_side = str(side or "").strip().lower()
            if normalized_symbol not in ALL_DEMO_SYMBOLS:
                raise ValueError("symbol is not part of the demo universe")
            if normalized_side not in {"buy", "sell"}:
                raise ValueError("side must be buy or sell")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
                raise ValueError("quantity must be a positive whole number")
            ledger = self._ledger(user_id)
            positions = ledger["positions"]
            assert isinstance(positions, dict)
            price = float(self.latest_prices.get(normalized_symbol) or self.scenario.seed_prices[normalized_symbol])
            if normalized_side == "sell":
                position = positions.get(normalized_symbol)
                held = int(position["quantity"]) if isinstance(position, dict) else 0
                if held < quantity:
                    raise ValueError("insufficient position for simulation sell")
                ledger["cash"] = round(float(ledger["cash"]) + quantity * price, 4)
                if held == quantity:
                    positions.pop(normalized_symbol, None)
                else:
                    position["quantity"] = held - quantity
            else:
                cost = quantity * price
                cash = float(ledger["cash"])
                if cost > cash + 1e-6:
                    raise ValueError("insufficient simulation cash")
                ledger["cash"] = round(cash - cost, 4)
                existing = positions.get(normalized_symbol)
                if isinstance(existing, dict):
                    held = int(existing["quantity"])
                    average = float(existing["averagePrice"])
                    existing["quantity"] = held + quantity
                    existing["averagePrice"] = round(((held * average) + cost) / (held + quantity), 4)
                else:
                    positions[normalized_symbol] = {
                        "symbol": normalized_symbol,
                        "name": normalized_symbol,
                        "sector": "Energy" if normalized_symbol in ENERGY_SYMBOLS else "Information Technology",
                        "industry": "Integrated Energy" if normalized_symbol in ENERGY_SYMBOLS else "Semiconductors",
                        "quantity": quantity,
                        "averagePrice": round(price, 4),
                    }
            order = self._record_order(
                ledger,
                symbol=normalized_symbol,
                side=normalized_side,
                quantity=quantity,
                price=price,
            )
            return {"order": deepcopy(order), "account": self._account_payload(ledger)}

    def _start_new_run(self) -> None:
        self.run_id = f"sim-{uuid.uuid4().hex[:12]}"
        self.started_at = self.clock()
        self.paused_at = None
        self.paused_duration = 0.0
        self.latest_prices = dict(self.scenario.seed_prices)
        self._applied_event_index = 0
        self._accounts.clear()

    def _elapsed_seconds(self) -> float:
        if self.mode != "simulation" or self.started_at is None:
            return 0.0
        current = self.paused_at if self.paused_at is not None else self.clock()
        return min(
            self.scenario.duration_seconds,
            max(0.0, current - self.started_at - self.paused_duration),
        )

    def _state(self, elapsed: float | None = None) -> str:
        if self.mode != "simulation":
            return "idle"
        if self.paused_at is not None:
            return "paused"
        if (self._elapsed_seconds() if elapsed is None else elapsed) >= self.scenario.duration_seconds:
            return "completed"
        return "running"

    def _phase(self, elapsed: float) -> str:
        if self.mode != "simulation":
            return "live"
        if elapsed >= self.scenario.duration_seconds:
            return "complete"
        if elapsed < self.scenario.breaking_news_at_seconds:
            return "pre-war"
        return "market-impact"

    def _news_released(self, elapsed: float) -> bool:
        return self.mode == "simulation" and elapsed >= self.scenario.breaking_news_at_seconds

    def _apply_events_until(self, elapsed: float) -> None:
        events = self.scenario.events
        while self._applied_event_index < len(events):
            event = events[self._applied_event_index]
            if event.at_seconds > elapsed:
                break
            payload = event.payload
            if str(payload.get("T") or "") == "t":
                symbol = str(payload.get("S") or "").upper()
                price = number_or_none(payload.get("p"))
                if symbol and price is not None:
                    self.latest_prices[symbol] = price
            self._applied_event_index += 1

    def _ledger(self, user_id: str) -> dict[str, object]:
        key = str(user_id or "demo-user")
        ledger = self._accounts.get(key)
        if ledger is None:
            ledger = self._initial_ledger()
            self._accounts[key] = ledger
        return ledger

    def _initial_ledger(self) -> dict[str, object]:
        positions: dict[str, dict[str, object]] = {}
        spent = 0.0
        for symbol, weight in SEMICONDUCTOR_WEIGHTS.items():
            price = self.scenario.seed_prices[symbol]
            quantity = max(1, math.floor((self.starting_cash * weight) / price))
            cost = quantity * price
            spent += cost
            positions[symbol] = {
                "symbol": symbol,
                "name": symbol,
                "sector": "Information Technology",
                "industry": "Semiconductors",
                "quantity": quantity,
                "averagePrice": round(price, 4),
            }
        return {
            "cash": round(max(0.0, self.starting_cash - spent), 4),
            "positions": positions,
            "orders": [],
        }

    def _sell_semiconductor_basket(self, ledger: dict[str, object]) -> list[dict[str, object]]:
        positions = ledger["positions"]
        assert isinstance(positions, dict)
        selected = [positions[symbol] for symbol in SEMICONDUCTOR_SYMBOLS if symbol in positions]
        if not selected:
            raise ValueError("no semiconductor positions to sell")
        orders = []
        for position in selected:
            symbol = str(position["symbol"])
            quantity = int(position["quantity"])
            price = float(self.latest_prices.get(symbol) or self.scenario.seed_prices[symbol])
            ledger["cash"] = float(ledger["cash"]) + quantity * price
            positions.pop(symbol, None)
            orders.append(self._record_order(ledger, symbol=symbol, side="sell", quantity=quantity, price=price))
        ledger["cash"] = round(float(ledger["cash"]), 4)
        return orders

    def _buy_energy_basket(self, ledger: dict[str, object]) -> list[dict[str, object]]:
        cash = float(ledger["cash"])
        if cash <= 0:
            raise ValueError("insufficient simulation cash")
        positions = ledger["positions"]
        assert isinstance(positions, dict)
        orders = []
        spent = 0.0
        for symbol, weight in ENERGY_WEIGHTS.items():
            price = float(self.latest_prices.get(symbol) or self.scenario.seed_prices[symbol])
            quantity = math.floor((cash * weight) / price)
            if quantity <= 0:
                continue
            cost = quantity * price
            spent += cost
            positions[symbol] = {
                "symbol": symbol,
                "name": symbol,
                "sector": "Energy",
                "industry": "Integrated Energy",
                "quantity": quantity,
                "averagePrice": round(price, 4),
            }
            orders.append(self._record_order(ledger, symbol=symbol, side="buy", quantity=quantity, price=price))
        if not orders or spent > cash + 1e-6:
            raise ValueError("insufficient simulation cash")
        ledger["cash"] = round(cash - spent, 4)
        return orders

    def _record_order(
        self,
        ledger: dict[str, object],
        *,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
    ) -> dict[str, object]:
        order = {
            "order_id": f"sim-order-{uuid.uuid4().hex[:12]}",
            "status": "filled",
            "symbol": symbol,
            "side": side,
            "qty": str(quantity),
            "price": f"{price:.4f}",
            "filled_qty": str(quantity),
            "filled_price": f"{price:.4f}",
            "simulation": True,
            "runId": self.run_id,
        }
        orders = ledger["orders"]
        assert isinstance(orders, list)
        orders.append(order)
        return order

    def _account_payload(self, ledger: dict[str, object]) -> dict[str, object]:
        positions = ledger["positions"]
        assert isinstance(positions, dict)
        rendered_positions: dict[str, dict[str, object]] = {}
        stock_value = 0.0
        unrealized = 0.0
        for symbol, value in positions.items():
            position = dict(value)
            current_price = float(self.latest_prices.get(symbol) or position["averagePrice"])
            quantity = int(position["quantity"])
            average_price = float(position["averagePrice"])
            market_value = current_price * quantity
            pnl = (current_price - average_price) * quantity
            position.update({
                "currentPrice": round(current_price, 4),
                "marketValueForeign": round(market_value, 4),
                "unrealizedPnlForeign": round(pnl, 4),
                "unrealizedPnlRate": round(((current_price - average_price) / average_price) * 100, 4) if average_price else 0.0,
            })
            rendered_positions[symbol] = position
            stock_value += market_value
            unrealized += pnl
        cash = float(ledger["cash"])
        return {
            "status": "ok",
            "source": "gops-simulator",
            "runId": self.run_id,
            "account": {
                "alias": "반도체 집중형 · SIMULATED",
                "market": "overseas",
                "currency": "USD",
                "cashForeign": round(cash, 4),
                "stockValueForeign": round(stock_value, 4),
                "totalValueForeign": round(cash + stock_value, 4),
                "unrealizedPnlForeign": round(unrealized, 4),
            },
            "positions": rendered_positions,
            "orders": deepcopy(ledger["orders"]),
            "limitations": ["simulation only", "no real broker order was submitted"],
        }


def load_demo_scenario(root: Path | None = None) -> DemoScenario:
    scenario_root = root or PROJECT_ROOT / "data" / "scenarios" / DEFAULT_SCENARIO_ID
    manifest_path = scenario_root / "scenario.json"
    events_path = scenario_root / "events.jsonl"
    if not manifest_path.exists():
        return default_demo_scenario()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    events = []
    if events_path.exists():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            events.append(DemoScenarioEvent(
                at_seconds=float(row["atSeconds"]),
                payload=dict(row["payload"]),
                source_timestamp=row.get("sourceTimestamp"),
            ))
    events.sort(key=lambda item: item.at_seconds)
    return DemoScenario(
        scenario_id=str(manifest.get("scenarioId") or DEFAULT_SCENARIO_ID),
        title=str(manifest.get("title") or "Iran ceasefire collapse demo"),
        duration_seconds=float(manifest.get("durationSeconds") or 300.0),
        breaking_news_at_seconds=float(manifest.get("breakingNewsAtSeconds") or 5.0),
        seed_prices={key.upper(): float(value) for key, value in (manifest.get("seedPrices") or DEFAULT_SEED_PRICES).items()},
        events=events,
        breaking_news=dict(manifest.get("breakingNews") or {}),
    )


def default_demo_scenario() -> DemoScenario:
    return DemoScenario(
        scenario_id=DEFAULT_SCENARIO_ID,
        title="Iran ceasefire collapse semiconductor-to-energy rotation",
        seed_prices=dict(DEFAULT_SEED_PRICES),
        breaking_news={
            "id": "iran-ceasefire-collapse",
            "headline": "[속보] 이란 휴전 붕괴, 추가 공습 가능성",
            "summary": "호르무즈 해협을 둘러싼 군사 긴장이 다시 고조되며 에너지 가격과 위험자산 변동성이 확대됐습니다.",
            "source": "GOPS Simulator",
            "url": "https://www.investing.com/news/commodities-news/trump-on-iran-us-will-probably-hit-them-again-wednesday-night-4781812",
            "symbols": list(ALL_DEMO_SYMBOLS),
        },
    )


def number_or_none(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def percentage_change(current: float | None, baseline: float | None) -> float | None:
    if current is None or baseline is None or baseline == 0:
        return None
    return round(((current - baseline) / baseline) * 100, 4)
