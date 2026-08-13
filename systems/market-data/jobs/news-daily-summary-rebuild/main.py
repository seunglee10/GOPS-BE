# 역할: 기존 뉴스 localization row에서 회사/날짜 daily summary를 재생성합니다.
# 사용: 배포 후 최근 30일 또는 필요한 구간을 보정하는 Kubernetes Job으로 실행합니다.
import importlib.util
import os
from datetime import date
from pathlib import Path

import redis

from market_data.common.env import load_dotenv, parse_csv
from market_data.storage.clickhouse_loader import ClickHouseHttpClient, should_ensure_schema_on_start


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
    from_date, to_date = normalized_date_range(
        os.getenv("NEWS_DAILY_SUMMARY_REBUILD_FROM_DATE"),
        os.getenv("NEWS_DAILY_SUMMARY_REBUILD_TO_DATE"),
    )
    requested_status = normalized_rebuild_status(os.getenv("NEWS_DAILY_SUMMARY_REBUILD_STATUS", "auto"))
    dry_run = bool_env(os.getenv("NEWS_DAILY_SUMMARY_REBUILD_DRY_RUN"), default=False)
    groups = read_dirty_groups(
        client,
        days=days,
        max_groups=max_groups,
        symbols=symbols,
        from_date=from_date,
        to_date=to_date,
    )
    scope = ",".join(symbols) if symbols else "all"
    window = f"{from_date}..{to_date}" if from_date and to_date else f"last-{days}-days"
    if dry_run:
        print(
            "News daily summary rebuild dry-run: "
            f"groups={len(groups)} window={window} maxGroups={max_groups} symbols={scope}",
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
        f"rebuilt={rebuilt} groups={len(groups)} window={window} symbols={scope}",
        flush=True,
    )


def read_dirty_groups(client, *, days, max_groups, symbols=None, from_date=None, to_date=None):
    normalized = normalized_symbols(symbols or [])
    normalized_from_date, normalized_to_date = normalized_date_range(from_date, to_date)
    symbol_filter = "\n      AND target_symbol IN {symbols:Array(String)}" if normalized else ""
    if normalized_from_date and normalized_to_date:
        date_filter = (
            "toDate(published_at) BETWEEN "
            "toDate({fromDate:String}) AND toDate({toDate:String})"
        )
        relevance_levels = ["primary", "secondary", "mention"]
    else:
        date_filter = "published_at >= now64(3) - INTERVAL {days:UInt32} DAY"
        relevance_levels = ["primary", "secondary"]
    query = f"""
    SELECT
      target_symbol AS symbol,
      toString(toDate(published_at)) AS date,
      locale
    FROM {client.database}.news_article_localizations
    WHERE {date_filter}
      AND subject_relevance IN {{relevanceLevels:Array(String)}}
      {symbol_filter}
    GROUP BY symbol, date, locale
    ORDER BY date DESC, symbol ASC
    LIMIT {{maxGroups:UInt32}}
    FORMAT JSONEachRow
    """
    parameters = {
        "maxGroups": max(1, int(max_groups)),
        "relevanceLevels": relevance_levels,
    }
    if normalized_from_date and normalized_to_date:
        parameters.update({"fromDate": normalized_from_date, "toDate": normalized_to_date})
    else:
        parameters["days"] = max(1, int(days))
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


def normalized_date_range(from_date, to_date):
    normalized_from = str(from_date or "").strip()
    normalized_to = str(to_date or "").strip()
    if bool(normalized_from) != bool(normalized_to):
        raise ValueError("NEWS_DAILY_SUMMARY_REBUILD_FROM_DATE and TO_DATE must be set together")
    if not normalized_from:
        return None, None
    try:
        parsed_from = date.fromisoformat(normalized_from)
        parsed_to = date.fromisoformat(normalized_to)
    except ValueError as exc:
        raise ValueError("News daily summary rebuild dates must use YYYY-MM-DD") from exc
    if parsed_from > parsed_to:
        raise ValueError("News daily summary rebuild from date must not be after to date")
    return parsed_from.isoformat(), parsed_to.isoformat()


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
