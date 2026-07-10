from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Iterable

from alfaka.common.redis_keys import RedisKeyBuilder


DEFAULT_REALTIME_LAYERS = ("trades", "quotes")
SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{0,9}(?:\.[A-Z])?$")
USER_ID_PATTERN = re.compile(r"[^A-Za-z0-9_.@-]+")
WATCHLIST_SOURCE = "watchlist"
PORTFOLIO_SOURCE = "portfolio"
ACTIVE_CHART_SOURCE = "active-chart"
RANK_SOURCE_PREFIX = "rank:"
MANUAL_SOURCE = "manual"
ORDER_FLOW_SOURCE = "orderflow"
ACTIVE_CHART_REASON = "active-chart-session"
RANKING_KINDS = ("dollar-volume", "volume", "gainers", "losers")
DEFAULT_SUBSCRIPTION_EVENTS_MAXLEN = 10000


class RealtimeSubscriptionCohortService:
    """Manages realtime subscription source state and final subscription reconciliation.

    API/WS runtimes should use ``auto_reconcile=False`` so they only mutate user/source
    state. The subscription-controller pod owns ``reconcile()`` and writes the final
    ``subscription:*`` keys that ingestors read.
    """

    def __init__(self, redis_client, keys: RedisKeyBuilder | None = None, auto_reconcile: bool = True):
        self.redis = redis_client
        self.keys = keys or RedisKeyBuilder()
        self.auto_reconcile = auto_reconcile

    def replace_user_watchlist(self, user_id: str, symbols: Iterable[str]) -> list[str]:
        user_id = normalize_user_id(user_id)
        next_symbols = normalize_symbol_list(symbols)
        self._replace_set(self.keys.user_watchlist_symbols(user_id), next_symbols)
        self._replace_list(self.keys.user_watchlist_symbol_order(user_id), next_symbols)
        self._sadd(self.keys.subscription_users(WATCHLIST_SOURCE), user_id)
        self._after_source_state_changed()
        return next_symbols

    def replace_user_portfolio(self, user_id: str, symbols: Iterable[str]) -> list[str]:
        user_id = normalize_user_id(user_id)
        next_symbols = normalize_symbol_list(symbols)
        self._replace_set(self.keys.user_portfolio_symbols(user_id), next_symbols)
        self._sadd(self.keys.subscription_users(PORTFOLIO_SOURCE), user_id)
        self._after_source_state_changed()
        return next_symbols

    def refresh_active_chart(self, user_id: str, session_id: str, symbol: str, ttl_seconds: int) -> None:
        user_id = normalize_user_id(user_id)
        session_id = normalize_session_id(session_id)
        symbol = normalize_market_symbol(symbol)
        session_key = self.keys.user_active_chart_session(user_id, session_id)
        self._hset(session_key, {
            "userId": user_id,
            "sessionId": session_id,
            "symbol": symbol,
            "updatedAt": now_iso(),
        })
        self._expire(session_key, ttl_seconds)
        self._sadd(self.keys.user_active_chart_sessions(user_id), session_id)
        self._sadd(self.keys.subscription_users(ACTIVE_CHART_SOURCE), user_id)
        self._after_source_state_changed()

    def remove_active_chart(self, user_id: str, session_id: str) -> None:
        user_id = normalize_user_id(user_id)
        session_id = normalize_session_id(session_id)
        self._delete(self.keys.user_active_chart_session(user_id, session_id))
        self._srem(self.keys.user_active_chart_sessions(user_id), session_id)
        self._after_source_state_changed()

    def replace_ranking_source(self, kind: str, symbols: Iterable[str]) -> list[str]:
        normalized_kind = normalize_rank_kind(kind)
        next_symbols = normalize_symbol_list(symbols)
        self._replace_set(self.keys.rank_symbols(normalized_kind), next_symbols)
        self._after_source_state_changed()
        return next_symbols

    def replace_order_flow_source(self, symbols: Iterable[str]) -> list[str]:
        next_symbols = normalize_symbol_list(symbols)
        self._replace_set(self.keys.subscription_source_symbols(ORDER_FLOW_SOURCE), next_symbols)
        self._after_source_state_changed()
        return next_symbols

    def add_manual_source(self, symbol: str, layers: Iterable[str] | None = None) -> dict[str, str]:
        symbol = normalize_market_symbol(symbol)
        self._sadd(self.keys.subscription_source_symbols(MANUAL_SOURCE), symbol)
        self._sadd(self.keys.subscription_source_manual(symbol), ",".join(sorted(normalize_layers(layers) or set(DEFAULT_REALTIME_LAYERS))))
        records = self._after_source_state_changed()
        return records.get(symbol, subscription_record(symbol, set(DEFAULT_REALTIME_LAYERS), {MANUAL_SOURCE}, {})) if records else subscription_record(symbol, set(DEFAULT_REALTIME_LAYERS), {MANUAL_SOURCE}, {})

    def remove_manual_source(self, symbol: str) -> None:
        symbol = normalize_market_symbol(symbol)
        self._srem(self.keys.subscription_source_symbols(MANUAL_SOURCE), symbol)
        self._delete(self.keys.subscription_source_manual(symbol))
        self._after_source_state_changed()

    def reconcile(self) -> dict[str, dict[str, str]]:
        source_members = self._collect_source_members()
        self._write_aggregate_source_keys(source_members)
        records = self._build_subscription_records(source_members)
        previous_symbols = set(self._smembers(self.keys.subscription_symbols()))
        next_symbols = set(records)

        for symbol in sorted(previous_symbols - next_symbols):
            self._srem(self.keys.subscription_symbols(), symbol)
            self._delete(self.keys.subscription_symbol(symbol))

        if next_symbols:
            self._sadd_many(self.keys.subscription_symbols(), sorted(next_symbols))
        for symbol, record in records.items():
            self._hset(self.keys.subscription_symbol(symbol), record)

        version = self._incr(self.keys.subscription_version())
        self._xadd(self.keys.subscription_events(), {
            "action": "reconcile",
            "symbols": ",".join(sorted(next_symbols)),
            "version": str(version),
            "updatedAt": now_iso(),
        })
        return records

    def user_watchlist_symbols(self, user_id: str) -> list[str]:
        normalized_user_id = normalize_user_id(user_id)
        ordered = normalize_symbol_list(self._lrange(self.keys.user_watchlist_symbol_order(normalized_user_id)))
        if ordered:
            return ordered
        return normalize_symbol_list(self._smembers(self.keys.user_watchlist_symbols(normalized_user_id)))

    def user_portfolio_symbols(self, user_id: str) -> list[str]:
        return normalize_symbol_list(self._smembers(self.keys.user_portfolio_symbols(normalize_user_id(user_id))))

    def subscription_symbols(self) -> list[str]:
        return self._smembers(self.keys.subscription_symbols())

    def _after_source_state_changed(self) -> dict[str, dict[str, str]]:
        if self.auto_reconcile:
            return self.reconcile()
        return {}

    def _collect_source_members(self) -> dict[str, dict[str, set[str]]]:
        by_source: dict[str, dict[str, set[str]]] = {
            WATCHLIST_SOURCE: defaultdict(set),
            PORTFOLIO_SOURCE: defaultdict(set),
            ACTIVE_CHART_SOURCE: defaultdict(set),
            MANUAL_SOURCE: defaultdict(set),
            ORDER_FLOW_SOURCE: defaultdict(set),
        }
        for kind in RANKING_KINDS:
            by_source[f"{RANK_SOURCE_PREFIX}{kind}"] = defaultdict(set)

        self._collect_user_symbol_source(WATCHLIST_SOURCE, self.keys.user_watchlist_symbols, by_source[WATCHLIST_SOURCE])
        self._collect_user_symbol_source(PORTFOLIO_SOURCE, self.keys.user_portfolio_symbols, by_source[PORTFOLIO_SOURCE])
        self._collect_active_chart_source(by_source[ACTIVE_CHART_SOURCE])
        self._collect_legacy_watchlist_fallback(by_source[WATCHLIST_SOURCE])
        self._collect_legacy_portfolio_fallback(by_source[PORTFOLIO_SOURCE])
        self._collect_ranking_sources(by_source)
        self._collect_manual_sources(by_source[MANUAL_SOURCE])
        self._collect_order_flow_sources(by_source[ORDER_FLOW_SOURCE])
        return {source: dict(symbols) for source, symbols in by_source.items()}

    def _collect_user_symbol_source(self, source: str, key_fn, target: dict[str, set[str]]) -> None:
        users = self._smembers(self.keys.subscription_users(source))
        for user_id in users:
            for symbol in self._smembers(key_fn(user_id)):
                try:
                    target[normalize_market_symbol(symbol)].add(user_id)
                except ValueError:
                    continue

    def _collect_active_chart_source(self, target: dict[str, set[str]]) -> None:
        users = self._smembers(self.keys.subscription_users(ACTIVE_CHART_SOURCE))
        for user_id in users:
            sessions_key = self.keys.user_active_chart_sessions(user_id)
            for session_id in list(self._smembers(sessions_key)):
                session_key = self.keys.user_active_chart_session(user_id, session_id)
                if not self._exists(session_key):
                    self._srem(sessions_key, session_id)
                    continue
                record = self._hgetall(session_key)
                try:
                    symbol = normalize_market_symbol(record.get("symbol", ""))
                except ValueError:
                    continue
                target[symbol].add(f"{user_id}:{session_id}")

    def _collect_legacy_watchlist_fallback(self, target: dict[str, set[str]]) -> None:
        if target or self._smembers(self.keys.subscription_users(WATCHLIST_SOURCE)):
            return
        for symbol in self._smembers(self.keys.watchlist_symbols()):
            try:
                target[normalize_market_symbol(symbol)].add("__legacy__")
            except ValueError:
                continue

    def _collect_legacy_portfolio_fallback(self, target: dict[str, set[str]]) -> None:
        if target or self._smembers(self.keys.subscription_users(PORTFOLIO_SOURCE)):
            return
        for symbol in self._smembers(self.keys.portfolio_symbols()):
            try:
                target[normalize_market_symbol(symbol)].add("__legacy__")
            except ValueError:
                continue

    def _collect_ranking_sources(self, by_source: dict[str, dict[str, set[str]]]) -> None:
        for kind in RANKING_KINDS:
            source = f"{RANK_SOURCE_PREFIX}{kind}"
            for symbol in self._smembers(self.keys.rank_symbols(kind)):
                try:
                    by_source[source][normalize_market_symbol(symbol)].add(kind)
                except ValueError:
                    continue

    def _collect_manual_sources(self, target: dict[str, set[str]]) -> None:
        for symbol in self._smembers(self.keys.subscription_source_symbols(MANUAL_SOURCE)):
            try:
                target[normalize_market_symbol(symbol)].add("manual-monitor")
            except ValueError:
                continue

    def _collect_order_flow_sources(self, target: dict[str, set[str]]) -> None:
        for symbol in self._smembers(self.keys.subscription_source_symbols(ORDER_FLOW_SOURCE)):
            try:
                target[normalize_market_symbol(symbol)].add("orderflow-pin")
            except ValueError:
                continue

    def _write_aggregate_source_keys(self, source_members: dict[str, dict[str, set[str]]]) -> None:
        source_key_fns = {
            WATCHLIST_SOURCE: self.keys.subscription_source_watchlist,
            PORTFOLIO_SOURCE: self.keys.subscription_source_portfolio,
            ACTIVE_CHART_SOURCE: self.keys.subscription_source_active_chart,
        }
        for source, key_fn in source_key_fns.items():
            self._replace_aggregate_source(source, key_fn, source_members.get(source, {}))
        for kind in RANKING_KINDS:
            source = f"{RANK_SOURCE_PREFIX}{kind}"
            self._replace_aggregate_source(source, lambda symbol, kind=kind: self.keys.subscription_source_ranking(kind, symbol), source_members.get(source, {}))
        self._replace_set(self.keys.subscription_source_symbols(ORDER_FLOW_SOURCE), sorted(source_members.get(ORDER_FLOW_SOURCE, {})))

    def _replace_aggregate_source(self, source: str, key_fn, members_by_symbol: dict[str, set[str]]) -> None:
        index_key = self.keys.subscription_source_symbols(source)
        previous = set(self._smembers(index_key))
        current = set(members_by_symbol)
        for symbol in sorted(previous - current):
            self._delete(key_fn(symbol))
        self._replace_set(index_key, sorted(current))
        for symbol, members in members_by_symbol.items():
            self._replace_set(key_fn(symbol), sorted(members))

    def _build_subscription_records(self, source_members: dict[str, dict[str, set[str]]]) -> dict[str, dict[str, str]]:
        by_symbol: dict[str, set[str]] = defaultdict(set)
        counts: dict[str, dict[str, int]] = defaultdict(dict)
        for source, symbols in source_members.items():
            for symbol, members in symbols.items():
                if not members:
                    continue
                by_symbol[symbol].add(source)
                if source == WATCHLIST_SOURCE:
                    counts[symbol]["watchlistUserCount"] = len([member for member in members if member != "__legacy__"])
                elif source == PORTFOLIO_SOURCE:
                    counts[symbol]["portfolioUserCount"] = len([member for member in members if member != "__legacy__"])
                elif source == ACTIVE_CHART_SOURCE:
                    counts[symbol]["activeChartSessionCount"] = len(members)
        records = {}
        for symbol, sources in sorted(by_symbol.items()):
            layers = set(DEFAULT_REALTIME_LAYERS)
            if ACTIVE_CHART_SOURCE in sources:
                layers.add("candles")
            if MANUAL_SOURCE in sources:
                for member in self._smembers(self.keys.subscription_source_manual(symbol)):
                    layers.update(read_csv_set(member))
            records[symbol] = subscription_record(symbol, layers, sources, counts[symbol])
        return records

    def _replace_set(self, key: str, values: Iterable[str]) -> None:
        next_values = list(values)
        self._delete(key)
        self._sadd_many(key, next_values)

    def _replace_list(self, key: str, values: Iterable[str]) -> None:
        next_values = list(values)
        self._delete(key)
        self._rpush_many(key, next_values)

    def _hgetall(self, key: str) -> dict[str, str]:
        method = getattr(self.redis, "hgetall", None) if self.redis else None
        if not callable(method):
            return {}
        raw = method(key) or {}
        return {decode(k): decode(v) for k, v in raw.items()}

    def _hset(self, key: str, mapping: dict[str, Any]) -> None:
        method = getattr(self.redis, "hset", None) if self.redis else None
        if callable(method):
            method(key, mapping={k: encode_record_value(v) for k, v in mapping.items()})

    def _smembers(self, key: str) -> list[str]:
        method = getattr(self.redis, "smembers", None) if self.redis else None
        if not callable(method):
            return []
        return sorted(decode(value) for value in method(key) or [])

    def _sadd(self, key: str, value: str) -> None:
        method = getattr(self.redis, "sadd", None) if self.redis else None
        if callable(method):
            method(key, value)

    def _sadd_many(self, key: str, values: Iterable[str]) -> None:
        values = list(values)
        method = getattr(self.redis, "sadd", None) if self.redis else None
        if callable(method) and values:
            method(key, *values)

    def _rpush_many(self, key: str, values: Iterable[str]) -> None:
        values = list(values)
        method = getattr(self.redis, "rpush", None) if self.redis else None
        if callable(method) and values:
            method(key, *values)

    def _lrange(self, key: str) -> list[str]:
        method = getattr(self.redis, "lrange", None) if self.redis else None
        if not callable(method):
            return []
        return [decode(value) for value in method(key, 0, -1) or []]

    def _srem(self, key: str, value: str) -> None:
        method = getattr(self.redis, "srem", None) if self.redis else None
        if callable(method):
            method(key, value)

    def _delete(self, key: str) -> None:
        method = getattr(self.redis, "delete", None) if self.redis else None
        if callable(method):
            method(key)

    def _expire(self, key: str, seconds: int) -> None:
        method = getattr(self.redis, "expire", None) if self.redis else None
        if callable(method):
            method(key, seconds)

    def _exists(self, key: str) -> bool:
        method = getattr(self.redis, "exists", None) if self.redis else None
        if callable(method):
            return bool(method(key))
        return bool(self._hgetall(key))

    def _incr(self, key: str) -> int:
        method = getattr(self.redis, "incr", None) if self.redis else None
        if callable(method):
            try:
                return int(method(key))
            except Exception:
                return 0
        return 0

    def _xadd(self, key: str, fields: dict[str, Any]) -> None:
        method = getattr(self.redis, "xadd", None) if self.redis else None
        if callable(method):
            payload = {k: encode_record_value(v) for k, v in fields.items()}
            try:
                method(key, payload, maxlen=subscription_events_maxlen_from_env(), approximate=True)
            except TypeError:
                method(key, payload)


