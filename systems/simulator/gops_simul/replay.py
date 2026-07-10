from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Iterable

from fastapi import WebSocket, WebSocketDisconnect

from gops_simul.config import Settings
from gops_simul.demo import DemoScenarioController
from gops_simul.storage import VALID_STREAM_CHANNELS, SessionStore, normalize_symbols
from gops_simul.time_utils import isoformat_z, parse_record_time


ALL_SUBSCRIPTION_CHANNELS = (
    "trades",
    "quotes",
    "bars",
    "updatedBars",
    "dailyBars",
    "statuses",
    "lulds",
    "corrections",
    "cancelErrors",
)


@dataclass
class WebSocketState:
    feed: str
    settings: Settings
    store: SessionStore
    demo_controller: DemoScenarioController | None = None
    subscriptions: dict[str, set[str]] = field(default_factory=lambda: {name: set() for name in ALL_SUBSCRIPTION_CHANNELS})
    authenticated: bool = False
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    replay_task: asyncio.Task[None] | None = None
    rng: random.Random = field(init=False)
    price_state: dict[str, float] = field(default_factory=dict)
    bar_state: dict[tuple[str, str], dict[str, object]] = field(default_factory=dict)
    replay_sequence: int = 0

    def __post_init__(self) -> None:
        self.rng = random.Random(self.settings.random_seed)

    async def send_json(self, websocket: WebSocket, payload: object) -> None:
        async with self.send_lock:
            await websocket.send_text(json.dumps(payload, separators=(",", ":")))

    async def subscribe(self, websocket: WebSocket, message: dict[str, object]) -> None:
        update_subscriptions(self.subscriptions, message, action="subscribe")
        await self.send_json(websocket, [subscription_snapshot(self.subscriptions)])
        self.restart_replay(websocket)

    async def unsubscribe(self, websocket: WebSocket, message: dict[str, object]) -> None:
        update_subscriptions(self.subscriptions, message, action="unsubscribe")
        await self.send_json(websocket, [subscription_snapshot(self.subscriptions)])
        self.restart_replay(websocket)

    def restart_replay(self, websocket: WebSocket) -> None:
        self.cancel_replay()
        if any(self.subscriptions.values()):
            self.replay_task = asyncio.create_task(run_replay(websocket, self))

    def cancel_replay(self) -> None:
        if self.replay_task and not self.replay_task.done():
            self.replay_task.cancel()
        self.replay_task = None


def update_subscriptions(subscriptions: dict[str, set[str]], message: dict[str, object], *, action: str) -> None:
    for channel in ALL_SUBSCRIPTION_CHANNELS:
        if channel not in message:
            continue
        raw_values = message[channel]
        if not isinstance(raw_values, list):
            raise ValueError(f"{channel} must be a list")
        values = normalize_subscription_symbols(raw_values)
        if action == "subscribe":
            subscriptions[channel].update(values)
        else:
            subscriptions[channel].difference_update(values)


def normalize_subscription_symbols(values: Iterable[object]) -> set[str]:
    raw = {str(value).strip().upper() for value in values if str(value).strip()}
    if "*" in raw:
        return {"*"}
    return set(normalize_symbols(raw))


def subscription_snapshot(subscriptions: dict[str, set[str]]) -> dict[str, object]:
    snapshot: dict[str, object] = {"T": "subscription"}
    for channel in ALL_SUBSCRIPTION_CHANNELS:
        snapshot[channel] = sorted(subscriptions.get(channel, set()))
    return snapshot


async def run_replay(websocket: WebSocket, state: WebSocketState) -> None:
    if state.demo_controller is not None:
        await run_demo_replay(websocket, state)
        return
    events = state.store.records_for_stream(feed=state.feed, subscriptions=state.subscriptions)
    if not events:
        return

    loop_index = 0
    try:
        while True:
            await replay_events_once(websocket, state, events, loop_index)
            loop_index += 1
            if not state.settings.replay_loop:
                return
            if state.settings.replay_loop_pause_seconds > 0:
                await asyncio.sleep(state.settings.replay_loop_pause_seconds)
    except WebSocketDisconnect:
        return


