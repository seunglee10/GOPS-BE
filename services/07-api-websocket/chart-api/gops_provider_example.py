# 역할: GOPS backend/app/routes/charts.py에서 provider를 붙일 때 참고할 예시입니다.
# 사용: 이 파일을 그대로 운영에 쓰기보다 GOPS backend 구조에 맞게 복사합니다.
# 출력: GOPS CandleSnapshot 형식입니다.
from alfaka.serving.provider import MarketDataProvider


provider = MarketDataProvider()


def chart_candles(symbol="AAPL", interval="1m", limit=160):
    return provider.candle_snapshot(symbol=symbol, interval=interval, limit=limit)
