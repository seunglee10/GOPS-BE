from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from alfaka.analytics.analysis_repair import AnalysisCandleRepairService
from alfaka.analytics.geometry import ALGORITHM_VERSION, MINIMUM_BARS, TARGET_BARS, analyze_geometry

from .candles import ChartAssetCandleLoader
from .envelope import ChartAssetBuildEnvelope, utc_now_iso
from .progress import build_progress_store_from_env
from .storage import MAX_ASSET_BYTES, build_chart_asset_storage_from_env


ASSET_VERSION = "geometry"


class ChartAssetBuilder:
    def __init__(self, *, candle_loader=None, storage=None, progress=None, repair_service=None, concurrency=None):
        supplied_loader = candle_loader is not None
        self.candle_loader = candle_loader or ChartAssetCandleLoader()
        self.storage = storage or build_chart_asset_storage_from_env()
        self.progress = progress or build_progress_store_from_env()
        self.repair_service = repair_service if repair_service is not None else (
            None if supplied_loader else AnalysisCandleRepairService(provider=self.candle_loader.provider)
        )
        self.concurrency = max(1, int(concurrency or os.getenv("CHART_ASSET_BUILD_CONCURRENCY", "4")))

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
            existing = self.storage.get(symbol, interval)
            if existing and not envelope.force:
                item = _item(symbol, interval, "unchanged", "policy", started, reason="existing_asset_preserved")
                self.progress.record_item(envelope.job_id, item)
                return item
            repair = self._repair(envelope, symbol, interval)
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
            asset, encoded = _fit_asset_payload(asset)
            geometry = asset["geometry"]
            payload_bytes = len(encoded.encode("utf-8"))
            saved = self.storage.save(asset)
            self._record_asset_log(
                envelope.job_id,
                symbol=symbol,
                interval=interval,
                algorithm_version=str(asset["algorithmVersion"]),
                payload_bytes=payload_bytes,
                trace=geometry.get("analysisTrace"),
                saved=saved is not False,
            )
            status = "saved" if saved is not False else "unchanged"
            item = _item(
                symbol, interval, status, "storage", started,
                reason=None if saved is not False else "monotonic_noop",
                created_entities=len(result["drawings"]) if saved is not False else 0,
                warning="partial_coverage" if coverage_state == "partial" else None,
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
    ) -> None:
        trace_payload = trace if isinstance(trace, dict) else {}
        omitted = trace_payload.get("omittedCounts")
        omitted_payload = omitted if isinstance(omitted, dict) else {}
        log = {
            "event": "chart_asset_saved" if saved else "chart_asset_unchanged",
            "symbol": symbol,
            "interval": interval,
            "algorithmVersion": algorithm_version,
            "payloadBytes": payload_bytes,
            "traceCandidates": {
                name: len(trace_payload.get(name) or [])
                for name in ("levelCandidates", "trendCandidates", "patternCandidates")
            },
            "traceOmitted": {
                str(name): int(count or 0)
                for name, count in sorted(omitted_payload.items(), key=lambda item: str(item[0]))
            },
        }
        try:
            self.progress.add_log(
                job_id,
                json.dumps(log, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
        except Exception:
            # Asset persistence is authoritative. A bounded operational log must
            # never turn a successful conditional UPSERT into a failed build.
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
