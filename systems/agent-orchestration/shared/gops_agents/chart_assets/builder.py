from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from alfaka.analytics.analysis_candles import canonicalize_candle_identity
from alfaka.analytics.analysis_repair import AnalysisCandleRepairService
from alfaka.analytics.geometry import ALGORITHM_VERSION, MINIMUM_BARS, TARGET_BARS, analyze_geometry

from .candles import ChartAssetCandleLoader
from .commentary import (
    COMMENTARY_PROMPT_VERSION,
    ChartCommentaryGenerationError,
    ClickHouseChartCommentaryContextLoader,
    build_chart_commentary_fact_pack,
    build_chart_commentary_writer_from_env,
    commentary_output_metrics,
    commentary_required_from_env,
    generate_chart_commentary,
)
from .envelope import ChartAssetBuildEnvelope, utc_now_iso
from .progress import build_progress_store_from_env
from .simulation_demo import (
    is_complete_nvda_simulation_demo_snapshot,
    is_nvda_simulation_demo_target,
    project_nvda_simulation_demo_snapshot,
)
from .storage import MAX_ASSET_BYTES, build_chart_asset_storage_from_env


ASSET_VERSION = "geometry"


def _parse_timestamp(value: str | None) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("simulation snapshot cutoff is required")
    parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    if parsed.tzinfo is None:
        raise ValueError("simulation snapshot cutoff must include timezone")
    return parsed.astimezone(timezone.utc)


