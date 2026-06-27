# Flink Job: market-data-normalizer

이 폴더는 운영에서 실제 Apache Flink로 옮길 스트리밍 처리 작업의 자리입니다.

현재 로컬 Docker에서는 `services/03-flink-stream-processor/local_main.py`가 Flink 역할을 Python worker로 흉내 냅니다. 운영에서는 아래 역할을 Flink JobManager/TaskManager 또는 Amazon Managed Service for Apache Flink로 분리합니다.

## 입력 Topic

```text
market.raw.bars
market.raw.updated-bars
market.raw.trades
```

## 출력 Topic

```text
market.ticks.v1
market.candles.live.1m.v1
market.candles.closed.v1
```

## 처리 책임

```text
trades -> 현재가, 실시간 1분봉
bars -> 확정 1분봉
updatedBars -> 확정 1분봉 보정
1분봉 -> 5분봉, 10분봉
close -> ma5, ma20, ma60
```

## 운영 선택지

| 선택 | 의미 |
|---|---|
| Amazon Managed Service for Apache Flink | AWS 관리형 Flink. 운영 부담이 작음 |
| Flink on EKS | JobManager/TaskManager를 EKS Pod로 직접 운영 |
| 현재 Python local worker | 로컬 검증용. 운영 최종 형태 아님 |
