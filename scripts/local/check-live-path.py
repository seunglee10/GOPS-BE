# 역할: 한 종목 기준 live market-data path를 read-only로 추적합니다.
# 사용: PYTHONPATH=systems/market-data/shared python scripts/local/check-live-path.py AAPL --interval 1m
import os

from alfaka.tools.live_path_trace import main


if __name__ == "__main__":
    os.environ.setdefault("KAFKA_PROCESSOR_GROUP_ID", "alfaka-local-stream-processor")
    main()
