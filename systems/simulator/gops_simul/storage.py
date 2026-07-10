from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from gops_simul.config import Settings
from gops_simul.errors import BadRequest
from gops_simul.pagination import decode_cursor, encode_cursor, fingerprint_for
from gops_simul.time_utils import parse_record_time, parse_time


VALID_FEEDS = {"sip", "iex", "delayed_sip", "test", "boats", "overnight"}
VALID_STREAM_CHANNELS = {
    "trades",
    "quotes",
    "bars",
    "updatedBars",
    "dailyBars",
    "statuses",
    "lulds",
    "corrections",
    "cancelErrors",
}
KIND_FILES = {
    "bars": "bars_1m.jsonl",
    "trades": "trades.jsonl",
    "quotes": "quotes.jsonl",
}
KIND_RESPONSE_KEYS = {
    "bars": "bars",
    "trades": "trades",
    "quotes": "quotes",
}
KIND_MESSAGE_TYPES = {
    "bars": "b",
    "trades": "t",
    "quotes": "q",
}
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]{0,9}(\.[A-Z])?$")


@dataclass(frozen=True)
class QueryResult:
    payload: dict[str, object]
    flat_count: int


class SessionStore:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def root(self) -> Path:
        return self.settings.data_root

    def query(
        self,
        *,
        kind: str,
        symbols: list[str],
        feed: str,
        start: str | None,
        end: str | None,
        limit: int,
        sort: str,
        page_token: str | None = None,
        timeframe: str | None = None,
        adjustment: str | None = None,
    ) -> QueryResult:
        self.validate_feed(feed)
        symbols = normalize_symbols(symbols)
        normalized_timeframe = normalize_timeframe(timeframe) if kind == "bars" else None
        if kind == "bars" and normalized_timeframe != "1Min":
            raise BadRequest("simulator v1 supports only 1Min bars")
        if kind not in KIND_FILES:
            raise BadRequest(f"unsupported data kind: {kind}")
        if sort not in {"asc", "desc"}:
            raise BadRequest("sort must be asc or desc")
        if limit < 1 or limit > 10_000:
            raise BadRequest("limit must be between 1 and 10000")

        query_fingerprint = fingerprint_for(
            {
                "kind": kind,
                "symbols": sorted(symbols),
                "feed": feed,
                "start": start,
                "end": end,
                "sort": sort,
                "timeframe": normalized_timeframe,
                "adjustment": adjustment,
            }
        )
        cursor = decode_cursor(page_token, query_fingerprint)
        flat = self._flatten_records(kind=kind, feed=feed, symbols=symbols, start=start, end=end, sort=sort)
        page = flat[cursor.offset:cursor.offset + limit]
        next_offset = cursor.offset + len(page)
        next_page_token = encode_cursor(query_fingerprint, next_offset) if next_offset < len(flat) else None
        grouped = {symbol: [] for symbol in symbols}
        response_key = KIND_RESPONSE_KEYS[kind]
        for symbol, record in page:
            grouped.setdefault(symbol, []).append(record)
        return QueryResult(
            payload={response_key: grouped, "next_page_token": next_page_token},
            flat_count=len(flat),
        )

    def records_for_stream(
        self,
        *,
        feed: str,
        subscriptions: dict[str, set[str]],
    ) -> list[dict[str, object]]:
        self.validate_feed(feed)
        events: list[dict[str, object]] = []
        for channel, symbols in subscriptions.items():
            if not symbols:
                continue
            if channel not in VALID_STREAM_CHANNELS:
                continue
            if channel == "statuses":
                events.extend(self.status_events(feed=feed, symbols=symbols))
                continue
            if channel in {"lulds", "corrections", "cancelErrors", "dailyBars"}:
                continue
            kind = "bars" if channel in {"bars", "updatedBars"} else channel
            for symbol in self.expand_symbols(feed, kind, symbols):
                session_dates = self.resolve_stream_session_dates(feed, symbol, kind)
                for session_date in session_dates:
                    for record in self.read_records(feed, symbol, kind, session_date):
                        event = dict(record)
                        if channel == "updatedBars":
                            event["T"] = "u"
                        events.append(event)
        return sorted(events, key=lambda item: parse_record_time(item))

    def status_events(self, *, feed: str, symbols: Iterable[str]) -> list[dict[str, object]]:
        now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        expanded = sorted(symbols) if symbols and "*" not in symbols else sorted(self.available_symbols(feed, "trades"))
        return [
            {
                "T": "s",
                "S": symbol,
                "sc": "T",
                "sm": "Trading",
                "rc": "",
                "rm": "",
                "t": now,
                "z": "C",
            }
            for symbol in expanded
        ]

    def assets(self, *, status: str | None = "active", asset_class: str | None = "us_equity") -> list[dict[str, object]]:
        assets_file = self.root / "assets.json"
        if assets_file.exists():
            rows = json.loads(assets_file.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                raise BadRequest("assets.json must contain a list")
        else:
            rows = [default_asset(symbol) for symbol in sorted(self.all_symbols())]
        result = []
        for row in rows:
            if status and row.get("status") != status:
                continue
            if asset_class and row.get("asset_class") != asset_class and row.get("class") != asset_class:
                continue
            result.append(row)
        return result

    def all_symbols(self) -> set[str]:
        symbols: set[str] = set()
        if not self.root.exists():
            return symbols
        for feed_path in self.root.iterdir():
            if not feed_path.is_dir() or feed_path.name == "__pycache__":
                continue
            for symbol_path in feed_path.iterdir():
                if symbol_path.is_dir():
                    symbols.add(symbol_path.name.upper())
        return symbols

    def available_symbols(self, feed: str, kind: str) -> set[str]:
        self.validate_feed(feed)
        feed_path = self.root / feed
        if not feed_path.exists():
            return set()
        return {
            path.name.upper()
            for path in feed_path.iterdir()
            if path.is_dir() and any((date_dir / KIND_FILES[kind]).exists() for date_dir in path.iterdir() if date_dir.is_dir())
        }

    def expand_symbols(self, feed: str, kind: str, symbols: set[str]) -> list[str]:
        if not symbols or "*" in symbols:
            return sorted(self.available_symbols(feed, kind))
        return normalize_symbols(sorted(symbols))

    def resolve_session_date(self, feed: str, symbol: str, kind: str, requested: str) -> str | None:
        dates = self.available_session_dates(feed, symbol, kind)
        if not dates:
            return None
        if requested == "latest":
            return dates[-1]
        return requested if requested in dates else None

    def resolve_stream_session_dates(self, feed: str, symbol: str, kind: str) -> list[str]:
        dates = self.available_session_dates(feed, symbol, kind)
        if not dates:
            return []
        anchor = self._stream_anchor_date(dates)
        start = anchor - timedelta(days=self.settings.replay_lookback_days - 1)
        return [value for value in dates if start <= date.fromisoformat(value) <= anchor]

    def available_session_dates(self, feed: str, symbol: str, kind: str) -> list[str]:
        symbol_path = self.root / feed / symbol
        if not symbol_path.exists():
            return []
        return sorted(
            path.name
            for path in symbol_path.iterdir()
            if path.is_dir() and (path / KIND_FILES[kind]).exists()
        )

    def _stream_anchor_date(self, available_dates: list[str]) -> date:
        requested = self.settings.replay_date
        if requested == "latest":
            return date.fromisoformat(available_dates[-1])
        try:
            return date.fromisoformat(requested)
        except ValueError:
            return date.fromisoformat(available_dates[-1])

    def read_records(self, feed: str, symbol: str, kind: str, session_date: str) -> list[dict[str, object]]:
        path = self.root / feed / symbol / session_date / KIND_FILES[kind]
        if not path.exists():
            return []
        rows = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BadRequest(f"invalid JSONL at {path}:{line_number}") from exc
            if not isinstance(row, dict):
                raise BadRequest(f"JSONL row must be an object at {path}:{line_number}")
            rows.append(normalize_record(row, symbol=symbol, kind=kind))
        return rows

    def validate_feed(self, feed: str) -> None:
        if feed not in VALID_FEEDS:
            raise BadRequest(f"unsupported feed: {feed}")

    def _flatten_records(
        self,
        *,
        kind: str,
        feed: str,
        symbols: list[str],
        start: str | None,
        end: str | None,
        sort: str,
    ) -> list[tuple[str, dict[str, object]]]:
        start_time = parse_time(start)
        end_time = parse_time(end)
        if start_time and end_time and start_time > end_time:
            raise BadRequest("start must be before end")
        rows: list[tuple[str, dict[str, object]]] = []
        for symbol in symbols:
            for session_date in self._candidate_dates(feed, symbol, kind, start_time, end_time):
                for record in self.read_records(feed, symbol, kind, session_date):
                    timestamp = parse_record_time(record)
                    if start_time and timestamp < start_time:
                        continue
                    if end_time and timestamp > end_time:
                        continue
                    rows.append((symbol, record))
        reverse = sort == "desc"
        return sorted(rows, key=lambda item: (item[0], parse_record_time(item[1])), reverse=reverse)

    def _candidate_dates(
        self,
        feed: str,
        symbol: str,
        kind: str,
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> list[str]:
        symbol_path = self.root / feed / symbol
        if not symbol_path.exists():
            return []
        available = sorted(path.name for path in symbol_path.iterdir() if path.is_dir() and (path / KIND_FILES[kind]).exists())
        if not start_time and not end_time:
            resolved = self.resolve_session_date(feed, symbol, kind, self.settings.replay_date)
            return [resolved] if resolved else []
        start_date = (start_time or datetime.min.replace(tzinfo=UTC)).date()
        end_date = (end_time or datetime.max.replace(tzinfo=UTC)).date()
        return [value for value in available if start_date <= date.fromisoformat(value) <= end_date]


def normalize_symbols(symbols: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in symbols:
        symbol = str(value or "").strip().upper()
        if not symbol:
            continue
        if not SYMBOL_RE.fullmatch(symbol):
            raise BadRequest(f"invalid symbol: {symbol}")
        if symbol not in seen:
            normalized.append(symbol)
            seen.add(symbol)
    if not normalized:
        raise BadRequest("at least one symbol is required")
    return normalized


def parse_symbols_csv(value: str) -> list[str]:
    return normalize_symbols(part.strip() for part in value.split(","))


def normalize_timeframe(value: str | None) -> str:
    text = str(value or "1Min").strip()
    aliases = {
        "1m": "1Min",
        "1min": "1Min",
        "1Min": "1Min",
        "1T": "1Min",
        "1D": "1Day",
        "1day": "1Day",
        "1Day": "1Day",
    }
    return aliases.get(text, text)


def normalize_record(row: dict[str, object], *, symbol: str, kind: str) -> dict[str, object]:
    record = dict(row)
    record.setdefault("S", symbol)
    record.setdefault("T", KIND_MESSAGE_TYPES[kind])
    parse_record_time(record)
    return record


def default_asset(symbol: str) -> dict[str, object]:
    return {
        "id": f"sim-{symbol.lower()}",
        "class": "us_equity",
        "asset_class": "us_equity",
        "exchange": "NASDAQ",
        "symbol": symbol,
        "name": f"{symbol} Simulator Asset",
        "status": "active",
        "tradable": True,
        "marginable": True,
        "shortable": True,
        "easy_to_borrow": True,
        "fractionable": True,
    }
