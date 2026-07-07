from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from app.alerts.notifications import RedisNotificationBroker
from app.alerts.repository import InMemoryAlertRepository, PostgresAlertRepository
from app.core.sectors import normalize_sector_list, sector_payload_fields
from app.market_data.heatmap.service import get_heatmap_service
from app.services.alfaka_market_data import get_market_data_provider, read_watchlist_symbols

from .repository import RecommendationRunCreate, RecommendationRepository
from .scoring import (
    NEWS_LOOKBACK_DAYS,
    RecommendationInput,
    build_candidates,
    filter_candles_for_session,
    is_market_session_open,
    market_session,
    min_candle_count,
    normalize_profile,
    normalize_session_mode,
    parse_datetime,
    recommendation_slot,
    score_recommendations,
)


class RecommendationDataSource:
    def __init__(self, app: Any) -> None:
        self.app = app

    def watchlist_symbols(self, user_sub: str) -> list[str]:
        provider = getattr(self.app.state, "recommendation_watchlist_provider", None)
        if callable(provider):
            return list(provider(user_sub))
        try:
            return read_watchlist_symbols(user_sub)
        except Exception:
            return []

    def portfolio_positions(self, user_sub: str) -> list[dict[str, Any]]:
        provider = getattr(self.app.state, "recommendation_portfolio_provider", None)
        if callable(provider):
            return list(provider(user_sub))
        snapshots = getattr(self.app.state, "portfolio_holdings_snapshots", {})
        payload = snapshots.get(user_sub) if isinstance(snapshots, dict) else None
        if payload is None:
            repository = getattr(self.app.state, "recommendation_repository", None)
            get_snapshot = getattr(repository, "get_portfolio_snapshot", None)
            if callable(get_snapshot):
                snapshot = get_snapshot(user_sub)
                payload = snapshot.get("payload") if isinstance(snapshot, dict) else None
        positions = payload.get("positions") if isinstance(payload, dict) else []
        return [dict(item) for item in positions if isinstance(item, dict)]

    def market_items(self) -> list[dict[str, Any]]:
        provider = getattr(self.app.state, "recommendation_market_provider", None)
        if callable(provider):
            return list(provider())
        try:
            payload = get_heatmap_service().snapshot("sp500")
        except Exception:
            return []
        items = payload.get("items") if isinstance(payload, dict) else []
        return [dict(item) for item in items if isinstance(item, dict)]

    def candles(self, symbol: str, now: datetime) -> list[dict[str, Any]]:
        provider = getattr(self.app.state, "recommendation_candles_provider", None)
        if callable(provider):
            return list(provider(symbol, now))
        try:
            market_provider = get_market_data_provider()
            redis_candles = market_provider.redis_provider.recent_candles(symbol, "1m", 240)
            clickhouse_candles: list[dict[str, Any]] = []
            if len(redis_candles or []) < 120:
                clickhouse_candles = market_provider.clickhouse_provider.candles(symbol, "1m", 240)
            return merge_candles([*(clickhouse_candles or []), *(redis_candles or [])])[-240:]
        except Exception:
            return []

    def news(self, symbol: str, now: datetime) -> list[dict[str, Any]]:
        normalized_symbol = str(symbol or "").strip().upper()
        return self.news_for_symbols([normalized_symbol], now).get(normalized_symbol, [])

    def news_for_symbols(self, symbols: list[str], now: datetime) -> dict[str, list[dict[str, Any]]]:
        normalized_symbols = normalize_news_symbols(symbols)
        provider = getattr(self.app.state, "recommendation_news_provider", None)
        if callable(provider):
            result: dict[str, list[dict[str, Any]]] = {}
            for symbol in normalized_symbols:
                try:
                    rows = provider(symbol, now)
                except TypeError:
                    rows = provider(symbol)
                result[symbol] = filter_recent_news(list(rows or []), now=now)
            return result
        result: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in normalized_symbols}
        missing_symbols = list(normalized_symbols)
        try:
            market_provider = get_market_data_provider()
            redis_provider = getattr(market_provider, "redis_provider", None)
            method = getattr(redis_provider, "localized_news_articles", None)
            if callable(method):
                missing_symbols = []
                for symbol in normalized_symbols:
                    rows = filter_recent_news(list(method(symbol, limit=5, locale="ko-KR") or []), now=now)
                    result[symbol] = rows
                    if not rows:
                        missing_symbols.append(symbol)
        except Exception:
            missing_symbols = list(normalized_symbols)
        if missing_symbols:
            result.update(self.alpaca_news_fallback_for_symbols(missing_symbols, now))
        return result

    def alpaca_news_fallback(self, symbol: str, now: datetime) -> list[dict[str, Any]]:
        normalized_symbol = str(symbol or "").strip().upper()
        return self.alpaca_news_fallback_for_symbols([normalized_symbol], now).get(normalized_symbol, [])

    def alpaca_news_fallback_for_symbols(self, symbols: list[str], now: datetime) -> dict[str, list[dict[str, Any]]]:
        normalized_symbols = normalize_news_symbols(symbols)
        provider = getattr(self.app.state, "recommendation_alpaca_news_provider", None)
        if callable(provider):
            result: dict[str, list[dict[str, Any]]] = {}
            for symbol in normalized_symbols:
                try:
                    rows = provider(symbol, now)
                except TypeError:
                    rows = provider(symbol)
                result[symbol] = filter_recent_news(normalize_alpaca_news_rows(rows or [], symbol=symbol, now=now), now=now)
            return result
        try:
            from alfaka.alpaca.news import build_news_events, fetch_alpaca_news
            from alfaka.common.secrets import load_alpaca_credentials
        except Exception:
            return {symbol: [] for symbol in normalized_symbols}

        key_id, secret_key = load_alpaca_credentials()
        if not key_id or not secret_key:
            return {symbol: [] for symbol in normalized_symbols}

        try:
            articles = fetch_alpaca_news(
                key_id,
                secret_key,
                symbols=normalized_symbols,
                limit=positive_int_env(
                    "RECOMMENDATION_ALPACA_NEWS_FALLBACK_LIMIT",
                    default=min(50, max(5, len(normalized_symbols) * 5)),
                    maximum=50,
                ),
                include_content=bool_env("RECOMMENDATION_ALPACA_NEWS_INCLUDE_CONTENT", default=False),
                start=iso_z(now - timedelta(days=NEWS_LOOKBACK_DAYS)),
                end=iso_z(now),
                sort="desc",
            )
            grouped: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in normalized_symbols}
            for article in articles or []:
                if not isinstance(article, dict):
                    continue
                for event in build_news_events(article, requested_symbols=normalized_symbols, received_at=iso_z(now)):
                    event_symbol = str(event.get("symbol") or "").strip().upper()
                    if event_symbol in grouped:
                        grouped[event_symbol].append({**event, "dataSource": "alpaca-direct"})
            return {symbol: filter_recent_news(rows, now=now) for symbol, rows in grouped.items()}
        except Exception:
            return {symbol: [] for symbol in normalized_symbols}


