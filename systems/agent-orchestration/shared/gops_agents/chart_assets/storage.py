from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Protocol

import psycopg
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


POSTGRES_TABLE = "chart_assets.geometry_assets"
ASSET_INTERVALS = ("1m", "5m", "10m", "1h", "4h", "1D", "1W")
MAX_ASSET_BYTES = 64 * 1024
GEOMETRY_ASSET_SCHEMA_PATH = (
    Path(__file__).resolve().parents[5]
    / "shared"
    / "chart-contract"
    / "chart-geometry-asset.schema.json"
)


class ChartAssetStore(Protocol):
    def save(self, asset: dict[str, Any]) -> bool: ...
    def get(self, symbol: str, interval: str) -> dict[str, Any] | None: ...
    def get_symbol_assets(self, symbol: str) -> dict[str, dict[str, Any] | None]: ...
    def coverage(self, symbols: list[str] | None = None) -> list[dict[str, Any]]: ...
    def delete(self, symbols: list[str], intervals: list[str]) -> int: ...


class PostgresChartAssetStorage:
    def __init__(self, conninfo: str | None = None, *, connect: Callable[..., Any] | None = None) -> None:
        self.conninfo = conninfo or _database_conninfo()
        self._connector = connect or psycopg.connect

    def save(self, asset: dict[str, Any]) -> bool:
        if asset.get("assetVersion") != "geometry":
            raise ValueError("PostgreSQL geometry store accepts only assetVersion=geometry")
        _validate_asset_identity(asset)
        _validate_asset_schema(asset)
        payload = _canonical_payload(asset)
        payload_bytes = len(payload.encode("utf-8"))
        if payload_bytes > MAX_ASSET_BYTES:
            raise ValueError(f"Geometry asset payload exceeds {MAX_ASSET_BYTES} bytes")
        projection = _asset_projection(asset, payload)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                INSERT INTO {POSTGRES_TABLE} (
                    symbol, "interval", as_of, generated_at, asset_version,
                    algorithm_version, status, coverage_state, drawing_count,
                    payload_bytes, input_digest, payload_digest, payload, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (symbol, "interval") DO UPDATE SET
                    as_of = EXCLUDED.as_of,
                    generated_at = EXCLUDED.generated_at,
                    asset_version = EXCLUDED.asset_version,
                    algorithm_version = EXCLUDED.algorithm_version,
                    status = EXCLUDED.status,
                    coverage_state = EXCLUDED.coverage_state,
                    drawing_count = EXCLUDED.drawing_count,
                    payload_bytes = EXCLUDED.payload_bytes,
                    input_digest = EXCLUDED.input_digest,
                    payload_digest = EXCLUDED.payload_digest,
                    payload = EXCLUDED.payload,
                    updated_at = now()
                WHERE EXCLUDED.generated_at > {POSTGRES_TABLE}.generated_at
                   OR (
                       EXCLUDED.generated_at = {POSTGRES_TABLE}.generated_at
                       AND EXCLUDED.payload_digest IS DISTINCT FROM {POSTGRES_TABLE}.payload_digest
                   )
                """,
                (
                    projection["symbol"], projection["interval"], projection["as_of"], projection["generated_at"],
                    projection["asset_version"], projection["algorithm_version"], projection["status"],
                    projection["coverage_state"], projection["drawing_count"], projection["payload_bytes"],
                    projection["input_digest"], projection["payload_digest"], Jsonb(asset),
                ),
            )
            conn.commit()
            return int(cursor.rowcount) > 0

    def get(self, symbol: str, interval: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT payload FROM {POSTGRES_TABLE} WHERE symbol = %s AND \"interval\" = %s",
                (symbol.upper(), interval),
            ).fetchone()
        return _decode_asset(row.get("payload")) if row else None

    def get_symbol_assets(self, symbol: str) -> dict[str, dict[str, Any] | None]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT \"interval\", payload FROM {POSTGRES_TABLE} WHERE symbol = %s ORDER BY \"interval\"",
                (symbol.upper(),),
            ).fetchall()
        result = {interval: None for interval in ASSET_INTERVALS}
        for row in rows:
            interval = str(row.get("interval") or "")
            if interval in result:
                result[interval] = _decode_asset(row.get("payload"))
        return result

    def coverage(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        where = ""
        parameters: tuple[Any, ...] = ()
        if symbols:
            where = "WHERE symbol = ANY(%s)"
            parameters = ([symbol.upper() for symbol in symbols],)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT symbol, "interval", generated_at, status, asset_version,
                       coverage_state, payload_bytes, drawing_count,
                       COALESCE(
                           NULLIF(payload #> '{{geometry,primaryPattern}}', 'null'::jsonb),
                           NULLIF(payload #> '{{geometry,primaryTriangle}}', 'null'::jsonb)
                       ) AS primary_pattern
                FROM {POSTGRES_TABLE} {where}
                ORDER BY symbol, "interval"
                """, parameters,
            ).fetchall()
        return [{
            "symbol": row["symbol"], "interval": row["interval"], "generatedAt": _iso(row["generated_at"]),
            "status": row["status"], "assetVersion": row["asset_version"], "coverageState": row["coverage_state"],
            "payloadBytes": int(row["payload_bytes"]), "drawingCount": int(row["drawing_count"]),
            "storedDrawingCount": int(row["drawing_count"]), "freshness": "unknown", "staleByBars": None,
            "primaryPattern": _pattern_summary(row.get("primary_pattern")),
        } for row in rows]

    def delete(self, symbols: list[str], intervals: list[str]) -> int:
        normalized_symbols = list(dict.fromkeys(symbol.upper() for symbol in symbols))
        normalized_intervals = list(dict.fromkeys(intervals))
        if not normalized_symbols or not normalized_intervals:
            return 0
        with self._connect() as conn:
            rows = conn.execute(
                f"DELETE FROM {POSTGRES_TABLE} WHERE symbol = ANY(%s) AND \"interval\" = ANY(%s) RETURNING symbol",
                (normalized_symbols, normalized_intervals),
            ).fetchall()
            conn.commit()
        return len(rows)

    def _connect(self) -> Any:
        return self._connector(self.conninfo, row_factory=dict_row)


