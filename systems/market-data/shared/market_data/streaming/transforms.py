# 역할: Raw Envelope를 차트용 Trade/Candle 데이터로 변환합니다.
# 사용: stream_processor가 현재가, 실시간 1분봉, 확정 1/5/10분봉, 이동평균을 계산합니다.
# 주의: 운영에서는 이 로직을 Python/Kubernetes processor pod에서 실행합니다.
from collections import OrderedDict, defaultdict, deque
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from market_data.common.canonical import candle_metadata
from market_data.common.symbols import is_crypto_symbol
from market_data.serving.intervals import INTRADAY_DERIVED_INTERVALS, INTRADAY_INTERVAL_MINUTES
from market_data.serving.session_buckets import (
    BUCKET_POLICY_CLOCK_ALIGNED,
    BUCKET_POLICY_EXTENDED_SESSION,
    BUCKET_POLICY_REGULAR_SESSION,
    extended_session_bucket,
    regular_session_bucket,
)

MARKET_TIMEZONE = ZoneInfo("America/New_York")
DEFAULT_CLOSED_KEY_CAP = 2048


class BoundedKeySet:
    def __init__(self, max_items=DEFAULT_CLOSED_KEY_CAP):
        self.max_items = max(1, int(max_items))
        self._items = OrderedDict()
        self.evictions = 0

    def add(self, key):
        self._items.pop(key, None)
        self._items[key] = None
        while len(self._items) > self.max_items:
            self._items.popitem(last=False)
            self.evictions += 1

    def __contains__(self, key):
        return key in self._items

    def __len__(self):
        return len(self._items)


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def to_iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def float_or_zero(value):
    """crypto 거래량처럼 소수일 수 있는 값을 float로 바꾸고 실패하면 0.0을 씁니다."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def int_or_zero(value):
    """ClickHouse UInt64 JSON 문자열을 포함한 정수형 값을 안전하게 정규화합니다."""
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        try:
            parsed = int(float(value or 0))
        except (TypeError, ValueError, OverflowError):
            return 0
    return max(0, parsed)


def normalize_candle_numeric_fields(candle):
    """캔들 경계에서 OHLCV와 거래 수의 런타임 타입을 하나로 통일합니다."""
    normalized = dict(candle)
    for field in ("open", "high", "low", "close", "volume"):
        if field in normalized:
            normalized[field] = float_or_zero(normalized.get(field))
    if normalized.get("vwap") is not None:
        normalized["vwap"] = float_or_zero(normalized.get("vwap"))
    normalized["tradeCount"] = int_or_zero(
        normalized.get("tradeCount", normalized.get("trade_count"))
    )
    return normalized


def floor_minute(value):
    dt = parse_time(value) if isinstance(value, str) else value
    return dt.replace(second=0, microsecond=0)


def floor_interval(value, minutes):
    dt = parse_time(value) if isinstance(value, str) else value
    if minutes >= 60:
        bucket_hours = max(1, minutes // 60)
        bucket_hour = (dt.hour // bucket_hours) * bucket_hours
        return dt.replace(hour=bucket_hour, minute=0, second=0, microsecond=0)
    bucket_minute = (dt.minute // minutes) * minutes
    return dt.replace(minute=bucket_minute, second=0, microsecond=0)


def floor_day(value):
    dt = parse_time(value) if isinstance(value, str) else value
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def floor_market_day(value):
    dt = parse_time(value) if isinstance(value, str) else value
    market_dt = dt.astimezone(MARKET_TIMEZONE)
    market_day = market_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return market_day.astimezone(timezone.utc)


def floor_week(value):
    day = floor_day(value)
    return day - timedelta(days=day.weekday())


def floor_month(value):
    day = floor_day(value)
    return day.replace(day=1)


def normalize_bar(envelope, correction_type="NONE"):
    raw = envelope["raw"]
    interval = "1D" if envelope["channel"] == "dailyBars" else "1m"
    source = {
        "updatedBars": "alpaca.updatedBars",
        "dailyBars": "alpaca.dailyBars",
    }.get(envelope["channel"], "alpaca.bars")
    metadata = candle_metadata(envelope.get("priceAdjustment") or envelope.get("price_adjustment"), envelope.get("canonicalVersion") or envelope.get("canonical_version"))
    return normalize_candle_numeric_fields({
        "eventType": "CANDLE",
        "symbol": envelope["symbol"],
        "interval": interval,
        "timestamp": raw.get("t"),
        "open": raw.get("o"),
        "high": raw.get("h"),
        "low": raw.get("l"),
        "close": raw.get("c"),
        "volume": raw.get("v"),
        "tradeCount": raw.get("n"),
        "vwap": raw.get("vw"),
        "ma": {},
        "isClosed": True,
        "correctionType": correction_type,
        "source": source,
        "feed": envelope.get("feed"),
        "feedProfile": envelope.get("feedProfile"),
        "marketSession": envelope.get("marketSession"),
        "sourceEventId": envelope.get("sourceEventId"),
        "createdAt": envelope.get("receivedAt"),
        **({"simulation": envelope["simulation"]} if envelope.get("simulation") else {}),
        **metadata,
    })


def normalize_trade(envelope):
    raw = envelope["raw"]
    return {
        "eventType": "TRADE",
        "symbol": envelope["symbol"],
        "tradeId": raw.get("i"),
        "price": raw.get("p"),
        "size": raw.get("s"),
        "exchange": raw.get("x"),
        "conditions": raw.get("c", []),
        "tape": raw.get("z"),
        "timestamp": raw.get("t"),
        "source": "alpaca",
        "feed": envelope.get("feed"),
        "feedProfile": envelope.get("feedProfile"),
        "marketSession": envelope.get("marketSession"),
        "sourceEventId": envelope.get("sourceEventId"),
        "receivedAt": envelope.get("receivedAt"),
        **({"simulation": envelope["simulation"]} if envelope.get("simulation") else {}),
    }


def normalize_quote(envelope):
    raw = envelope["raw"]
    return {
        "eventType": "QUOTE",
        "layer": "quotes",
        "symbol": envelope["symbol"],
        "bidPrice": raw.get("bp") or raw.get("bidPrice"),
        "bidSize": raw.get("bs") or raw.get("bidSize"),
        "askPrice": raw.get("ap") or raw.get("askPrice"),
        "askSize": raw.get("as") or raw.get("askSize"),
        "bidExchange": raw.get("bx") or raw.get("bidExchange"),
        "askExchange": raw.get("ax") or raw.get("askExchange"),
        "conditions": raw.get("c", []),
        "timestamp": raw.get("t"),
        "source": "alpaca.quotes",
        "feed": envelope.get("feed"),
        "feedProfile": envelope.get("feedProfile"),
        "marketSession": envelope.get("marketSession"),
        "sourceEventId": envelope.get("sourceEventId"),
        "receivedAt": envelope.get("receivedAt"),
        **({"simulation": envelope["simulation"]} if envelope.get("simulation") else {}),
    }


def normalize_status(envelope):
    raw = envelope.get("raw") or {}
    status = raw.get("sc") or raw.get("status") or raw.get("msg") or raw.get("T") or "unknown"
    status_type = raw.get("st") or raw.get("statusType") or raw.get("T") or "market"
    return {
        "eventType": "MARKET_STATUS",
        "eventTime": envelope.get("eventTime") or envelope.get("receivedAt"),
        "symbol": envelope.get("symbol") or "_MARKET",
        "statusType": status_type,
        "status": status,
        "reason": raw.get("r") or raw.get("reason"),
        "source": "alpaca",
        "feed": envelope.get("feed"),
        "feedProfile": envelope.get("feedProfile"),
        "marketSession": envelope.get("marketSession"),
        "sourceEventId": envelope.get("sourceEventId"),
        "raw": raw,
        **({"simulation": envelope["simulation"]} if envelope.get("simulation") else {}),
    }


class LiveCandleBuilder:
    def __init__(self, max_minutes_per_symbol=2):
        self.candles = {}
        self.max_minutes_per_symbol = max(1, int(max_minutes_per_symbol))
        self.evictions = 0

    def seed(self, candle):
        candle = normalize_candle_numeric_fields(candle)
        if candle.get("interval") != "1m" or candle.get("isClosed"):
            return False
        timestamp = candle.get("timestamp")
        symbol = candle.get("symbol")
        if not timestamp or not symbol:
            return False
        self.candles[(symbol, timestamp)] = dict(candle)
        self._prune_symbol(symbol)
        return True

    def update(self, trade):
        bucket = floor_minute(trade["timestamp"])
        key = (trade["symbol"], to_iso(bucket))
        price = float_or_zero(trade["price"])
        size = float_or_zero(trade.get("size"))
        candle = self.candles.get(key)

        if candle is None:
            candle = {
                "eventType": "LIVE_CANDLE",
                "symbol": trade["symbol"],
                "interval": "1m",
                "timestamp": to_iso(bucket),
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": size,
                "tradeCount": 1,
                "isClosed": False,
                "source": "alpaca.trades",
                "sourceInterval": "trades",
                "feed": trade.get("feed"),
                "feedProfile": trade.get("feedProfile"),
                "marketSession": trade.get("marketSession"),
                "sourceEventId": trade.get("sourceEventId"),
                "updatedAt": trade.get("receivedAt"),
                **({"simulation": trade["simulation"]} if trade.get("simulation") else {}),
                **candle_metadata("live"),
            }
        else:
            candle["high"] = max(candle["high"], price)
            candle["low"] = min(candle["low"], price)
            candle["close"] = price
            candle["volume"] += size
            candle["tradeCount"] = int_or_zero(candle.get("tradeCount")) + 1
            candle["feed"] = trade.get("feed") or candle.get("feed")
            candle["feedProfile"] = trade.get("feedProfile") or candle.get("feedProfile")
            candle["marketSession"] = trade.get("marketSession") or candle.get("marketSession")
            candle["sourceEventId"] = trade.get("sourceEventId")
            candle["updatedAt"] = trade.get("receivedAt")
            if trade.get("simulation"):
                candle["simulation"] = trade["simulation"]

        self.candles[key] = candle
        self._prune_symbol(trade["symbol"])
        return candle

    def _prune_symbol(self, symbol):
        keys = sorted(
            (key for key in self.candles if key[0] == symbol),
            key=lambda key: parse_time(key[1]),
        )
        for key in keys[:-self.max_minutes_per_symbol]:
            if self.candles.pop(key, None) is not None:
                self.evictions += 1


class ProvisionalCandleState:
    def __init__(self, max_closed_1m=2000, max_closed_1d=400):
        self.closed = defaultdict(dict)
        self.max_closed = {"1m": max_closed_1m, "1D": max_closed_1d}

    def record_closed(self, candle):
        candle = normalize_candle_numeric_fields(candle)
        interval = candle.get("interval")
        if interval not in self.max_closed:
            return
        key = (candle["symbol"], interval)
        self.closed[key][candle["timestamp"]] = dict(candle)
        self._prune(key, self.max_closed[interval])

    def build_from_1m(self, symbol, target_interval, anchor_timestamp=None, live_1m=None):
        anchor = anchor_timestamp or (live_1m or {}).get("timestamp")
        if not anchor:
            return None
        if target_interval in INTRADAY_DERIVED_INTERVALS:
            minutes = INTRADAY_INTERVAL_MINUTES[target_interval]
            if is_crypto_symbol(symbol):
                bucket = floor_interval(anchor, minutes)
                end = bucket + timedelta(minutes=minutes)
                bucket_policy = BUCKET_POLICY_CLOCK_ALIGNED
            else:
                session_bucket = regular_session_bucket(anchor, target_interval)
                if session_bucket is None:
                    extended_bucket = extended_session_bucket(anchor, target_interval)
                    if extended_bucket is None:
                        return None
                    bucket, end = extended_bucket.start, extended_bucket.end
                    bucket_policy = BUCKET_POLICY_EXTENDED_SESSION
                else:
                    bucket, end = session_bucket.start, session_bucket.end
                    bucket_policy = BUCKET_POLICY_REGULAR_SESSION
        elif target_interval == "1D":
            bucket = floor_market_day(anchor)
            end = bucket + timedelta(days=1)
            bucket_policy = BUCKET_POLICY_REGULAR_SESSION
        else:
            raise ValueError(f"Unsupported 1m provisional target: {target_interval}")

        rows = self._closed_rows_in_window(symbol, "1m", bucket, end)
        if live_1m and self._contains_timestamp(live_1m, bucket, end):
            rows = [row for row in rows if row.get("timestamp") != live_1m.get("timestamp")]
            rows.append(live_1m)
        if not rows:
            return None
        return build_provisional_candle(
            symbol=symbol,
            interval=target_interval,
            bucket=bucket,
            rows=rows,
            source_interval="1m",
            bucket_policy=bucket_policy,
        )

    def build_from_1d(self, symbol, target_interval, anchor_timestamp=None, provisional_1d=None):
        anchor = anchor_timestamp or (provisional_1d or {}).get("timestamp")
        if not anchor:
            return None
        if target_interval == "1W":
            bucket = floor_week(anchor)
            end = bucket + timedelta(days=7)
        elif target_interval == "1M":
            bucket = floor_month(anchor)
            month = bucket.month + 1
            year = bucket.year + (1 if month == 13 else 0)
            next_month = bucket.replace(year=year, month=1 if month == 13 else month)
            end = next_month
        else:
            raise ValueError(f"Unsupported 1D provisional target: {target_interval}")

        rows = self._closed_rows_in_window(symbol, "1D", bucket, end)
        if provisional_1d and self._contains_timestamp(provisional_1d, bucket, end):
            rows = [row for row in rows if row.get("timestamp") != provisional_1d.get("timestamp")]
            rows.append(provisional_1d)
        if not rows:
            return None
        return build_provisional_candle(
            symbol=symbol,
            interval=target_interval,
            bucket=bucket,
            rows=rows,
            source_interval="1D",
        )

    def _closed_rows_in_window(self, symbol, interval, start, end):
        rows = []
        for candle in self.closed.get((symbol, interval), {}).values():
            if self._contains_timestamp(candle, start, end):
                rows.append(candle)
        return rows

    @staticmethod
    def _contains_timestamp(candle, start, end):
        timestamp = candle.get("timestamp")
        if not timestamp:
            return False
        value = parse_time(timestamp)
        return start <= value < end

    def _prune(self, key, max_items):
        rows = self.closed[key]
        if len(rows) <= max_items:
            return
        for timestamp in sorted(rows, key=parse_time)[:len(rows) - max_items]:
            rows.pop(timestamp, None)


def build_provisional_candle(symbol, interval, bucket, rows, source_interval, bucket_policy=BUCKET_POLICY_CLOCK_ALIGNED):
    candles = sorted(
        (normalize_candle_numeric_fields(candle) for candle in rows),
        key=lambda candle: parse_time(candle["timestamp"]),
    )
    latest = candles[-1]
    return {
        "eventType": "LIVE_CANDLE",
        "symbol": symbol,
        "interval": interval,
        "timestamp": to_iso(bucket),
        "open": candles[0]["open"],
        "high": max(candle["high"] for candle in candles),
        "low": min(candle["low"] for candle in candles),
        "close": latest["close"],
        "volume": sum(candle.get("volume") or 0 for candle in candles),
        "tradeCount": sum(candle.get("tradeCount") or 0 for candle in candles),
        "vwap": latest.get("vwap"),
        "ma": {},
        "isClosed": False,
        "source": "derived.live",
        "sourceInterval": source_interval,
        "bucketPolicy": bucket_policy,
        "feed": latest.get("feed"),
        "feedProfile": latest.get("feedProfile"),
        "marketSession": latest.get("marketSession"),
        "sourceEventId": latest.get("sourceEventId"),
        "updatedAt": latest.get("updatedAt") or latest.get("createdAt"),
        **({"simulation": latest["simulation"]} if latest.get("simulation") else {}),
        **candle_metadata(latest.get("priceAdjustment"), latest.get("canonicalVersion")),
    }


class MovingAverageState:
    def __init__(self):
        self.closes = defaultdict(dict)
        self.evictions = 0

    def attach_ma(self, candle):
        key = (candle["symbol"], candle["interval"])
        self.closes[key][candle["timestamp"]] = candle["close"]
        timestamps = sorted(self.closes[key], key=parse_time)
        for timestamp in timestamps[:-60]:
            if self.closes[key].pop(timestamp, None) is not None:
                self.evictions += 1
        timestamps = timestamps[-60:]
        closes = [self.closes[key][timestamp] for timestamp in timestamps]
        ma = {}
        for window in (5, 20, 60):
            if len(closes) >= window:
                values = closes[-window:]
                ma[f"ma{window}"] = sum(values) / window
        candle["ma"] = ma
        return candle


class CandleAggregator:
    def __init__(self, max_closed_keys=DEFAULT_CLOSED_KEY_CAP, bucket_policy=BUCKET_POLICY_CLOCK_ALIGNED):
        self.windows = defaultdict(dict)
        self.window_ends = {}
        self.closed_keys = BoundedKeySet(max_closed_keys)
        self.recomputes = 0
        self.bucket_policy = bucket_policy

    def update(self, candle_1m, interval_minutes):
        candle_1m = normalize_candle_numeric_fields(candle_1m)
        resolved = self._bucket_window(candle_1m, interval_minutes)
        if resolved is None:
            return None
        interval, bucket, end = resolved
        key = (candle_1m["symbol"], interval, to_iso(bucket))
        if key in self.closed_keys:
            return None
        previous = None
        if self.bucket_policy == BUCKET_POLICY_REGULAR_SESSION:
            stale_keys = sorted(
                (
                    candidate for candidate in self.windows
                    if candidate[0] == key[0] and candidate[1] == key[1] and candidate != key
                ),
                key=lambda candidate: parse_time(candidate[2]),
            )
            for stale_key in stale_keys:
                stale_window = self.windows.pop(stale_key, {})
                self.window_ends.pop(stale_key, None)
                if stale_window:
                    previous = self._aggregate(stale_key, stale_window.values())
                    self.closed_keys.add(stale_key)
        window = self.windows[key]
        window[candle_1m["timestamp"]] = candle_1m
        self.window_ends[key] = end

        if self.bucket_policy == BUCKET_POLICY_REGULAR_SESSION:
            candle_end = parse_time(candle_1m["timestamp"]) + timedelta(minutes=1)
            ready = candle_end >= end
        else:
            ready = len(window) >= interval_minutes
        if not ready:
            return previous

        result = self._aggregate(key, window.values())
        self.windows.pop(key, None)
        self.window_ends.pop(key, None)
        self.closed_keys.add(key)
        return result

    def recompute(self, corrected_candle, interval_minutes, source_candles):
        corrected_candle = normalize_candle_numeric_fields(corrected_candle)
        resolved = self._bucket_window(corrected_candle, interval_minutes)
        if resolved is None:
            return None
        interval, bucket, end = resolved
        key = (corrected_candle["symbol"], interval, to_iso(bucket))
        rows = {
            candle["timestamp"]: normalize_candle_numeric_fields(candle)
            for candle in source_candles
            if candle.get("symbol", corrected_candle["symbol"]) == corrected_candle["symbol"]
            and candle.get("timestamp")
            and bucket <= parse_time(candle["timestamp"]) < end
        }
        rows[corrected_candle["timestamp"]] = dict(corrected_candle)
        if self.bucket_policy != BUCKET_POLICY_REGULAR_SESSION and len(rows) < interval_minutes:
            return None
        result = self._aggregate(key, rows.values())
        result["correctionType"] = "UPDATED"
        result["sourceEventId"] = corrected_candle.get("sourceEventId")
        result["createdAt"] = corrected_candle.get("createdAt")
        self.recomputes += 1
        return result

    def has_open_window(self, candle_1m, interval_minutes):
        key = self._key(candle_1m, interval_minutes)
        return key in self.windows

    def is_closed_window(self, candle_1m, interval_minutes):
        return self._key(candle_1m, interval_minutes) in self.closed_keys

    def _key(self, candle_1m, interval_minutes):
        resolved = self._bucket_window(candle_1m, interval_minutes)
        if resolved is None:
            return (candle_1m["symbol"], intraday_interval_for_minutes(interval_minutes), "outside-session")
        interval, bucket, _end = resolved
        return (candle_1m["symbol"], interval, to_iso(bucket))

    def _bucket_window(self, candle_1m, interval_minutes):
        interval = intraday_interval_for_minutes(interval_minutes)
        if self.bucket_policy == BUCKET_POLICY_REGULAR_SESSION and not is_crypto_symbol(candle_1m.get("symbol")):
            session_bucket = regular_session_bucket(candle_1m.get("timestamp"), interval)
            if session_bucket is None:
                return None
            return interval, session_bucket.start, session_bucket.end
        bucket = floor_interval(candle_1m["timestamp"], interval_minutes)
        return interval, bucket, bucket + timedelta(minutes=interval_minutes)

    def _aggregate(self, key, source_candles):
        candles = sorted(source_candles, key=lambda candle: parse_time(candle["timestamp"]))
        latest = candles[-1]
        return {
            "eventType": "CANDLE",
            "symbol": key[0],
            "interval": key[1],
            "timestamp": key[2],
            "open": candles[0]["open"],
            "high": max(candle["high"] for candle in candles),
            "low": min(candle["low"] for candle in candles),
            "close": candles[-1]["close"],
            "volume": sum(candle.get("volume") or 0 for candle in candles),
            "tradeCount": sum(candle.get("tradeCount") or 0 for candle in candles),
            "vwap": candles[-1].get("vwap"),
            "ma": {},
            "isClosed": True,
            "correctionType": latest.get("correctionType", "NONE"),
            "source": "stream-processor",
            "sourceInterval": "1m",
            "bucketPolicy": self.bucket_policy,
            "feed": latest.get("feed"),
            "feedProfile": latest.get("feedProfile"),
            "marketSession": latest.get("marketSession"),
            "sourceEventId": latest.get("sourceEventId"),
            "createdAt": latest.get("createdAt"),
            **({"simulation": latest["simulation"]} if latest.get("simulation") else {}),
            **candle_metadata(latest.get("priceAdjustment"), latest.get("canonicalVersion")),
        }


def intraday_interval_for_minutes(interval_minutes):
    minutes = int(interval_minutes)
    for interval, candidate in INTRADAY_INTERVAL_MINUTES.items():
        if candidate == minutes:
            return interval
    return f"{minutes}m"


class TickWindowCandleBuilder:
    def __init__(self, grace_seconds=5, max_closed_keys=DEFAULT_CLOSED_KEY_CAP):
        self.grace = timedelta(seconds=max(0, int(grace_seconds)))
        self.windows = {}
        self.closed_keys = BoundedKeySet(max_closed_keys)
        self.max_event_time = None

    def update(self, trade):
        event_time = parse_time(trade["timestamp"])
        if self.max_event_time is None or event_time > self.max_event_time:
            self.max_event_time = event_time
        bucket = floor_minute(event_time)
        key = (trade["symbol"], to_iso(bucket), trade.get("feedProfile") or trade.get("feed") or "unknown")
        if key in self.closed_keys:
            return False
        price = float(trade["price"])
        size = float_or_zero(trade.get("size"))
        current = self.windows.get(key)
        if current is None:
            current = {
                "symbol": trade["symbol"],
                "timestamp": to_iso(bucket),
                "windowEnd": to_iso(bucket + timedelta(minutes=1)),
                "open": price,
                "openTime": event_time,
                "high": price,
                "low": price,
                "close": price,
                "closeTime": event_time,
                "volume": 0,
                "tradeCount": 0,
                "notional": 0.0,
                "feed": trade.get("feed"),
                "feedProfile": trade.get("feedProfile"),
                "marketSession": trade.get("marketSession"),
                "sourceEventId": trade.get("sourceEventId"),
                "createdAt": trade.get("receivedAt"),
                **({"simulation": trade["simulation"]} if trade.get("simulation") else {}),
            }
        if event_time < current["openTime"]:
            current["open"] = price
            current["openTime"] = event_time
        if event_time >= current["closeTime"]:
            current["close"] = price
            current["closeTime"] = event_time
        current["high"] = max(current["high"], price)
        current["low"] = min(current["low"], price)
        current["volume"] += size
        current["tradeCount"] += 1
        current["notional"] += price * size
        current["feed"] = trade.get("feed") or current.get("feed")
        current["feedProfile"] = trade.get("feedProfile") or current.get("feedProfile")
        current["marketSession"] = trade.get("marketSession") or current.get("marketSession")
        current["sourceEventId"] = trade.get("sourceEventId")
        current["createdAt"] = trade.get("receivedAt") or current.get("createdAt")
        if trade.get("simulation"):
            current["simulation"] = trade["simulation"]
        self.windows[key] = current
        return True

    def flush_ready(self, reference_time=None):
        reference = reference_time or self.max_event_time
        if reference is None:
            return []
        reference = parse_time(reference) if isinstance(reference, str) else reference
        ready = []
        for key, window in list(self.windows.items()):
            window_end = parse_time(window["windowEnd"])
            if window_end + self.grace > reference:
                continue
            self.windows.pop(key, None)
            self.closed_keys.add(key)
            ready.append(self._to_candle(window))
        return sorted(ready, key=lambda candle: (candle["symbol"], parse_time(candle["timestamp"])))

    def _to_candle(self, window):
        volume = window.get("volume") or 0
        return {
            "eventType": "CANDLE",
            "symbol": window["symbol"],
            "interval": "1m",
            "timestamp": window["timestamp"],
            "open": window["open"],
            "high": window["high"],
            "low": window["low"],
            "close": window["close"],
            "volume": volume,
            "tradeCount": window.get("tradeCount") or 0,
            "vwap": (window["notional"] / volume) if volume else window["close"],
            "ma": {},
            "isClosed": True,
            "correctionType": "NONE",
            "source": "stream-processor",
            "sourceInterval": "trades",
            "feed": window.get("feed"),
            "feedProfile": window.get("feedProfile"),
            "marketSession": window.get("marketSession"),
            "sourceEventId": window.get("sourceEventId"),
            "createdAt": window.get("createdAt"),
            **({"simulation": window["simulation"]} if window.get("simulation") else {}),
            **candle_metadata("split"),
        }


class CalendarCandleAggregator:
    def __init__(self, source_interval, target_interval, max_closed_keys=DEFAULT_CLOSED_KEY_CAP):
        self.source_interval = source_interval
        self.target_interval = target_interval
        self.windows = defaultdict(dict)
        self.closed_keys = BoundedKeySet(max_closed_keys)

    def update(self, candle):
        candle = normalize_candle_numeric_fields(candle)
        bucket, end = self._bucket(candle["timestamp"])
        key = (candle["symbol"], self.target_interval, to_iso(bucket), candle.get("feedProfile") or candle.get("feed") or "unknown")
        if key in self.closed_keys:
            return None
        self.windows[key][candle["timestamp"]] = dict(candle)
        return key, end

    def flush_ready(self, reference_time):
        if reference_time is None:
            return []
        reference = parse_time(reference_time) if isinstance(reference_time, str) else reference_time
        ready = []
        for key, window in list(self.windows.items()):
            bucket = parse_time(key[2])
            _bucket, end = self._bucket(bucket)
            if end > reference:
                continue
            self.windows.pop(key, None)
            self.closed_keys.add(key)
            ready.append(self._to_candle(key, window))
        return sorted(ready, key=lambda candle: (candle["symbol"], parse_time(candle["timestamp"])))

    def _bucket(self, timestamp):
        if self.target_interval == "1D":
            bucket = floor_market_day(timestamp)
            return bucket, bucket + timedelta(days=1)
        if self.target_interval == "1W":
            bucket = floor_week(timestamp)
            return bucket, bucket + timedelta(days=7)
        if self.target_interval == "1M":
            bucket = floor_month(timestamp)
            month = bucket.month + 1
            year = bucket.year + (1 if month == 13 else 0)
            return bucket, bucket.replace(year=year, month=1 if month == 13 else month)
        raise ValueError(f"Unsupported calendar rollup target: {self.target_interval}")

    def _to_candle(self, key, window):
        candles = [window[timestamp] for timestamp in sorted(window, key=parse_time)]
        latest = candles[-1]
        return {
            "eventType": "CANDLE",
            "symbol": key[0],
            "interval": self.target_interval,
            "timestamp": key[2],
            "open": candles[0]["open"],
            "high": max(candle["high"] for candle in candles),
            "low": min(candle["low"] for candle in candles),
            "close": latest["close"],
            "volume": sum(candle.get("volume") or 0 for candle in candles),
            "tradeCount": sum(candle.get("tradeCount") or 0 for candle in candles),
            "vwap": latest.get("vwap"),
            "ma": {},
            "isClosed": True,
            "correctionType": latest.get("correctionType", "NONE"),
            "source": "stream-processor",
            "sourceInterval": self.source_interval,
            "feed": latest.get("feed"),
            "feedProfile": latest.get("feedProfile"),
            "marketSession": latest.get("marketSession"),
            "sourceEventId": latest.get("sourceEventId"),
            "createdAt": latest.get("createdAt"),
            **({"simulation": latest["simulation"]} if latest.get("simulation") else {}),
            **candle_metadata(latest.get("priceAdjustment"), latest.get("canonicalVersion")),
        }


class SourceEventDeduper:
    def __init__(self, max_seen=10000):
        self.max_seen = max_seen
        self.seen = set()
        self.order = deque()

    def is_duplicate(self, source_event_id):
        if not source_event_id:
            return False
        if source_event_id in self.seen:
            return True
        self.seen.add(source_event_id)
        self.order.append(source_event_id)
        if len(self.order) > self.max_seen:
            oldest = self.order.popleft()
            self.seen.discard(oldest)
        return False