async def run_demo_replay(websocket: WebSocket, state: WebSocketState) -> None:
    controller = state.demo_controller
    if controller is None:
        return
    active_run_id: str | None = None
    cursor_seconds = -0.000001
    try:
        while True:
            status = controller.status()
            run_id = str(status.get("runId") or "") or None
            if status["mode"] != "simulation" or run_id is None:
                active_run_id = None
                cursor_seconds = -0.000001
                await asyncio.sleep(0.05)
                continue
            if run_id != active_run_id:
                active_run_id = run_id
                cursor_seconds = -0.000001
            elapsed = float(status["elapsedSeconds"])
            due = controller.events_between(cursor_seconds, elapsed)
            selected = [
                demo_stream_payload(event.payload, status=status, sequence=state.replay_sequence + index)
                for index, event in enumerate(due)
                if is_demo_event_subscribed(event.payload, state.subscriptions)
            ]
            state.replay_sequence += len(due)
            if due:
                cursor_seconds = max(event.at_seconds for event in due)
            if selected:
                for offset in range(0, len(selected), state.settings.replay_batch_size):
                    await state.send_json(websocket, selected[offset:offset + state.settings.replay_batch_size])
            await asyncio.sleep(0.02)
    except WebSocketDisconnect:
        return


def is_demo_event_subscribed(
    payload: dict[str, object],
    subscriptions: dict[str, set[str]],
) -> bool:
    channel = {
        "t": "trades",
        "q": "quotes",
        "b": "bars",
        "u": "updatedBars",
        "d": "dailyBars",
        "s": "statuses",
        "l": "lulds",
        "c": "corrections",
        "x": "cancelErrors",
    }.get(str(payload.get("T") or ""))
    if channel is None:
        return False
    symbols = subscriptions.get(channel, set())
    symbol = str(payload.get("S") or "").upper()
    return "*" in symbols or symbol in symbols


def demo_stream_payload(
    payload: dict[str, object],
    *,
    status: dict[str, object],
    sequence: int,
) -> dict[str, object]:
    rendered = dict(payload)
    source_timestamp = rendered.get("t")
    rendered["t"] = isoformat_z(datetime.now(UTC))
    if rendered.get("T") == "t" and "i" in rendered:
        # GOPS의 ClickHouse 스키마는 Alpaca 거래 ID를 정수로 저장한다.
        # 원본 ID에 문자열 접미사를 붙이지 않고, 시뮬레이터 전용 범위의
        # 정수 ID를 발급해 재생 이벤트끼리 충돌하지 않게 한다.
        rendered["i"] = 1_000_000_000_000_000 + sequence
    rendered["simulator"] = {
        "scenarioId": status["scenarioId"],
        "runId": status["runId"],
        "phase": status["phase"],
        "elapsedSeconds": status["elapsedSeconds"],
        "sourceTimestamp": source_timestamp,
    }
    return rendered


async def replay_events_once(
    websocket: WebSocket,
    state: WebSocketState,
    events: list[dict[str, object]],
    loop_index: int,
) -> None:
    first_timestamp = parse_record_time(events[0])
    loop_base = datetime.now(UTC) + timedelta(milliseconds=loop_index)
    previous_source = None
    batch: list[dict[str, object]] = []
    for event in events:
        if previous_source is not None:
            await sleep_for_replay_delta(state.settings, previous_source, event)
        previous_source = event
        replay_event = replay_event_for_loop(
            event,
            loop_index=loop_index,
            sequence=state.replay_sequence,
            first_timestamp=first_timestamp,
            loop_base=loop_base,
            state=state,
            rewrite_timestamps=state.settings.replay_rewrite_timestamps,
        )
        state.replay_sequence += 1
        batch.append(replay_event)
        if len(batch) >= state.settings.replay_batch_size:
            await state.send_json(websocket, list(batch))
            batch.clear()
    if batch:
        await state.send_json(websocket, list(batch))


def replay_event_for_loop(
    event: dict[str, object],
    *,
    loop_index: int,
    sequence: int,
    first_timestamp: datetime,
    loop_base: datetime,
    state: WebSocketState,
    rewrite_timestamps: bool,
) -> dict[str, object]:
    replay_event = dict(event)
    if rewrite_timestamps:
        if state.settings.replay_wall_clock_timestamps:
            replay_event["t"] = isoformat_z(datetime.now(UTC))
        else:
            original_timestamp = parse_record_time(event)
            replay_event["t"] = isoformat_z(loop_base + (original_timestamp - first_timestamp))
    if state.settings.randomize_ticks:
        randomize_market_event(replay_event, state)
    if replay_event.get("T") == "t" and "i" in replay_event:
        replay_event["i"] = f"{replay_event['i']}-r{loop_index}-s{sequence}"
    return replay_event


