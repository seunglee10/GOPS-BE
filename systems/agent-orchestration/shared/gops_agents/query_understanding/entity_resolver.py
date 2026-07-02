from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Any

try:
    from rapidfuzz import fuzz
except Exception:  # pragma: no cover - exercised only when optional dependency is absent.
    fuzz = None

from .alias_index import EntityAlias, EntityAliasIndex, default_alias_index
from .korean_text import choseong_key, choseong_tokens, compact_text, jamo_key, query_fragments, similarity


@dataclass
class EntityCandidate:
    symbol: str
    canonical_name: str
    matched_text: str
    matched_alias: str
    match_type: str
    score: float
    confidence: float
    source: str
    entity_type: str = "company"
    entity_id: str = ""
    theme_name: str | None = None
    theme_symbols: tuple[str, ...] = ()
    theme_category: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["entityType"] = payload.pop("entity_type")
        payload["entityId"] = payload.pop("entity_id")
        payload["themeName"] = payload.pop("theme_name")
        payload["themeSymbols"] = list(payload.pop("theme_symbols") or [])
        payload["themeCategory"] = payload.pop("theme_category")
        return payload


@dataclass
class EntityResolution:
    status: str
    symbol: str | None = None
    canonical_name: str | None = None
    confidence: float = 0.0
    match_type: str = "none"
    matched_text: str = ""
    matched_alias: str = ""
    candidates: list[EntityCandidate] = field(default_factory=list)
    needs_clarification: bool = False
    reason: str = ""
    entity_type: str | None = None
    entity_id: str | None = None
    catalog_source: str | None = None
    theme_name: str | None = None
    theme_symbols: tuple[str, ...] = ()
    theme_category: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "symbol": self.symbol,
            "canonicalName": self.canonical_name,
            "confidence": round(float(self.confidence), 4),
            "matchType": self.match_type,
            "matchedText": self.matched_text,
            "matchedAlias": self.matched_alias,
            "needsClarification": bool(self.needs_clarification),
            "reason": self.reason,
            "entityType": self.entity_type,
            "entityId": self.entity_id,
            "catalogSource": self.catalog_source,
            "themeName": self.theme_name,
            "themeSymbols": list(self.theme_symbols or []),
            "themeCategory": self.theme_category,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


class KoreanEntityResolver:
    def __init__(self, aliases: tuple[EntityAlias, ...] | None = None, index: EntityAliasIndex | None = None):
        self.index = index if index is not None else (EntityAliasIndex(aliases, known_symbols_from_aliases(aliases)) if aliases is not None else default_alias_index())
        self.aliases = self.index.aliases
        self.known_symbols = self.index.known_symbols

    def resolve(self, query: Any, chart_context: Any = None) -> EntityResolution:
        text = str(query or "")
        ticker = self._ticker_candidate(text)
        if ticker is not None:
            return self._confirmed(ticker, reason="matched ticker token")

        exact_candidates = self._exact_alias_candidates(text)
        if exact_candidates:
            return self._select(exact_candidates, reason="matched exact catalog alias")

        choseong_candidates = self._choseong_candidates(text)
        if choseong_candidates:
            return self._select(choseong_candidates, reason="matched unique initial-consonant alias")

        fuzzy_candidates = self._fuzzy_candidates(text)
        if fuzzy_candidates:
            return self._select(fuzzy_candidates, reason="matched fuzzy catalog alias")

        return EntityResolution(status="not_found", needs_clarification=False, reason="no entity candidate matched")

    def _ticker_candidate(self, text: str) -> EntityCandidate | None:
        for match in re.finditer(r"(?<![A-Za-z0-9.])([A-Za-z][A-Za-z0-9]{0,4}(?:\.[A-Za-z])?)(?![A-Za-z0-9.])", text):
            raw_token = match.group(1)
            token = raw_token.upper()
            if token in EXCLUDED_TICKER_TOKENS:
                continue
            if raw_token != token and raw_token.lower() in AMBIGUOUS_LOWERCASE_WORDS:
                continue
            if token in self.known_symbols:
                return EntityCandidate(
                    symbol=token,
                    canonical_name=token,
                    matched_text=raw_token,
                    matched_alias=token,
                    match_type="ticker_exact",
                    score=1.0,
                    confidence=0.99,
                    source="ticker",
                    entity_type="company",
                    entity_id=f"company:{token}",
                )
        return None

    def _exact_alias_candidates(self, text: str) -> list[EntityCandidate]:
        candidates = []
        for alias in self.index.exact_matches(text):
            if alias.alias_type == "ticker":
                continue
            candidates.append(candidate_from_alias(alias, matched_text=alias.alias, match_type="alias_exact", score=0.96 + min(alias.priority, 1.0) * 0.02, confidence=0.96))
        return candidates

    def _choseong_candidates(self, text: str) -> list[EntityCandidate]:
        tokens = choseong_tokens(text, min_length=2)
        if not tokens:
            return []
        candidates = []
        for token, alias in self.index.choseong_matches(tokens):
            if alias.alias_type == "ticker":
                continue
            candidates.append(candidate_from_alias(alias, matched_text=token, match_type="choseong_exact", score=0.9, confidence=0.86))
        return candidates

    def _fuzzy_candidates(self, text: str) -> list[EntityCandidate]:
        best: dict[tuple[str, str, str], EntityCandidate] = {}
        fragments = query_fragments(text, min_length=2, max_length=12)
        for fragment in fragments:
            fragment_compact = compact_text(fragment)
            fragment_jamo = jamo_key(fragment)
            fragment_choseong = choseong_key(fragment)
            for alias in self.index.fuzzy_aliases(fragment):
                if alias.alias_type == "ticker" or len(alias.compact) < 2:
                    continue
                if abs(len(fragment_compact) - len(alias.compact)) > 2:
                    continue
                if abs(len(fragment_jamo) - len(alias.jamo)) > 2:
                    continue
                score = max(
                    fuzzy_ratio(fragment_compact, alias.compact),
                    fuzzy_ratio(fragment_jamo, alias.jamo),
                    similarity(fragment_choseong, alias.choseong) if len(alias.choseong) >= 3 else 0.0,
                )
                if score < 0.78:
                    continue
                candidate = candidate_from_alias(alias, matched_text=fragment, match_type="alias_fuzzy", score=score, confidence=min(0.9, score))
                key = (alias.entity_id, alias.symbol, alias.alias)
                current = best.get(key)
                if current is None or (candidate.confidence, candidate.score) > (current.confidence, current.score):
                    best[key] = candidate
        return list(best.values())

    def _confirmed(self, candidate: EntityCandidate, *, reason: str) -> EntityResolution:
        return EntityResolution(
            status="confirmed",
            symbol=candidate.symbol or None,
            canonical_name=candidate.canonical_name,
            confidence=candidate.confidence,
            match_type=candidate.match_type,
            matched_text=candidate.matched_text,
            matched_alias=candidate.matched_alias,
            candidates=[candidate],
            needs_clarification=False,
            reason=reason,
            entity_type=candidate.entity_type,
            entity_id=candidate.entity_id,
            catalog_source=candidate.source,
            theme_name=candidate.theme_name,
            theme_symbols=candidate.theme_symbols,
            theme_category=candidate.theme_category,
        )

    def _select(self, candidates: list[EntityCandidate], *, reason: str) -> EntityResolution:
        deduped = best_by_entity(candidates)
        ordered = sorted(deduped, key=lambda item: (item.confidence, item.score), reverse=True)
        if not ordered:
            return EntityResolution(status="not_found", reason="no entity candidate matched")
        best = ordered[0]
        runner_up = ordered[1] if len(ordered) > 1 else None
        if runner_up and best.confidence - runner_up.confidence < 0.08:
            return EntityResolution(
                status="ambiguous",
                candidates=ordered[:5],
                needs_clarification=True,
                reason="multiple entity candidates are too close",
            )
        if best.confidence < 0.78:
            return EntityResolution(
                status="ambiguous",
                candidates=ordered[:5],
                needs_clarification=True,
                reason="best entity candidate confidence is too low",
            )
        return self._confirmed(best, reason=reason)


