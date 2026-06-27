# 역할: GOPS /ws/charts가 Redis live candle을 push할 때 참고할 예시입니다.
# 사용: FastAPI WebSocket handler 안에서 주기적으로 호출하거나 Pub/Sub 방식으로 교체합니다.
# 출력: GOPS CandleEvent 형식입니다.
from alfaka.serving.redis_provider import RedisMarketDataProvider


provider = RedisMarketDataProvider()


def latest_live_event(symbol="AAPL"):
    return provider.live_event(symbol)


def latest_closed_event(symbol="AAPL", interval="1m"):
    return provider.closed_event(symbol, interval)
