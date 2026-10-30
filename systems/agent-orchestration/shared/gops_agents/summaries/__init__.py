"""종목 AI 요약(D14)과 오늘 시장 요약(D15) 생성기.

LLM 호출은 `JsonModelClient` 뒤에 숨긴다. 모델이 꺼져 있거나 실패하면
결정론적 템플릿 요약으로 대신하므로, 데이터가 있는 종목의 캐시는 비지 않는다.
"""

from .keys import (
    MARKET_BRIEF_TTL_SECONDS,
    STOCK_SUMMARY_TTL_SECONDS,
    market_brief_key,
    stock_summary_key,
)
from .llm import JsonModelClient, OpenAIResponsesJsonClient, SummaryModelError, build_summary_model_client_from_env
from .market_brief import MarketBriefGenerator, MarketBriefInput
from .stock_summary import StockSummaryGenerator, StockSummaryInput

__all__ = [
    "JsonModelClient",
    "MARKET_BRIEF_TTL_SECONDS",
    "MarketBriefGenerator",
    "MarketBriefInput",
    "OpenAIResponsesJsonClient",
    "STOCK_SUMMARY_TTL_SECONDS",
    "StockSummaryGenerator",
    "StockSummaryInput",
    "SummaryModelError",
    "build_summary_model_client_from_env",
    "market_brief_key",
    "stock_summary_key",
]
