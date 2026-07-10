from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from alfaka.analytics import DISPLAY_BARS, KERNEL_VERSION, LOOKBACK_BARS, compute_feature_pack

from .candles import ChartAssetCandleLoader
from .compilers import compile_rule_layers, fallback_commentary, recommended_indicators
from .envelope import BUILD_INTERVAL_ORDER, ChartAssetBuildEnvelope, utc_now_iso
from .intent_compiler import merge_indicator_suggestions
from .llm import ChartAssetLLMService, degraded_result
from .progress import InMemoryChartAssetProgressStore, build_progress_store_from_env
from .storage import ChartAssetStorage


ASSET_VERSION = "v1"


class ChartAssetBuilder:
    def __init__(
        self,
        *,
        candle_loader: Any | None = None,
        storage: Any | None = None,
        progress: InMemoryChartAssetProgressStore | None = None,
        llm_service: Any | None = None,
        concurrency: int | None = None,
    ):
        self.candle_loader = candle_loader or ChartAssetCandleLoader()
        self.storage = storage or ChartAssetStorage()
        self.progress = progress or build_progress_store_from_env()
        self.llm_service = llm_service if llm_service is not None else ChartAssetLLMService()
        self.concurrency = max(1, concurrency or int(os.getenv("CHART_ASSET_BUILD_CONCURRENCY", "4")))

    def process_message(self, message: dict[str, Any]) -> dict[str, Any]:
        from .envelope import envelope_from_dict
        return self.run(envelope_from_dict(message))

    def run(self, envelope: ChartAssetBuildEnvelope) -> dict[str, Any]:
        if self.progress.get(envelope.job_id) is None:
            self.progress.initialize(envelope)
        self.progress.set_status(envelope.job_id, "running", startedAt=utc_now_iso())
        self.progress.add_log(envelope.job_id, f"build started: symbols={len(envelope.symbols)} intervals={','.join(envelope.intervals)}")
        errors = 0
        try:
            with ThreadPoolExecutor(max_workers=min(self.concurrency, len(envelope.symbols))) as executor:
                futures = [executor.submit(self._process_symbol, envelope, symbol) for symbol in envelope.symbols]
                for future in as_completed(futures):
                    errors += future.result()
        except Exception as exc:
            self.progress.add_log(envelope.job_id, f"job failed: {exc.__class__.__name__}: {exc}")
            state = self.progress.set_status(envelope.job_id, "failed", finishedAt=utc_now_iso())
            return state or {}

        state = self.progress.get(envelope.job_id) or {}
        if state.get("cancelRequested"):
            status = "canceled"
        elif errors or int(state.get("progress", {}).get("failed") or 0):
            status = "completed_with_errors"
        else:
            status = "completed"
        self.progress.add_log(envelope.job_id, f"build finished: status={status}")
        return self.progress.set_status(envelope.job_id, status, finishedAt=utc_now_iso()) or {}

    def _process_symbol(self, envelope: ChartAssetBuildEnvelope, symbol: str) -> int:
        errors = 0
        built_assets: dict[str, dict[str, Any]] = {}
        requested = [interval for interval in BUILD_INTERVAL_ORDER if interval in envelope.intervals]
        for position, interval in enumerate(requested):
            if self.progress.is_cancel_requested(envelope.job_id):
                for remaining in requested[position:]:
                    self.progress.record_item(envelope.job_id, _item(symbol, remaining, "skipped", "cancel", None, 0))
                break
            started = time.monotonic()
            stage = "kernel"
            try:
                existing = self.storage.get(symbol, interval)
                if envelope.skip_fresh_hours > 0 and self.storage.is_fresh(symbol, interval, envelope.skip_fresh_hours):
                    if existing:
                        built_assets[interval] = existing
                    self.progress.record_item(envelope.job_id, _item(symbol, interval, "skipped", "fresh", None, _elapsed(started)))
                    continue
                candles = self.candle_loader.load(symbol, interval)
                if len(candles) < 20:
                    raise NoDataError(f"only {len(candles)} closed candles")
                higher_assets, missing_higher = self._higher_assets(interval, built_assets, symbol)
                features = compute_feature_pack(candles, interval)
                generated_at = utc_now_iso()
                display = candles[-DISPLAY_BARS[interval]:]
                layers = compile_rule_layers(
                    symbol=symbol,
                    interval=interval,
                    features=features,
                    candles=display,
                    generated_at=generated_at,
                    higher_assets=higher_assets,
                )
                stage = "llm"
                asset = self._assemble_asset(
                    envelope=envelope,
                    symbol=symbol,
                    interval=interval,
                    candles=candles,
                    features=features,
                    layers=layers,
                    existing=existing,
                    higher_assets=higher_assets,
                    missing_higher=missing_higher,
                    generated_at=generated_at,
                )
                stage = "save"
                self.storage.save(asset)
                built_assets[interval] = asset
                if envelope.llm_enabled and asset.get("status") == "degraded":
                    errors += 1
                    failure_reason = str(
                        asset.get("layers", {}).get("agent", {}).get("meta", {}).get("failureReason")
                        or "llm_degraded"
                    )
                    self.progress.add_log(envelope.job_id, f"{symbol}:{interval} llm degraded: {failure_reason}")
                    self.progress.record_item(
                        envelope.job_id,
                        _item(symbol, interval, "failed", "llm", failure_reason, _elapsed(started)),
                    )
                else:
                    self.progress.record_item(envelope.job_id, _item(symbol, interval, "saved", stage, None, _elapsed(started)))
            except Exception as exc:
                errors += 1
                error = f"{exc.__class__.__name__}: {exc}"
                self.progress.add_log(envelope.job_id, f"{symbol}:{interval} {stage} failed: {error}")
                self.progress.record_item(envelope.job_id, _item(symbol, interval, "failed", stage, error, _elapsed(started)))
        return errors

    def _assemble_asset(
        self,
        *,
        envelope: ChartAssetBuildEnvelope,
        symbol: str,
        interval: str,
        candles: list[dict[str, Any]],
        features: dict[str, Any],
        layers: dict[str, Any],
        existing: dict[str, Any] | None,
        higher_assets: dict[str, dict[str, Any]],
        missing_higher: bool,
        generated_at: str,
    ) -> dict[str, Any]:
        display = candles[-DISPLAY_BARS[interval]:]
        if envelope.llm_enabled:
            try:
                agent_result = self.llm_service.build(
                    symbol=symbol,
                    interval=interval,
                    candles=candles,
                    features=features,
                    rule_layers=layers,
                    higher_assets=higher_assets,
                    generated_at=generated_at,
                )
            except Exception as exc:
                model = getattr(self.llm_service, "model", None)
                agent_result = degraded_result(
                    symbol=symbol,
                    interval=interval,
                    features=features,
                    candles=candles,
                    reason=f"llm_{exc.__class__.__name__}",
                    model=model if isinstance(model, str) else None,
                )
            agent_layer = agent_result["agentLayer"]
            commentary = {**agent_result["commentary"], "enrichment": None}
            prompt_version = agent_result.get("promptVersion")
            status = "degraded" if agent_layer.get("degraded") else "ready"
            llm_suggestions = agent_result.get("indicatorSuggestions") or []
        elif existing:
            agent_layer = existing.get("layers", {}).get("agent") or _empty_agent_layer()
            commentary = existing.get("commentary") or fallback_commentary(symbol, interval, features, float(candles[-1]["close"]))
            commentary["enrichment"] = None
            prompt_version = existing.get("promptVersion")
            status = existing.get("status") or "degraded"
            llm_suggestions = [
                item for item in existing.get("chartSetup", {}).get("recommended", [])
                if item.get("source") == "llm"
            ]
        else:
            agent_layer = _empty_agent_layer()
            commentary = fallback_commentary(symbol, interval, features, float(candles[-1]["close"]))
            prompt_version = None
            status = "degraded"
            llm_suggestions = []
        quality_flags: list[str] = []
        if len(candles) < DISPLAY_BARS[interval]: quality_flags.append("short_history")
        higher_context = {source: {"asOf": asset.get("asOf")} for source, asset in higher_assets.items()}
        existing_recommendations = existing.get("chartSetup", {}).get("recommended") if existing else None
        recommendations = (
            [dict(item) for item in existing_recommendations]
            if not envelope.llm_enabled and isinstance(existing_recommendations, list)
            else merge_indicator_suggestions(recommended_indicators(features), llm_suggestions)
        )
        return {
            "assetVersion": ASSET_VERSION,
            "kernelVersion": KERNEL_VERSION,
            "promptVersion": prompt_version,
            "symbol": symbol,
            "interval": interval,
            "asOf": candles[-1]["timestamp"],
            "generatedAt": generated_at,
            "status": status,
            "window": {
                "displayFrom": display[0]["timestamp"], "displayTo": display[-1]["timestamp"],
                "displayBars": DISPLAY_BARS[interval], "lookbackBars": LOOKBACK_BARS[interval],
            },
            "coverage": {
                "expectedBars": LOOKBACK_BARS[interval], "actualBars": len(candles),
                "missingBars": max(0, LOOKBACK_BARS[interval] - len(candles)), "qualityFlags": quality_flags,
            },
            "features": features,
            "layers": {"structure": layers["structure"], "trend": layers["trend"], "agent": agent_layer},
            "chartSetup": {
                "alwaysOn": ["volume-profile", "volume"],
                "recommended": recommendations,
            },
            "commentary": commentary,
            "buildContext": {"higherTf": higher_context or None, "flags": ["no_higher_tf_context"] if missing_higher else []},
        }

    def _higher_assets(
        self, interval: str, built_assets: dict[str, dict[str, Any]], symbol: str,
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        required = ("1M",) if interval == "1W" else ("1M", "1W") if interval == "1D" else ()
        assets: dict[str, dict[str, Any]] = {}
        for source in required:
            asset = built_assets.get(source) or self.storage.get(symbol, source)
            if asset:
                assets[source] = asset
        return assets, bool(required and len(assets) < len(required))


class NoDataError(RuntimeError):
    pass


def _empty_agent_layer() -> dict[str, Any]:
    return {"drawings": [], "intents": [], "rationale": "", "degraded": True, "model": None, "droppedIntents": []}


def _item(symbol: str, interval: str, status: str, stage: str, error: str | None, elapsed_ms: int) -> dict[str, Any]:
    return {"symbol": symbol, "interval": interval, "status": status, "stage": stage, "error": error, "elapsedMs": elapsed_ms}


def _elapsed(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))
