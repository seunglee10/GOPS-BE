# 역할: 한 종목 기준 live market-data path를 read-only로 추적합니다.
# 사용: python scripts/local/check-live-path.py NVDA --interval 1m
import os
import sys
from pathlib import Path


repo_root = Path(__file__).resolve().parents[2]
shared_path = repo_root / "systems" / "market-data" / "shared"
if str(shared_path) not in sys.path:
    sys.path.insert(0, str(shared_path))

from market_data.tools.live_path_trace import main


if __name__ == "__main__":
    os.environ.setdefault("KAFKA_PROCESSOR_GROUP_ID", "alfaka-local-stream-processor")
    main()
