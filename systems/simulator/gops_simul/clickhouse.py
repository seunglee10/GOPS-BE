"""Dependency-light ClickHouse HTTP client and chunked replay event source."""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from gops_simul.dataset import (
    DATASET_ID,
    HEATMAP_BASELINE_SESSION_DATE,
    REPLAY_SYMBOL_SET,
    REPLAY_SYMBOLS,
    isoformat_z,
    parse_timestamp,
)
from gops_simul.tick_replay import ReplayEvent


INTERVAL_SECONDS = {"1m": 60, "5m": 300, "10m": 600, "1h": 3600, "4h": 14_400, "1D": 86_400, "1d": 86_400}
MARKET_TIMEZONE = ZoneInfo("America/New_York")


class ClickHouseHttpClient:
    def __init__(self, base_url: str, *, database: str = "market_data", user: str = "default",
                 password: str = "", timeout_seconds: float = 30.0) -> None:
        self.base_url, self.database, self.user, self.password = base_url.rstrip("/"), database, user, password
        self.timeout_seconds = timeout_seconds

    def query_rows(self, sql: str) -> list[dict[str, object]]:
        request = self._request(f"{sql.rstrip().rstrip(';')} FORMAT JSONEachRow\n".encode())
        with self._open(request) as response:
            return [json.loads(line) for line in response.read().decode().splitlines() if line.strip()]

    def execute(self, sql: str) -> None:
        with self._open(self._request(sql.encode())) as response: response.read()

    def insert_json_each_row(self, table: str, rows: Iterable[dict[str, object]]) -> int:
        rows = list(rows)
        if not rows: return 0
        body = f"INSERT INTO {table} FORMAT JSONEachRow\n" + "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)
        with self._open(self._request(body.encode())) as response: response.read()
        return len(rows)

    def _open(self, request: urllib.request.Request):
        try:
            return urllib.request.urlopen(request, timeout=self.timeout_seconds)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace").strip()
            raise RuntimeError(f"ClickHouse HTTP {exc.code}: {detail or exc.reason}") from exc

    def _request(self, body: bytes) -> urllib.request.Request:
        headers = {"Content-Type": "text/plain; charset=utf-8", "X-ClickHouse-User": self.user}
        if self.password: headers["X-ClickHouse-Key"] = self.password
        query = urllib.parse.urlencode({
            "database": self.database,
            "date_time_input_format": "best_effort",
            "date_time_output_format": "iso",
        })
        return urllib.request.Request(
            f"{self.base_url}/?{query}",
            data=body,
            headers=headers,
            method="POST",
        )


