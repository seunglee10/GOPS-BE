from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .decision_v1 import (
    enrich_direct_recommendations,
    personalization_digest,
    response_digest,
)
from .professional_v3 import process_evidence_preference_events, rank_evidence_candidates


ENABLED_ENV = "RECOMMENDATION_FIXED_REPLAY_ENABLED"
ARTIFACT_PATH_ENV = "RECOMMENDATION_FIXED_REPLAY_PATH"
DECISION_ENABLED_ENV = "RECOMMENDATION_DECISION_V1_ENABLED"
DEFAULT_SCENARIO_ID = "recommendation-v3-2026-07-15"
DEFAULT_ARTIFACT_PATH = Path(__file__).resolve().parent / "artifacts" / DEFAULT_SCENARIO_ID


class FixedReplayProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class FixedReplayRecommendationProvider:
    artifact_path: Path
    manifest: dict[str, Any]
    payload: dict[str, Any]

    @classmethod
    def load(cls, artifact_path: Path | None = None) -> "FixedReplayRecommendationProvider":
        root = artifact_path or configured_artifact_path()
        try:
            manifest_bytes = (root / "manifest.json").read_bytes()
            recommendation_bytes = (root / "recommendation.json").read_bytes()
            manifest = json.loads(manifest_bytes)
            payload = json.loads(recommendation_bytes)
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise FixedReplayProviderError("fixed recommendation artifact is unavailable") from exc
        expected_file = ((manifest.get("files") or {}).get("recommendation.json") or {}).get("sha256")
        if not expected_file or hashlib.sha256(recommendation_bytes).hexdigest() != expected_file:
            raise FixedReplayProviderError("fixed recommendation artifact hash mismatch")
        digest = recommendation_digest(payload)
        if payload.get("recommendationDigest") != digest or manifest.get("recommendationDigest") != digest:
            raise FixedReplayProviderError("fixed recommendation digest mismatch")
        validate_contract(payload, manifest)
        return cls(root, manifest, payload)

    def response(
        self,
        *,
        profile: dict[str, Any] | None = None,
        portfolio_snapshot: dict[str, Any] | None = None,
        preference_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not decision_v1_enabled():
            result = copy.deepcopy(self.payload)
            result.pop("candidatePool", None)
            for item in result.get("items") or []:
                item.pop("decision", None)
                item.pop("sizing", None)
                item.pop("keyEvidence", None)
                item.pop("counterEvidence", None)
            return result
        return self.personalized_response(
            profile=profile,
            portfolio_snapshot=portfolio_snapshot,
            preference_state=preference_state,
        )

    def personalized_response(
        self,
        *,
        profile: dict[str, Any] | None,
        portfolio_snapshot: dict[str, Any] | None,
        preference_state: dict[str, Any] | None,
    ) -> dict[str, Any]:
        cutoff = datetime.fromisoformat(str(self.payload["evidenceAsOf"]))
        normalized_profile = _profile_snapshot(profile)
        profile_object = SimpleNamespace(
            risk_level=normalized_profile["riskLevel"],
            recommendation_style=normalized_profile["recommendationStyle"],
            excluded_symbols=tuple(normalized_profile["excludedSymbols"]),
            excluded_sectors=tuple(normalized_profile["excludedSectors"]),
        )
        positions = _portfolio_positions(portfolio_snapshot)
        state = preference_state
        if not state:
            state, _events = process_evidence_preference_events(
                None,
                [],
                style=normalized_profile["recommendationStyle"],
                cutoff=cutoff,
            )
        ranking = rank_evidence_candidates(
            copy.deepcopy(self.payload.get("candidatePool") or []),
            profile=profile_object,
            preference_state=state,
            risk_state={},
            watchlist_symbols=[],
            portfolio_positions=positions,
            portfolio_snapshot=portfolio_snapshot,
            position_daily_candles={},
            active_symbol=None,
            now=cutoff,
            snapshot_id=None,
            penalize_missing_portfolio=False,
            exclude_portfolio_hard_caps=False,
        )
        items = enrich_direct_recommendations(
            ranking.items,
            risk_level=normalized_profile["riskLevel"],
            portfolio_snapshot=portfolio_snapshot,
            target_session_date=str(self.payload["targetSessionDate"]),
            cutoff=cutoff,
        )
        context = {
            "profile": normalized_profile,
            "portfolio": _portfolio_digest_payload(portfolio_snapshot),
            "preference": state,
            "cutoff": self.payload["evidenceAsOf"],
        }
        result = copy.deepcopy(self.payload)
        result.pop("candidatePool", None)
        result["personalizationMode"] = "cutoff_user_context"
        result["profile"] = normalized_profile
        result["personalizationDigest"] = personalization_digest(context)
        result["items"] = items
        result["summary"] = {
            **(result.get("summary") or {}),
            "qualifiedCount": ranking.qualified_count,
            "actionCounts": {
                action: sum(item.get("action") == action for item in items)
                for action in ("buy", "conditional_buy", "watch", "not_suitable")
            },
        }
        result["recommendationDigest"] = response_digest(result)
        return result


def fixed_replay_enabled() -> bool:
    return os.getenv(ENABLED_ENV, "false").strip().lower() in {"1", "true", "yes", "on"}


def decision_v1_enabled() -> bool:
    return os.getenv(DECISION_ENABLED_ENV, "false").strip().lower() in {"1", "true", "yes", "on"}


def configured_artifact_path() -> Path:
    configured = os.getenv(ARTIFACT_PATH_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_ARTIFACT_PATH


def prepare_fixed_replay_provider(app: Any) -> None:
    if not fixed_replay_enabled():
        app.state.fixed_replay_recommendation_provider = None
        app.state.fixed_replay_recommendation_error = None
        return
    try:
        provider = FixedReplayRecommendationProvider.load()
    except FixedReplayProviderError as exc:
        app.state.fixed_replay_recommendation_provider = None
        app.state.fixed_replay_recommendation_error = exc
        return
    app.state.fixed_replay_recommendation_provider = provider
    app.state.fixed_replay_recommendation_error = None


def fixed_replay_provider(app: Any) -> FixedReplayRecommendationProvider | None:
    if not fixed_replay_enabled():
        return None
    provider = getattr(app.state, "fixed_replay_recommendation_provider", None)
    error = getattr(app.state, "fixed_replay_recommendation_error", None)
    if provider is None and error is None:
        prepare_fixed_replay_provider(app)
        provider = getattr(app.state, "fixed_replay_recommendation_provider", None)
        error = getattr(app.state, "fixed_replay_recommendation_error", None)
    if isinstance(error, FixedReplayProviderError):
        raise error
    if not isinstance(provider, FixedReplayRecommendationProvider):
        raise FixedReplayProviderError("fixed recommendation provider is not ready")
    return provider


def recommendation_digest(payload: dict[str, Any]) -> str:
    core = {
        "scenarioId": payload.get("scenarioId"),
        "evidenceAsOf": payload.get("evidenceAsOf"),
        "targetSessionDate": payload.get("targetSessionDate"),
        "algorithmVersion": payload.get("algorithmVersion"),
        "personalizationMode": payload.get("personalizationMode"),
        "narrativeMode": payload.get("narrativeMode"),
        "evidencePoolDigest": payload.get("evidencePoolDigest"),
        "items": payload.get("items") or [],
    }
    encoded = json.dumps(
        core,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_contract(payload: dict[str, Any], manifest: dict[str, Any]) -> None:
    expected = {
        "scenarioId": DEFAULT_SCENARIO_ID,
        "evidenceAsOf": "2026-07-14T16:00:00-04:00",
        "targetSessionDate": "2026-07-15",
        "sourceMode": "historical_reconstruction",
        "personalizationMode": "cutoff_user_context",
        "narrativeMode": "deterministic_grounded",
        "algorithmVersion": "deterministic-evidence-v3",
    }
    for key, value in expected.items():
        if payload.get(key) != value or manifest.get(key) != value:
            raise FixedReplayProviderError(f"fixed recommendation contract mismatch: {key}")
    items = payload.get("items")
    if payload.get("status") != "completed" or payload.get("marketDate") != "2026-07-15":
        raise FixedReplayProviderError("fixed recommendation response metadata mismatch")
    if not isinstance(items, list) or len(items) != 15:
        raise FixedReplayProviderError("fixed recommendation artifact must contain exactly 15 items")
    candidates = payload.get("candidatePool")
    if not isinstance(candidates, list) or len(candidates) < 15:
        raise FixedReplayProviderError("fixed recommendation artifact candidate pool is incomplete")
    evidence_pool_digest = hashlib.sha256(
        json.dumps(candidates, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    if payload.get("evidencePoolDigest") != evidence_pool_digest or manifest.get("evidencePoolDigest") != evidence_pool_digest:
        raise FixedReplayProviderError("fixed recommendation evidence pool digest mismatch")
    for rank, item in enumerate(items, start=1):
        if not isinstance(item, dict) or item.get("rank") != rank:
            raise FixedReplayProviderError("fixed recommendation ranks are invalid")
        if float(item.get("confidence") or 0) < 0.70:
            raise FixedReplayProviderError("fixed recommendation contains confidence below 70")
        primary = ((item.get("explanation") or {}).get("primary") or {})
        if primary.get("source") != "deterministic" or primary.get("status") != "ready":
            raise FixedReplayProviderError("fixed recommendation narrative must be grounded and frozen")
        if primary.get("model") is not None or primary.get("promptVersion") != "recommendation-decision-renderer.ko.v2":
            raise FixedReplayProviderError("fixed recommendation narrative provenance is invalid")
    actions = {str(item.get("symbol")): str(item.get("action")) for item in items}
    if {symbol for symbol, action in actions.items() if action == "buy"} != {"JPM", "AMZN"}:
        raise FixedReplayProviderError("fixed recommendation direct-buy golden set mismatch")
    if {symbol for symbol, action in actions.items() if action == "conditional_buy"} != {
        "NVDA", "GOOGL", "PANW", "PLTR"
    }:
        raise FixedReplayProviderError("fixed recommendation conditional-buy golden set mismatch")


def _profile_snapshot(profile: dict[str, Any] | None) -> dict[str, Any]:
    source = profile or {}
    risk = str(source.get("risk_level") or source.get("riskLevel") or "balanced").lower()
    style = str(source.get("recommendation_style") or source.get("recommendationStyle") or "balanced").lower()
    if risk not in {"conservative", "balanced", "aggressive"}:
        risk = "balanced"
    if style not in {"momentum", "balanced", "stable"}:
        style = "balanced"
    return {
        "riskLevel": risk,
        "recommendationStyle": style,
        "horizon": "intraday",
        "maxDrawdownPct": float(source.get("max_drawdown_pct") or source.get("maxDrawdownPct") or 6),
        "preferredSectors": list(source.get("preferred_sectors") or source.get("preferredSectors") or []),
        "excludedSectors": list(source.get("excluded_sectors") or source.get("excludedSectors") or []),
        "excludedSymbols": [
            str(value).upper()
            for value in source.get("excluded_symbols") or source.get("excludedSymbols") or []
        ],
        "source": "cutoff_snapshot" if profile else "balanced_default",
    }


def _portfolio_positions(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not snapshot:
        return []
    payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else snapshot
    return [row for row in payload.get("positions", []) if isinstance(row, dict)]


def _portfolio_digest_payload(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if not snapshot:
        return None
    return {
        "id": snapshot.get("id"),
        "sourceAsOf": snapshot.get("source_as_of") or snapshot.get("sourceAsOf"),
        "payload": snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else snapshot,
    }