class RecommendationService:
    def __init__(self, *, repository: RecommendationRepository, data_source: RecommendationDataSource, app: Any | None = None) -> None:
        self.repository = repository
        self.data_source = data_source
        self.app = app

    def latest(self, user_sub: str, *, session_mode: str = "regular") -> dict[str, Any]:
        session_mode = normalize_session_mode(session_mode)
        profile = self.repository.get_profile(user_sub)
        if profile is None:
            return {"status": "profile_required", "items": []}
        latest_for_session = getattr(self.repository, "latest_run_for_session", None)
        run = latest_for_session(user_sub, session_mode) if callable(latest_for_session) else self.repository.latest_run(user_sub)
        if not run:
            return {"status": "empty", "items": [], "profile": profile, "summary": base_summary(session_mode=session_mode)}
        return response_for_run(run, profile=profile)

    def refresh(
        self,
        user_sub: str,
        *,
        now: datetime | None = None,
        active_symbol: str | None = None,
        session_mode: str = "regular",
    ) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        session_mode = normalize_session_mode(session_mode)
        profile_row = self.repository.get_profile(user_sub)
        if profile_row is None:
            return {"status": "profile_required", "items": []}
        actual_session = market_session(now)
        slot = recommendation_slot(now, session_mode=session_mode)
        run_key = f"{user_sub}:{slot['marketDate']}:{session_mode}:{slot['slotStart']}"
        existing = self.repository.get_run_by_key(user_sub, run_key)
        existing_summary = existing.get("summary") if isinstance(existing, dict) else {}
        if existing and not (isinstance(existing_summary, dict) and existing_summary.get("retryable")):
            return response_for_run(existing, profile=profile_row, idempotent_replay=True)
        if not is_market_session_open(now, session_mode):
            latest_for_session = getattr(self.repository, "latest_run_for_session", None)
            latest = latest_for_session(user_sub, session_mode) if callable(latest_for_session) else self.repository.latest_run(user_sub)
            payload = response_for_run(latest, profile=profile_row, stale=True) if latest else {"items": [], "profile": profile_row}
            payload["status"] = "market_closed"
            payload["summary"] = {
                **base_summary(session_mode=session_mode, actual_session=actual_session),
                "emptyReason": f"{session_mode}_not_active",
            }
            return payload

        profile = normalize_profile(profile_row)
        watchlist = self.data_source.watchlist_symbols(user_sub)
        positions = self.data_source.portfolio_positions(user_sub)
        market_items = self.data_source.market_items()
        candidates = build_candidates(
            watchlist_symbols=watchlist,
            portfolio_positions=positions,
            preferred_sectors=list(profile.preferred_sectors),
            market_items=market_items,
        )
        symbols = [candidate.symbol for candidate in candidates]
        candle_symbols = [*symbols, "SPY"]
        candles_by_symbol = {symbol: self.data_source.candles(symbol, now) for symbol in candle_symbols}
        spy_candles = candles_by_symbol.get("SPY", [])
        news_by_symbol = self.data_source.news_for_symbols(symbols, now)
        latest_for_session = getattr(self.repository, "latest_run_for_session", None)
        previous_run = latest_for_session(user_sub, session_mode) if callable(latest_for_session) else self.repository.latest_run(user_sub)
        items = score_recommendations(
            RecommendationInput(
                profile=profile,
                watchlist_symbols=watchlist,
                portfolio_positions=positions,
                market_items=market_items,
                candles_by_symbol=candles_by_symbol,
                spy_candles=spy_candles,
                active_symbol=active_symbol,
                session_mode=session_mode,
                news_by_symbol=news_by_symbol,
                now=now,
            )
        )
        summary = {
            **base_summary(session_mode=session_mode, actual_session=actual_session),
            "candidateCount": len(candidates),
            "recommendedCount": len(items),
            "marketItemCount": len(market_items),
            "excludedHeldCount": len({str(item.get("symbol") or "").strip().upper() for item in positions if str(item.get("symbol") or "").strip()}),
            "excludedWatchlistCount": len({symbol.strip().upper() for symbol in watchlist if symbol.strip()}),
            "excludedActiveSymbol": bool(active_symbol),
            "emptyReason": None,
            "rejectedByReason": rejection_summary(candidates, candles_by_symbol, session_mode=session_mode, now=now),
        }
        if not items:
            empty_reason = empty_reason_for(candidates, candles_by_symbol, session_mode=session_mode, now=now)
            summary["emptyReason"] = empty_reason
            summary["retryable"] = empty_reason == "insufficient_session_data"
            if summary["retryable"]:
                return {
                    "status": "empty",
                    "items": [],
                    "profile": profile_row,
                    "summary": summary,
                    "runKey": run_key,
                    "slotStart": slot["slotStart"],
                    "marketDate": slot["marketDate"],
                    "retryable": True,
                }
        run = self.repository.create_or_replace_run(
            RecommendationRunCreate(
                user_sub=user_sub,
                run_key=run_key,
                slot_start=slot["slotStart"],
                market_date=slot["marketDate"],
                status="completed" if items else "empty",
                profile_snapshot=profile_row,
                market_snapshot_time=now.isoformat(),
                summary=summary,
            ),
            items,
        )
        self._maybe_notify(user_sub, run, previous=previous_run)
        return response_for_run(run, profile=profile_row)

    def _maybe_notify(self, user_sub: str, run: dict[str, Any], *, previous: dict[str, Any] | None) -> None:
        if not self.app:
            return
        items = run.get("items") if isinstance(run.get("items"), list) else []
        if not items:
            return
        top = items[0]
        should_notify = float(top.get("score") or 0) >= 80 and float(top.get("confidence") or 0) >= 0.75
        previous_items = previous.get("items") if isinstance(previous, dict) and isinstance(previous.get("items"), list) else []
        if previous_items and previous_items[0].get("symbol") != top.get("symbol"):
            should_notify = True
        previous_by_symbol = {item.get("symbol"): item for item in previous_items if isinstance(item, dict)}
        previous_score = float((previous_by_symbol.get(top.get("symbol")) or {}).get("score") or 0)
        if previous_score and float(top.get("score") or 0) - previous_score >= 15:
            should_notify = True
        if not should_notify:
            return
        payload = {
            "runId": run.get("id"),
            "runKey": run.get("run_key"),
            "symbol": top.get("symbol"),
            "score": top.get("score"),
            "confidence": top.get("confidence"),
            "reasonSummary": " / ".join(str(item.get("text")) for item in top.get("reasons", [])[:2] if isinstance(item, dict)),
            "riskWarnings": top.get("riskWarnings") or top.get("risk_warnings") or [],
        }
        summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
        event_id = f"{user_sub}:{run.get('slot_start')}:{summary.get('sessionMode', 'regular')}:{top.get('symbol')}:stock_buy"
        try:
            alert_repo = getattr(self.app.state, "alert_repository", None)
            if alert_repo is None:
                alert_repo = InMemoryAlertRepository() if _recommendation_memory_mode() else PostgresAlertRepository.from_env()
                self.app.state.alert_repository = alert_repo
            notification = alert_repo.create_notification(
                user_sub=user_sub,
                alert_id=None,
                event_id=event_id,
                notification_type="recommendation.stock_buy",
                payload=payload,
            )
            broker = getattr(self.app.state, "alert_notification_broker", None)
            if broker is None:
                broker = RedisNotificationBroker.from_env()
                self.app.state.alert_notification_broker = broker
            broker.publish_user(user_sub, {"type": "notification", "notification": notification, "event": payload})
        except Exception:
            return


