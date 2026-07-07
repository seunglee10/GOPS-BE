# 역할: Redis에 저장된 최신 체결/호가와 120개 캔들 상태를 확인합니다.
# 사용: 로컬 Docker 또는 운영 Redis 연결값으로 같은 key 규칙을 검증합니다.
# 출력: live trade/quote/candle/latest closed/cache key의 현재 값.
import argparse
import json
import os

import redis

from alfaka.common.env import load_dotenv
from alfaka.common.redis_keys import RedisKeyBuilder


def pretty(value):
    if value is None:
        return "값 없음"
    try:
        return json.dumps(json.loads(value), indent=2, ensure_ascii=False)
    except Exception:
        return value


def main():
    parser = argparse.ArgumentParser(description="Redis 최신 시장 데이터를 확인합니다.")
    parser.add_argument("symbol", help="확인할 심볼입니다. 예: NVDA")
    parser.add_argument("--interval", default="1m", choices=["1m", "5m", "10m", "1h", "4h", "1D", "1W", "1M"], help="확인할 캔들 주기입니다.")
    args = parser.parse_args()

    load_dotenv()
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    client = redis.from_url(redis_url, decode_responses=True)
    keys = RedisKeyBuilder()
    symbol = args.symbol.upper()
    interval = args.interval

    trade_key = keys.live_trade(symbol)
    quote_key = keys.live_quote(symbol)
    live_key = keys.live_candle(symbol, interval)
    latest_key = keys.latest_closed_candle(symbol, interval)
    series_key = keys.recent_candles(symbol, interval)

    print(f"Redis URL: {redis_url}")
    print(f"실시간 체결 key: {trade_key}")
    print(pretty(client.get(trade_key)))
    print()

    print(f"실시간 호가 key: {quote_key}")
    print(pretty(client.get(quote_key)))
    print()

    print(f"실시간 임시봉 key: {live_key}")
    print(pretty(client.get(live_key)))
    print()

    print(f"최신 확정 캔들 key: {latest_key}")
    print(pretty(client.get(latest_key)))
    print()

    print(f"최근 확정 캔들 120개 cache key: {series_key}")
    rows = client.lrange(series_key, -10, -1)
    if not rows:
        rows = client.zrange(series_key, -10, -1)
    print(json.dumps([json.loads(row) for row in rows], indent=2, ensure_ascii=False) if rows else "값 없음")


if __name__ == "__main__":
    main()
