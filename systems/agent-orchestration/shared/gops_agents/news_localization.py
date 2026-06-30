from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass
from typing import Any

from .contracts import EvidenceItem
from .router import parse_openai_text_json


@dataclass
class CachedNewsLocalization:
    localizedTitle: str
    localizedSummary: str
    expiresAt: float


class NewsLocalizationService:
    def __init__(self, *, cache_ttl_seconds: int | None = None, limit: int | None = None):
        self.cache_ttl_seconds = cache_ttl_seconds if cache_ttl_seconds is not None else int(os.getenv("AGENT_NEWS_LOCALIZATION_CACHE_TTL_SECONDS", "3600"))
        self.limit = limit if limit is not None else int(os.getenv("AGENT_NEWS_LOCALIZATION_LIMIT", "12"))
        self.batch_size = max(1, int(os.getenv("AGENT_NEWS_LOCALIZATION_BATCH_SIZE", "4")))
        self._cache: dict[str, CachedNewsLocalization] = {}

    def localize(self, *, symbol: str, intent: str, evidence: list[EvidenceItem]) -> list[EvidenceItem]:
        news_items = [item for item in evidence if item.provider == "news" and item.status == "available"]
        if not news_items:
            return evidence

        for item in news_items:
            preserve_original_news_text(item)

        uncached = []
        now = time.time()
        for item in news_items[: self.limit]:
            key = localization_cache_key(item)
            cached = self._cache.get(key)
            if cached and cached.expiresAt > now:
                apply_localized_news_text(item, cached.localizedTitle, cached.localizedSummary)
            else:
                uncached.append(item)

        if not uncached or os.getenv("AGENT_NEWS_LOCALIZATION_PROVIDER") == "deterministic":
            return evidence

        localized = []
        for batch in chunked(uncached, self.batch_size):
            localized.extend(self._localize_with_openai(symbol=symbol, intent=intent, evidence=batch) or [])
        if not localized:
            return evidence

        by_key = {item["key"]: item for item in localized if isinstance(item, dict) and isinstance(item.get("key"), str)}
        expires_at = time.time() + max(1, self.cache_ttl_seconds)
        for item in uncached:
            key = localization_cache_key(item)
            translated = by_key.get(key)
            if not translated:
                continue
            localized_title = clean_localized_text(translated.get("localizedTitle"), 90)
            localized_summary = clean_localized_text(translated.get("localizedSummary"), 140)
            if not localized_title or not localized_summary:
                continue
            apply_localized_news_text(item, localized_title, localized_summary)
            self._cache[key] = CachedNewsLocalization(
                localizedTitle=localized_title,
                localizedSummary=localized_summary,
                expiresAt=expires_at,
            )
        return evidence

    def _localize_with_openai(self, *, symbol: str, intent: str, evidence: list[EvidenceItem]) -> list[dict[str, Any]] | None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            return None
        try:
            payload = {
                "model": os.getenv("AGENT_NEWS_LOCALIZATION_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.2")),
                "input": [
                    {
                        "role": "system",
                        "content": (
                            "You localize financial news for Korean retail investors. "
                            "Use only the supplied article title and summary. "
                            "Do not add prices, investment judgment, recommendations, sources, or facts not present in the article. "
                            "Write a short Korean title and a one-sentence Korean summary. Return strict JSON only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "symbol": symbol,
                                "intent": intent,
                                "articles": [compact_news_for_localization(item) for item in evidence[: self.limit]],
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "news_localization",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "items": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {
                                            "key": {"type": "string"},
                                            "localizedTitle": {"type": "string"},
                                            "localizedSummary": {"type": "string"},
                                        },
                                        "required": ["key", "localizedTitle", "localizedSummary"],
                                    },
                                }
                            },
                            "required": ["items"],
                        },
                    }
                },
            }
            request = urllib.request.Request(
                "https://api.openai.com/v1/responses",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=float(os.getenv("AGENT_NEWS_LOCALIZATION_TIMEOUT_SECONDS", "8"))) as response:
                data = json.loads(response.read().decode("utf-8"))
            parsed = parse_openai_text_json(data)
            items = parsed.get("items")
            return items if isinstance(items, list) else None
        except Exception:
            return None


def preserve_original_news_text(item: EvidenceItem) -> None:
    raw = item.raw if isinstance(item.raw, dict) else {}
    if item.raw is not raw:
        item.raw = raw
    raw.setdefault("originalTitle", item.title)
    raw.setdefault("originalSummary", item.summary)


def apply_localized_news_text(item: EvidenceItem, localized_title: str, localized_summary: str) -> None:
    preserve_original_news_text(item)
    item.raw["localizedTitle"] = localized_title
    item.raw["localizedSummary"] = localized_summary


def compact_news_for_localization(item: EvidenceItem) -> dict[str, Any]:
    raw = item.raw if isinstance(item.raw, dict) else {}
    return {
        "key": localization_cache_key(item),
        "title": str(raw.get("originalTitle") or item.title),
        "summary": str(raw.get("originalSummary") or item.summary),
        "source": raw.get("source"),
        "publishedAt": raw.get("publishedAt") or item.observedAt,
        "symbols": raw.get("symbols"),
    }


def localization_cache_key(item: EvidenceItem) -> str:
    raw = item.raw if isinstance(item.raw, dict) else {}
    article_id = raw.get("articleId") or raw.get("article_id")
    if article_id:
        return f"article:{article_id}"
    return f"url-title:{item.url or ''}|{item.title}"


def clean_localized_text(value: Any, max_length: int) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    return text[:max_length].rstrip()


def chunked(items: list[EvidenceItem], size: int) -> list[list[EvidenceItem]]:
    return [items[index:index + size] for index in range(0, len(items), size)]