def randomize_market_event(event: dict[str, object], state: WebSocketState) -> None:
    message_type = str(event.get("T") or "")
    symbol = str(event.get("S") or "").upper()
    if not symbol:
        return
    if message_type == "t":
        randomize_trade(event, state, symbol)
    elif message_type == "q":
        randomize_quote(event, state, symbol)
    elif message_type in {"b", "u", "d"}:
        randomize_bar(event, state, symbol)


def randomize_trade(event: dict[str, object], state: WebSocketState, symbol: str) -> None:
    price = next_price(state, symbol, base_price=number_or_none(event.get("p")))
    event["p"] = price
    event["s"] = jitter_size(state, event.get("s"))


def randomize_quote(event: dict[str, object], state: WebSocketState, symbol: str) -> None:
    midpoint = next_price(state, symbol, base_price=quote_midpoint(event))
    spread = max(0.01, midpoint * state.settings.quote_spread_bps / 10_000)
    event["bp"] = round_price(midpoint - spread / 2)
    event["ap"] = round_price(midpoint + spread / 2)
    event["bs"] = jitter_size(state, event.get("bs"))
    event["as"] = jitter_size(state, event.get("as"))


def randomize_bar(event: dict[str, object], state: WebSocketState, symbol: str) -> None:
    timestamp = str(event.get("t") or isoformat_z(datetime.now(UTC)))
    minute = timestamp[:16] + ":00.000Z"
    price = next_price(state, symbol, base_price=number_or_none(event.get("c") or event.get("p")))
    key = (symbol, minute)
    current = state.bar_state.get(key)
    if current is None:
        current = {
            "o": price,
            "h": price,
            "l": price,
            "c": price,
            "v": 0,
            "n": 0,
            "weighted": 0.0,
        }
    size = jitter_size(state, event.get("v") or event.get("s") or 1)
    current["h"] = max(float(current["h"]), price)
    current["l"] = min(float(current["l"]), price)
    current["c"] = price
    current["v"] = int(current["v"]) + size
    current["n"] = int(current["n"]) + 1
    current["weighted"] = float(current["weighted"]) + price * size
    state.bar_state[key] = current
    event["t"] = minute
    event["o"] = round_price(float(current["o"]))
    event["h"] = round_price(float(current["h"]))
    event["l"] = round_price(float(current["l"]))
    event["c"] = round_price(float(current["c"]))
    event["v"] = int(current["v"])
    event["n"] = int(current["n"])
    event["vw"] = round_price(float(current["weighted"]) / max(1, int(current["v"])))


def next_price(state: WebSocketState, symbol: str, *, base_price: float | None) -> float:
    current = state.price_state.get(symbol)
    if current is None:
        current = base_price or 100.0
    jitter = state.rng.uniform(-state.settings.tick_price_jitter_bps, state.settings.tick_price_jitter_bps)
    next_value = max(0.01, current * (1 + jitter / 10_000))
    state.price_state[symbol] = next_value
    return round_price(next_value)


def jitter_size(state: WebSocketState, value: object) -> int:
    base = max(1, int(number_or_none(value) or 1))
    ratio = state.settings.tick_size_jitter_ratio
    low = max(1, int(base * max(0.0, 1 - ratio)))
    high = max(low, int(base * (1 + ratio)))
    return state.rng.randint(low, high)


def quote_midpoint(event: dict[str, object]) -> float | None:
    bid = number_or_none(event.get("bp"))
    ask = number_or_none(event.get("ap"))
    if bid is not None and ask is not None:
        return (bid + ask) / 2
    return bid or ask


def number_or_none(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def round_price(value: float) -> float:
    return round(value, 4)


async def sleep_for_replay_delta(settings: Settings, previous: dict[str, object], current: dict[str, object]) -> None:
    speed = settings.replay_speed
    if speed <= 0:
        return
    previous_time = parse_record_time(previous)
    current_time = parse_record_time(current)
    delta = max(0.0, (current_time - previous_time).total_seconds() / speed)
    delay = min(delta, settings.replay_max_delay_seconds)
    if delay > 0:
        await asyncio.sleep(delay)
