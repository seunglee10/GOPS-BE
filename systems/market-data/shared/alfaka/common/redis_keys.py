import os


class RedisKeyBuilder:
    def __init__(self, prefix=None):
        raw_prefix = prefix if prefix is not None else os.getenv("REDIS_KEY_PREFIX", "")
        self.prefix = raw_prefix.strip().strip(":")

    def key(self, value):
        return f"{self.prefix}:{value}" if self.prefix else value

    def price_latest(self, symbol):
        return self.key(f"price:{symbol}:latest")

    def live_candle(self, symbol):
        return self.key(f"candle:{symbol}:1m:live")

    def latest_candle(self, symbol, interval):
        return self.key(f"candle:{symbol}:{interval}:latest")

    def recent_candles(self, symbol, interval):
        return self.key(f"candles:{symbol}:{interval}")

    def market_events(self):
        return self.key("market.events")

    def market_events_symbol(self, symbol):
        return self.key(f"market.events:{symbol}")

    def active_symbols(self):
        return self.key("active:charts:symbols")

    def active_symbol(self, symbol):
        return self.key(f"active:charts:{symbol}")

    def market_status_latest(self):
        return self.key("market:status:latest")

    def market_status_symbol_latest(self, symbol):
        return self.key(f"market:status:{symbol}:latest")

    def symbol_metadata(self, symbol):
        return self.key(f"symbols:metadata:{symbol}")

    def symbols_search_index(self):
        return self.key("symbols:search:index")

    def volume_profile_live(self, symbol):
        return self.key(f"volume-profile:{symbol}:1m:live")

    def backfill_lock(self, symbol, interval, range_digest):
        return self.key(f"backfill:lock:{symbol}:{interval}:{range_digest}")

    def backfill_status(self, request_id):
        return self.key(f"backfill:status:{request_id}")

    def backfill_latest(self, symbol, interval):
        return self.key(f"backfill:latest:{symbol}:{interval}")

    def backfill_queue(self):
        return self.key("backfill:queue")