class ChartAssetBuilder:
    def __init__(
        self,
        *,
        candle_loader=None,
        storage=None,
        progress=None,
        repair_service=None,
        concurrency=None,
        commentary_writer=None,
        commentary_context_loader=None,
        commentary_required=None,
    ):
        supplied_loader = candle_loader is not None
        self.candle_loader = candle_loader or ChartAssetCandleLoader()
        self.storage = storage or build_chart_asset_storage_from_env()
        self.progress = progress or build_progress_store_from_env()
        self.repair_service = repair_service if repair_service is not None else (
            None if supplied_loader else AnalysisCandleRepairService(
                provider=getattr(self.candle_loader, "repair_provider", self.candle_loader.provider),
            )
        )
        self.concurrency = max(1, int(concurrency or os.getenv("CHART_ASSET_BUILD_CONCURRENCY", "4")))
        self.commentary_writer = commentary_writer if commentary_writer is not None else build_chart_commentary_writer_from_env()
        self.commentary_required = commentary_required_from_env() if commentary_required is None else bool(commentary_required)
        self.commentary_context_loader = commentary_context_loader
        if self.commentary_required and self.commentary_writer is None:
            raise ChartCommentaryGenerationError(
                "commentary provider is disabled in required mode",
                code="provider_config",
            )
        validate_commentary_configuration = getattr(self.commentary_writer, "validate_configuration", None)
        if self.commentary_required and callable(validate_commentary_configuration):
            validate_commentary_configuration()
        if self.commentary_writer is not None and self.commentary_context_loader is None:
            self.commentary_context_loader = ClickHouseChartCommentaryContextLoader()

    def run(self, envelope: ChartAssetBuildEnvelope) -> dict[str, Any]:
        if self.progress.get(envelope.job_id) is None:
            self.progress.initialize(envelope)
        self.progress.set_status(envelope.job_id, "running", startedAt=utc_now_iso())
        work = [(symbol, interval) for symbol in envelope.symbols for interval in envelope.intervals]
        with ThreadPoolExecutor(max_workers=min(self.concurrency, len(work))) as executor:
            futures = [executor.submit(self.run_item, envelope, symbol, interval) for symbol, interval in work]
            for future in as_completed(futures):
                future.result()
        state = self.progress.get(envelope.job_id) or {}
        if state.get("status") == "running":
            progress = state.get("progress") or {}
            status = "completed_with_errors" if progress.get("failed") else "completed_with_warnings" if progress.get("warnings") else "completed"
            return self.progress.set_status(envelope.job_id, status, finishedAt=utc_now_iso()) or {}
        return state

    def run_item(self, envelope: ChartAssetBuildEnvelope, symbol: str, interval: str) -> dict[str, Any]:
        started = time.monotonic()
        commentary_diagnostics: dict[str, Any] | None = None
        simulation_demo_projection = False
        symbol = symbol.upper()
        if self.progress.is_cancel_requested(envelope.job_id):
            item = _item(symbol, interval, "skipped", "cancel", started, reason="cancel_requested")
            self.progress.record_item(envelope.job_id, item)
            return item
        try:
            if envelope.source == "scheduled":
                item = _item(symbol, interval, "skipped", "policy", started, reason="manual_refresh_only")
                self.progress.record_item(envelope.job_id, item)
                return item
            existing = (
                self.storage.get_snapshot(envelope.dataset_id, symbol, interval, envelope.snapshot_cutoff)
                if envelope.target == "simulation"
                else self.storage.get(symbol, interval)
            )
            if existing and not envelope.force:
                item = _item(symbol, interval, "unchanged", "policy", started, reason="existing_asset_preserved")
                self.progress.record_item(envelope.job_id, item)
                return item
            repair = (
                {"reason": "simulation_snapshot_no_repair"}
                if envelope.target == "simulation"
                else self._repair(envelope, symbol, interval)
            )
            if envelope.target == "simulation":
                cutoff = _parse_timestamp(envelope.snapshot_cutoff)
                load_at = getattr(self.candle_loader, "load_symbol_at", None)
                if not callable(load_at):
                    raise RuntimeError("simulation snapshot candle loader does not support cutoff reads")
                bundle = load_at(symbol, [interval], cutoff)
            else:
                bundle = self.candle_loader.load_symbol(symbol, [interval])
            rows = list(bundle.rows[interval])
            coverage = dict(bundle.coverage[interval])
            confirmed_empty = int(repair.get("confirmed_empty_bars") or 0)
            if confirmed_empty:
                unresolved = max(0, int(coverage.get("missingBars") or 0) - confirmed_empty)
                coverage["confirmedEmptyBars"] = confirmed_empty
                coverage["missingBars"] = unresolved
                quality_flags = [
                    flag for flag in list(coverage.get("qualityFlags") or [])
                    if flag not in {"interior_gap", "stale_input", "recent_contiguous_below_minimum"}
                ]
                quality_flags.append("provider_confirmed_empty")
                coverage["qualityFlags"] = quality_flags
                if unresolved == 0 and len(rows) >= MINIMUM_BARS:
                    coverage["coverageState"] = "full" if len(rows) >= TARGET_BARS[interval] else "partial"
            if len(rows) < MINIMUM_BARS or coverage.get("coverageState") == "data_insufficient":
                reason = repair.get("reason") or "data_insufficient"
                item = _item(symbol, interval, "failed", "coverage", started, reason=reason, error=f"{len(rows)} completed candles available")
                self.progress.record_item(envelope.job_id, item)
                return item
            coverage_state = "full" if len(rows) >= TARGET_BARS[interval] and coverage.get("missingBars", 0) == 0 else "partial"
            input_digest = bundle.digests[interval]
            if (
                not envelope.force and existing
                and existing.get("assetVersion") == ASSET_VERSION
                and existing.get("algorithmVersion") == ALGORITHM_VERSION
                and existing.get("inputDigest") == input_digest
            ):
                item = _item(symbol, interval, "unchanged", "digest", started, reason="input_unchanged")
                self.progress.record_item(envelope.job_id, item)
                return item
            result = analyze_geometry(symbol, interval, rows)
            generated_at = utc_now_iso()
            geometry = {
                "drawings": result["drawings"],
                "supports": result["supports"],
                "resistances": result["resistances"],
                "patterns": result["patterns"],
                "primaryPattern": result["primaryPattern"],
                "tradePlan": result["tradePlan"],
                "primaryTriangle": result["primaryTriangle"],
                "historicalTriangle": result["historicalTriangle"],
                "evidence": result["evidence"],
            }
            for optional_field in ("trends", "primaryTrend", "drawingGroups", "analysisTrace"):
                if optional_field in result:
                    geometry[optional_field] = result[optional_field]
            asset = {
                "assetVersion": ASSET_VERSION,
                "algorithmVersion": ALGORITHM_VERSION,
                "symbol": symbol,
                "interval": interval,
                "sourceInterval": interval,
                "asOf": rows[-1]["timestamp"],
                "generatedAt": generated_at,
                "status": "ready",
                "inputDigest": input_digest,
                "coverage": {
                    "state": coverage_state,
                    "targetBars": TARGET_BARS[interval],
                    "actualBars": len(rows),
                    "contiguousBars": int(coverage.get("recentContiguousBars") or len(rows)),
                    "missingBars": int(coverage.get("missingBars") or 0),
                    "confirmedEmptyBars": int(coverage.get("confirmedEmptyBars") or 0),
                    "lastExpectedClosedAt": coverage.get("lastExpectedClosedAt"),
                    "lastActualClosedAt": coverage.get("lastActualClosedAt") or rows[-1]["timestamp"],
                    "qualityFlags": list(coverage.get("qualityFlags") or []),
                },
                "geometry": geometry,
                "indicators": result["indicators"],
            }
            simulation_demo_projection = is_nvda_simulation_demo_target(
                envelope.dataset_id,
                symbol,
                interval,
            )
            if simulation_demo_projection:
                asset = project_nvda_simulation_demo_snapshot(
                    dataset_id=str(envelope.dataset_id),
                    base_asset=asset,
                    source_asset=self.storage.get(symbol, interval),
                )
                geometry = asset["geometry"]
            commentary_latency_ms = None
            if self.commentary_writer is not None:
                commentary_started = time.monotonic()
                commentary_diagnostics = {
                    "model": str(getattr(self.commentary_writer, "model", "injected")),
                    "promptVersion": COMMENTARY_PROMPT_VERSION,
                    "contextDigest": None,
                    "newsAsOf": None,
                    "earningsAsOf": None,
                    "started": commentary_started,
                }
                if self.commentary_context_loader is None:
                    raise ChartCommentaryGenerationError("commentary context loader is not configured")
                context = self.commentary_context_loader.load(
                    symbol=symbol,
                    interval=interval,
                    candles=rows,
                    as_of=str(asset["asOf"]),
                    build_cutoff=envelope.snapshot_cutoff if envelope.target == "simulation" else generated_at,
                )
                fact_pack = build_chart_commentary_fact_pack(
                    symbol=symbol,
                    interval=interval,
                    candles=rows,
                    geometry=geometry,
                    geometry_input_digest=input_digest,
                    context=context,
                )
                commentary_diagnostics.update({
                    "contextDigest": fact_pack.get("contextDigest"),
                    "newsAsOf": max((
                        str(item.get("generatedAt")) for item in fact_pack.get("news") or []
                        if isinstance(item, dict) and item.get("generatedAt")
                    ), default=None),
                    "earningsAsOf": max((
                        str(item.get("sourceAsOf")) for item in fact_pack.get("earnings") or []
                        if isinstance(item, dict) and item.get("sourceAsOf")
                    ), default=None),
                })
                asset["commentary"], commentary_latency_ms = generate_chart_commentary(
                    fact_pack=fact_pack,
                    writer=self.commentary_writer,
                    generated_at=generated_at,
                )
            elif self.commentary_required or simulation_demo_projection:
                raise ChartCommentaryGenerationError(
                    "NVDA simulation demo snapshot requires the commentary provider"
                    if simulation_demo_projection
                    else "commentary provider is disabled in required mode",
                    code="provider_config",
                )
            if simulation_demo_projection and not is_complete_nvda_simulation_demo_snapshot(asset):
                raise ValueError(
                    "NVDA simulation demo snapshot requires matching v5 commentary and geometry identity"
                )

            _validate_writer_asset(asset, existing=existing)
            asset, encoded = _fit_asset_payload(asset)
            geometry = asset["geometry"]
            payload_bytes = len(encoded.encode("utf-8"))
            saved = (
                self.storage.save_snapshot(envelope.dataset_id, envelope.snapshot_cutoff, asset)
                if envelope.target == "simulation"
                else self.storage.save(asset)
            )
            persisted = (
                self.storage.get_snapshot(envelope.dataset_id, symbol, interval, envelope.snapshot_cutoff)
                if envelope.target == "simulation"
                else self.storage.get(symbol, interval)
            )
            write_verified = bool(saved is not False and _saved_asset_matches(asset, persisted))
            if saved is not False and not write_verified:
                raise RuntimeError("chart asset write verification failed")
            self._record_asset_log(
                envelope.job_id,
                symbol=symbol,
                interval=interval,
                algorithm_version=str(asset["algorithmVersion"]),
                payload_bytes=payload_bytes,
                trace=geometry.get("analysisTrace"),
                saved=saved is not False,
                as_of=str(asset["asOf"]),
                generated_at=str(asset["generatedAt"]),
                write_verified=write_verified,
                commentary=asset.get("commentary"),
                commentary_latency_ms=commentary_latency_ms,
                target=envelope.target,
                dataset_id=envelope.dataset_id,
                snapshot_cutoff=envelope.snapshot_cutoff,
                simulation_demo_projection=simulation_demo_projection,
            )
            status = "saved" if saved is not False else "unchanged"
            item = _item(
                symbol, interval, status, "storage", started,
                reason=None if saved is not False else "monotonic_noop",
                created_entities=len(result["drawings"]) if saved is not False else 0,
                warning="partial_coverage" if coverage_state == "partial" else None,
            )
        except ChartCommentaryGenerationError as exc:
            self._record_commentary_failure_log(
                envelope.job_id,
                symbol=symbol,
                interval=interval,
                diagnostics=commentary_diagnostics,
                error=exc,
            )
            item = _item(
                symbol,
                interval,
                "failed",
                "commentary",
                started,
                reason="commentary_generation_failed",
                error=f"[{exc.code}] {exc}",
            )
        except Exception as exc:
            item = _item(symbol, interval, "failed", "build", started, error=f"{exc.__class__.__name__}: {exc}")
        self.progress.record_item(envelope.job_id, item)
        return item

    def _record_asset_log(
        self,
        job_id: str,
        *,
        symbol: str,
        interval: str,
        algorithm_version: str,
        payload_bytes: int,
        trace: Any,
        saved: bool,
        as_of: str,
        generated_at: str,
        write_verified: bool,
        commentary: Any = None,
        commentary_latency_ms: int | None = None,
        target: str = "live",
        dataset_id: str | None = None,
        snapshot_cutoff: str | None = None,
        simulation_demo_projection: bool = False,
    ) -> None:
        trace_payload = trace if isinstance(trace, dict) else {}
        omitted = trace_payload.get("omittedCounts")
        omitted_payload = omitted if isinstance(omitted, dict) else {}
        asset_log = {
            "event": "chart_asset_saved" if saved else "chart_asset_unchanged",
            "symbol": symbol,
            "interval": interval,
            "algorithmVersion": algorithm_version,
            "asOf": as_of,
            "payloadBytes": payload_bytes,
            "traceMode": str(trace_payload.get("version") or "none"),
            "writeVerified": write_verified,
            "traceCandidates": {
                name: len(trace_payload.get(name) or [])
                for name in ("levelCandidates", "trendCandidates", "patternCandidates")
            },
            "target": target,
            **({"datasetId": dataset_id, "snapshotCutoff": snapshot_cutoff} if target == "simulation" else {}),
            **({"simulationDemoProjection": True} if simulation_demo_projection else {}),
        }
        trace_log = {
            "event": "chart_trace_summary",
            "symbol": symbol,
            "interval": interval,
            "generatedAt": generated_at,
            "traceOmitted": {
                str(name): int(count or 0)
                for name, count in sorted(omitted_payload.items(), key=lambda item: str(item[0]))
            },
        }
        commentary_log = None
        if isinstance(commentary, dict):
            source_identity = commentary.get("sourceIdentity") if isinstance(commentary.get("sourceIdentity"), dict) else {}
            commentary_log = {
                "event": "chart_commentary_saved",
                "symbol": symbol,
                "interval": interval,
                "commentary": {
                    "status": commentary.get("status"),
                    "model": commentary.get("model"),
                    "promptVersion": commentary.get("promptVersion"),
                    "contextDigest": source_identity.get("contextDigest"),
                    "newsAsOf": source_identity.get("newsAsOf"),
                    "earningsAsOf": source_identity.get("earningsAsOf"),
                    "latencyMs": commentary_latency_ms,
                    **commentary_output_metrics(commentary),
                },
            }
        try:
            for log in (trace_log, commentary_log, asset_log):
                if log is not None:
                    self.progress.add_log(
                        job_id,
                        json.dumps(log, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    )
        except Exception:
            # Asset persistence is authoritative. A bounded operational log must
            # never turn a successful conditional UPSERT into a failed build.
            return None

    def _record_commentary_failure_log(
        self,
        job_id: str,
        *,
        symbol: str,
        interval: str,
        diagnostics: dict[str, Any] | None,
        error: ChartCommentaryGenerationError,
    ) -> None:
        payload = diagnostics or {}
        started = payload.get("started")
        latency_ms = int(round((time.monotonic() - started) * 1000)) if isinstance(started, (int, float)) else None
        log = {
            "event": "chart_commentary_failed",
            "symbol": symbol,
            "interval": interval,
            "commentary": {
                "status": "failed",
                "failureCode": error.code,
                "retryable": error.retryable,
                "attempts": error.attempts,
                "model": payload.get("model"),
                "promptVersion": payload.get("promptVersion") or COMMENTARY_PROMPT_VERSION,
                "contextDigest": payload.get("contextDigest"),
                "newsAsOf": payload.get("newsAsOf"),
                "earningsAsOf": payload.get("earningsAsOf"),
                "latencyMs": latency_ms,
                **{
                    key: value
                    for key, value in error.details.items()
                    if key in {"httpStatus", "requestId", "providerType", "providerCode", "providerParam"}
                },
            },
        }
        try:
            self.progress.add_log(
                job_id,
                json.dumps(log, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
        except Exception:
            return None

    def _repair(self, envelope: ChartAssetBuildEnvelope, symbol: str, interval: str) -> dict[str, Any]:
        if self.repair_service is None:
            return {"checked": False, "reason": "repair_not_configured"}
        latest: dict[str, Any] = {}
        for attempt in range(2):
            result = self.repair_service.ensure_ready(
                symbol, [interval], request_id=f"{envelope.job_id}-{symbol}-{interval}-{attempt + 1}",
                is_cancel_requested=lambda: self.progress.is_cancel_requested(envelope.job_id),
            )
            latest = result.to_dict()
            if not result.unavailable:
                break
        self.progress.record_repair(envelope.job_id, {"symbol": symbol, "interval": interval, **latest})
        return latest


def _fit_asset_payload(asset: dict[str, Any]) -> tuple[dict[str, Any], str]:
    encoded = _canonical_asset_payload(asset)
    if len(encoded.encode("utf-8")) > MAX_ASSET_BYTES:
        raise ValueError(f"geometry asset payload exceeds {MAX_ASSET_BYTES} bytes")
    return asset, encoded


def _validate_writer_asset(asset: dict[str, Any], *, existing: dict[str, Any] | None) -> None:
    if asset.get("algorithmVersion") != ALGORITHM_VERSION:
        raise ValueError("geometry writer algorithm version mismatch")
    geometry = asset.get("geometry") if isinstance(asset.get("geometry"), dict) else {}
    trace = geometry.get("analysisTrace") if isinstance(geometry.get("analysisTrace"), dict) else {}
    completeness = trace.get("completeness") if isinstance(trace.get("completeness"), dict) else {}
    if trace.get("version") != "geometry-analysis-trace-v2" or completeness.get("complete") is not True:
        raise ValueError("geometry writer requires a complete analysisTrace v2")
    if completeness.get("detected") != completeness.get("stored"):
        raise ValueError("geometry writer analysisTrace is incomplete")

    interval = str(asset.get("interval") or "")
    as_of_identity = canonicalize_candle_identity({"timestamp": asset.get("asOf")}, interval)
    coverage = asset.get("coverage") if isinstance(asset.get("coverage"), dict) else {}
    last_actual = coverage.get("lastActualClosedAt")
    last_actual_identity = canonicalize_candle_identity({"timestamp": last_actual}, interval) if last_actual else as_of_identity
    if not as_of_identity or not last_actual_identity or as_of_identity["candleKey"] != last_actual_identity["candleKey"]:
        raise ValueError("geometry asset asOf does not match the canonical completed-candle watermark")

    if existing:
        existing_identity = canonicalize_candle_identity({"timestamp": existing.get("asOf")}, interval)
        if existing_identity and as_of_identity["candleKey"] < existing_identity["candleKey"]:
            raise ValueError("geometry asset asOf is older than the stored asset")


def _saved_asset_matches(expected: dict[str, Any], actual: dict[str, Any] | None) -> bool:
    if not isinstance(actual, dict):
        return False
    base_matches = all(actual.get(key) == expected.get(key) for key in (
        "algorithmVersion", "symbol", "interval", "asOf", "generatedAt", "inputDigest",
    )) and actual.get("geometry", {}).get("analysisTrace", {}).get("version") == "geometry-analysis-trace-v2"
    if not base_matches:
        return False
    expected_commentary = expected.get("commentary")
    if not isinstance(expected_commentary, dict):
        return True
    actual_commentary = actual.get("commentary") if isinstance(actual.get("commentary"), dict) else {}
    return (
        actual_commentary.get("version") == expected_commentary.get("version")
        and actual_commentary.get("sourceIdentity", {}).get("contextDigest")
        == expected_commentary.get("sourceIdentity", {}).get("contextDigest")
    )


def _canonical_asset_payload(asset: dict[str, Any]) -> str:
    return json.dumps(asset, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _item(
    symbol: str,
    interval: str,
    status: str,
    stage: str,
    started: float,
    *,
    error: str | None = None,
    warning: str | None = None,
    reason: str | None = None,
    created_entities: int = 0,
) -> dict[str, Any]:
    return {
        "symbol": symbol, "interval": interval, "status": status, "stage": stage,
        "error": error, "warning": warning, "reason": reason,
        "elapsedMs": int(round((time.monotonic() - started) * 1000)),
        "createdEntities": created_entities,
    }