def subscription_record(symbol: str, layers: set[str], sources: set[str], counts: dict[str, int]) -> dict[str, str]:
    return {
        "symbol": symbol,
        "enabled": "true",
        "layers": ",".join(sorted(layers)),
        "sources": ",".join(sorted(sources)),
        "reason": reason_for_sources(sources),
        "source": "subscription-controller",
        "updatedAt": now_iso(),
        "watchlistUserCount": str(counts.get("watchlistUserCount", 0)),
        "portfolioUserCount": str(counts.get("portfolioUserCount", 0)),
        "activeChartSessionCount": str(counts.get("activeChartSessionCount", 0)),
    }


def reason_for_sources(sources: set[str]) -> str:
    if len(sources) > 1:
        return "multi-source"
    source = next(iter(sources), "")
    if source == ACTIVE_CHART_SOURCE:
        return ACTIVE_CHART_REASON
    if source == WATCHLIST_SOURCE:
        return "watchlist"
    if source == PORTFOLIO_SOURCE:
        return "portfolio"
    if source.startswith(RANK_SOURCE_PREFIX):
        return source
    if source == MANUAL_SOURCE:
        return "manual-monitor"
    if source == ORDER_FLOW_SOURCE:
        return "orderflow"
    return source or "unknown"


def read_sources(record: dict[str, str]) -> set[str]:
    return read_csv_set(record.get("sources"))


