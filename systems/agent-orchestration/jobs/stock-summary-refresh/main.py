"""종목 AI 요약(D14), 오늘 시장 요약(D15), 캘린더 주간 요약(D28)을 만들어 Redis 에 저장하는 주기 잡.

  python main.py                 # 대상 종목 요약 갱신 + 장 마감 뒤면 시장 요약 1회
  python main.py --brief         # 시장 요약만 지금 바로(가장 최근 마감일 기준) 다시 만든다
  python main.py --symbols NVDA,AMD --force   # 지정 종목만, 1시간 제한 무시
  python main.py --calendar-brief  # 캘린더 주간 요약(이번 주·다음 주)만 지금 다시 만든다

STOCK_SUMMARY_LLM_ENABLED=false(기본)면 OpenAI 없이 템플릿 요약만 쓴다.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from gops_agents.summaries import CalendarBriefGenerator, MarketBriefGenerator, StockSummaryGenerator, build_summary_model_client_from_env
from gops_agents.summaries.refresh import (
    MarketBriefArchive,
    RefreshConfig,
    refresh_calendar_briefs,
    refresh_market_brief,
    refresh_stock_summaries,
    utc_now,
)
from gops_agents.summaries.sources import StoreSummarySource


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh stock AI summaries and the daily market brief.")
    parser.add_argument("--brief", action="store_true", help="시장 요약만 지금 다시 만든다(종목 요약은 건너뜀).")
    parser.add_argument("--no-brief", action="store_true", help="시장 요약 단계를 건너뛴다.")
    parser.add_argument("--symbols", default=os.getenv("STOCK_SUMMARY_SYMBOLS", ""), help="쉼표로 구분한 종목. 비우면 자동 선택.")
    parser.add_argument("--force", action="store_true", help="종목별 1시간 제한을 무시한다.")
    parser.add_argument("--calendar-brief", action="store_true", help="캘린더 주간 요약만 지금 다시 만든다(6시간 제한 무시).")
    parser.add_argument("--no-calendar-brief", action="store_true", help="캘린더 주간 요약 단계를 건너뛴다.")
    return parser.parse_args(argv)


def redis_from_env():
    import redis

    return redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    args = parse_args(argv)
    config = RefreshConfig.from_env()
    redis_client = redis_from_env()
    source = StoreSummarySource(redis_client=redis_client)
    model_client = build_summary_model_client_from_env()
    now = utc_now()
    result: dict[str, object] = {"llm": model_client is not None}
    only_calendar = args.calendar_brief
    if not args.brief and not only_calendar:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or None
        result["stocks"] = refresh_stock_summaries(
            source,
            redis_client,
            StockSummaryGenerator(model_client),
            config=config,
            now=now,
            symbols=symbols,
            force=args.force,
        )
    if not args.no_brief and not only_calendar:
        result["brief"] = refresh_market_brief(
            source,
            redis_client,
            MarketBriefGenerator(model_client),
            now=now,
            force=args.brief,
            archive=MarketBriefArchive(),
            config=config,
        )
    if not args.no_calendar_brief and not args.brief:
        result["calendarBrief"] = refresh_calendar_briefs(
            source,
            redis_client,
            CalendarBriefGenerator(model_client),
            now=now,
            force=only_calendar,
            config=config,
        )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
