# 06 S3 Store

역할: Raw/Processed 시장 데이터를 장기 보관합니다.

현재 구현:

```text
processed_sink.py                                      Processed Kafka -> S3/MinIO
services/01-alpaca-connector/historical_backfill.py    Alpaca Historical REST -> S3 Raw
packages/alfaka/storage/processed_s3_sink.py           S3 sink logic
packages/alfaka/alpaca/historical_backfill.py          Raw backfill logic
```

저장 포맷:

```text
Raw          JSON Lines
Processed   Parquet 기본, 디버깅 시 JSONL 가능
```
