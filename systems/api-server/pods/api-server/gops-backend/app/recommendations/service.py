from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from app.alerts.notifications import RedisNotificationBroker
from app.alerts.repository import InMemoryAlertRepository, PostgresAlertRepository
from app.market_data.heatmap.service import get_heatmap_service
from app.services.alfaka_market_data import get_market_data_provider, read_watchlist_symbols

from .repository import RecommendationRunCreate, RecommendationRepository
from .scoring import (
    RecommendationInput,
    is_regular_market_open,
    normalize_profile,
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
            if redis_candles:
                return redis_candles[-180:]
            return market_provider.clickhouse_provider.candles(symbol, "1m", 180)
        except Exception:
            return []

    def news(self, symbol: str) -> list[dict[str, Any]]:
        provider = getattr(self.app.state, "recommendation_news_provider", None)
        if callable(provider):
            return list(provider(symbol))
        return []


class RecommendationService:
    def __init__(self, *, repository: RecommendationRepository, data_source: RecommendationDataSource, app: Any | None = None) -> None:
        self.repository = repository
        self.data_source = data_source
        self.app = app

    def latest(self, user_sub: str) -> dict[str, Any]:
        profile = self.repository.get_profile(user_sub)
        if profile is None:
            return {"status": "profile_required", "items": []}
        run = self.repository.latest_run(user_sub)
        if not run:
            return {"status": "empty", "items": [], "profile": profile}
        return response_for_run(run, profile=profile)

    def refresh(self, user_sub: str, *, now: datetime | None = None, active_symbol: str | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        profile_row = self.repository.get_profile(user_sub)
        if profile_row is None:
            return {"status": "profile_required", "items": []}
        slot = recommendation_slot(now)
        run_key = f"{user_sub}:{slot['marketDate']}:{slot['slotStart']}"
        existing = self.repository.get_run_by_key(user_sub, run_key)
        if existing:
            return response_for_run(existing, profile=profile_row, idempotent_replay=True)
        if not is_regular_market_open(now):
            latest = self.repository.latest_run(user_sub)
            payload = response_for_run(latest, profile=profile_row, stale=True) if latest else {"items": []}
            payload["status"] = "market_closed"
            return payload

        profile = normalize_profile(profile_row)
        watchlist = self.data_source.watchlist_symbols(user_sub)
        positions = self.data_source.portfolio_positions(user_sub)
        market_items = self.data_source.market_items()
        symbols = candidate_symbols(watchlist, positions, market_items)
        candles_by_symbol = {symbol: self.data_source.candles(symbol, now) for symbol in symbols}
        spy_candles = self.data_source.candles("SPY", now)
        news_by_symbol = {symbol: self.data_source.news(symbol) for symbol in symbols}
        previous_run = self.repository.latest_run(user_sub)
        items = score_recommendations(
            RecommendationInput(
                profile=profile,
                watchlist_symbols=watchlist,
                portfolio_positions=positions,
                market_items=market_items,
                candles_by_symbol=candles_by_symbol,
                spy_candles=spy_candles,
                active_symbol=active_symbol,
                news_by_symbol=news_by_symbol,
                now=now,
            )
        )
        run = self.repository.create_or_replace_run(
            RecommendationRunCreate(
                user_sub=user_sub,
                run_key=run_key,
                slot_start=slot["slotStart"],
                market_date=slot["marketDate"],
                status="completed" if items else "empty",
                profile_snapshot=profile_row,
                market_snapshot_time=now.isoformat(),
                summary={
                    "candidateCount": len(symbols),
                    "recommendedCount": len(items),
                    "regularMarketOnly": True,
                    "excludedHeldCount": len({str(item.get("symbol") or "").strip().upper() for item in positions if str(item.get("symbol") or "").strip()}),
                    "excludedWatchlistCount": len({symbol.strip().upper() for symbol in watchlist if symbol.strip()}),
                    "excludedActiveSymbol": bool(active_symbol),
                },
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
        event_id = f"{user_sub}:{run.get('slot_start')}:{top.get('symbol')}:stock_buy"
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


def response_for_run(run: dict[str, Any] | None, *, profile: dict[str, Any] | None = None, stale: bool = False, idempotent_replay: bool = False) -> dict[str, Any]:
    if run is None:
        return {"status": "empty", "items": [], "profile": profile}
    return {
        "status": run.get("status") or "empty",
        "runId": run.get("id"),
        "runKey": run.get("run_key"),
        "slotStart": run.get("slot_start"),
        "marketDate": run.get("market_date"),
        "generatedAt": run.get("generated_at"),
        "summary": run.get("summary") or {},
        "items": normalize_items(run.get("items") or []),
        "profile": profile,
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
            "sector": item.get("sector"),
            "reasons": item.get("reasons") or [],
            "riskWarnings": item.get("riskWarnings") or item.get("risk_warnings") or [],
            "metricsSnapshot": item.get("metricsSnapshot") or item.get("metrics_snapshot") or {},
        })
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
