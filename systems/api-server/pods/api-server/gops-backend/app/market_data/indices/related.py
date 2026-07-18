from __future__ import annotations

import inspect
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from app.market_data.heatmap.service import load_heatmap_seed_items
from app.market_data.indices.service import INDEX_DEFINITIONS
from app.services.alfaka_market_data import normalize_market_symbol


RELATED_INDEX_LIMIT = 4
CORRELATION_WINDOW_DAYS = 60
CORRELATION_CACHE_SECONDS = 21_600
SEMICONDUCTOR_KEYWORDS = ("semiconductor", "semiconductors")
DOLLAR_SENSITIVE_SECTORS = frozenset({"basic materials", "energy", "industrials"})

# Ordered rules keep selection deterministic and inside INDEX_DEFINITIONS.
RELATED_INDEX_RULES: tuple[dict[str, Any], ...] = (
    {"symbol": "^GSPC", "relType": "constituent", "priority": 10, "match": "sp500"},
    {"symbol": "^IXIC", "relType": "constituent", "priority": 20, "match": "nasdaq"},
    {"symbol": "^SOX", "relType": "sector", "priority": 30, "match": "semiconductor"},
    {"symbol": "DX-Y.NYB", "relType": "macro", "priority": 40, "match": "dollar-sensitive"},
    {"symbol": "^VIX", "relType": "macro", "priority": 40, "match": "default-macro"},
)


@dataclass(frozen=True)
class RelatedIndexSelection:
    symbol: str
    rel_type: str
    rel_label: str
    priority: int
    weight_pct: float | None = None


