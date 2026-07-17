#!/usr/bin/env python3
"""Build and verify the fixed 2026-07-15 recommendation artifact.

Extraction is read-only. Historical rows are selected by event/source time at the
2026-07-14 16:00 ET cutoff; later ClickHouse insertion time is retained only as
reconstruction provenance and never used to discard an otherwise valid row.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import inspect
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo


REPOSITORY_ROOT = Path(os.getenv("REPOSITORY_ROOT") or Path(__file__).resolve().parents[3])
BACKEND_ROOT = REPOSITORY_ROOT / "systems/api-server/pods/api-server/gops-backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.recommendations.explanations import deterministic_explanation as runtime_deterministic_explanation  # noqa: E402
from app.recommendations import professional_v3 as professional_v3_module  # noqa: E402
from app.recommendations.professional import parse_datetime  # noqa: E402
from app.recommendations.professional_v2 import stable_digest  # noqa: E402
from app.recommendations.professional_v3 import (  # noqa: E402
    ALGORITHM_VERSION,
    RELIABILITY_MINIMUM,
    RULE_SET_VERSION,
    EvidenceContext,
    build_evidence_snapshot,
    process_evidence_preference_events,
    rank_evidence_candidates,
)
from app.recommendations.decision_v1 import enrich_direct_recommendations  # noqa: E402


SCENARIO_ID = "recommendation-v3-2026-07-15"
EVIDENCE_AS_OF = "2026-07-14T16:00:00-04:00"
TARGET_SESSION_DATE = "2026-07-15"
SOURCE_MODE = "historical_reconstruction"
PERSONALIZATION_MODE = "cutoff_user_context"
PREVIOUS_START_UTC = "2026-07-13 13:30:00"
PREVIOUS_END_UTC = "2026-07-13 20:00:00"
CURRENT_START_UTC = "2026-07-14 13:30:00"
CUTOFF_UTC = "2026-07-14 20:00:00"
DEFAULT_OUTPUT = BACKEND_ROOT / "app/recommendations/artifacts" / SCENARIO_ID

# The extractor may run from the currently deployed image while preparing the
# next image. Pin its daily eligibility to the same close-aware implementation
# shipped by this change so reconstruction never depends on the old runtime.
_runtime_reliability_components = professional_v3_module.evidence_reliability_components
professional_v3_module.completed_daily = lambda rows, now: completed_daily_at_cutoff(rows, now)
professional_v3_module.evidence_reliability_components = (
    lambda raw, factors, blocks: reliability_components_for_extractor(raw, factors, blocks)
)


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    extract = commands.add_parser("extract")
    extract.add_argument("--authorization-token", required=True)
    extract.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    extract.add_argument("--raw-output", type=Path)
    rebuild = commands.add_parser("rebuild")
    rebuild.add_argument("--raw-input", type=Path, required=True)
    rebuild.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    verify = commands.add_parser("verify")
    verify.add_argument("--artifact", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.command == "extract":
        authenticate(args.authorization_token)
        extract_artifact(args.output, raw_output=args.raw_output)
    elif args.command == "rebuild":
        rebuild_artifact(args.raw_input, args.output)
    else:
        verify_artifact(args.artifact)
    return 0


def authenticate(presented: str) -> None:
    expected = os.getenv("REPLAY_EXTRACTOR_TOKEN", "")
    if not expected or not hmac.compare_digest(
        hashlib.sha256(presented.encode()).digest(),
        hashlib.sha256(expected.encode()).digest(),
    ):
        raise SystemExit("authenticated extractor token is missing or invalid")


class ClickHouseReadOnly:
    def __init__(self) -> None:
        self.url = os.environ["CLICKHOUSE_HTTP_URL"].rstrip("/")
        self.database = os.getenv("CLICKHOUSE_DATABASE", "market_data")
        self.user = os.getenv("CLICKHOUSE_USER", "default")
        self.password = os.getenv("CLICKHOUSE_PASSWORD", "")

    def rows(self, select_sql: str) -> list[dict[str, Any]]:
        normalized = select_sql.strip().rstrip(";")
        if not normalized.upper().startswith(("SELECT", "WITH")):
            raise ValueError("artifact extractor accepts SELECT statements only")
        request = urllib.request.Request(
            f"{self.url}/?{urllib.parse.urlencode({'database': self.database})}",
            data=f"{normalized} FORMAT JSONEachRow".encode(),
            headers={"X-ClickHouse-User": self.user, "X-ClickHouse-Key": self.password},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            return [json.loads(line) for line in response if line.strip()]


def extract_artifact(output: Path, *, raw_output: Path | None) -> None:
    client = ClickHouseReadOnly()
    qualified_rows = client.rows(candidate_gate_query())
    candidates = [str(row["symbol"]) for row in qualified_rows]
    if len(candidates) < 15:
        raise SystemExit(f"artifact extraction rejected: only {len(candidates)} complete candidates")
    symbols = [*candidates, "SPY"]
    quoted = ",".join("'" + symbol.replace("'", "''") + "'" for symbol in symbols)
    minute = client.rows(
        "SELECT event_time AS timestamp,symbol,open,high,low,close,volume,source,inserted_at "
        "FROM chart_candles FINAL WHERE interval='1m' "
        f"AND symbol IN ({quoted}) AND ((event_time >= toDateTime64('{PREVIOUS_START_UTC}',3,'UTC') "
        f"AND event_time < toDateTime64('{PREVIOUS_END_UTC}',3,'UTC')) OR "
        f"(event_time >= toDateTime64('{CURRENT_START_UTC}',3,'UTC') "
        f"AND event_time < toDateTime64('{CUTOFF_UTC}',3,'UTC'))) ORDER BY symbol,event_time"
    )
    daily = client.rows(
        "SELECT event_time AS timestamp,symbol,open,high,low,close,volume,source,inserted_at,is_closed "
        "FROM chart_candles FINAL WHERE interval='1D' AND is_closed=1 "
        f"AND symbol IN ({quoted}) AND event_time <= toDateTime64('{CUTOFF_UTC}',3,'UTC') "
        "ORDER BY symbol,event_time"
    )
    quotes = client.rows(
        "SELECT symbol,argMax(bid_price,(event_time,inserted_at)) AS bid,"
        "argMax(ask_price,(event_time,inserted_at)) AS ask,"
        "argMax(source,(event_time,inserted_at)) AS source,"
        "argMax(event_time,(event_time,inserted_at)) AS eventTime,"
        "argMax(inserted_at,(event_time,inserted_at)) AS insertedAt "
        "FROM quote_ticks "
        f"WHERE symbol IN ({quoted}) AND event_time >= toDateTime64('{CURRENT_START_UTC}',3,'UTC') "
        f"AND event_time <= toDateTime64('{CUTOFF_UTC}',3,'UTC') GROUP BY symbol"
    )
    news = client.rows(
        "SELECT published_at AS publishedAt,received_at AS receivedAt,symbol,article_id AS articleId,"
        "headline,summary,source,inserted_at AS insertedAt FROM news_articles "
        f"WHERE symbol IN ({quoted}) AND published_at <= toDateTime64('{CUTOFF_UTC}',3,'UTC') "
        f"AND coalesce(received_at,published_at) <= toDateTime64('{CUTOFF_UTC}',3,'UTC') "
        "AND published_at >= toDateTime64('2026-07-01 00:00:00',3,'UTC') ORDER BY symbol,published_at"
    )
    metadata = load_company_metadata()
    raw = {
        "schemaVersion": "fixed-recommendation-extract.v1",
        "scenarioId": SCENARIO_ID,
        "evidenceAsOf": EVIDENCE_AS_OF,
        "sourceMode": SOURCE_MODE,
        "source": {"system": "AWS ClickHouse", "database": client.database, "canonical": "FINAL"},
        "candidateGate": qualified_rows,
        "minuteCandles": minute,
        "dailyCandles": daily,
        "quotes": quotes,
        "news": news,
    }
    payload, manifest = build_artifact(raw, metadata=metadata)
    write_artifact(output, payload, manifest)
    if raw_output is not None:
        raw_output.parent.mkdir(parents=True, exist_ok=True)
        raw_output.write_bytes(canonical_json(raw) + b"\n")
    verify_artifact(output)


def rebuild_artifact(raw_input: Path, output: Path) -> None:
    raw = json.loads(raw_input.read_text(encoding="utf-8"))
    if raw.get("scenarioId") != SCENARIO_ID or raw.get("evidenceAsOf") != EVIDENCE_AS_OF:
        raise SystemExit("raw reconstruction input does not match the fixed scenario")
    payload, manifest = build_artifact(raw, metadata=load_company_metadata())
    write_artifact(output, payload, manifest)
    verify_artifact(output)


def candidate_gate_query() -> str:
    return (
        "WITH p AS (SELECT symbol,count() AS c FROM chart_candles FINAL WHERE interval='1m' "
        f"AND event_time >= toDateTime64('{PREVIOUS_START_UTC}',3,'UTC') "
        f"AND event_time < toDateTime64('{PREVIOUS_END_UTC}',3,'UTC') GROUP BY symbol),"
        "c AS (SELECT symbol,count() AS c FROM chart_candles FINAL WHERE interval='1m' "
        f"AND event_time >= toDateTime64('{CURRENT_START_UTC}',3,'UTC') "
        f"AND event_time < toDateTime64('{CUTOFF_UTC}',3,'UTC') GROUP BY symbol),"
        "d AS (SELECT symbol,count() AS c FROM chart_candles FINAL WHERE interval='1D' AND is_closed=1 "
        f"AND event_time <= toDateTime64('{CUTOFF_UTC}',3,'UTC') GROUP BY symbol),"
        "q AS (SELECT symbol,argMax(bid_price,(event_time,inserted_at)) AS bid,"
        "argMax(ask_price,(event_time,inserted_at)) AS ask FROM quote_ticks "
        f"WHERE event_time >= toDateTime64('{CURRENT_START_UTC}',3,'UTC') "
        f"AND event_time <= toDateTime64('{CUTOFF_UTC}',3,'UTC') GROUP BY symbol) "
        "SELECT c.symbol AS symbol,p.c AS previousCount,c.c AS currentCount,d.c AS dailyCount,q.bid,q.ask "
        "FROM c INNER JOIN p USING symbol INNER JOIN d USING symbol INNER JOIN q USING symbol "
        "WHERE c.symbol!='SPY' AND p.c>=380 AND c.c>=380 AND d.c>=252 "
        "AND q.bid>0 AND q.ask>=q.bid ORDER BY c.symbol"
    )


def build_artifact(raw: dict[str, Any], *, metadata: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    cutoff = datetime.fromisoformat(EVIDENCE_AS_OF).astimezone(timezone.utc)
    minute_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    daily_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    news_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    inserted_values: list[datetime] = []
    for row in raw["minuteCandles"]:
        minute_by_symbol[str(row["symbol"])].append(normalize_candle(row))
        remember_inserted_at(inserted_values, row)
    for row in raw["dailyCandles"]:
        daily_by_symbol[str(row["symbol"])].append(normalize_candle(row))
        remember_inserted_at(inserted_values, row)
    for row in raw["news"]:
        news_by_symbol[str(row["symbol"])].append(normalize_news(row))
        remember_inserted_at(inserted_values, row)
    quote_by_symbol = {str(row["symbol"]): row for row in raw["quotes"]}
    for row in raw["quotes"]:
        remember_inserted_at(inserted_values, row)
    candidates = [str(row["symbol"]) for row in raw["candidateGate"]]
    market_items: list[dict[str, Any]] = []
    for symbol in candidates:
        current = minute_by_symbol[symbol]
        quote = quote_by_symbol.get(symbol) or {}
        bid, ask = number(quote.get("bid")), number(quote.get("ask"))
        if not current or bid <= 0 or ask < bid:
            continue
        info = metadata.get(symbol) or {}
        market_items.append({
            "symbol": symbol,
            "name": info.get("companyName") or symbol,
            "sector": info.get("sector") or "Unclassified",
            "industry": info.get("industry") or "Unclassified",
            "changePercent": round((number(current[-1]["close"]) / number(current[0]["open"]) - 1) * 100, 6),
            "quotedSpreadBps": (ask - bid) / ((ask + bid) / 2) * 10_000,
            "bid": bid,
            "ask": ask,
            "availableAt": iso_timestamp(quote.get("eventTime")),
            "priceSource": "canonical",
            "sourceClass": "canonical",
            "tradable": True,
            "active": True,
        })
    context = EvidenceContext(
        session_mode="regular",
        now=cutoff,
        market_items=market_items,
        candles_by_symbol=dict(minute_by_symbol),
        daily_candles_by_symbol=dict(daily_by_symbol),
        previous_session_candles_by_symbol={
            symbol: [row for row in rows if PREVIOUS_START_UTC <= timestamp_sql(row) < PREVIOUS_END_UTC]
            for symbol, rows in minute_by_symbol.items()
        },
        news_by_symbol=dict(news_by_symbol),
        fundamentals_by_symbol={},
        fundamental_provenance={"status": "historically_unavailable"},
    )
    built = build_evidence_snapshot(context)
    reliability_qualified = sum(
        float(row.get("evidenceReliability") or 0) >= RELIABILITY_MINIMUM
        and (row.get("rawFactors") or {}).get("quotedSpreadBps") is not None
        for row in built.candidates
    )
    if reliability_qualified < 15:
        diagnostics = sorted(
            [
                (
                str(row.get("symbol")),
                float(row.get("evidenceReliability") or 0),
                list(row.get("rejectionReasons") or []),
                row.get("reliabilityComponents") or {},
                )
            for row in built.candidates
            ],
            key=lambda row: row[1],
            reverse=True,
        )[:20]
        raise SystemExit(
            f"artifact extraction rejected: only {reliability_qualified} candidates meet reliability/spread gates; "
            f"top diagnostics={diagnostics}"
        )
    profile = SimpleNamespace(
        risk_level="balanced",
        recommendation_style="balanced",
        excluded_symbols=(),
        excluded_sectors=(),
    )
    preference, _events = process_evidence_preference_events(None, [], style="balanced", cutoff=cutoff)
    ranking_options = {}
    if "penalize_missing_portfolio" in inspect.signature(rank_evidence_candidates).parameters:
        ranking_options["penalize_missing_portfolio"] = False
    candidate_pool = [
        {**row, "evaluatedAt": EVIDENCE_AS_OF, "sourceDigests": built.source_digests}
        for row in built.candidates
    ]
    ranking = rank_evidence_candidates(
        candidate_pool,
        profile=profile,
        preference_state=preference,
        risk_state={},
        watchlist_symbols=[],
        portfolio_positions=[],
        portfolio_snapshot=None,
        position_daily_candles={},
        active_symbol=None,
        now=cutoff,
        snapshot_id=None,
        **ranking_options,
    )
    if ranking.qualified_count < 15 or len(ranking.items) < 15:
        raise SystemExit(f"artifact extraction rejected: only {ranking.qualified_count} ranked candidates")
    ranked_items = ranking.items
    if not ranking_options:
        ranked_items = [without_common_portfolio_penalty(item) for item in ranked_items]
    grounded_items = [{**item, "explanation": artifact_deterministic_explanation(item)} for item in ranked_items]
    items = enrich_direct_recommendations(
        grounded_items,
        risk_level="balanced",
        portfolio_snapshot=None,
        target_session_date=TARGET_SESSION_DATE,
        cutoff=cutoff,
    )
    evidence_pool_digest = hashlib.sha256(canonical_json(candidate_pool)).hexdigest()
    reconstructed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload: dict[str, Any] = {
        "schemaVersion": "fixed-recommendation-payload.v1",
        "status": "completed",
        "scenarioId": SCENARIO_ID,
        "evidenceAsOf": EVIDENCE_AS_OF,
        "targetSessionDate": TARGET_SESSION_DATE,
        "sourceMode": SOURCE_MODE,
        "reconstructedAt": reconstructed_at,
        "personalizationMode": PERSONALIZATION_MODE,
        "narrativeMode": "deterministic_grounded",
        "marketDate": TARGET_SESSION_DATE,
        "slotStart": EVIDENCE_AS_OF,
        "generatedAt": EVIDENCE_AS_OF,
        "algorithmVersion": ALGORITHM_VERSION,
        "evidencePoolDigest": evidence_pool_digest,
        "candidatePool": candidate_pool,
        "items": items,
        "summary": {
            "source": "fixed-replay-override",
            "sessionMode": "regular",
            "recommendationStyle": "balanced",
            "qualifiedCount": ranking.qualified_count,
            "algorithmVersion": ALGORITHM_VERSION,
            "ruleSetVersion": RULE_SET_VERSION,
            "confidenceMeaning": "evidence_reliability_not_success_probability",
        },
    }
    payload["recommendationDigest"] = recommendation_digest(payload)
    recommendation_bytes = pretty_json(payload)
    manifest = {
        "schemaVersion": "fixed-recommendation-manifest.v1",
        "scenarioId": SCENARIO_ID,
        "evidenceAsOf": EVIDENCE_AS_OF,
        "targetSessionDate": TARGET_SESSION_DATE,
        "sourceMode": SOURCE_MODE,
        "reconstructedAt": reconstructed_at,
        "personalizationMode": PERSONALIZATION_MODE,
        "narrativeMode": "deterministic_grounded",
        "algorithmVersion": ALGORITHM_VERSION,
        "ruleSetVersion": RULE_SET_VERSION,
        "recommendationDigest": payload["recommendationDigest"],
        "evidencePoolDigest": evidence_pool_digest,
        "sourceDigests": built.source_digests,
        "sourceInputDigest": built.input_digest,
        "sourceRowCounts": {
            "candidates": len(candidates),
            "minuteCandles": len(raw["minuteCandles"]),
            "dailyCandles": len(raw["dailyCandles"]),
            "quotes": len(raw["quotes"]),
            "news": len(raw["news"]),
        },
        "insertedAtRange": {
            "minimum": min(inserted_values).isoformat().replace("+00:00", "Z") if inserted_values else None,
            "maximum": max(inserted_values).isoformat().replace("+00:00", "Z") if inserted_values else None,
            "usedForEligibility": False,
        },
        "files": {
            "recommendation.json": {
                "sha256": hashlib.sha256(recommendation_bytes).hexdigest(),
                "bytes": len(recommendation_bytes),
            }
        },
    }
    return payload, manifest


def write_artifact(output: Path, payload: dict[str, Any], manifest: dict[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "recommendation.json").write_bytes(pretty_json(payload))
    (output / "manifest.json").write_bytes(pretty_json(manifest))


def verify_artifact(root: Path) -> None:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    payload_bytes = (root / "recommendation.json").read_bytes()
    payload = json.loads(payload_bytes)
    expected_file_digest = str(manifest["files"]["recommendation.json"]["sha256"])
    if hashlib.sha256(payload_bytes).hexdigest() != expected_file_digest:
        raise SystemExit("hash mismatch: recommendation.json")
    actual_digest = recommendation_digest(payload)
    if payload.get("recommendationDigest") != actual_digest or manifest.get("recommendationDigest") != actual_digest:
        raise SystemExit("recommendation digest mismatch")
    if payload.get("scenarioId") != SCENARIO_ID or len(payload.get("items") or []) != 15:
        raise SystemExit("fixed recommendation contract mismatch")
    candidates = payload.get("candidatePool") or []
    if len(candidates) < 15:
        raise SystemExit("fixed recommendation candidate pool is incomplete")
    evidence_pool_digest = hashlib.sha256(canonical_json(candidates)).hexdigest()
    if payload.get("evidencePoolDigest") != evidence_pool_digest or manifest.get("evidencePoolDigest") != evidence_pool_digest:
        raise SystemExit("evidence pool digest mismatch")
    if any(float(item.get("confidence") or 0) < RELIABILITY_MINIMUM / 100 for item in payload["items"]):
        raise SystemExit("artifact contains an item below the reliability minimum")
    for item in payload["items"]:
        primary = ((item.get("explanation") or {}).get("primary") or {})
        if primary.get("source") != "deterministic" or primary.get("status") != "ready":
            raise SystemExit("artifact contains an ungrounded primary narrative")
        if primary.get("promptVersion") != "recommendation-decision-renderer.ko.v2":
            raise SystemExit("artifact narrative renderer version mismatch")
    actions = {str(item.get("symbol")): str(item.get("action")) for item in payload["items"]}
    if {symbol for symbol, action in actions.items() if action == "buy"} != {"JPM", "AMZN"}:
        raise SystemExit("artifact direct-buy golden set mismatch")
    if {symbol for symbol, action in actions.items() if action == "conditional_buy"} != {
        "NVDA", "GOOGL", "PANW", "PLTR"
    }:
        raise SystemExit("artifact conditional-buy golden set mismatch")
    print(f"verified {SCENARIO_ID}: {actual_digest} ({len(payload['items'])} items)")


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
    return hashlib.sha256(canonical_json(core)).hexdigest()


def load_company_metadata() -> dict[str, dict[str, Any]]:
    path = REPOSITORY_ROOT / "systems/market-data/config/sp500-heatmap-seed.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(row.get("symbol") or "").upper(): row for row in data.get("items") or []}


def artifact_deterministic_explanation(item: dict[str, Any]) -> dict[str, Any]:
    module_path = os.getenv("RECOMMENDATION_EXPLANATIONS_MODULE", "").strip()
    if not module_path:
        return runtime_deterministic_explanation(item)
    spec = importlib.util.spec_from_file_location("fixed_recommendation_explanations", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("recommendation explanation module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.deterministic_explanation(item)


def without_common_portfolio_penalty(item: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(item))
    metrics = result.get("metricsSnapshot") or {}
    penalties = metrics.get("softPenalties") or {}
    penalty = number(penalties.pop("limitedPortfolioEvidence", 0))
    if penalty <= 0:
        return result
    metrics["adjustedSetupScore"] = round(number(metrics.get("adjustedSetupScore")) + penalty, 4)
    metrics["adjustedSetupContribution"] = round(number(metrics.get("adjustedSetupContribution")) + penalty, 8)
    metrics["personalScore"] = round(number(metrics.get("personalScore")) + penalty, 4)
    result["score"] = round(number(result.get("score")) + penalty, 4)
    result["riskWarnings"] = [
        warning
        for warning in result.get("riskWarnings") or []
        if "포트폴리오 근거가 없어" not in str(warning)
    ]
    return result


def normalize_candle(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": iso_timestamp(row["timestamp"]),
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "close": row["close"],
        "volume": row["volume"],
        "isClosed": row.get("is_closed") is not False,
        "sourceClass": "canonical",
        "availableAt": iso_timestamp(row["timestamp"]),
    }


def completed_daily_at_cutoff(rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    market_now = now.astimezone(ZoneInfo("America/New_York"))
    ordered = sorted(rows, key=lambda row: str(row.get("timestamp") or row.get("eventTime") or ""))
    result: list[dict[str, Any]] = []
    for row in ordered:
        observed = parse_datetime(row.get("timestamp") or row.get("eventTime"))
        observed_date = observed.date() if observed else None
        explicitly_closed = row.get("isClosed") is True or row.get("is_closed") is True
        if observed_date and observed_date > market_now.date():
            continue
        if observed_date == market_now.date() and not (
            (market_now.hour, market_now.minute) >= (16, 0) and explicitly_closed
        ):
            continue
        if row.get("isClosed") is False or row.get("is_closed") is False:
            continue
        result.append(row)
    return result


def reliability_components_for_extractor(
    raw: dict[str, Any], factors: dict[str, float], blocks: dict[str, float]
) -> dict[str, float]:
    result = _runtime_reliability_components(raw, factors, blocks)
    groups = (
        ("currentSessionRelativeStrength", "last60MinuteRelativeStrength"),
        ("clockAdjustedVolumeRatio", "abnormalDollarVolume"),
        ("confirmedBreakoutSupport", "vwapHoldQuality"),
        ("medianDollarVolume", "quotedSpreadBps"),
    )
    result["confirmation"] = round(
        sum(all(value_is_finite(raw.get(key)) for key in group) for group in groups) / len(groups) * 100.0,
        4,
    )
    return result


def value_is_finite(value: Any) -> bool:
    try:
        return float(value) == float(value) and abs(float(value)) != float("inf")
    except (TypeError, ValueError):
        return False


def normalize_news(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "publishedAt": iso_timestamp(row["publishedAt"]),
        "availableAt": iso_timestamp(row.get("receivedAt") or row["publishedAt"]),
        "articleId": row.get("articleId"),
        "headline": row.get("headline"),
        "summary": row.get("summary"),
        "source": row.get("source"),
    }


def remember_inserted_at(values: list[datetime], row: dict[str, Any]) -> None:
    value = row.get("inserted_at") or row.get("insertedAt")
    if value:
        values.append(datetime.fromisoformat(iso_timestamp(value).replace("Z", "+00:00")))


def timestamp_sql(row: dict[str, Any]) -> str:
    return datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00")).astimezone(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def iso_timestamp(value: Any) -> str:
    text = str(value).replace(" ", "T")
    return text if text.endswith("Z") or "+" in text[10:] else text + "Z"


def number(value: Any) -> float:
    return float(value or 0)


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def pretty_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False).encode() + b"\n"


if __name__ == "__main__":
    raise SystemExit(main())
