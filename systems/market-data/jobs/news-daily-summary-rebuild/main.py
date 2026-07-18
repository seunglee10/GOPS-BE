# 역할: 기존 뉴스 localization row에서 회사/날짜 daily summary를 재생성합니다.
# 사용: 배포 후 최근 30일 또는 필요한 구간을 보정하는 Kubernetes Job으로 실행합니다.
import importlib.util
import os
from pathlib import Path

import redis

from alfaka.common.env import load_dotenv, parse_csv
from alfaka.storage.clickhouse_loader import ClickHouseHttpClient, should_ensure_schema_on_start


def main():
    load_dotenv()
    client = ClickHouseHttpClient(
        url=os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
        database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
        user=os.getenv("CLICKHOUSE_USER", "alfaka"),
        password=os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
    )
    if should_ensure_schema_on_start():
        client.ensure_market_data_schema()
    days = int(os.getenv("NEWS_DAILY_SUMMARY_REBUILD_DAYS", "5"))
    max_groups = int(os.getenv("NEWS_DAILY_SUMMARY_REBUILD_MAX_GROUPS", "5"))
    symbols = normalized_symbols(parse_csv(os.getenv("NEWS_DAILY_SUMMARY_REBUILD_SYMBOLS", "NVDA")))
    requested_status = normalized_rebuild_status(os.getenv("NEWS_DAILY_SUMMARY_REBUILD_STATUS", "auto"))
    dry_run = bool_env(os.getenv("NEWS_DAILY_SUMMARY_REBUILD_DRY_RUN"), default=False)
    groups = read_dirty_groups(client, days=days, max_groups=max_groups, symbols=symbols)
    scope = ",".join(symbols) if symbols else "all"
    if dry_run:
        print(
            "News daily summary rebuild dry-run: "
            f"groups={len(groups)} days={days} maxGroups={max_groups} symbols={scope}",
            flush=True,
        )
        return

    worker = load_worker_module()
    redis_client = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
    rebuilt = 0
    for group in groups:
        event = {
            "eventType": "NEWS_DAILY_SUMMARY_DIRTY",
            "symbol": group["symbol"],
            "date": group["date"],
            "locale": group["locale"],
        }
        if requested_status:
            event["status"] = requested_status
        record = worker.process_dirty_event(
            event,
            clickhouse_client=client,
            redis_client=redis_client,
        )
        if record:
            rebuilt += 1
    print(
        "News daily summary rebuild 완료: "
        f"rebuilt={rebuilt} groups={len(groups)} days={days} symbols={scope}",
        flush=True,
    )


def read_dirty_groups(client, *, days, max_groups, symbols=None):
    normalized = normalized_symbols(symbols or [])
    symbol_filter = "\n      AND target_symbol IN {symbols:Array(String)}" if normalized else ""
    query = f"""
    SELECT
      target_symbol AS symbol,
      toString(toDate(published_at)) AS date,
      locale
    FROM {client.database}.news_article_localizations
    WHERE published_at >= now64(3) - INTERVAL {{days:UInt32}} DAY
      AND subject_relevance IN ['primary', 'secondary']
      {symbol_filter}
    GROUP BY symbol, date, locale
    ORDER BY date DESC, symbol ASC
    LIMIT {{maxGroups:UInt32}}
    FORMAT JSONEachRow
    """
    parameters = {"days": max(1, int(days)), "maxGroups": max(1, int(max_groups))}
    if normalized:
        parameters["symbols"] = normalized
    return client.query_json_each_row(query, parameters)


def normalized_symbols(values):
    result = []
    for value in values or []:
        symbol = str(value or "").strip().upper()
        if symbol and symbol not in result:
            result.append(symbol)
    return result


def normalized_rebuild_status(value):
    status = str(value or "").strip().lower()
    return None if status in {"", "auto"} else status


def load_worker_module():
    module_path = Path(__file__).resolve().parents[2] / "pods" / "news-daily-summary-worker" / "main.py"
    spec = importlib.util.spec_from_file_location("news_daily_summary_worker", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bool_env(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    main()