def candidate_symbols(watchlist: list[str], positions: list[dict[str, Any]], market_items: list[dict[str, Any]]) -> list[str]:
    symbols: list[str] = []
    for value in [*watchlist, *[str(item.get("symbol") or "") for item in positions], *[str(item.get("symbol") or "") for item in market_items]]:
        symbol = value.strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
        if len(symbols) >= 50:
            break
    if "SPY" not in symbols:
        symbols.append("SPY")
    return symbols


def merge_candles(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_timestamp: dict[str, dict[str, Any]] = {}
    for candle in candles:
        timestamp = str(candle.get("timestamp") or candle.get("eventTime") or "")
        if timestamp:
            by_timestamp[timestamp] = dict(candle)
    return [by_timestamp[key] for key in sorted(by_timestamp)]


def filter_recent_news(rows: list[dict[str, Any]], *, now: datetime) -> list[dict[str, Any]]:
    cutoff = now - timedelta(days=NEWS_LOOKBACK_DAYS)
    filtered: list[dict[str, Any]] = []
    for row in rows:
        observed = parse_datetime(row.get("publishedAt") or row.get("published_at") or row.get("localizedAt") or row.get("receivedAt"))
        if observed is not None and observed >= cutoff:
            filtered.append(dict(row))
        if len(filtered) >= 5:
            break
    return filtered


def normalize_news_symbols(symbols: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in symbols:
        symbol = str(value or "").strip().upper()
        if symbol and symbol not in normalized:
            normalized.append(symbol)
    return normalized


def normalize_alpaca_news_rows(rows: list[dict[str, Any]], *, symbol: str, now: datetime) -> list[dict[str, Any]]:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        published_at = (
            row.get("publishedAt")
            or row.get("published_at")
            or row.get("created_at")
            or row.get("createdAt")
            or row.get("receivedAt")
            or iso_z(now)
        )
        normalized.append({
            "symbol": str(row.get("symbol") or normalized_symbol),
            "articleId": row.get("articleId") or row.get("article_id") or row.get("id"),
            "headline": row.get("headline") or row.get("title") or "Untitled news",
            "summary": row.get("summary") or row.get("content"),
            "content": row.get("content"),
            "url": row.get("url"),
            "source": row.get("source") or "alpaca",
            "author": row.get("author"),
            "publishedAt": published_at,
            "updatedAt": row.get("updatedAt") or row.get("updated_at"),
            "receivedAt": row.get("receivedAt") or row.get("received_at") or iso_z(now),
            "symbols": row.get("symbols") if isinstance(row.get("symbols"), list) else [normalized_symbol],
            "impactDirection": row.get("impactDirection") or row.get("impact_direction"),
            "sentiment": row.get("sentiment"),
            "dataSource": "alpaca-direct",
            "raw": row.get("raw") or row,
        })
    return normalized


def bool_env(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def positive_int_env(name: str, *, default: int, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    value = max(1, value)
    return min(value, maximum) if maximum else value


def iso_z(value: datetime) -> str:
    normalized = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def base_summary(*, session_mode: str, actual_session: str | None = None) -> dict[str, Any]:
    return {
        "sessionMode": normalize_session_mode(session_mode),
        "actualMarketSession": actual_session,
        "newsSource": "redis_alpaca_fallback",
        "newsLookbackDays": NEWS_LOOKBACK_DAYS,
        "regularMarketOnly": False,
    }


def rejection_summary(candidates: list[Any], candles_by_symbol: dict[str, list[dict[str, Any]]], *, session_mode: str, now: datetime) -> dict[str, int]:
    min_count = min_candle_count(session_mode, now)
    insufficient = 0
    for candidate in candidates:
        candles = filter_candles_for_session(candles_by_symbol.get(candidate.symbol) or [], session_mode, now)
        if len(candles) < min_count:
            insufficient += 1
    return {"insufficientSessionData": insufficient}


def empty_reason_for(candidates: list[Any], candles_by_symbol: dict[str, list[dict[str, Any]]], *, session_mode: str, now: datetime) -> str:
    if not candidates:
        return "no_candidates"
    rejected = rejection_summary(candidates, candles_by_symbol, session_mode=session_mode, now=now)
    if rejected["insufficientSessionData"] >= len(candidates):
        return "insufficient_session_data"
    return "no_candidates_after_filters"


def response_for_run(run: dict[str, Any] | None, *, profile: dict[str, Any] | None = None, stale: bool = False, idempotent_replay: bool = False) -> dict[str, Any]:
    if run is None:
        return {"status": "empty", "items": [], "profile": normalize_profile_sector_fields(profile)}
    return {
        "status": run.get("status") or "empty",
        "runId": run.get("id"),
        "runKey": run.get("run_key"),
        "slotStart": run.get("slot_start"),
        "marketDate": run.get("market_date"),
        "generatedAt": run.get("generated_at"),
        "summary": run.get("summary") or {},
        "items": normalize_items(run.get("items") or []),
        "profile": normalize_profile_sector_fields(profile),
        "stale": stale,
        "idempotentReplay": idempotent_replay,
    }


def normalize_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for item in items:
        normalized.append({
            "symbol": item.get("symbol"),
            "action": item.get("action", "buy"),
            "rank": item.get("rank"),
            "score": item.get("score"),
            "confidence": item.get("confidence"),
            **sector_payload_fields(item.get("sector")),
            "reasons": item.get("reasons") or [],
            "riskWarnings": item.get("riskWarnings") or item.get("risk_warnings") or [],
            "metricsSnapshot": item.get("metricsSnapshot") or item.get("metrics_snapshot") or {},
        })
    return normalized


def normalize_profile_sector_fields(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    if profile is None:
        return None
    normalized = dict(profile)
    preferred = normalize_sector_list(normalized.get("preferred_sectors") or normalized.get("preferredSectors") or [])
    excluded = normalize_sector_list(normalized.get("excluded_sectors") or normalized.get("excludedSectors") or [])
    if "preferred_sectors" in normalized:
        normalized["preferred_sectors"] = preferred
    if "preferredSectors" in normalized:
        normalized["preferredSectors"] = preferred
    if "excluded_sectors" in normalized:
        normalized["excluded_sectors"] = excluded
    if "excludedSectors" in normalized:
        normalized["excludedSectors"] = excluded
    return normalized


def _recommendation_memory_mode() -> bool:
    return os.getenv("AUTH_ENABLED", "false").strip().lower() not in {"1", "true", "yes", "on"} and not _database_configured()


def _database_configured() -> bool:
    return bool(
        os.getenv("DATABASE_URL")
        or (
            os.getenv("DATABASE_HOST")
            and os.getenv("DATABASE_NAME")
            and os.getenv("DATABASE_USER")
            and os.getenv("DATABASE_PASSWORD")
        )
    )