class MaintenanceChartAssetStorage:
    def __init__(self, delegate: ChartAssetStore) -> None:
        self.delegate = delegate
    def save(self, _asset): raise RuntimeError("chart asset storage is read-only during migration maintenance")
    def get(self, symbol, interval): return self.delegate.get(symbol, interval)
    def get_symbol_assets(self, symbol): return self.delegate.get_symbol_assets(symbol)
    def coverage(self, symbols=None): return self.delegate.coverage(symbols)
    def delete(self, _symbols, _intervals): raise RuntimeError("chart asset storage is read-only during migration maintenance")


def build_chart_asset_storage_from_env() -> ChartAssetStore:
    storage: ChartAssetStore = PostgresChartAssetStorage()
    return MaintenanceChartAssetStorage(storage) if _storage_maintenance_enabled() else storage


def _storage_maintenance_enabled() -> bool:
    return os.getenv("CHART_ASSET_STORAGE_MAINTENANCE", "false").strip().lower() in {"1", "true", "yes", "on"}


def _database_conninfo() -> str:
    if os.getenv("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    required = ("DATABASE_HOST", "DATABASE_NAME", "DATABASE_USER", "DATABASE_PASSWORD")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"chart asset PostgreSQL settings missing: {','.join(missing)}")
    return make_conninfo(
        host=os.environ["DATABASE_HOST"], port=os.getenv("DATABASE_PORT", "5432"),
        dbname=os.environ["DATABASE_NAME"], user=os.environ["DATABASE_USER"], password=os.environ["DATABASE_PASSWORD"],
    )


def _asset_projection(asset: dict[str, Any], payload: str | None = None) -> dict[str, Any]:
    payload = payload or _canonical_payload(asset)
    coverage = asset.get("coverage") or {}
    geometry = asset.get("geometry") or {}
    return {
        "symbol": str(asset["symbol"]).upper(), "interval": str(asset["interval"]),
        "as_of": _timestamp(asset["asOf"]), "generated_at": _timestamp(asset["generatedAt"]),
        "asset_version": str(asset.get("assetVersion") or ""), "algorithm_version": str(asset.get("algorithmVersion") or ""),
        "status": str(asset.get("status") or ""), "coverage_state": str(coverage.get("state") or ""),
        "drawing_count": len(geometry.get("drawings") or []), "payload_bytes": len(payload.encode("utf-8")),
        "input_digest": str(asset.get("inputDigest") or ""), "payload_digest": _payload_digest(payload),
    }


def _canonical_payload(asset: dict[str, Any]) -> str:
    return json.dumps(asset, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


@lru_cache(maxsize=1)
def _asset_schema_validator() -> Draft202012Validator:
    try:
        schema = json.loads(GEOMETRY_ASSET_SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, ValueError, SchemaError) as exc:
        raise RuntimeError("Chart geometry asset JSON Schema is unavailable or invalid") from exc
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _validate_asset_schema(asset: dict[str, Any]) -> None:
    try:
        _asset_schema_validator().validate(asset)
    except ValidationError as exc:
        location = ".".join(str(part) for part in exc.absolute_path) or "$"
        rule = str(exc.validator or "contract")
        raise ValueError(f"Geometry asset schema validation failed at {location}: {rule}") from exc


def _validate_asset_identity(asset: dict[str, Any]) -> None:
    symbol = str(asset.get("symbol") or "").strip().upper()
    interval = str(asset.get("interval") or "")
    if not symbol or interval not in ASSET_INTERVALS or asset.get("sourceInterval") != interval:
        raise ValueError("Geometry asset symbol and interval identity is invalid")
    geometry = asset.get("geometry") if isinstance(asset.get("geometry"), dict) else {}
    drawings = geometry.get("drawings") or []
    if not isinstance(drawings, list) or len(drawings) > 8:
        raise ValueError("Geometry asset drawing limit exceeded")
    if any(
        not isinstance(drawing, dict)
        or str(drawing.get("symbol") or "").strip().upper() != symbol
        or drawing.get("interval") != interval
        or drawing.get("sourceInterval") != interval
        for drawing in drawings
    ):
        raise ValueError("Geometry drawing identity does not match its asset")
    _validate_optional_v6_geometry(asset, geometry, drawings)


def _validate_optional_v6_geometry(
    asset: dict[str, Any],
    geometry: dict[str, Any],
    drawings: list[Any],
) -> None:
    v6_payload = str(asset.get("algorithmVersion") or "") == "ohlcv-consensus-pattern-families-v6" or any(
        field in geometry for field in ("trends", "primaryTrend", "drawingGroups", "analysisTrace")
    )
    if v6_payload:
        _validate_v6_drawings(drawings)
    trends = geometry.get("trends")
    if trends is not None and (
        not isinstance(trends, list)
        or len(trends) > 1
        or any(not isinstance(item, dict) for item in trends)
    ):
        raise ValueError("Geometry v6 trends contract is invalid")
    primary_trend = geometry.get("primaryTrend")
    if primary_trend is not None and not isinstance(primary_trend, dict):
        raise ValueError("Geometry v6 primaryTrend contract is invalid")
    if isinstance(primary_trend, dict) and isinstance(trends, list):
        trend_ids = {str(item.get("id") or "") for item in trends}
        if str(primary_trend.get("id") or "") not in trend_ids:
            raise ValueError("Geometry v6 primaryTrend is not present in trends")
    if isinstance(trends, list):
        for trend in trends:
            _validate_v6_trend(trend, drawings)
    if isinstance(primary_trend, dict):
        _validate_v6_trend(primary_trend, drawings)

    drawing_groups = geometry.get("drawingGroups")
    if drawing_groups is not None:
        if not isinstance(drawing_groups, dict) or set(drawing_groups) != {"levels", "trend", "pattern"}:
            raise ValueError("Geometry v6 drawingGroups contract is invalid")
        grouped_ids: list[str] = []
        limits = {"levels": 4, "trend": 1, "pattern": 3}
        for name, limit in limits.items():
            values = drawing_groups.get(name)
            if (
                not isinstance(values, list)
                or len(values) > limit
                or any(not isinstance(value, str) or not value for value in values)
                or len(values) != len(set(values))
            ):
                raise ValueError(f"Geometry v6 drawingGroups.{name} contract is invalid")
            grouped_ids.extend(values)
        drawing_ids = [str(item.get("id") or "") for item in drawings if isinstance(item, dict)]
        if len(grouped_ids) != len(set(grouped_ids)) or set(grouped_ids) != set(drawing_ids):
            raise ValueError("Geometry v6 drawingGroups do not match drawings")

    trace = geometry.get("analysisTrace")
    if trace is not None:
        _validate_analysis_trace(trace, asset.get("asOf"))


def _validate_v6_drawings(drawings: list[Any]) -> None:
    drawing_ids: list[str] = []
    for drawing in drawings:
        if not isinstance(drawing, dict):
            raise ValueError("Geometry v6 drawing contract is invalid")
        drawing_id = str(drawing.get("id") or "")
        drawing_type = drawing.get("type")
        anchors = drawing.get("anchors")
        required_anchor_count = 3 if drawing_type == "trendParallelLines" else 2
        if (
            not drawing_id
            or drawing_type not in {"horizontalLine", "trendLine", "trendParallelLines"}
            or not isinstance(anchors, list)
            or len(anchors) != required_anchor_count
            or not all(_valid_anchor(anchor) for anchor in anchors)
            or not isinstance(drawing.get("style"), dict)
            or not isinstance(drawing.get("visible"), bool)
            or drawing.get("createdBy") != "system"
            or not str(drawing.get("sourceProposalId") or "")
        ):
            raise ValueError("Geometry v6 drawing contract is invalid")
        _required_timestamp(drawing.get("createdAt"), "drawing createdAt")
        _required_timestamp(drawing.get("updatedAt"), "drawing updatedAt")
        if drawing_type == "trendParallelLines" and drawing.get("parallelLineCount") != 2:
            raise ValueError("Geometry v6 trendParallelLines contract is invalid")
        drawing_ids.append(drawing_id)
    if len(drawing_ids) != len(set(drawing_ids)):
        raise ValueError("Geometry v6 drawing IDs are not unique")


def _validate_v6_trend(trend: dict[str, Any], drawings: list[Any]) -> None:
    required = {
        "id", "kind", "direction", "score", "drawingId", "anchors",
        "anchorPivotIds", "touchPivotIds", "reactionPivotIds", "touchCount",
        "reactionCount", "slopeAtrPerBar", "medianResidualAtr",
        "currentDistanceAtr", "lastTouchAgeBars",
    }
    kind = trend.get("kind")
    anchors = trend.get("anchors")
    anchor_count = 3 if kind == "channel" else 2
    pivot_fields = ("anchorPivotIds", "touchPivotIds", "reactionPivotIds")
    if (
        not required.issubset(trend)
        or not str(trend.get("id") or "")
        or kind not in {"uptrend", "downtrend", "channel"}
        or trend.get("direction") not in {"up", "down"}
        or not _bounded_number(trend.get("score"), minimum=0, maximum=1)
        or not str(trend.get("drawingId") or "")
        or not isinstance(anchors, list)
        or len(anchors) != anchor_count
        or not all(_valid_anchor(anchor) for anchor in anchors)
        or any(not _unique_string_list(trend.get(field)) for field in pivot_fields)
        or not set(trend["reactionPivotIds"]).issubset(trend["touchPivotIds"])
        or not _non_negative_integer(trend.get("touchCount"))
        or not _non_negative_integer(trend.get("reactionCount"))
        or not _finite_number(trend.get("slopeAtrPerBar"))
        or not _bounded_number(trend.get("medianResidualAtr"), minimum=0)
        or not _bounded_number(trend.get("currentDistanceAtr"), minimum=0)
        or not _non_negative_integer(trend.get("lastTouchAgeBars"))
    ):
        raise ValueError("Geometry v6 trend contract is invalid")
    drawing = next(
        (item for item in drawings if isinstance(item, dict) and item.get("id") == trend["drawingId"]),
        None,
    )
    expected_type = "trendParallelLines" if kind == "channel" else "trendLine"
    if not isinstance(drawing, dict) or drawing.get("type") != expected_type:
        raise ValueError("Geometry v6 trend drawing reference is invalid")
    if kind == "channel" and (
        not _bounded_number(trend.get("channelWidthAtr"), minimum=0)
        or not _bounded_number(trend.get("parallelSlopeError"), minimum=0)
        or not _bounded_number(trend.get("containment"), minimum=0, maximum=1)
    ):
        raise ValueError("Geometry v6 channel metrics are invalid")


def _valid_trace_candidate(candidate: dict[str, Any], expected_category: str) -> bool:
    required = {
        "id", "category", "score", "selected", "hardPass", "evidencePass",
        "activePass", "rejectReasons", "selectionTier", "importanceTier",
        "importanceRank", "anchors", "evidenceRefs", "anchorPivotIds",
        "touchPivotIds", "reactionPivotIds", "touchRefs", "reactionRefs",
        "touches", "metrics",
    }
    selection_tier = candidate.get("selectionTier")
    importance_tier = candidate.get("importanceTier")
    importance_rank = candidate.get("importanceRank")
    anchors = candidate.get("anchors")
    touches = candidate.get("touches")
    has_role_or_kind = (
        candidate.get("role") in {"support", "resistance"}
        or bool(str(candidate.get("kind") or ""))
    )
    return (
        required.issubset(candidate)
        and candidate.get("category") == expected_category
        and has_role_or_kind
        and _bounded_number(candidate.get("score"), minimum=0, maximum=1)
        and all(isinstance(candidate.get(field), bool) for field in ("selected", "hardPass", "evidencePass", "activePass"))
        and isinstance(candidate.get("rejectReasons"), list)
        and all(isinstance(value, str) and value for value in candidate["rejectReasons"])
        and selection_tier in {None, "confirmed", "contextual", "reference"}
        and importance_tier in {None, "major", "standard", "minor"}
        and (
            importance_rank is None
            or (isinstance(importance_rank, int) and not isinstance(importance_rank, bool) and importance_rank >= 1)
        )
        and isinstance(anchors, list)
        and all(_valid_anchor(anchor) for anchor in anchors)
        and isinstance(touches, list)
        and all(_valid_trace_touch(touch) for touch in touches)
        and isinstance(candidate.get("metrics"), dict)
    )


def _valid_trace_touch(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and bool(str(value.get("id") or ""))
        and _valid_anchor(value)
        and (value.get("outcome") is None or isinstance(value.get("outcome"), str))
    )


def _validate_analysis_trace(trace: Any, as_of: Any) -> None:
    if not isinstance(trace, dict) or trace.get("version") != "geometry-analysis-trace-v1":
        raise ValueError("Geometry v6 analysisTrace contract is invalid")
    pivots = trace.get("pivots")
    selections = trace.get("selections")
    omitted = trace.get("omittedCounts")
    if not isinstance(pivots, list) or not isinstance(selections, dict) or not isinstance(omitted, dict):
        raise ValueError("Geometry v6 analysisTrace collections are invalid")
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in omitted.values()):
        raise ValueError("Geometry v6 analysisTrace omittedCounts are invalid")
    pivot_ids: list[str] = []
    cutoff = _required_timestamp(as_of, "asset asOf")
    for pivot in pivots:
        if not isinstance(pivot, dict) or not str(pivot.get("id") or ""):
            raise ValueError("Geometry v6 trace pivot is invalid")
        pivot_ids.append(str(pivot["id"]))
        if _required_timestamp(pivot.get("confirmedAt"), "trace pivot confirmedAt") > cutoff:
            raise ValueError("Geometry v6 trace pivot confirms after asset asOf")
    if len(pivot_ids) != len(set(pivot_ids)):
        raise ValueError("Geometry v6 trace pivot IDs are not unique")

    group_contracts = (
        ("levelCandidates", "levelCandidateIds", "level", 8, 4),
        ("trendCandidates", "trendCandidateIds", "trend", 7, 1),
        ("patternCandidates", "patternCandidateIds", "pattern", 4, 1),
    )
    referenced_pivots: set[str] = set()
    for candidate_key, selection_key, expected_category, limit, selection_limit in group_contracts:
        candidates = trace.get(candidate_key)
        selected_ids = selections.get(selection_key)
        if (
            not isinstance(candidates, list)
            or len(candidates) > limit
            or not isinstance(selected_ids, list)
            or len(selected_ids) > selection_limit
            or any(not isinstance(value, str) or not value for value in selected_ids)
            or len(selected_ids) != len(set(selected_ids))
        ):
            raise ValueError(f"Geometry v6 analysisTrace {candidate_key} contract is invalid")
        candidate_ids: list[str] = []
        projected_selected: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, dict) or not str(candidate.get("id") or ""):
                raise ValueError(f"Geometry v6 analysisTrace {candidate_key} candidate is invalid")
            candidate_id = str(candidate["id"])
            candidate_ids.append(candidate_id)
            if not _valid_trace_candidate(candidate, expected_category):
                raise ValueError(f"Geometry v6 analysisTrace {candidate_key} candidate is invalid")
            if candidate["selected"]:
                projected_selected.add(candidate_id)
            evidence_refs = candidate.get("evidenceRefs")
            anchor_pivot_ids = candidate.get("anchorPivotIds", [])
            touch_pivot_ids = candidate.get("touchPivotIds", [])
            reaction_pivot_ids = candidate.get("reactionPivotIds", [])
            touches = candidate.get("touches")
            touch_refs = candidate.get("touchRefs")
            reaction_refs = candidate.get("reactionRefs")
            if (
                not isinstance(evidence_refs, list)
                or any(not isinstance(value, str) or not value for value in evidence_refs)
                or len(evidence_refs) != len(set(evidence_refs))
                or not isinstance(anchor_pivot_ids, list)
                or any(not isinstance(value, str) or not value for value in anchor_pivot_ids)
                or len(anchor_pivot_ids) != len(set(anchor_pivot_ids))
                or not isinstance(touch_pivot_ids, list)
                or any(not isinstance(value, str) or not value for value in touch_pivot_ids)
                or len(touch_pivot_ids) != len(set(touch_pivot_ids))
                or not isinstance(reaction_pivot_ids, list)
                or any(not isinstance(value, str) or not value for value in reaction_pivot_ids)
                or len(reaction_pivot_ids) != len(set(reaction_pivot_ids))
                or not set(reaction_pivot_ids).issubset(touch_pivot_ids)
                or not isinstance(touches, list)
                or len(touches) > 8
                or not isinstance(touch_refs, list)
                or any(not isinstance(value, str) or not value for value in touch_refs)
                or len(touch_refs) != len(set(touch_refs))
                or not isinstance(reaction_refs, list)
                or any(not isinstance(value, str) or not value for value in reaction_refs)
                or len(reaction_refs) != len(set(reaction_refs))
            ):
                raise ValueError(f"Geometry v6 analysisTrace {candidate_key} evidence is invalid")
            touch_ids = {
                str(touch.get("id") or "")
                for touch in touches
                if isinstance(touch, dict) and str(touch.get("id") or "")
            }
            if len(touch_ids) != len(touches) or not set(touch_refs).issubset(touch_ids) or not set(reaction_refs).issubset(touch_ids):
                raise ValueError(f"Geometry v6 analysisTrace {candidate_key} touch references are invalid")
            candidate_pivot_refs = {
                *evidence_refs,
                *anchor_pivot_ids,
                *touch_pivot_ids,
                *reaction_pivot_ids,
            }
            if not candidate_pivot_refs.issubset(pivot_ids):
                raise ValueError(f"Geometry v6 analysisTrace {candidate_key} pivot references are invalid")
            referenced_pivots.update(candidate_pivot_refs)
        if len(candidate_ids) != len(set(candidate_ids)) or not set(selected_ids).issubset(candidate_ids):
            raise ValueError(f"Geometry v6 analysisTrace {candidate_key} IDs are invalid")
        if projected_selected != set(selected_ids):
            raise ValueError(f"Geometry v6 analysisTrace {candidate_key} selections are inconsistent")
    if referenced_pivots != set(pivot_ids):
        raise ValueError("Geometry v6 analysisTrace pivot registry does not match evidence references")


