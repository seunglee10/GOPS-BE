# 역할: Redis에 저장된 최신 현재가와 캔들 데이터를 확인합니다.
# 사용: 로컬 Docker 또는 운영 Redis 연결값으로 같은 key 규칙을 검증합니다.
# 출력: price/candle/candles key의 현재 값.
import argparse
import json
import os

import redis

from alfaka.common.env import load_dotenv


def pretty(value):
    if value is None:
        return "값 없음"
    try:
        return json.dumps(json.loads(value), indent=2, ensure_ascii=False)
    except Exception:
        return value


def main():
    parser = argparse.ArgumentParser(description="Redis 최신 시장 데이터를 확인합니다.")
    parser.add_argument("symbol", nargs="?", default="AAPL", help="확인할 심볼입니다. 예: AAPL")
    parser.add_argument("--interval", default="1m", choices=["1m", "5m", "10m"], help="확인할 캔들 주기입니다.")
    args = parser.parse_args()

    load_dotenv()
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    client = redis.from_url(redis_url, decode_responses=True)
    symbol = args.symbol.upper()
    interval = args.interval

    price_key = f"price:{symbol}:latest"
    live_key = f"candle:{symbol}:1m:live"
    latest_key = f"candle:{symbol}:{interval}:latest"
    series_key = f"candles:{symbol}:{interval}"

    print(f"Redis URL: {redis_url}")
    print(f"현재가 key: {price_key}")
    print(pretty(json.dumps(client.hgetall(price_key), ensure_ascii=False) if client.exists(price_key) else None))
    print()

    print(f"실시간 1분봉 key: {live_key}")
    print(pretty(client.get(live_key)))
    print()

    print(f"최신 확정 캔들 key: {latest_key}")
    print(pretty(client.get(latest_key)))
    print()

    print(f"최근 캔들 시리즈 key: {series_key}")
    rows = client.zrange(series_key, -10, -1)
    print(json.dumps([json.loads(row) for row in rows], indent=2, ensure_ascii=False) if rows else "값 없음")


if __name__ == "__main__":
    main()
