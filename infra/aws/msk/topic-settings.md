# MSK Topic 설정 초안

| Topic | 용도 | partitions | retention |
|---|---|---:|---:|
| `market.raw.bars` | Alpaca 확정 1분봉 원본 | 6 | 7일 |
| `market.raw.updated-bars` | Alpaca 보정 1분봉 원본 | 6 | 7일 |
| `market.raw.trades` | Alpaca 체결 tick 원본 | 12 | 3일 |
| `market.ticks.v1` | 정규화된 체결 tick | 12 | 3일 |
| `market.candles.live.1m.v1` | 실시간 진행 중 1분봉 | 6 | 1일 |
| `market.candles.closed.v1` | 확정 1/5/10분봉 + MA | 6 | 30일 |

운영에서는 종목 수와 tick 양에 따라 partitions를 늘립니다.
