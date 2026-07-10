import os


DEFAULT_REDIS_KEY_PREFIX = "gops:market:on-demand:v1"


class RedisKeyBuilder:
    def __init__(self, prefix=None):
        raw_prefix = prefix if prefix is not None else (os.getenv("REDIS_KEY_PREFIX") or DEFAULT_REDIS_KEY_PREFIX)
        self.prefix = raw_prefix.strip().strip(":")

    def key(self, value):
        return f"{self.prefix}:{value}" if self.prefix else value

    def candle_cache(self, symbol, interval):
        return self.key(f"cache:candles:{symbol}:{interval}")

    def live_candle(self, symbol, interval="1m"):
        return self.key(f"live:candle:{symbol}:{interval}")

    def latest_closed_candle(self, symbol, interval):
        return self.key(f"latest:closed:candle:{symbol}:{interval}")

    def closed_candle_watermark(self, symbol, interval):
        return self.key(f"latest:closed:watermark:{symbol}:{interval}")

    def recent_candles(self, symbol, interval):
        return self.candle_cache(symbol, interval)

    def latest_candle(self, symbol, interval):
        return self.latest_closed_candle(symbol, interval)

    def pending_replace_candle(self, symbol, interval, timestamp):
        return self.key(f"pending:replace:{symbol}:{interval}:{timestamp}")

    def candle_window(self, symbol, interval, bucket):
        return self.key(f"state:candle-window:{symbol}:{interval}:{bucket}")

    def live_trade(self, symbol):
        return self.key(f"live:trade:{symbol}")

    def live_quote(self, symbol):
        return self.key(f"live:quote:{symbol}")

    def order_flow_minutes(self, symbol):
        return self.key(f"order-flow:{symbol}:minutes")

    def order_flow_live_minute(self, symbol):
        return self.key(f"order-flow:{symbol}:live-minute")

    def live_event(self, symbol):
        return self.key(f"live:event:{symbol}")

    def price_latest(self, symbol):
        return self.live_trade(symbol)

    def market_events(self):
        return self.key("market.events")

    def market_events_symbol(self, symbol):
        return self.key(f"market.events:{symbol}")

    def component_health(self, component):
        return self.key(f"pipeline:health:{component}")

    def active_symbols(self):
        return self.key("active:charts:symbols")

    def active_symbol(self, symbol):
        return self.key(f"active:charts:{symbol}")

    def subscription_users(self, source=None):
        suffix = f":{source}" if source else ""
        return self.key(f"subscription:users{suffix}")

    def user_watchlist_symbols(self, user_id):
        return self.key(f"user:{user_id}:watchlist:symbols")

    def user_watchlist_symbol_order(self, user_id):
        return self.key(f"user:{user_id}:watchlist:symbol-order")

    def user_portfolio_symbols(self, user_id):
        return self.key(f"user:{user_id}:portfolio:symbols")

    def user_active_chart_session(self, user_id, session_id):
        return self.key(f"user:{user_id}:active-chart:{session_id}")

    def user_active_chart_sessions(self, user_id):
        return self.key(f"user:{user_id}:active-chart:sessions")

    def subscription_source_symbols(self, source):
        return self.key(f"subscription:source:{source}:symbols")

    def subscription_source_watchlist(self, symbol):
        return self.key(f"subscription:source:watchlist:{symbol}")

    def subscription_source_portfolio(self, symbol):
        return self.key(f"subscription:source:portfolio:{symbol}")

    def subscription_source_active_chart(self, symbol):
        return self.key(f"subscription:source:active-chart:{symbol}")

    def subscription_source_ranking(self, kind, symbol):
        return self.key(f"subscription:source:rank:{kind}:{symbol}")

    def subscription_source_manual(self, symbol):
        return self.key(f"subscription:source:manual:{symbol}")

    def subscription_symbols(self):
        return self.key("subscription:symbols")

    def subscription_symbol(self, symbol):
        return self.key(f"subscription:symbol:{symbol}")

    def subscription_version(self):
        return self.key("subscription:version")

    def subscription_events(self):
        return self.key("subscription:events")

    def watchlist_symbols(self):
        return self.key("ui:watchlist:symbols")

    def portfolio_symbols(self):
        return self.key("cohort:portfolio:symbols")

    def rank_symbols(self, kind):
        return self.key(f"cohort:rank:{kind}:top10")

    def feed_active(self):
        return self.key("feed:active")

    def feed_active_profile(self):
        return self.key("feed:active:profile")

    def feed_active_epoch(self):
        return self.key("feed:active:epoch")

    def feed_lease(self, feed_profile):
        return self.key(f"feed:lease:{str(feed_profile).lower()}")

    def feed_switch_lock(self):
        return self.key("feed:switch:lock")

    def feed_switch_state(self):
        return self.key("feed:switch:state")

    def feed_quarantine(self, date):
        return self.key(f"feed:quarantine:{date}")

    def hot_symbols(self):
        return self.key("hot:symbols")

    def hot_symbols_snapshot(self):
        return self.key("hot:symbols:snapshot")

    def news_latest(self, locale, symbol):
        return self.key(f"news:latest:{redis_locale(locale)}:{symbol}")

    def news_topic(self, locale, topic):
        return self.key(f"news:topic:{redis_locale(locale)}:{topic}")

    def news_latest_v2(self, locale, symbol):
        return self.key(f"news:v2:latest:{redis_locale(locale)}:{symbol}")

    def news_topic_v2(self, locale, topic):
        return self.key(f"news:v2:topic:{redis_locale(locale)}:{topic}")

    def news_daily_v2(self, locale, symbol):
        return self.key(f"news:v2:daily:{redis_locale(locale)}:{symbol}")

    def news_daily_coverage_v2(self, locale, symbol):
        return self.key(f"news:v2:daily:coverage:{redis_locale(locale)}:{symbol}")

    def market_status_latest(self):
        return self.key("market:status:latest")

    def market_status_symbol_latest(self, symbol):
        return self.key(f"market:status:{symbol}:latest")

    def symbol_metadata(self, symbol):
        return self.key(f"symbols:metadata:{symbol}")

    def symbols_search_index(self):
        return self.key("symbols:search:index")

    def backfill_lock(self, symbol, interval, range_digest):
        return self.key(f"backfill:lock:{range_digest}")

    def backfill_status(self, request_id):
        return self.key(f"backfill:status:{request_id}")

    def backfill_latest(self, symbol, interval):
        return self.key(f"backfill:latest:{symbol}:{interval}")

    def backfill_coverage(self, symbol, interval):
        return self.key(f"backfill:coverage:{symbol}:{interval}")

    def backfill_queue(self):
        return self.key("backfill:queue")

    def backfill_stream(self):
        return self.key("backfill:stream")

    def backfill_dead_letter_stream(self):
        return self.key("backfill:dead-letter")


def redis_locale(locale):
    value = str(locale or "ko-KR").strip().lower()
    return value.split("-", 1)[0] or "ko"