class ClickHouseReplayEventSource:
    def __init__(self, client: ClickHouseHttpClient, dataset_id: str = DATASET_ID) -> None:
        self.client, self.dataset_id = client, dataset_id
        rows = client.query_rows("SELECT status, total_events FROM market_data.simulation_replay_datasets FINAL "
            f"WHERE dataset_id = {sql_string(dataset_id)} LIMIT 1")
        if not rows or rows[0].get("status") != "READY": raise RuntimeError(f"replay dataset {dataset_id} is not READY")
        self.total_events = int(rows[0].get("total_events") or 0)
        if self.total_events <= 0: raise RuntimeError(f"replay dataset {dataset_id} has no events")

    def previous_close_snapshot(self) -> dict[str, float]:
        session_start = datetime.fromisoformat(HEATMAP_BASELINE_SESSION_DATE).replace(tzinfo=timezone.utc)
        session_end = session_start + timedelta(days=1)
        symbols = ",".join(sql_string(symbol) for symbol in REPLAY_SYMBOLS)
        rows = self.client.query_rows(
            "SELECT symbol, "
            "argMax(close, tuple(inserted_at, event_time, ifNull(source_event_id, ''))) AS previous_close "
            "FROM market_data.chart_candles "
            f"PREWHERE symbol IN ({symbols}) AND interval IN ('1D', '1d') "
            f"AND event_time >= parseDateTime64BestEffort({sql_string(isoformat_z(session_start))}, 3) "
            f"AND event_time < parseDateTime64BestEffort({sql_string(isoformat_z(session_end))}, 3) "
            "WHERE is_closed = 1 AND market_session = 'regular' "
            "AND canonical_version = 'v2' AND price_adjustment = 'split' "
            "AND close > 0 AND isFinite(close) GROUP BY symbol ORDER BY symbol"
        )
        snapshot: dict[str, float] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            try:
                previous_close = float(row.get("previous_close"))
            except (TypeError, ValueError):
                continue
            if symbol in REPLAY_SYMBOL_SET and previous_close > 0 and math.isfinite(previous_close):
                snapshot[symbol] = previous_close
        missing = sorted(REPLAY_SYMBOL_SET.difference(snapshot))
        if missing:
            preview = ",".join(missing[:10])
            raise RuntimeError(
                "previous close baseline is incomplete: "
                f"{len(snapshot)}/{len(REPLAY_SYMBOLS)} symbols; missing={preview}"
            )
        return snapshot

    def events_after_sequence(self, sequence: int, limit: int) -> list[ReplayEvent]:
        rows = self.client.query_rows("SELECT sequence, event_time, feed, payload FROM market_data.simulation_replay_events "
            f"WHERE dataset_id = {sql_string(self.dataset_id)} AND sequence > {max(0, int(sequence))} "
            f"ORDER BY sequence LIMIT {max(1, int(limit))}")
        return self._replay_events(rows)

    def events_after(self, sequence: int, through: datetime, limit: int) -> list[ReplayEvent]:
        return [
            event
            for event in self.events_after_sequence(sequence, limit)
            if event.timestamp <= through
        ]

    def events_between(self, after_sequence: int, through_sequence: int, limit: int) -> list[ReplayEvent]:
        rows = self.client.query_rows(
            "SELECT sequence, event_time, feed, payload FROM market_data.simulation_replay_events "
            f"WHERE dataset_id = {sql_string(self.dataset_id)} AND sequence > {max(0, int(after_sequence))} "
            f"AND sequence <= {max(0, int(through_sequence))} "
            f"ORDER BY sequence LIMIT {max(1, int(limit))}"
        )
        return self._replay_events(rows)

    def events_for_symbol_after(self, symbol: str, sequence: int, through: datetime, limit: int) -> list[ReplayEvent]:
        normalized = symbol.strip().upper()
        if normalized not in REPLAY_SYMBOL_SET:
            raise ValueError(f"symbol is not available in {self.dataset_id}")
        rows = self.client.query_rows(
            "SELECT sequence, event_time, feed, payload FROM market_data.simulation_replay_events "
            f"WHERE dataset_id = {sql_string(self.dataset_id)} AND symbol = {sql_string(normalized)} "
            "AND event_type IN ('trade', 'quote') "
            f"AND sequence > {max(0, int(sequence))} "
            f"AND event_time <= parseDateTime64BestEffort({sql_string(isoformat_z(through))}, 9) "
            f"ORDER BY sequence LIMIT {max(1, int(limit))}"
        )
        return self._replay_events(rows)

    @staticmethod
    def _replay_events(rows: Iterable[dict[str, object]]) -> list[ReplayEvent]:
        return [ReplayEvent(sequence=int(row["sequence"]), timestamp=parse_timestamp(row["event_time"]),
            feed=str(row.get("feed") or "sip"), payload=json.loads(str(row.get("payload") or "{}"))) for row in rows]

    def candle_snapshot(self, symbol: str, interval: str, through: datetime, limit: int) -> dict[str, object]:
        symbol = symbol.strip().upper(); seconds = INTERVAL_SECONDS.get(interval)
        if symbol not in REPLAY_SYMBOL_SET: raise ValueError(f"symbol is not available in {self.dataset_id}")
        if seconds is None: raise ValueError(f"unsupported replay candle interval: {interval}")
        daily = interval in {"1D", "1d"}
        rows = self.client.query_rows(
            self._daily_candle_query(symbol, through, limit)
            if daily
            else self._intraday_candle_query(symbol, through, limit, seconds)
        )
        candles = []
        for row in reversed(rows):
            if daily:
                timestamp = market_midnight_utc(str(row["market_date"]))
                bucket_end = market_midnight_utc((timestamp.astimezone(MARKET_TIMEZONE).date() + timedelta(days=1)).isoformat())
            else:
                timestamp = parse_timestamp(row["timestamp"])
                bucket_end = timestamp + timedelta(seconds=seconds)
            candle_timestamp = isoformat_milliseconds_z(timestamp) if daily else isoformat_z(timestamp)
            candles.append({"symbol": symbol, "interval": interval, "timestamp": candle_timestamp,
                "open": float(row["open"]), "high": float(row["high"]), "low": float(row["low"]), "close": float(row["close"]),
                "volume": float(row.get("volume") or 0), "tradeCount": int(row.get("trade_count") or 0),
                "isClosed": bucket_end <= through, "source": "simulation_replay", "feed": "mixed", "sourceInterval": "trades"})
        return replay_candle_payload(symbol, interval, candles, through)

    def _intraday_candle_query(self, symbol: str, through: datetime, limit: int, seconds: int) -> str:
        return ("WITH " + str(seconds) + " AS bucket_seconds, "
            "toDateTime64(intDiv(toUnixTimestamp64Milli(event_time), bucket_seconds * 1000) * bucket_seconds, 3, 'UTC') AS bucket "
            "SELECT bucket AS timestamp, argMin(JSONExtractFloat(payload, 'p'), tuple(event_time, sequence)) AS open, "
            "max(JSONExtractFloat(payload, 'p')) AS high, min(JSONExtractFloat(payload, 'p')) AS low, "
            "argMax(JSONExtractFloat(payload, 'p'), tuple(event_time, sequence)) AS close, sum(JSONExtractFloat(payload, 's')) AS volume, count() AS trade_count "
            "FROM market_data.simulation_replay_events "
            f"WHERE dataset_id = {sql_string(self.dataset_id)} AND symbol = {sql_string(symbol)} AND event_type = 'trade' "
            f"AND event_time <= parseDateTime64BestEffort({sql_string(isoformat_z(through))}, 9) "
            f"GROUP BY bucket ORDER BY bucket DESC LIMIT {max(1, int(limit))}")

    def _daily_candle_query(self, symbol: str, through: datetime, limit: int) -> str:
        return ("SELECT toString(toDate(event_time, 'America/New_York')) AS market_date, "
            "argMin(JSONExtractFloat(payload, 'p'), tuple(event_time, sequence)) AS open, "
            "max(JSONExtractFloat(payload, 'p')) AS high, min(JSONExtractFloat(payload, 'p')) AS low, "
            "argMax(JSONExtractFloat(payload, 'p'), tuple(event_time, sequence)) AS close, sum(JSONExtractFloat(payload, 's')) AS volume, count() AS trade_count "
            "FROM market_data.simulation_replay_events "
            f"WHERE dataset_id = {sql_string(self.dataset_id)} AND symbol = {sql_string(symbol)} AND event_type = 'trade' "
            f"AND event_time <= parseDateTime64BestEffort({sql_string(isoformat_z(through))}, 9) "
            "GROUP BY toDate(event_time, 'America/New_York') "
            f"ORDER BY toDate(event_time, 'America/New_York') DESC LIMIT {max(1, int(limit))}")


def replay_candle_payload(symbol: str, interval: str, candles: list[dict[str, object]], through: datetime) -> dict[str, object]:
    return {"symbol": symbol, "interval": interval, "source": "simulation_replay", "feed": "sip+boats", "simulation": True,
        "asOf": isoformat_z(through), "dataStatus": "ready" if candles else "empty", "backfillStatus": "not_available",
        "canBackfill": False, "indicators": {"ma": [], "volume": True}, "candles": candles}


def market_midnight_utc(market_date: str) -> datetime:
    parsed = date.fromisoformat(market_date)
    return datetime.combine(parsed, time.min, tzinfo=MARKET_TIMEZONE).astimezone(timezone.utc)


def isoformat_milliseconds_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sql_string(value: object) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"
