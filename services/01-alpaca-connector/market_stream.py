# 역할: Alpaca WebSocket 시세 수집 서비스를 실행합니다.
# 사용: 실제 결제 후 sip feed를 Kafka Raw Topic으로 넣는 운영 entrypoint입니다.
# 설정: ALPACA_FEED, ALPACA_SYMBOLS, ALPACA_CHANNELS, Alpaca API 키가 필요합니다.
from alfaka.alpaca.websocket_collector import run


if __name__ == "__main__":
    run()