def _payload_digest(payload: str) -> str:
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _decode_asset(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict): return dict(value)
    try: decoded = json.loads(str(value))
    except (TypeError, ValueError): return None
    return decoded if isinstance(decoded, dict) else None


def _pattern_summary(value: Any) -> dict[str, Any] | None:
    source = _decode_asset(value)
    if source is None:
        return None
    kind = str(source.get("kind") or "").strip()
    state = str(source.get("state") or "").strip()
    try:
        score = float(source.get("score"))
    except (TypeError, ValueError):
        return None
    if not kind or state not in {"forming", "confirmed", "inactive", "invalidated"}:
        return None
    return {"kind": kind, "state": state, "score": score}


def _timestamp(value: Any) -> Any:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value


def _required_timestamp(value: Any, field_name: str) -> datetime:
    try:
        parsed = _timestamp(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Geometry v6 {field_name} is invalid") from exc
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise ValueError(f"Geometry v6 {field_name} is invalid")
    return parsed


def _valid_anchor(value: Any) -> bool:
    if not isinstance(value, dict) or not str(value.get("timestamp") or ""):
        return False
    try:
        _required_timestamp(value.get("timestamp"), "anchor timestamp")
    except ValueError:
        return False
    return _bounded_number(value.get("price"), minimum=0, exclusive_minimum=True)


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and float("-inf") < float(value) < float("inf")
    )


def _bounded_number(
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    exclusive_minimum: bool = False,
) -> bool:
    if not _finite_number(value):
        return False
    number = float(value)
    if minimum is not None and (number <= minimum if exclusive_minimum else number < minimum):
        return False
    return maximum is None or number <= maximum


def _non_negative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _unique_string_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(item, str) and item for item in value)
        and len(value) == len(set(value))
    )


def _iso(value: Any) -> str:
    if isinstance(value, str): return value
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
