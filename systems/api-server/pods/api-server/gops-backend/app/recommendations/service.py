from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from app.alerts.notifications import RedisNotificationBroker
from app.alerts.repository import InMemoryAlertRepository, PostgresAlertRepository
from app.core.sectors import normalize_sector_list, sector_payload_fields
from app.market_data.heatmap.service import get_heatmap_service
from app.services.alfaka_market_data import get_market_data_provider, read_watchlist_symbols

from .repository import RecommendationRunCreate, RecommendationRepository, RecommendationStateConflict
from .explanations import compose_explanations
from .professional import (
    ProfessionalContext,
    apply_professional_personalization,
    completed_daily,
    personalization_digest,
    raw_factors,
    resolve_weight_set,
)
from .professional_v2 import (
    ALGORITHM_VERSION,
    apply_continuous_personalization,
    infer_risk_state,
    normalize_fundamental_batch,
    process_preference_events,
    resolve_algorithm_version,
    stable_digest,
)
from .professional_v3 import (
    ALGORITHM_VERSION as EVIDENCE_ALGORITHM_VERSION,
    RULE_SET_VERSION as EVIDENCE_RULE_SET_VERSION,
    EvidenceContext,
    build_evidence_snapshot,
    process_evidence_preference_events,
    rank_evidence_candidates,
    rules_snapshot as evidence_rules_snapshot,
)
from .scoring import (
    MARKET_TZ,
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
            redis_candles = market_provider.redis_provider.recent_candles(symbol, "1m", 420)
            clickhouse_candles: list[dict[str, Any]] = []
            session_mode = market_session(now)
            if session_mode in {"pre", "regular"}:
                session_candles = filter_candles_for_session(redis_candles or [], session_mode, now)
                if len(session_candles) < min_candle_count(session_mode, now):
                    clickhouse_candles = market_provider.clickhouse_provider.candles(
                        symbol,
                        "1m",
                        720,
                        **session_candle_window(now, session_mode),
                    )
            elif len(redis_candles or []) < 120:
                clickhouse_candles = market_provider.clickhouse_provider.candles(symbol, "1m", 240)
            return candles_not_after(merge_candles([*(clickhouse_candles or []), *(redis_candles or [])]), now)[-420:]
        except Exception:
            return []

    def daily_candles(self, symbol: str, now: datetime) -> list[dict[str, Any]]:
        provider = getattr(self.app.state, "recommendation_daily_candles_provider", None)
        if callable(provider):
            return list(provider(symbol, now))
        try:
            market_provider = get_market_data_provider()
            rows = market_provider.clickhouse_provider.candles(symbol, "1D", 260)
            return candles_not_after(list(rows or []), now)[-260:]
        except Exception:
            return []

    def previous_session_candles(self, symbol: str, now: datetime) -> list[dict[str, Any]]:
        provider = getattr(self.app.state, "recommendation_previous_session_candles_provider", None)
        if callable(provider):
            return list(provider(symbol, now))
        try:
            market_provider = get_market_data_provider()
            start, end = previous_regular_session_window(now)
            return list(market_provider.clickhouse_provider.candles(
                symbol,
                "1m",
                420,
                from_time=iso_z(start),
                to_time=iso_z(end),
            ) or [])
        except Exception:
            return []

    def benchmark_health(
        self,
        *,
        now: datetime,
        session_mode: str,
        merged_candles: list[dict[str, Any]],
        daily_candles: list[dict[str, Any]],
        previous_session_candles: list[dict[str, Any]],
    ) -> dict[str, Any]:
        provider = getattr(self.app.state, "recommendation_benchmark_health_provider", None)
        if callable(provider):
            return dict(provider(now, session_mode))
        reasons: list[str] = []
        redis_rows: list[dict[str, Any]] = []
        clickhouse_rows: list[dict[str, Any]] = []
        try:
            market_provider = get_market_data_provider()
            redis_rows = list(market_provider.redis_provider.recent_candles("SPY", "1m", 420) or [])
            clickhouse_rows = list(market_provider.clickhouse_provider.candles(
                "SPY", "1m", 420, **session_candle_window(now, session_mode)
            ) or [])
        except Exception:
            reasons.append("benchmark_storage_check_unavailable")
        current = filter_candles_for_session(merged_candles, session_mode, now)
        redis_current = filter_candles_for_session(redis_rows, session_mode, now)
        clickhouse_current = filter_candles_for_session(clickhouse_rows, session_mode, now)
        minimum = min_candle_count(session_mode, now)
        completed_dailies = completed_daily(daily_candles, now)
        if len(completed_dailies) < 252:
            reasons.append("insufficient_spy_daily_history")
        if len(previous_session_candles) < 380:
            reasons.append("insufficient_spy_previous_session_candles")
        if len(current) < minimum:
            reasons.append("insufficient_spy_session_candles")
        latest = _latest_candle_time(current)
        freshness = 120 if session_mode == "regular" else 300
        if latest is None or latest > now or (now - latest).total_seconds() > freshness:
            reasons.append("stale_spy_data")
        redis_latest = _latest_candle(redis_current)
        clickhouse_latest = _latest_candle(clickhouse_current)
        if redis_latest is None or clickhouse_latest is None:
            reasons.append("spy_storage_copy_missing")
        elif not _matching_candle(redis_latest, clickhouse_latest):
            reasons.append("spy_storage_copy_mismatch")
        return {
            "symbol": "SPY",
            "ready": not reasons,
            "reasons": sorted(set(reasons)),
            "dailyCandleCount": len(completed_dailies),
            "previousSessionCandleCount": len(previous_session_candles),
            "currentSessionCandleCount": len(current),
            "latestAt": latest.isoformat() if latest else None,
            "redisLatestAt": _candle_time_text(redis_latest),
            "clickHouseLatestAt": _candle_time_text(clickhouse_latest),
        }

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
        _state_retry: bool = False,
    ) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        session_mode = normalize_session_mode(session_mode)
        profile_row = self.repository.get_profile(user_sub)
        if profile_row is None:
            return {"status": "profile_required", "items": []}
        actual_session = market_session(now)
        slot = recommendation_slot(now, session_mode=session_mode)
        run_key = f"{user_sub}:{slot['marketDate']}:{session_mode}:{slot['slotStart']}"
        legacy_enabled = bool_env("RECOMMENDATION_PERSONALIZATION_ENABLED", default=False)
        legacy_shadow = bool_env("RECOMMENDATION_PERSONALIZATION_SHADOW", default=True)
        algorithm_mode, personalization_shadow = resolve_algorithm_version(
            os.getenv("RECOMMENDATION_ALGORITHM_VERSION"),
            enabled=legacy_enabled,
            shadow=legacy_shadow,
        )
        personalization_enabled = algorithm_mode != "legacy"
        continuous_v2 = algorithm_mode == "continuous-v2"
        deterministic_v3 = algorithm_mode == EVIDENCE_ALGORITHM_VERSION
        weight_payload = professional_weight_payload(self.app) if personalization_enabled and not deterministic_v3 else None
        if personalization_enabled and not deterministic_v3 and weight_payload is None:
            get_active_weight_set = getattr(self.repository, "get_active_weight_set", None)
            weight_payload = get_active_weight_set() if callable(get_active_weight_set) else None
        weight_set = resolve_weight_set(weight_payload)
        get_snapshot_at = getattr(self.repository, "get_portfolio_snapshot_at", None)
        portfolio_snapshot = get_snapshot_at(user_sub, now) if personalization_enabled and callable(get_snapshot_at) else None
        if personalization_enabled and portfolio_snapshot is None:
            portfolio_snapshot = self.repository.get_portfolio_snapshot(user_sub)
            observed_at = snapshot_observed_at(portfolio_snapshot) if portfolio_snapshot else None
            if observed_at and observed_at > now:
                portfolio_snapshot = None
        portfolio_observed_at = snapshot_observed_at(portfolio_snapshot) if portfolio_snapshot else None
        portfolio_data_stale = bool(
            portfolio_snapshot
            and (portfolio_observed_at is None or now - portfolio_observed_at > timedelta(hours=24))
        )
        if deterministic_v3:
            input_digest = stable_digest({
                "algorithmVersion": EVIDENCE_ALGORITHM_VERSION,
                "rules": evidence_rules_snapshot(),
                "profile": profile_row,
                "portfolioSnapshot": portfolio_snapshot,
            })
        else:
            input_digest = personalization_digest(
                profile=profile_row,
                portfolio_snapshot=portfolio_snapshot,
                shadow=personalization_shadow,
                weights_version=weight_set.version,
                style_weights=weight_set.styles,
            ) if personalization_enabled else None
        existing = self.repository.get_run_by_key(user_sub, run_key)
        existing_summary = existing.get("summary") if isinstance(existing, dict) else {}
        digest_matches = not personalization_enabled or existing.get("personalization_input_digest") == input_digest if existing else False
        if existing and (continuous_v2 or deterministic_v3 or digest_matches) and not (isinstance(existing_summary, dict) and existing_summary.get("retryable")):
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
        if deterministic_v3:
            return self._refresh_deterministic_evidence_v3(
                user_sub=user_sub,
                now=now,
                active_symbol=active_symbol,
                session_mode=session_mode,
                actual_session=actual_session,
                slot=slot,
                run_key=run_key,
                profile_row=profile_row,
                profile=profile,
                portfolio_snapshot=portfolio_snapshot,
                portfolio_data_stale=portfolio_data_stale,
                input_digest=input_digest,
                watchlist=watchlist,
                positions=positions,
                market_items=market_items,
                state_retry=_state_retry,
            )
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
        scoring_input = RecommendationInput(
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
        professional_eligible_count = 0
        candidate_features: list[dict[str, Any]] = []
        fundamental_provenance: dict[str, Any] = {}
        preference_state: dict[str, Any] | None = None
        preference_events: list[dict[str, Any]] = []
        risk_state: dict[str, Any] | None = None
        v2_context: dict[str, Any] = {}
        if personalization_enabled:
            professional_symbols = list(dict.fromkeys([*symbols, *[str(row.get("symbol") or "").upper() for row in positions], "SPY"]))
            daily_candles_by_symbol = {symbol: self.data_source.daily_candles(symbol, now) for symbol in professional_symbols if symbol}
            previous_session_candles_by_symbol = {
                symbol: self.data_source.previous_session_candles(symbol, now)
                for symbol in [*symbols, "SPY"]
            }
            candidate_items = score_recommendations(scoring_input, limit=50) if personalization_shadow and not continuous_v2 else []
            if not candidate_items:
                candidate_items = professional_candidate_items(candidates, profile, active_symbol=active_symbol)
            professional_context = ProfessionalContext(
                style=profile.recommendation_style,
                risk_level=profile.risk_level,
                daily_candles_by_symbol=daily_candles_by_symbol,
                previous_session_candles_by_symbol=previous_session_candles_by_symbol,
                news_by_symbol=news_by_symbol,
                portfolio_snapshot=portfolio_snapshot,
                now=now,
                weights_version=weight_set.version,
                style_weights=weight_set.styles,
            )
            professional_eligible_count = sum(
                raw_factors(candidate.symbol, professional_context) is not None
                for candidate in candidates
            )
            if continuous_v2:
                v2_context = self.repository.get_v2_context(user_sub, now)
                preference_state, preference_events = process_preference_events(
                    v2_context.get("preferenceState"),
                    v2_context.get("fills") or [],
                    style=profile.recommendation_style,
                    cutoff=now,
                    existing_order_strengths=v2_context.get("orderStrengths") or {},
                )
                risk_state = infer_risk_state(
                    profile_row,
                    v2_context.get("portfolioSnapshots") or [],
                    v2_context.get("allFills") or [],
                    cutoff=now,
                )
                provider = getattr(self.app.state, "recommendation_fundamental_provider", None) if self.app else None
                if provider is None:
                    fundamental_payload = None
                else:
                    try:
                        fundamental_payload = provider.snapshots_as_of(symbols, now)
                    except Exception:
                        fundamental_payload = object()
                continuous = apply_continuous_personalization(
                    candidate_items,
                    context=professional_context,
                    preference_state=preference_state,
                    risk_state=risk_state,
                    fundamental_payload=fundamental_payload,
                )
                items = continuous.items
                candidate_features = continuous.candidate_features
                fundamental_provenance = continuous.fundamental_provenance
            else:
                items = apply_professional_personalization(
                    candidate_items,
                    context=professional_context,
                    shadow=personalization_shadow,
                )
        else:
            items = score_recommendations(scoring_input)
        summary = {
            **base_summary(session_mode=session_mode, actual_session=actual_session),
            "candidateCount": len(candidates),
            "recommendedCount": len(items),
            "marketItemCount": len(market_items),
            "excludedHeldCount": len({str(item.get("symbol") or "").strip().upper() for item in positions if str(item.get("symbol") or "").strip()}),
            "excludedWatchlistCount": len({symbol.strip().upper() for symbol in watchlist if symbol.strip()}),
            "excludedActiveSymbol": bool(active_symbol),
            "emptyReason": None,
            "rejectedByReason": (
                {"missingProfessionalData": max(0, len(candidates) - professional_eligible_count)}
                if personalization_enabled
                else rejection_summary(candidates, candles_by_symbol, session_mode=session_mode, now=now)
            ),
            "personalization": {
                "enabled": personalization_enabled,
                "shadow": personalization_shadow if personalization_enabled else False,
                "algorithmVersion": ALGORITHM_VERSION if continuous_v2 else weight_set.version if personalization_enabled else "legacy",
                "recommendationStyle": profile.recommendation_style,
                "weightsVersion": weight_set.version if personalization_enabled else "legacy",
                "portfolioDataStale": personalization_enabled and portfolio_data_stale,
                "preferenceConfidence": preference_state.get("preferenceConfidence") if preference_state else None,
                "fundamentalStatus": fundamental_provenance.get("status") if continuous_v2 else None,
            },
        }
        if not items:
            if personalization_enabled:
                empty_reason = "insufficient_professional_data" if professional_eligible_count == 0 else "no_positive_excess_candidates"
            else:
                empty_reason = empty_reason_for(candidates, candles_by_symbol, session_mode=session_mode, now=now)
            summary["emptyReason"] = empty_reason
            summary["retryable"] = empty_reason in {"insufficient_session_data", "insufficient_professional_data"}
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
        run_create = RecommendationRunCreate(
            user_sub=user_sub,
            run_key=run_key,
            slot_start=slot["slotStart"],
            market_date=slot["marketDate"],
            status="completed" if items else "empty",
            profile_snapshot=profile_row,
            market_snapshot_time=now.isoformat(),
            summary=summary,
            portfolio_snapshot_history_id=int(portfolio_snapshot["id"]) if portfolio_snapshot and portfolio_snapshot.get("id") is not None else None,
            weights_version=ALGORITHM_VERSION if continuous_v2 else weight_set.version if personalization_enabled else "legacy",
            personalization_input_digest=input_digest,
            personalization_snapshot={
                "recommendationStyle": profile.recommendation_style,
                "riskLevel": profile.risk_level,
                "shadow": personalization_shadow,
                "effectiveWeights": preference_state.get("effectiveWeights") if preference_state else None,
            } if personalization_enabled else {},
            algorithm_version=ALGORITHM_VERSION if continuous_v2 else weight_set.version if personalization_enabled else "legacy",
            fundamental_snapshot_provenance=fundamental_provenance,
            v2_input_digest=stable_digest({
                "preference": preference_state.get("inputDigest") if preference_state else None,
                "risk": risk_state.get("inputDigest") if risk_state else None,
                "fundamental": fundamental_provenance,
                "features": [row.get("input_digest") for row in candidate_features],
            }) if continuous_v2 else None,
        )
        if continuous_v2 and preference_state is not None and risk_state is not None:
            try:
                run = self.repository.commit_v2_run(
                    run_create,
                    items,
                    candidate_features,
                    preference_state,
                    preference_events,
                    risk_state,
                    expected_preference_state_id=v2_context.get("preferenceStateId"),
                )
            except RecommendationStateConflict:
                if _state_retry:
                    raise
                return self.refresh(
                    user_sub,
                    now=now,
                    active_symbol=active_symbol,
                    session_mode=session_mode,
                    _state_retry=True,
                )
        else:
            run = self.repository.create_or_replace_run(run_create, items)
        self._maybe_notify(user_sub, run, previous=previous_run)
        return response_for_run(run, profile=profile_row)

    def _refresh_deterministic_evidence_v3(
        self,
        *,
        user_sub: str,
        now: datetime,
        active_symbol: str | None,
        session_mode: str,
        actual_session: str,
        slot: dict[str, str],
        run_key: str,
        profile_row: dict[str, Any],
        profile: Any,
        portfolio_snapshot: dict[str, Any] | None,
        portfolio_data_stale: bool,
        input_digest: str | None,
        watchlist: list[str],
        positions: list[dict[str, Any]],
        market_items: list[dict[str, Any]],
        state_retry: bool,
    ) -> dict[str, Any]:
        symbols = sorted({
            str(item.get("symbol") or "").strip().upper()
            for item in market_items
            if str(item.get("symbol") or "").strip().upper() not in {"", "SPY"}
        })
        snapshot_key = (
            f"{slot['marketDate']}:{session_mode}:{slot['slotStart']}:"
            f"{EVIDENCE_RULE_SET_VERSION}"
        )
        snapshot = self.repository.get_evidence_snapshot(snapshot_key)
        if snapshot is None:
            source_symbols = [*symbols, "SPY"]
            candles_by_symbol = {
                symbol: self.data_source.candles(symbol, now) for symbol in source_symbols
            }
            daily_by_symbol = {
                symbol: self.data_source.daily_candles(symbol, now) for symbol in source_symbols
            }
            previous_by_symbol = {
                symbol: self.data_source.previous_session_candles(symbol, now)
                for symbol in source_symbols
            }
            benchmark_health_method = getattr(self.data_source, "benchmark_health", None)
            benchmark_health = (
                benchmark_health_method(
                    now=now,
                    session_mode=session_mode,
                    merged_candles=candles_by_symbol.get("SPY") or [],
                    daily_candles=daily_by_symbol.get("SPY") or [],
                    previous_session_candles=previous_by_symbol.get("SPY") or [],
                )
                if callable(benchmark_health_method)
                else injected_benchmark_health(
                    now=now,
                    session_mode=session_mode,
                    merged_candles=candles_by_symbol.get("SPY") or [],
                    daily_candles=daily_by_symbol.get("SPY") or [],
                    previous_session_candles=previous_by_symbol.get("SPY") or [],
                )
            )
            if not benchmark_health.get("ready"):
                return data_not_ready_response(
                    profile=profile_row,
                    session_mode=session_mode,
                    actual_session=actual_session,
                    reason="benchmark_data_not_ready",
                    benchmark_health=benchmark_health,
                    qualified_count=0,
                )
            news_by_symbol = self.data_source.news_for_symbols(symbols, now)
            provider = getattr(self.app.state, "recommendation_fundamental_provider", None) if self.app else None
            if provider is None:
                fundamental_payload = None
            else:
                try:
                    fundamental_payload = provider.snapshots_as_of(symbols, now)
                except Exception:
                    fundamental_payload = object()
            normalized_fundamentals, fundamental_provenance = normalize_fundamental_batch(
                fundamental_payload, symbols, now
            )
            fundamentals = {
                symbol: dict(row.get("scores") or {})
                for symbol, row in normalized_fundamentals.items()
                if row.get("status") == "ready"
            }
            built = build_evidence_snapshot(EvidenceContext(
                session_mode=session_mode,
                now=now,
                market_items=market_items,
                candles_by_symbol=candles_by_symbol,
                daily_candles_by_symbol=daily_by_symbol,
                previous_session_candles_by_symbol=previous_by_symbol,
                news_by_symbol=news_by_symbol,
                fundamentals_by_symbol=fundamentals,
                fundamental_provenance=fundamental_provenance,
            ))
            reliability_qualified = sum(
                float(row.get("evidenceReliability") or 0) >= 70
                and (row.get("rawFactors") or {}).get("quotedSpreadBps") is not None
                for row in built.candidates
            )
            if reliability_qualified < 15:
                return data_not_ready_response(
                    profile=profile_row,
                    session_mode=session_mode,
                    actual_session=actual_session,
                    reason="candidate_data_not_ready",
                    benchmark_health=benchmark_health,
                    qualified_count=reliability_qualified,
                    rejected_by_reason=_count_snapshot_rejections(built.rejected),
                )
            source_status = {
                "market": "ready" if market_items else "unavailable",
                "candles": "ready" if candles_by_symbol else "unavailable",
                "daily": "ready" if daily_by_symbol else "unavailable",
                "previousSession": "ready" if previous_by_symbol else "unavailable",
                "news": "ready" if any(news_by_symbol.values()) else "neutral_no_news",
                "fundamentals": fundamental_provenance.get("status", "unavailable"),
                "benchmark": benchmark_health,
            }
            snapshot = self.repository.create_evidence_snapshot({
                "snapshotKey": snapshot_key,
                "slotStart": slot["slotStart"],
                "marketDate": slot["marketDate"],
                "sessionMode": session_mode,
                "cutoff": now,
                "universe": symbols,
                "ruleSetVersion": EVIDENCE_RULE_SET_VERSION,
                "sourceDigests": built.source_digests,
                "sourceStatus": source_status,
                "status": (
                    "completed"
                    if any(not row.get("rejectionReasons") for row in built.candidates)
                    else "empty"
                ),
                "inputDigest": built.input_digest,
            }, built.candidates)
        fundamental_provenance = {
            "status": (snapshot.get("sourceStatus") or {}).get("fundamentals", "unavailable"),
            "digest": (snapshot.get("sourceDigests") or {}).get("fundamentals"),
            "cutoff": snapshot.get("cutoff"),
        }
        v2_context = self.repository.get_v2_context(user_sub, now)
        preference_state, preference_events = process_evidence_preference_events(
            v2_context.get("preferenceState"),
            v2_context.get("fills") or [],
            style=profile.recommendation_style,
            cutoff=now,
            existing_order_strengths=v2_context.get("orderStrengths") or {},
        )
        risk_state = infer_risk_state(
            profile_row,
            v2_context.get("portfolioSnapshots") or [],
            v2_context.get("allFills") or [],
            cutoff=now,
        )
        position_symbols = sorted({
            str(row.get("symbol") or "").strip().upper()
            for row in positions if str(row.get("symbol") or "").strip()
        })
        position_daily = {
            symbol: self.data_source.daily_candles(symbol, now) for symbol in position_symbols
        }
        ranking = rank_evidence_candidates(
            [
                {
                    **row,
                    "evaluatedAt": row.get("evaluatedAt") or snapshot.get("cutoff"),
                    "sourceDigests": snapshot.get("sourceDigests") or {},
                }
                for row in (snapshot.get("candidates") or [])
            ],
            profile=profile,
            preference_state=preference_state,
            risk_state=risk_state,
            watchlist_symbols=watchlist,
            portfolio_positions=positions,
            portfolio_snapshot=portfolio_snapshot,
            position_daily_candles=position_daily,
            active_symbol=active_symbol,
            now=now,
            snapshot_id=int(snapshot["id"]) if snapshot.get("id") is not None else None,
        )
        items = ranking.items
        benchmark_health = (snapshot.get("sourceStatus") or {}).get("benchmark") or {
            "symbol": "SPY",
            "ready": True,
        }
        if ranking.qualified_count < 15 or len(items) < 15:
            return data_not_ready_response(
                profile=profile_row,
                session_mode=session_mode,
                actual_session=actual_session,
                reason="candidate_data_not_ready",
                benchmark_health=benchmark_health,
                qualified_count=ranking.qualified_count,
                rejected_by_reason=ranking.rejected_by_reason,
            )
        narrative_provider = (
            getattr(self.app.state, "recommendation_narrative_provider", None)
            if self.app else None
        )
        items = compose_explanations(items, provider=narrative_provider)
        previous_run = self.repository.latest_run_for_session(user_sub, session_mode)
        summary = {
            **base_summary(session_mode=session_mode, actual_session=actual_session),
            "candidateCount": len(snapshot.get("candidates") or []),
            "universeCount": len(snapshot.get("universe") or []),
            "qualifiedCount": ranking.qualified_count,
            "recommendedCount": len(items),
            "marketItemCount": len(market_items),
            "excludedHeldCount": len(position_symbols),
            "excludedWatchlistCount": len({symbol.strip().upper() for symbol in watchlist if symbol.strip()}),
            "excludedActiveSymbol": bool(active_symbol),
            "emptyReason": None if items else "no_qualified_evidence",
            "rejectedByReason": ranking.rejected_by_reason,
            "evidenceSnapshotId": snapshot.get("id"),
            "evidenceSnapshotKey": snapshot_key,
            "evidenceCutoff": snapshot.get("cutoff"),
            "benchmarkHealth": benchmark_health,
            "ruleSetVersion": EVIDENCE_RULE_SET_VERSION,
            "personalization": {
                "enabled": True,
                "shadow": False,
                "algorithmVersion": EVIDENCE_ALGORITHM_VERSION,
                "recommendationStyle": profile.recommendation_style,
                "weightsVersion": EVIDENCE_RULE_SET_VERSION,
                "portfolioDataStale": portfolio_data_stale,
                "preferenceConfidence": preference_state.get("preferenceConfidence"),
                "fundamentalStatus": fundamental_provenance.get("status"),
                "confidenceMeaning": "evidence_reliability_not_success_probability",
            },
        }
        run_create = RecommendationRunCreate(
            user_sub=user_sub,
            run_key=run_key,
            slot_start=slot["slotStart"],
            market_date=slot["marketDate"],
            status="completed" if items else "empty",
            profile_snapshot=profile_row,
            market_snapshot_time=str(snapshot.get("cutoff") or now.isoformat()),
            summary=summary,
            portfolio_snapshot_history_id=(
                int(portfolio_snapshot["id"])
                if portfolio_snapshot and portfolio_snapshot.get("id") is not None else None
            ),
            weights_version=EVIDENCE_RULE_SET_VERSION,
            personalization_input_digest=input_digest,
            personalization_snapshot={
                **evidence_rules_snapshot(),
                "recommendationStyle": profile.recommendation_style,
                "riskLevel": profile.risk_level,
                "effectivePreferenceWeights": preference_state.get("effectiveWeights"),
                "preferenceConfidence": preference_state.get("preferenceConfidence"),
                "evidenceSnapshotInputDigest": snapshot.get("inputDigest"),
            },
            algorithm_version=EVIDENCE_ALGORITHM_VERSION,
            fundamental_snapshot_provenance=fundamental_provenance,
            v2_input_digest=stable_digest({
                "preference": preference_state.get("inputDigest"),
                "risk": risk_state.get("inputDigest"),
                "evidenceSnapshot": snapshot.get("inputDigest"),
                "features": [row.get("input_digest") for row in ranking.candidate_features],
            }),
            evidence_snapshot_id=(
                int(snapshot["id"]) if snapshot.get("id") is not None else None
            ),
        )
        try:
            run = self.repository.commit_v2_run(
                run_create,
                items,
                ranking.candidate_features,
                preference_state,
                preference_events,
                risk_state,
                expected_preference_state_id=v2_context.get("preferenceStateId"),
            )
        except RecommendationStateConflict:
            if state_retry:
                raise
            return self.refresh(
                user_sub,
                now=now,
                active_symbol=active_symbol,
                session_mode=session_mode,
                _state_retry=True,
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


def professional_candidate_items(candidates: list[Any], profile: Any, *, active_symbol: str | None = None) -> list[dict[str, Any]]:
    excluded_sectors = set(profile.excluded_sectors)
    excluded_symbols = set(profile.excluded_symbols)
    if active_symbol:
        excluded_symbols.add(str(active_symbol).strip().upper())
    return [
        {
            "symbol": candidate.symbol,
            "action": "buy",
            "rank": 0,
            "score": 0.0,
            "confidence": 0.7,
            "changePercent": candidate.change_percent,
            **sector_payload_fields(candidate.sector),
            "reasons": [],
            "riskWarnings": [],
            "metricsSnapshot": {"source": candidate.source, "changePercent": candidate.change_percent},
        }
        for candidate in candidates
        if candidate.symbol not in excluded_symbols and candidate.sector not in excluded_sectors
    ]


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


def professional_weight_payload(app: Any | None) -> dict[str, Any] | None:
    provider = getattr(app.state, "recommendation_weight_provider", None) if app else None
    if callable(provider):
        payload = provider()
        return payload if isinstance(payload, dict) else None
    raw = os.getenv("RECOMMENDATION_PROFESSIONAL_WEIGHTS_JSON", "").strip()
    if not raw:
        return None
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("RECOMMENDATION_PROFESSIONAL_WEIGHTS_JSON must be a JSON object")
    return payload


def positive_int_env(name: str, *, default: int, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    value = max(1, value)
    return min(value, maximum) if maximum else value


def session_candle_window(now: datetime, session_mode: str) -> dict[str, str]:
    local = now.astimezone(MARKET_TZ)
    if normalize_session_mode(session_mode) == "pre":
        start_local = local.replace(hour=4, minute=0, second=0, microsecond=0)
    else:
        start_local = local.replace(hour=9, minute=30, second=0, microsecond=0)
    return {
        "from_time": iso_z(start_local.astimezone(timezone.utc)),
        "to_time": iso_z(now),
    }


def previous_regular_session_window(now: datetime) -> tuple[datetime, datetime]:
    local = now.astimezone(MARKET_TZ)
    previous = local.date() - timedelta(days=1)
    while previous.weekday() >= 5:
        previous -= timedelta(days=1)
    start = datetime.combine(previous, datetime.min.time(), MARKET_TZ).replace(hour=9, minute=30)
    end = datetime.combine(previous, datetime.min.time(), MARKET_TZ).replace(hour=16)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def candles_not_after(candles: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candle in candles:
        observed = parse_datetime(candle.get("timestamp") or candle.get("eventTime") or candle.get("updatedAt"))
        if observed is None or observed <= now:
            rows.append(candle)
    return rows


def snapshot_observed_at(snapshot: dict[str, Any]) -> datetime | None:
    payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
    return parse_datetime(
        snapshot.get("source_as_of")
        or snapshot.get("sourceAsOf")
        or payload.get("sourceAsOf")
        or payload.get("asOf")
        or snapshot.get("updated_at")
    )


def iso_z(value: datetime) -> str:
    normalized = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _latest_candle(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    dated = [(observed, row) for row in rows if (observed := _candle_time(row)) is not None]
    return max(dated, key=lambda pair: pair[0])[1] if dated else None


def _latest_candle_time(rows: list[dict[str, Any]]) -> datetime | None:
    latest = _latest_candle(rows)
    return _candle_time(latest) if latest else None


def _candle_time(row: dict[str, Any] | None) -> datetime | None:
    if not row:
        return None
    return parse_datetime(row.get("timestamp") or row.get("eventTime") or row.get("updatedAt"))


def _candle_time_text(row: dict[str, Any] | None) -> str | None:
    observed = _candle_time(row)
    return observed.isoformat() if observed else None


def _matching_candle(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if _candle_time(left) != _candle_time(right):
        return False
    for key in ("open", "high", "low", "close", "volume"):
        left_value = left.get(key)
        right_value = right.get(key)
        if left_value is not None and right_value is not None:
            try:
                if abs(float(left_value) - float(right_value)) > 1e-8:
                    return False
            except (TypeError, ValueError):
                if str(left_value) != str(right_value):
                    return False
    return True


def base_summary(*, session_mode: str, actual_session: str | None = None) -> dict[str, Any]:
    return {
        "sessionMode": normalize_session_mode(session_mode),
        "actualMarketSession": actual_session,
        "newsSource": "redis_alpaca_fallback",
        "newsLookbackDays": NEWS_LOOKBACK_DAYS,
        "regularMarketOnly": False,
    }


def data_not_ready_response(
    *,
    profile: dict[str, Any],
    session_mode: str,
    actual_session: str,
    reason: str,
    benchmark_health: dict[str, Any],
    qualified_count: int,
    rejected_by_reason: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "status": "data_not_ready",
        "items": [],
        "profile": normalize_profile_sector_fields(profile),
        "stale": False,
        "idempotentReplay": False,
        "summary": {
            **base_summary(session_mode=session_mode, actual_session=actual_session),
            "emptyReason": reason,
            "retryable": True,
            "algorithmVersion": EVIDENCE_ALGORITHM_VERSION,
            "ruleSetVersion": EVIDENCE_RULE_SET_VERSION,
            "benchmarkHealth": benchmark_health,
            "qualifiedCount": qualified_count,
            "minimumQualifiedCount": 15,
            "rejectedByReason": rejected_by_reason or {},
        },
    }


def injected_benchmark_health(
    *,
    now: datetime,
    session_mode: str,
    merged_candles: list[dict[str, Any]],
    daily_candles: list[dict[str, Any]],
    previous_session_candles: list[dict[str, Any]],
) -> dict[str, Any]:
    current = filter_candles_for_session(merged_candles, session_mode, now)
    latest = _latest_candle_time(current)
    reasons = []
    completed_dailies = completed_daily(daily_candles, now)
    if len(completed_dailies) < 252:
        reasons.append("insufficient_spy_daily_history")
    if len(previous_session_candles) < 380:
        reasons.append("insufficient_spy_previous_session_candles")
    if len(current) < min_candle_count(session_mode, now):
        reasons.append("insufficient_spy_session_candles")
    freshness = 120 if session_mode == "regular" else 300
    if latest is None or latest > now or (now - latest).total_seconds() > freshness:
        reasons.append("stale_spy_data")
    return {
        "symbol": "SPY",
        "ready": not reasons,
        "reasons": reasons,
        "dailyCandleCount": len(completed_dailies),
        "previousSessionCandleCount": len(previous_session_candles),
        "currentSessionCandleCount": len(current),
        "latestAt": latest.isoformat() if latest else None,
        "storageMatch": "injected_provider",
    }


def _count_snapshot_rejections(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        for reason in row.get("rejectionReasons") or []:
            counts[str(reason)] = counts.get(str(reason), 0) + 1
    return counts


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
        metrics_snapshot = item.get("metricsSnapshot") or item.get("metrics_snapshot") or {}
        decision_json = item.get("decision_json") if isinstance(item.get("decision_json"), dict) else {}
        change_percent = item.get("changePercent")
        normalized.append({
            "symbol": item.get("symbol"),
            "action": item.get("action", "buy"),
            "rank": item.get("rank"),
            "score": item.get("score"),
            "confidence": item.get("confidence"),
            "baseAlphaScore": metrics_snapshot.get("baseAlphaScore"),
            "extendedBaseAlphaScore": metrics_snapshot.get("extendedBaseAlphaScore"),
            "styleSignalScore": metrics_snapshot.get("styleSignalScore"),
            "preferenceFitScore": metrics_snapshot.get("preferenceFitScore"),
            "preferenceConfidence": metrics_snapshot.get("preferenceConfidence"),
            "personalizationDelta": metrics_snapshot.get("personalizationDelta"),
            "portfolioFitScore": metrics_snapshot.get("portfolioFitScore"),
            "personalScore": metrics_snapshot.get("personalScore"),
            "fundamentalScore": metrics_snapshot.get("fundamentalScore"),
            "fundamentalWeight": metrics_snapshot.get("fundamentalWeight"),
            "fundamentalStatus": metrics_snapshot.get("fundamentalStatus"),
            "fundamentalProvenance": metrics_snapshot.get("fundamentalProvenance") or {},
            "algorithmVersion": metrics_snapshot.get("algorithmVersion"),
            "effectiveWeights": metrics_snapshot.get("effectiveWeights") or {},
            "riskBudget": metrics_snapshot.get("riskBudget") or {},
            "observedRisk": metrics_snapshot.get("observedRisk") or {},
            "changePercent": change_percent if change_percent is not None else metrics_snapshot.get("changePercent"),
            **sector_payload_fields(item.get("sector")),
            "reasons": item.get("reasons") or [],
            "riskWarnings": item.get("riskWarnings") or item.get("risk_warnings") or [],
            "explanation": item.get("explanation") or item.get("explanation_json"),
            "decision": item.get("decision") or decision_json.get("decision"),
            "sizing": item.get("sizing") or decision_json.get("sizing"),
            "keyEvidence": item.get("keyEvidence") or decision_json.get("keyEvidence") or [],
            "counterEvidence": item.get("counterEvidence") or decision_json.get("counterEvidence"),
            "metricsSnapshot": metrics_snapshot,
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
