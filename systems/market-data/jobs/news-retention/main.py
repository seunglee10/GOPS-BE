# 역할: ClickHouse hot storage에 남은 오래된 Alpaca 뉴스 기사를 정리합니다.
# 사용: Docker/K8s CronJob에서 주기 실행하며 CLICKHOUSE_NEWS_RETENTION_DAYS로 보관 기간을 조정합니다.
import os


def build_delete_query(database: str, table: str, retention_days: int) -> str:
    if retention_days <= 0:
        raise ValueError("CLICKHOUSE_NEWS_RETENTION_DAYS must be positive.")
    return (
        f"ALTER TABLE {clickhouse_identifier(database)}.{clickhouse_identifier(table)} "
        f"DELETE WHERE published_at < now64(3) - INTERVAL {int(retention_days)} DAY"
    )


def clickhouse_identifier(value: str) -> str:
    text = str(value or "").strip()
    if not text.replace("_", "").isalnum():
        raise ValueError(f"Invalid ClickHouse identifier: {value}")
    return f"`{text}`"


def run_cleanup() -> dict[str, str | int]:
    import requests

    url = os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123")
    database = os.getenv("CLICKHOUSE_DATABASE", "market_data")
    table = os.getenv("CLICKHOUSE_NEWS_TABLE", "news_articles")
    user = os.getenv("CLICKHOUSE_USER", "alfaka")
    password = os.getenv("CLICKHOUSE_PASSWORD", "alfaka")
    retention_days = int(os.getenv("CLICKHOUSE_NEWS_RETENTION_DAYS", "30"))
    query = build_delete_query(database, table, retention_days)
    response = requests.post(
        url,
        params={"user": user, "password": password, "database": database, "query": query},
        timeout=float(os.getenv("CLICKHOUSE_NEWS_RETENTION_TIMEOUT_SECONDS", "10")),
    )
    response.raise_for_status()
    return {"database": database, "table": table, "retentionDays": retention_days}


def main():
    result = run_cleanup()
    print(f"ClickHouse news retention cleanup requested: {result}", flush=True)


if __name__ == "__main__":
    main()
