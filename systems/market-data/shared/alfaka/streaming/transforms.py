# 역할: Raw Envelope를 차트용 Trade/Candle 데이터로 변환합니다.
# 사용: stream_processor가 현재가, 실시간 1분봉, 확정 1/5/10분봉, 이동평균을 계산합니다.
# 주의: 운영에서는 이 로직을 PyFlink Job으로 이전할 수 있습니다.
from collections import defaultdict
from datetime import datetime, timedelta, timezone


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def to_iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def floor_minute(value):
    dt = parse_time(value) if isinstance(value, str) else value
    return dt.replace(second=0, microsecond=0)


def floor_interval(value, minutes):
    dt = parse_time(value) if isinstance(value, str) else value
    bucket_minute = (dt.minute // minutes) * minutes
    return dt.replace(minute=bucket_minute, second=0, microsecond=0)


def floor_day(value):
    dt = parse_time(value) if isinstance(value, str) else value
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


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
    return {
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
    }


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
    }


class LiveCandleBuilder:
    def __init__(self):
        self.candles = {}

    def seed(self, candle):
        if candle.get("interval") != "1m" or candle.get("isClosed"):
            return False
        timestamp = candle.get("timestamp")
        symbol = candle.get("symbol")
        if not timestamp or not symbol:
            return False
        self.candles[(symbol, timestamp)] = dict(candle)
        return True

    def update(self, trade):
        bucket = floor_minute(trade["timestamp"])
        key = (trade["symbol"], to_iso(bucket))
        price = trade["price"]
        size = trade.get("size") or 0
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
                "isClosed": False,
                "source": "alpaca.trades",
                "sourceInterval": "trades",
                "feed": trade.get("feed"),
                "feedProfile": trade.get("feedProfile"),
                "marketSession": trade.get("marketSession"),
                "sourceEventId": trade.get("sourceEventId"),
                "updatedAt": trade.get("receivedAt"),
            }
        else:
            candle["high"] = max(candle["high"], price)
            candle["low"] = min(candle["low"], price)
            candle["close"] = price
            candle["volume"] += size
            candle["feed"] = trade.get("feed") or candle.get("feed")
            candle["feedProfile"] = trade.get("feedProfile") or candle.get("feedProfile")
            candle["marketSession"] = trade.get("marketSession") or candle.get("marketSession")
            candle["sourceEventId"] = trade.get("sourceEventId")
            candle["updatedAt"] = trade.get("receivedAt")

        self.candles[key] = candle
        return candle


class ProvisionalCandleState:
    def __init__(self, max_closed_1m=2000, max_closed_1d=400):
        self.closed = defaultdict(dict)
        self.max_closed = {"1m": max_closed_1m, "1D": max_closed_1d}

    def record_closed(self, candle):
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
        if target_interval in {"5m", "10m"}:
            minutes = int(target_interval.removesuffix("m"))
            bucket = floor_interval(anchor, minutes)
            end = bucket + timedelta(minutes=minutes)
        elif target_interval == "1D":
            bucket = floor_day(anchor)
            end = bucket + timedelta(days=1)
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


def build_provisional_candle(symbol, interval, bucket, rows, source_interval):
    candles = sorted(rows, key=lambda candle: parse_time(candle["timestamp"]))
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
        "feed": latest.get("feed"),
        "feedProfile": latest.get("feedProfile"),
        "marketSession": latest.get("marketSession"),
        "sourceEventId": latest.get("sourceEventId"),
        "updatedAt": latest.get("updatedAt") or latest.get("createdAt"),
    }


class MovingAverageState:
    def __init__(self):
        self.closes = defaultdict(dict)

    def attach_ma(self, candle):
        key = (candle["symbol"], candle["interval"])
        self.closes[key][candle["timestamp"]] = candle["close"]
        timestamps = sorted(self.closes[key], key=parse_time)[-60:]
        closes = [self.closes[key][timestamp] for timestamp in timestamps]
        ma = {}
        for window in (5, 20, 60):
            if len(closes) >= window:
                values = closes[-window:]
                ma[f"ma{window}"] = sum(values) / window
        candle["ma"] = ma
        return candle


class CandleAggregator:
    def __init__(self):
        self.windows = defaultdict(dict)

    def update(self, candle_1m, interval_minutes):
        bucket = floor_interval(candle_1m["timestamp"], interval_minutes)
        key = (candle_1m["symbol"], interval_minutes, to_iso(bucket))
        window = self.windows[key]
        window[candle_1m["timestamp"]] = candle_1m

        if len(window) < interval_minutes:
            return None

        candles = [window[timestamp] for timestamp in sorted(window, key=parse_time)]
        return {
            "eventType": "CANDLE",
            "symbol": candle_1m["symbol"],
            "interval": f"{interval_minutes}m",
            "timestamp": to_iso(bucket),
            "open": candles[0]["open"],
            "high": max(candle["high"] for candle in candles),
            "low": min(candle["low"] for candle in candles),
            "close": candles[-1]["close"],
            "volume": sum(candle.get("volume") or 0 for candle in candles),
            "tradeCount": sum(candle.get("tradeCount") or 0 for candle in candles),
            "vwap": candles[-1].get("vwap"),
            "ma": {},
            "isClosed": True,
            "correctionType": candle_1m.get("correctionType", "NONE"),
            "source": "stream-processor",
            "feed": candle_1m.get("feed"),
            "feedProfile": candle_1m.get("feedProfile"),
            "marketSession": candle_1m.get("marketSession"),
            "sourceEventId": candle_1m.get("sourceEventId"),
            "createdAt": candle_1m.get("createdAt"),
        }


class SourceEventDeduper:
    def __init__(self, max_seen=10000):
        self.max_seen = max_seen
        self.seen = set()
        self.order = []

    def is_duplicate(self, source_event_id):
        if not source_event_id:
            return False
        if source_event_id in self.seen:
            return True
        self.seen.add(source_event_id)
        self.order.append(source_event_id)
        if len(self.order) > self.max_seen:
            oldest = self.order.pop(0)
            self.seen.discard(oldest)
        return False


class VolumeProfileBinBuilder:
    def __init__(self, price_bin_size=0.05):
        self.price_bin_size = price_bin_size
        self.bins = {}

    def update(self, trade):
        minute = to_iso(floor_minute(trade["timestamp"]))
        price = float(trade["price"])
        size = int(trade.get("size") or 0)
        price_bin = round(round(price / self.price_bin_size) * self.price_bin_size, 6)
        key = (trade["symbol"], minute, price_bin)
        current = self.bins.get(key)
        if current is None:
            current = {
                "eventType": "VOLUME_PROFILE_BIN",
                "eventMinute": minute,
                "symbol": trade["symbol"],
                "priceBin": price_bin,
                "priceBinSize": self.price_bin_size,
                "volume": 0,
                "tradeCount": 0,
                "notional": 0.0,
                "vwap": None,
                "source": "alpaca",
                "feed": trade.get("feed"),
                "sourceEventId": trade.get("sourceEventId"),
                "updatedAt": trade.get("receivedAt"),
            }
        current["volume"] += size
        current["tradeCount"] += 1
        current["notional"] += price * size
        current["vwap"] = current["notional"] / current["volume"] if current["volume"] else price
        current["sourceEventId"] = trade.get("sourceEventId")
        current["updatedAt"] = trade.get("receivedAt")
        self.bins[key] = current
        return {key: value for key, value in current.items() if key != "notional"}
