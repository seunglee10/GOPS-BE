# 역할: Raw Envelope를 차트용 Trade/Candle 데이터로 변환합니다.
# 사용: stream_processor가 현재가, 실시간 1분봉, 확정 1/5/10분봉, 이동평균을 계산합니다.
# 주의: 운영에서는 이 로직을 PyFlink Job으로 이전할 수 있습니다.
from collections import defaultdict
from datetime import datetime, timezone


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


def normalize_bar(envelope, correction_type="NONE"):
    raw = envelope["raw"]
    interval = "1d" if envelope["channel"] == "dailyBars" else "1m"
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
        "sourceEventId": envelope.get("sourceEventId"),
        "raw": raw,
    }


class LiveCandleBuilder:
    def __init__(self):
        self.candles = {}

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
                "sourceEventId": trade.get("sourceEventId"),
                "updatedAt": trade.get("receivedAt"),
            }
        else:
            candle["high"] = max(candle["high"], price)
            candle["low"] = min(candle["low"], price)
            candle["close"] = price
            candle["volume"] += size
            candle["sourceEventId"] = trade.get("sourceEventId")
            candle["updatedAt"] = trade.get("receivedAt")

        self.candles[key] = candle
        return candle


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
