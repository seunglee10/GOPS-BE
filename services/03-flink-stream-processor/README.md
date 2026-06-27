# 03 Flink Stream Processor

역할: Kafka Raw Topic을 읽어 차트용 Processed Topic, Redis 상태, 집계 캔들을 만듭니다.

로컬 구현:

```text
local_main.py                         Python local processor entrypoint
packages/alfaka/streaming/processor.py
packages/alfaka/streaming/transforms.py
```

운영 구현:

```text
flink-jobs/market-data-normalizer/
```

처리 책임:

```text
trades      -> 현재가, 체결 tick, live 1m candle
bars        -> closed 1m candle
updatedBars -> 같은 timestamp closed candle 보정
closed 1m   -> 5m/10m candle, ma5/ma20/ma60
```
