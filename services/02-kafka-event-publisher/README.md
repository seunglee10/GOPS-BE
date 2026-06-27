# 02 Kafka Event Publisher

역할: 외부 입력과 처리 결과를 Kafka 이벤트로 발행하는 계약 계층입니다.

현재 코드상 Kafka 발행 위치:

```text
packages/alfaka/alpaca/websocket_collector.py   Alpaca Raw Envelope 발행
packages/alfaka/streaming/processor.py          Processed tick/candle 발행
packages/alfaka/common/kafka_io.py              Kafka producer/consumer 공통 코드
```

Topic 계약:

```text
market.raw.bars
market.raw.updated-bars
market.raw.trades
market.ticks.v1
market.candles.live.1m.v1
market.candles.closed.v1
```

운영에서 Kafka publisher를 별도 라이브러리나 sidecar로 빼더라도 topic 이름과 envelope 형식은 여기 계약을 기준으로 유지합니다.