def candidate_from_alias(alias: EntityAlias, *, matched_text: str, match_type: str, score: float, confidence: float) -> EntityCandidate:
    return EntityCandidate(
        symbol=alias.symbol,
        canonical_name=alias.canonical_name,
        matched_text=matched_text,
        matched_alias=alias.alias,
        match_type=match_type,
        score=float(score),
        confidence=min(float(confidence), float(alias.confidence or 1.0)),
        source=alias.source,
        entity_type=alias.entity_type,
        entity_id=alias.entity_id,
        theme_name=alias.theme_name,
        theme_symbols=alias.theme_symbols,
        theme_category=alias.theme_category,
    )


def fuzzy_ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if fuzz is not None:
        return fuzz.ratio(left, right) / 100.0
    return similarity(left, right)


def best_by_entity(candidates: list[EntityCandidate]) -> list[EntityCandidate]:
    by_entity: dict[tuple[str, str, str], EntityCandidate] = {}
    for candidate in candidates:
        key = (candidate.entity_type, candidate.entity_id or candidate.symbol, candidate.symbol)
        current = by_entity.get(key)
        if current is None or (candidate.confidence, candidate.score) > (current.confidence, current.score):
            by_entity[key] = candidate
    return list(by_entity.values())


def best_by_symbol(candidates: list[EntityCandidate]) -> list[EntityCandidate]:
    return best_by_entity(candidates)


def known_symbols_from_aliases(aliases: tuple[EntityAlias, ...] | None) -> frozenset[str]:
    return frozenset(alias.symbol for alias in aliases or () if alias.symbol)


@lru_cache(maxsize=1)
def default_resolver() -> KoreanEntityResolver:
    return KoreanEntityResolver()


def resolve_entity(query: Any, chart_context: Any = None) -> EntityResolution:
    return default_resolver().resolve(query, chart_context=chart_context)


def extract_symbol_from_intent(intent: str) -> str | None:
    resolution = resolve_entity(intent)
    if resolution.status == "confirmed" and resolution.entity_type == "company":
        return resolution.symbol
    return None


EXCLUDED_TICKER_TOKENS = {
    "AI",
    "API",
    "CEO",
    "CFO",
    "ETF",
    "GDP",
    "KST",
    "MVP",
    "PER",
    "PBR",
    "RAG",
    "ROI",
    "USD",
}

AMBIGUOUS_LOWERCASE_WORDS = {
    "a",
    "all",
    "are",
    "at",
    "ball",
    "best",
    "bro",
    "cat",
    "cost",
    "day",
    "de",
    "do",
    "d",
    "f",
    "fix",
    "for",
    "fox",
    "has",
    "hi",
    "it",
    "key",
    "ko",
    "low",
    "now",
    "o",
    "on",
    "or",
    "q",
    "so",
    "sw",
    "t",
    "tell",
    "to",
    "v",
}
