# 역할: .env 파일을 읽고 CSV 설정값을 파싱합니다.
# 사용: 로컬 실험과 Docker/Kubernetes 환경에서 같은 설정 이름을 씁니다.
# 주의: 실제 API 키는 가능하면 AWS Secrets Manager 또는 Kubernetes Secret에 둡니다.
import os
from datetime import datetime, timezone
from pathlib import Path


def load_dotenv(path=".env"):
    env_file = Path(path)
    if not env_file.exists():
        return

    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse_csv(value):
    return [item.strip() for item in value.split(",") if item.strip()]


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