def build_related_indices_payload(
    symbol: str,
    *,
    indices_payload: dict[str, Any],
    provider: Any,
    now: datetime | None = None,
    seed_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    normalized_symbol = normalize_market_symbol(symbol)
    generated_at = isoformat_z(now or datetime.now(timezone.utc))
    universe = seed_items if seed_items is not None else safe_seed_items()
    company = next((item for item in universe if normalized_seed_symbol(item) == normalized_symbol), None)
    if company is None:
        return empty_related_payload(normalized_symbol, indices_payload, generated_at)

    metadata = safe_symbol_detail(provider, normalized_symbol)
    selections = select_related_index_rules(
        company,
        exchange=read_string(metadata.get("exchange") or metadata.get("market")),
        total_market_cap=sum_market_caps(universe),
    )
    index_items = {
        read_string(item.get("symbol")): item
        for item in indices_payload.get("items") or []
        if isinstance(item, dict) and read_string(item.get("symbol"))
    }
    company_closes = stored_daily_closes(provider, normalized_symbol)
    company_change = daily_change_percent(company_closes)
    if company_change is None:
        company_change = finite_float(company.get("changePercent"))

    items: list[dict[str, Any]] = []
    missing: list[str] = []
    for selection in selections[:RELATED_INDEX_LIMIT]:
        index_item = index_items.get(selection.symbol)
        if not isinstance(index_item, dict) or finite_float(index_item.get("price")) is None:
            missing.append(selection.symbol)
            continue
        correlation = cached_correlation(
            provider,
            normalized_symbol,
            selection.symbol,
            company_closes=company_closes,
        )
        commentary = build_template_commentary(
            company_symbol=normalized_symbol,
            company_name=read_string(company.get("companyName")) or normalized_symbol,
            index_name=read_string(index_item.get("name")) or selection.symbol,
            index_symbol=selection.symbol,
            rel_type=selection.rel_type,
            company_change_percent=company_change,
            index_change_percent=finite_float(index_item.get("changePercent")),
            correlation_60d=correlation,
            weight_pct=selection.weight_pct,
            generated_at=generated_at,
        )
        items.append({
            **index_item,
            "relType": selection.rel_type,
            "relLabel": selection.rel_label,
            "correlation60d": correlation,
            "weightPct": rounded(selection.weight_pct, 2),
            "companyChangePercent": rounded(company_change, 2),
            "commentary": commentary,
        })

    return {
        "source": "market-indices-related",
        "cacheStatus": indices_payload.get("cacheStatus") or "miss",
        "warning": indices_payload.get("warning"),
        "symbol": normalized_symbol,
        "companyName": read_string(company.get("companyName")) or normalized_symbol,
        "generatedAt": generated_at,
        "coverage": {
            "selected": len(selections[:RELATED_INDEX_LIMIT]),
            "priced": len(items),
            "missing": missing,
        },
        "items": items,
    }


def select_related_index_rules(
    company: dict[str, Any],
    *,
    exchange: str | None,
    total_market_cap: float | None,
) -> list[RelatedIndexSelection]:
    available = {definition["symbol"] for definition in INDEX_DEFINITIONS}
    industry = (read_string(company.get("industry")) or "").lower()
    sector = (read_string(company.get("sector")) or "").lower()
    normalized_exchange = (exchange or "").strip().upper()
    market_cap = finite_float(company.get("marketCap"))
    weight_pct = None
    if market_cap is not None and total_market_cap not in (None, 0):
        weight_pct = max(0.0, market_cap / total_market_cap * 100)

    selected: list[RelatedIndexSelection] = []
    if "^GSPC" in available:
        selected.append(RelatedIndexSelection(
            symbol="^GSPC",
            rel_type="constituent",
            rel_label=(f"편입 지수 · 비중 약 {weight_pct:.2f}%" if weight_pct is not None else "편입 지수"),
            priority=10,
            weight_pct=weight_pct,
        ))
    if any(token in normalized_exchange for token in ("NASDAQ", "NASD", "XNAS")) and "^IXIC" in available:
        selected.append(RelatedIndexSelection(
            symbol="^IXIC",
            rel_type="constituent",
            rel_label="편입 지수 · NASDAQ",
            priority=20,
        ))
    if any(keyword in industry for keyword in SEMICONDUCTOR_KEYWORDS) and "^SOX" in available:
        selected.append(RelatedIndexSelection(
            symbol="^SOX",
            rel_type="sector",
            rel_label="업종 지수 · 반도체",
            priority=30,
        ))

    if sector in DOLLAR_SENSITIVE_SECTORS and "DX-Y.NYB" in available:
        selected.append(RelatedIndexSelection(
            symbol="DX-Y.NYB",
            rel_type="macro",
            rel_label="거시 지표 · 달러 민감도",
            priority=40,
        ))
    elif "^VIX" in available:
        selected.append(RelatedIndexSelection(
            symbol="^VIX",
            rel_type="macro",
            rel_label="거시 지표 · 변동성",
            priority=40,
        ))
    return sorted(selected, key=lambda item: (item.priority, item.symbol))[:RELATED_INDEX_LIMIT]


def pearson_correlation(left: Iterable[float], right: Iterable[float]) -> float | None:
    left_values = [float(value) for value in left]
    right_values = [float(value) for value in right]
    if len(left_values) != len(right_values) or len(left_values) < 2:
        return None
    left_mean = sum(left_values) / len(left_values)
    right_mean = sum(right_values) / len(right_values)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left_values, right_values, strict=True))
    left_variance = sum((value - left_mean) ** 2 for value in left_values)
    right_variance = sum((value - right_mean) ** 2 for value in right_values)
    denominator = math.sqrt(left_variance * right_variance)
    if denominator == 0:
        return None
    return round(max(-1.0, min(1.0, numerator / denominator)), 2)


def correlation_from_closes(company_closes: dict[str, float], index_closes: dict[str, float]) -> float | None:
    shared_dates = sorted(set(company_closes).intersection(index_closes))[-CORRELATION_WINDOW_DAYS:]
    if len(shared_dates) < 20:
        return None
    return pearson_correlation(
        [company_closes[day] for day in shared_dates],
        [index_closes[day] for day in shared_dates],
    )