def read_csv_set(value) -> set[str]:
    return {item.strip() for item in str(value or "").split(",") if item.strip()}


def normalize_layers(layers: Iterable[str] | None) -> set[str]:
    allowed = {"trades", "quotes", "events", "candles"}
    normalized = {str(layer).strip().lower() for layer in layers or [] if str(layer).strip()}
    invalid = normalized - allowed
    if invalid:
        raise ValueError(f"Unsupported subscription layers: {', '.join(sorted(invalid))}")
    return normalized


def normalize_symbol_list(symbols: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in symbols or []:
        if not isinstance(value, str):
            continue
        symbol = normalize_market_symbol(value)
        if symbol in seen:
            continue
        normalized.append(symbol)
        seen.add(symbol)
    return normalized


def normalize_market_symbol(symbol: str) -> str:
    normalized = str(symbol).strip().upper()
    if not SYMBOL_PATTERN.fullmatch(normalized):
        raise ValueError(f"Invalid market symbol: {normalized}")
    return normalized


def normalize_rank_kind(kind: str) -> str:
    normalized = str(kind or "").strip().lower().replace("_", "-")
    aliases = {
        "dollar": "dollar-volume",
        "dollar-volume": "dollar-volume",
        "volume": "volume",
        "gainer": "gainers",
        "gainers": "gainers",
        "loser": "losers",
        "losers": "losers",
    }
    if normalized not in aliases:
        raise ValueError(f"Unsupported ranking kind: {kind}")
    return aliases[normalized]


def subscription_events_maxlen_from_env(environ=None) -> int:
    environ = environ or os.environ
    try:
        parsed = int(environ.get("SUBSCRIPTION_EVENTS_MAXLEN", str(DEFAULT_SUBSCRIPTION_EVENTS_MAXLEN)))
    except (TypeError, ValueError):
        return DEFAULT_SUBSCRIPTION_EVENTS_MAXLEN
    return parsed if parsed > 0 else DEFAULT_SUBSCRIPTION_EVENTS_MAXLEN


def normalize_user_id(user_id: str) -> str:
    normalized = USER_ID_PATTERN.sub("_", str(user_id or "").strip())
    return normalized.strip("_") or "anonymous"


def normalize_session_id(session_id: str) -> str:
    normalized = USER_ID_PATTERN.sub("_", str(session_id or "").strip())
    return normalized.strip("_") or "session"


def decode(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def encode_record_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, set, tuple)):
        return ",".join(str(item) for item in value)
    return str(value)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