def build_template_commentary(
    *,
    company_symbol: str,
    company_name: str,
    index_name: str,
    index_symbol: str,
    rel_type: str,
    company_change_percent: float | None,
    index_change_percent: float | None,
    correlation_60d: float | None,
    weight_pct: float | None,
    generated_at: str,
) -> dict[str, Any]:
    company_label = company_name if len(company_name) <= 18 else company_symbol
    index_label = compact_index_name(index_name, index_symbol)
    if rel_type == "constituent":
        relation = f"{company_label}는 {index_label} 편입 종목이다."
        relation_evidence = "지수 편입"
    elif rel_type == "sector":
        relation = f"{company_label}는 {index_label}와 같은 반도체 산업군에 속한다."
        relation_evidence = "동일 산업군"
    else:
        relation = f"{index_label}는 {company_label}에 영향을 주는 거시 지표다."
        relation_evidence = "거시 민감도"

    movement = movement_commentary(
        index_symbol=index_symbol,
        rel_type=rel_type,
        company_change_percent=company_change_percent,
        index_change_percent=index_change_percent,
    )
    body = f"{relation} {movement}"
    if len(body) > 100:
        relation = relation.replace(company_label, company_symbol)
        body = f"{relation} {movement}"

    evidence = [{"label": "관계", "value": relation_evidence}]
    if weight_pct is not None:
        evidence.append({"label": "비중", "value": f"약 {weight_pct:.2f}%"})
    if correlation_60d is not None:
        evidence.append({"label": "60일 상관", "value": signed_decimal(correlation_60d)})
    elif index_change_percent is not None:
        evidence.append({"label": "지수 등락", "value": signed_percent(index_change_percent)})

    return {
        "title": "왜 이 지수를 보여줬나요?",
        "body": body,
        "evidence": evidence[:3],
        "source": "template",
        "generatedAt": generated_at,
    }


def movement_commentary(
    *,
    index_symbol: str,
    rel_type: str,
    company_change_percent: float | None,
    index_change_percent: float | None,
) -> str:
    if company_change_percent is None or index_change_percent is None:
        return "당일 수치가 부족해 방향 비교는 유보한다."
    company_direction = direction(company_change_percent)
    index_direction = direction(index_change_percent)
    if rel_type == "macro" and index_symbol in {"^VIX", "DX-Y.NYB"}:
        if company_direction and index_direction and company_direction != index_direction:
            return "오늘 종목과 지표는 역방향으로 움직여 일반적인 민감도 흐름을 보였다."
        if company_direction and index_direction and company_direction == index_direction:
            return "오늘 종목과 지표가 동행해 개별 요인 영향이 우세한 것으로 해석된다."
        return "오늘 한쪽 변동이 제한돼 방향성 판단은 유보한다."
    if company_direction and index_direction and company_direction != index_direction:
        return "오늘 종목은 지수와 역행해 개별 요인 영향이 우세한 것으로 해석된다."
    if company_direction == "up" and index_direction == "up" and company_change_percent > index_change_percent + 0.05:
        return "오늘 같은 방향으로 상승했고 종목이 지수보다 초과 상승했다."
    if company_direction == "down" and index_direction == "down" and company_change_percent < index_change_percent - 0.05:
        return "오늘 같은 방향으로 하락했고 종목의 낙폭이 지수보다 컸다."
    if company_direction == index_direction and company_direction is not None:
        return "오늘 지수와 종목이 동행해 시장 수급 영향이 함께 나타났다."
    return "오늘 양쪽 변동이 제한돼 뚜렷한 방향성은 확인되지 않았다."


def cached_correlation(
    provider: Any,
    company_symbol: str,
    index_symbol: str,
    *,
    company_closes: dict[str, float] | None = None,
) -> float | None:
    redis_client = redis_client_for_provider(provider)
    cache_key = correlation_cache_key(company_symbol, index_symbol)
    cached = cache_get(redis_client, cache_key)
    if cached is not None and "correlation" in cached:
        return finite_float(cached.get("correlation"))
    company_values = company_closes if company_closes is not None else stored_daily_closes(provider, company_symbol)
    index_values = stored_daily_closes(provider, index_symbol)
    correlation = correlation_from_closes(company_values, index_values)
    cache_set(redis_client, cache_key, {"correlation": correlation}, CORRELATION_CACHE_SECONDS)
    return correlation


def stored_daily_closes(provider: Any, symbol: str) -> dict[str, float]:
    method = getattr(provider, "candle_snapshot", None)
    if not callable(method):
        return {}
    kwargs: dict[str, Any] = {}
    try:
        if "ma_windows" in inspect.signature(method).parameters:
            kwargs["ma_windows"] = ()
    except (TypeError, ValueError):
        pass
    try:
        payload = method(symbol, "1D", CORRELATION_WINDOW_DAYS + 1, **kwargs)
    except Exception:
        return {}
    candles = payload.get("candles") if isinstance(payload, dict) else None
    if not isinstance(candles, list):
        return {}
    closes: dict[str, float] = {}
    for candle in candles:
        if not isinstance(candle, dict):
            continue
        timestamp = read_string(candle.get("timestamp") or candle.get("time"))
        close = finite_float(candle.get("close") if "close" in candle else candle.get("Close"))
        if not timestamp or close is None:
            continue
        closes[timestamp[:10]] = close
    return dict(sorted(closes.items())[-(CORRELATION_WINDOW_DAYS + 1):])


def daily_change_percent(closes: dict[str, float]) -> float | None:
    values = list(closes.values())
    if len(values) < 2 or values[-2] == 0:
        return None
    return round((values[-1] - values[-2]) / values[-2] * 100, 2)


def empty_related_payload(symbol: str, indices_payload: dict[str, Any], generated_at: str) -> dict[str, Any]:
    return {
        "source": "market-indices-related",
        "cacheStatus": indices_payload.get("cacheStatus") or "miss",
        "warning": None,
        "symbol": symbol,
        "companyName": symbol,
        "generatedAt": generated_at,
        "coverage": {"selected": 0, "priced": 0, "missing": []},
        "items": [],
    }


def safe_seed_items() -> list[dict[str, Any]]:
    try:
        return load_heatmap_seed_items("sp500")
    except Exception:
        return []


def safe_symbol_detail(provider: Any, symbol: str) -> dict[str, Any]:
    method = getattr(provider, "symbol_detail", None)
    if not callable(method):
        return {}
    try:
        payload = method(symbol)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def sum_market_caps(items: list[dict[str, Any]]) -> float | None:
    values = [value for item in items if (value := finite_float(item.get("marketCap"))) is not None and value > 0]
    return sum(values) if values else None


def normalized_seed_symbol(item: dict[str, Any]) -> str | None:
    symbol = read_string(item.get("symbol"))
    if not symbol:
        return None
    try:
        return normalize_market_symbol(symbol)
    except ValueError:
        return None


def compact_index_name(name: str, symbol: str) -> str:
    aliases = {
        "^GSPC": "S&P 500",
        "^IXIC": "NASDAQ",
        "^SOX": "미국 반도체 지수",
        "^VIX": "VIX",
        "DX-Y.NYB": "달러 지수",
    }
    return aliases.get(symbol) or (name if len(name) <= 18 else symbol)


def direction(value: float) -> str | None:
    if value > 0:
        return "up"
    if value < 0:
        return "down"
    return None


def signed_decimal(value: float) -> str:
    prefix = "+" if value > 0 else "−" if value < 0 else ""
    return f"{prefix}{abs(value):.2f}"


def signed_percent(value: float) -> str:
    prefix = "+" if value > 0 else "−" if value < 0 else ""
    return f"{prefix}{abs(value):.2f}%"


def correlation_cache_key(company_symbol: str, index_symbol: str) -> str:
    safe_index_symbol = index_symbol.replace("^", "").replace("=", "-").replace(".", "-")
    return redis_key(f"indices:related:correlation:{company_symbol}:{safe_index_symbol}:{CORRELATION_WINDOW_DAYS}d")


def redis_key(value: str) -> str:
    prefix = (os.getenv("REDIS_KEY_PREFIX") or "gops:market:on-demand:v1").strip().strip(":")
    return f"{prefix}:{value}" if prefix else value


def redis_client_for_provider(provider: Any) -> Any | None:
    redis_provider = getattr(provider, "redis_provider", None)
    return getattr(redis_provider, "redis", None)


def cache_get(redis_client: Any | None, key: str) -> dict[str, Any] | None:
    if redis_client is None:
        return None
    try:
        raw = redis_client.get(key)
    except Exception:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def cache_set(redis_client: Any | None, key: str, payload: dict[str, Any], ttl: int) -> None:
    if redis_client is None:
        return
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        redis_client.set(key, encoded, ex=ttl)
    except TypeError:
        try:
            redis_client.set(key, encoded)
            redis_client.expire(key, ttl)
        except Exception:
            return
    except Exception:
        return


def finite_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def rounded(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


def read_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def isoformat_z(value: datetime) -> str:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
