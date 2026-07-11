from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from alfaka.analytics import DISPLAY_BARS, KERNEL_VERSION, LOOKBACK_BARS, QUALITY_POLICY_VERSION, compute_feature_pack
from alfaka.analytics.analysis_candles import (
    ADJUSTMENT_POLICY, CANONICAL_DATA_VERSION, CANDLE_CONTRACT_VERSION, SESSION_POLICY,
    analysis_input_digest,
)
from alfaka.analytics.analysis_repair import AnalysisCandleRepairService

from .candles import ChartAssetCandleLoader
from .commentary_v2 import assemble_commentary_v2
from .compilers import compile_rule_layers, recommended_indicators
from .curation import (
    MODEL_POLICY_VERSION, PROMPT_VERSION_V2, build_interval_palette, build_symbol_bundle,
    deterministic_curation, materialize_curation,
)
from .envelope import BUILD_INTERVAL_ORDER, ChartAssetBuildEnvelope, utc_now_iso
from .llm import ChartAssetLLMService
from .progress import InMemoryChartAssetProgressStore, build_progress_store_from_env
from .storage import build_chart_asset_storage_from_env


ASSET_VERSION = "v2"
ASSEMBLER_VERSION = "chart-asset-assembler-v3"
AGENT_PRESERVATION_POLICY = "preserve_valid_same_input"
MAX_ASSET_BYTES = 20 * 1024


class ChartAssetBuilder:
    def __init__(self, *, candle_loader=None, storage=None, progress=None, llm_service=None, repair_service=None, concurrency=None):
        supplied_loader = candle_loader is not None
        self.candle_loader = candle_loader or ChartAssetCandleLoader(); self.storage = storage or build_chart_asset_storage_from_env()
        self.progress = progress or build_progress_store_from_env(); self.llm_service = llm_service if llm_service is not None else ChartAssetLLMService()
        self.repair_service = repair_service if repair_service is not None else None if supplied_loader else AnalysisCandleRepairService(provider=self.candle_loader.provider)
        self.concurrency = max(1, concurrency or int(os.getenv("CHART_ASSET_BUILD_CONCURRENCY", "4")))

    def process_message(self, message):
        from .envelope import envelope_from_dict
        return self.run(envelope_from_dict(message))

    def run(self, envelope: ChartAssetBuildEnvelope):
        if self.progress.get(envelope.job_id) is None: self.progress.initialize(envelope)
        self.progress.set_status(envelope.job_id, "running", startedAt=utc_now_iso())
        self.progress.add_log(envelope.job_id, f"build started: symbols={len(envelope.symbols)} intervals={','.join(envelope.intervals)}")
        errors = warnings = created_entities = 0
        try:
            with ThreadPoolExecutor(max_workers=min(self.concurrency, len(envelope.symbols))) as executor:
                futures = [executor.submit(self._process_symbol, envelope, symbol) for symbol in envelope.symbols]
                for future in as_completed(futures):
                    current_errors, current_warnings, current_entities = future.result()
                    errors += current_errors; warnings += current_warnings; created_entities += current_entities
        except Exception as exc:
            self.progress.add_log(envelope.job_id, f"job failed: {exc.__class__.__name__}: {exc}")
            return self.progress.set_status(envelope.job_id, "failed", finishedAt=utc_now_iso(), createdEntities=created_entities) or {}
        state = self.progress.get(envelope.job_id) or {}
        status = "canceled" if state.get("cancelRequested") else "completed_with_errors" if errors or state.get("progress",{}).get("failed") else "completed_with_warnings" if warnings or state.get("progress",{}).get("warnings") else "completed"
        progress = state.get("progress", {})
        self.progress.add_log(
            envelope.job_id,
            f"build finished: status={status} created_entities={created_entities} "
            f"done={progress.get('done', 0)}/{progress.get('total', 0)} "
            f"warnings={progress.get('warnings', 0)} failed={progress.get('failed', 0)} skipped={progress.get('skipped', 0)}",
        )
        return self.progress.set_status(envelope.job_id, status, finishedAt=utc_now_iso(), createdEntities=created_entities) or {}

    def _process_symbol(self, envelope, symbol):
        requested = [item for item in BUILD_INTERVAL_ORDER if item in envelope.intervals]
        if self.progress.is_cancel_requested(envelope.job_id):
            for interval in requested:
                self.progress.record_item(envelope.job_id, _item(symbol, interval, "skipped", "cancel", None, 0))
                self.progress.add_log(envelope.job_id, f"{symbol}:{interval} skipped: cancel requested")
            return 0, 0, 0
        existing_all = self._load_existing_assets(symbol, requested)
        existing = {interval: existing_all.get(interval) for interval in requested}
        if not envelope.force and envelope.skip_fresh_hours and all(
            _asset_is_fresh(existing[interval], envelope.skip_fresh_hours)
            for interval in requested
        ):
            for interval in requested:
                self.progress.record_item(envelope.job_id, _item(symbol, interval, "skipped", "fresh", None, 0))
                self._log_asset_result(envelope.job_id, symbol, interval, "skipped: fresh asset retained", existing[interval])
            return 0, 0, 0
        repair_reason = None
        if self.repair_service is not None:
            self.progress.set_current(envelope.job_id, f"{symbol}:repair")
            try:
                repair = self.repair_service.ensure_ready(
                    symbol,
                    requested,
                    request_id=f"{envelope.job_id}-{symbol}",
                    on_event=lambda stage, payload: self._log_repair_event(envelope.job_id, symbol, stage, payload),
                    is_cancel_requested=lambda: self.progress.is_cancel_requested(envelope.job_id),
                )
                self.progress.record_repair(envelope.job_id, repair.to_dict())
                repair_reason = repair.reason if repair.unavailable else None
            except Exception as exc:
                repair_reason = "candle_repair_incomplete"
                self.progress.record_repair(envelope.job_id, {
                    "checked": False, "attempted": True, "repaired": False, "unavailable": True,
                    "missing_before": 0, "missing_after": 0, "materialized_rows": 0,
                    "reason": repair_reason,
                })
                self.progress.add_log(envelope.job_id, f"{symbol} repair failed: {exc.__class__.__name__}: {exc}")
            if self.progress.is_cancel_requested(envelope.job_id):
                for interval in requested:
                    self.progress.record_item(envelope.job_id, _item(symbol, interval, "skipped", "cancel", None, 0))
                    self.progress.add_log(envelope.job_id, f"{symbol}:{interval} skipped: cancel requested")
                return 0, 0, 0
        started = time.monotonic()
        try:
            bundle = self._load_symbol(symbol, requested)
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"
            for interval in requested:
                self.progress.record_item(envelope.job_id, _item(symbol, interval, "failed", "candle", error, _elapsed(started)))
                self.progress.add_log(envelope.job_id, f"{symbol}:{interval} failed at candle load: {error}")
            return len(requested), 0, 0
        eligible = []
        preflight_warnings = 0
        for interval in requested:
            coverage = bundle.coverage[interval]
            if not repair_reason and coverage.get("renderable") and len(bundle.rows[interval]) >= 20: eligible.append(interval)
            elif existing[interval]:
                warning = "candle_repair_incomplete_existing_asset_preserved" if repair_reason else "input_insufficient_existing_asset_preserved"
                preflight_warnings += 1
                self.progress.record_item(envelope.job_id, _item(symbol, interval, "skipped", "preflight", None, _elapsed(started), warning=warning, reason=repair_reason))
                self.progress.add_log(envelope.job_id, _preflight_log(symbol, interval, coverage, "input insufficient; existing asset preserved"))
            else:
                asset = self._degraded_data_asset(symbol, interval, bundle.rows[interval], coverage, bundle.digests[interval])
                write_result = self.storage.save(asset)
                storage_warnings = self._pop_storage_warnings()
                warning = "|".join(dict.fromkeys([
                    repair_reason or "input_insufficient",
                    *storage_warnings,
                ]))
                preflight_warnings += 1
                status = "unchanged" if write_result is False else "saved_with_warning"
                reason = "stale_write_suppressed" if write_result is False else repair_reason
                self.progress.record_item(envelope.job_id, _item(symbol, interval, status, "preflight", None, _elapsed(started), warning=warning, reason=reason))
                action = "newer asset retained" if write_result is False else "degraded asset saved; entities=0"
                self.progress.add_log(envelope.job_id, _preflight_log(symbol, interval, coverage, f"input insufficient; {action}"))
                for storage_warning in storage_warnings:
                    self.progress.add_log(envelope.job_id, f"{symbol}:{interval} warning={storage_warning}")
        if not eligible: return 0, preflight_warnings, 0
        llm_mode = "curate" if envelope.llm_enabled else "rule_only"
        requested_model = getattr(self.llm_service, "model", None) if envelope.llm_enabled else None
        stored_higher = _higher_summaries(existing_all, eligible)
        pre_kernel_digest = _digest({"inputs": [[item, bundle.digests[item]] for item in eligible], "versions": [KERNEL_VERSION, QUALITY_POLICY_VERSION, PROMPT_VERSION_V2, MODEL_POLICY_VERSION, ASSEMBLER_VERSION], "llmMode": llm_mode, "requestedModel": requested_model, "higher": stored_higher})
        if not envelope.force and all(_fast_noop(existing[interval], bundle.digests[interval], llm_mode, requested_model, pre_kernel_digest) for interval in eligible):
            for interval in eligible:
                self.progress.record_item(envelope.job_id, _item(symbol, interval, "unchanged", "digest", None, _elapsed(started), reason="build_intent_unchanged"))
                self._log_asset_result(envelope.job_id, symbol, interval, "unchanged: build intent matched", existing[interval])
            return 0, 0, 0
        generated_at = utc_now_iso(); features_by_interval={}; rules_by_interval={}; palettes={}; rule_digests={}
        for interval in eligible:
            rows=bundle.rows[interval]; features=compute_feature_pack(rows, interval)
            rules=compile_rule_layers(symbol=symbol, interval=interval, features=features, candles=rows, generated_at=generated_at)
            rule_digest=_digest({"input":bundle.digests[interval],"kernel":KERNEL_VERSION,"quality":QUALITY_POLICY_VERSION,"selected":[item.get("candidateId") for layer in rules.values() for item in layer.get("selected",[])]})
            palette=build_interval_palette(symbol=symbol,interval=interval,input_digest=bundle.digests[interval],features=features,rule_layers=rules,candles=rows,generated_at=generated_at)
            palette["ruleDigest"]=rule_digest; features_by_interval[interval]=features; rules_by_interval[interval]=rules; palettes[interval]=palette; rule_digests[interval]=rule_digest
        context_digest=_digest({"symbol":symbol,"rules":[[interval,rule_digests[interval]] for interval in eligible],"higher":stored_higher})
        symbol_bundle=build_symbol_bundle(symbol,list(palettes.values()),_cross_timeframe(palettes))
        intent_digests={interval:_build_intent_digest(rule_digest=rule_digests[interval],context_digest=context_digest,requested_model=requested_model,llm_mode=llm_mode) for interval in eligible}
        if not envelope.force and all(_late_intent_noop(existing[interval],intent_digests[interval],llm_mode,bundle.digests[interval]) for interval in eligible):
            for interval in eligible:
                self.progress.record_item(envelope.job_id,_item(symbol,interval,"unchanged","digest",None,_elapsed(started),reason="late_intent_unchanged"))
                self._log_asset_result(envelope.job_id, symbol, interval, "unchanged after kernel: build intent matched", existing[interval])
            return 0,0,0
        has_visual_candidates = any(palette.get("visualCandidates") for palette in palettes.values())
        if envelope.llm_enabled and has_visual_candidates:
            try: curation=self.llm_service.curate_symbol(symbol_bundle)
            except Exception as exc: curation={"output":deterministic_curation(symbol_bundle),"degraded":True,"reason":f"llm_{exc.__class__.__name__}","model":requested_model,"usage":{}}
        elif envelope.llm_enabled:
            curation={"output":deterministic_curation(symbol_bundle),"degraded":False,"reason":"no_visual_candidates","model":None,"usage":{},"llmSkipped":True}
        else:
            curation={"output":deterministic_curation(symbol_bundle),"degraded":False,"reason":None,"model":None,"usage":{}}
        if envelope.llm_enabled:
            agent_layers=materialize_curation(symbol=symbol,palettes=palettes,output=curation["output"],generated_at=generated_at,model=curation.get("model"))
            if curation.get("degraded"):
                for interval in eligible:
                    preserved = _preserved_agent(existing[interval], bundle.digests[interval], palettes[interval])
                    if preserved:
                        preserved.setdefault("meta", {}).update({"degraded": True, "failureReason": curation.get("reason"), "preservedOnFailure": True})
                        agent_layers[interval] = preserved
                    else:
                        agent_layers[interval].setdefault("meta", {}).update({"degraded": True, "failureReason": curation.get("reason")})
                        agent_layers[interval]["emptyReason"] = curation.get("reason") or "curator_unavailable"
        else:
            agent_layers={interval:_preserved_agent(existing[interval],bundle.digests[interval],palettes[interval]) or _empty_agent_layer("llm_not_requested") for interval in eligible}
        selections={item["interval"]:item for item in curation["output"].get("intervalSelections",[])}
        warnings=preflight_warnings; created_entities=0; context_assets=dict(existing_all)
        for position, interval in enumerate(eligible):
            if self.progress.is_cancel_requested(envelope.job_id):
                for remaining in eligible[position:]:
                    self.progress.record_item(envelope.job_id, _item(symbol, remaining, "skipped", "cancel", None, _elapsed(started)))
                    self.progress.add_log(envelope.job_id, f"{symbol}:{remaining} skipped: cancel requested")
                break
            outcome="degraded" if curation.get("degraded") and envelope.llm_enabled else "ready" if envelope.llm_enabled and agent_layers[interval]["drawings"] else "ready_empty" if envelope.llm_enabled else "preserved" if agent_layers[interval]["drawings"] else "not_requested_empty"
            intent_digest=intent_digests[interval]
            higher = _higher_summaries(context_assets, [interval])
            asset=self._assemble_asset(symbol=symbol,interval=interval,rows=bundle.rows[interval],coverage=bundle.coverage[interval],input_digest=bundle.digests[interval],features=features_by_interval[interval],rules=rules_by_interval[interval],agent=agent_layers[interval],palette=palettes[interval],selection=selections.get(interval),generated_at=generated_at,rule_digest=rule_digests[interval],context_digest=context_digest,intent_digest=intent_digest,pre_kernel_digest=pre_kernel_digest,llm_mode=llm_mode,outcome=outcome,curation=curation,higher=higher)
            required_higher = {"1W": {"1M"}, "1D": {"1M", "1W"}}.get(interval, set())
            asset["buildContext"] = {"higherTf": higher or None, "flags": ["no_higher_tf_context"] if not required_higher.issubset(higher) else []}
            content_digest=_asset_content_digest(asset); asset["build"]["assetContentDigest"]=content_digest
            encoded=json.dumps(asset,ensure_ascii=False,sort_keys=True,separators=(",",":"))
            if len(encoded.encode())>MAX_ASSET_BYTES: raise ValueError(f"chart asset payload exceeds {MAX_ASSET_BYTES} bytes")
            current=existing[interval]
            data_warning = "data_quality_blocked" if "data_quality_blocked" in features_by_interval[interval].get("qualityFlags", []) else None
            storage_warnings = []
            if current and current.get("build",{}).get("assetContentDigest")==content_digest:
                status="unchanged"; reason="unchanged_after_force" if envelope.force else "content_unchanged"
            else:
                write_result = self.storage.save(asset)
                storage_warnings = self._pop_storage_warnings()
                if write_result is False:
                    status = "unchanged"
                    reason = "stale_write_suppressed"
                else:
                    status="saved_with_warning" if outcome=="degraded" or data_warning or storage_warnings else "saved"; reason=None
                    created_entities += _entity_counts(asset)["total"]
            context_assets[interval] = (
                self.storage.get(symbol, interval) or current or asset
                if reason == "stale_write_suppressed"
                else asset
            )
            warning_parts = []
            if outcome=="degraded" and curation.get("reason"): warning_parts.append(str(curation["reason"]))
            if repair_reason: warning_parts.append(str(repair_reason))
            if data_warning: warning_parts.append(data_warning)
            warning_parts.extend(storage_warnings)
            warning="|".join(dict.fromkeys(warning_parts)) or None
            if warning: warnings+=1
            self.progress.record_item(envelope.job_id,_item(symbol,interval,status,"save",None,_elapsed(started),warning=warning,reason=reason))
            result = status if not warning else f"{status}; warning={warning}"
            if reason: result += f"; reason={reason}"
            self._log_asset_result(envelope.job_id, symbol, interval, result, asset)
        return 0,warnings,created_entities

    def _pop_storage_warnings(self):
        pop_warnings = getattr(self.storage, "pop_warnings", None)
        return [str(item) for item in pop_warnings() if item] if callable(pop_warnings) else []

    def _load_symbol(self,symbol,intervals):
        if hasattr(self.candle_loader,"load_symbol"): return self.candle_loader.load_symbol(symbol,intervals)
        rows={item:self.candle_loader.load(symbol,item) for item in intervals}; coverage={item:{"expectedBars":len(rows[item]),"actualBars":len(rows[item]),"missingBars":0,"coverageRatio":1.0,"recentContiguousBars":len(rows[item]),"largestGapBars":0,"lastExpectedClosedAt":rows[item][-1]["timestamp"] if rows[item] else None,"lastActualClosedAt":rows[item][-1]["timestamp"] if rows[item] else None,"renderable":True,"qualityFlags":[]} for item in intervals}
        return SimpleNamespace(rows=rows,coverage=coverage,digests={item:analysis_input_digest(symbol,item,rows[item]) for item in intervals})

    def _load_existing_assets(self, symbol, requested):
        if hasattr(self.storage, "get_symbol_assets"):
            snapshot = self.storage.get_symbol_assets(symbol)
            return {interval: snapshot.get(interval) for interval in BUILD_INTERVAL_ORDER}
        needed = set(requested)
        if "1D" in needed:
            needed.update({"1M", "1W"})
        if "1W" in needed:
            needed.add("1M")
        return {interval: self.storage.get(symbol, interval) for interval in BUILD_INTERVAL_ORDER if interval in needed}

    def _log_asset_result(self, job_id, symbol, interval, result, asset):
        counts = _entity_counts(asset)
        message = f"{symbol}:{interval} {result}; entities={counts['total']} (S={counts['structure']},T={counts['trend']},I={counts['agent']})"
        if counts["total"] == 0:
            reasons = _asset_rejection_reasons(asset)
            message += f"; no-candidate reasons={reasons}" if reasons else "; no candidate passed current quality gates"
        self.progress.add_log(job_id, message)

    def _log_repair_event(self, job_id, symbol, stage, payload):
        if stage in {"audit", "recheck", "final"}:
            source = f" source={payload.get('source')}" if payload.get("source") else ""
            self.progress.add_log(
                job_id,
                f"{symbol} repair {stage}:{source} missing={payload.get('missingBars', 0)} "
                f"actual={payload.get('actualBars', 0)}/{payload.get('expectedBars', 0)} "
                f"ranges={len(payload.get('ranges') or [])}",
            )
            return
        repair_range = payload.get("range") or {}
        message = (
            f"{symbol} repair {stage}: status={payload.get('status')} "
            f"kind={repair_range.get('kind')} missing={repair_range.get('missingCount')}"
        )
        if payload.get("materializedRows") is not None:
            message += f" materialized={payload.get('materializedRows')}"
        if payload.get("reason"):
            message += f" reason={payload.get('reason')}"
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
        if metrics and payload.get("status") != "started":
            metric_names = (
                "listCalls", "objectsListed", "manifestObjectsRead", "objectsSelected",
                "objectGets", "elapsedMs", "manifestSource",
            )
            rendered = " ".join(f"{name}={metrics[name]}" for name in metric_names if metrics.get(name) is not None)
            if rendered:
                message += f" metrics({rendered})"
        self.progress.add_log(job_id, message)

    def _assemble_asset(self,**value):
        symbol=value["symbol"]; interval=value["interval"]; rows=value["rows"]; coverage=value["coverage"]; display=rows[-DISPLAY_BARS[interval]:]
        commentary=assemble_commentary_v2(interval=interval,palette=value["palette"],rule_layers=value["rules"],agent_layer=value["agent"],curation_selection=value["selection"],coverage=coverage)
        curation=value["curation"]; data_blocked="data_quality_blocked" in value["features"].get("qualityFlags",[]); status="degraded" if value["outcome"]=="degraded" or data_blocked else "ready"
        drawing_count=sum(len(layer.get("drawings") or []) for layer in (*value["rules"].values(),value["agent"]))
        quality_reasons=["data_degraded","data_quality_blocked"] if data_blocked else ["recent_contiguous_history","exact_anchor_membership"] if drawing_count else ["quality_empty",*_empty_reasons(value["rules"],value["agent"])]
        return {"assetVersion":ASSET_VERSION,"kernelVersion":KERNEL_VERSION,"qualityPolicyVersion":QUALITY_POLICY_VERSION,"promptVersion":PROMPT_VERSION_V2,"modelPolicyVersion":MODEL_POLICY_VERSION,"symbol":symbol,"interval":interval,"asOf":rows[-1]["timestamp"],"generatedAt":value["generated_at"],"status":status,"window":{"displayFrom":display[0]["timestamp"],"displayTo":display[-1]["timestamp"],"displayBars":DISPLAY_BARS[interval],"lookbackBars":LOOKBACK_BARS[interval]},"coverage":coverage,"input":{"digest":value["input_digest"],"canonicalDataVersion":CANONICAL_DATA_VERSION,"sessionPolicy":SESSION_POLICY,"adjustmentPolicy":ADJUSTMENT_POLICY,"candleContractVersion":CANDLE_CONTRACT_VERSION},"build":{"ruleDigest":value["rule_digest"],"contextDigest":value["context_digest"],"preKernelDigest":value["pre_kernel_digest"],"buildIntentDigest":value["intent_digest"],"assetContentDigest":"sha256:"+"0"*64,"assemblerVersion":ASSEMBLER_VERSION,"llmMode":value["llm_mode"],"agentPreservationPolicy":AGENT_PRESERVATION_POLICY,"agentOutcome":value["outcome"],"requestedModel":getattr(self.llm_service,"model",None) if value["llm_mode"]=="curate" else None,"resolvedModel":curation.get("model"),"usage":curation.get("usage") or {},"latencyMs":curation.get("latencyMs"),"llmSkippedReason":curation.get("reason") if curation.get("llmSkipped") else None},"quality":{"state":"insufficient_data" if data_blocked else "eligible","score":0 if data_blocked else round(.7+.3*float(coverage.get("coverageRatio",0)),4) if drawing_count else 0,"reasons":quality_reasons,"penalties":list(dict.fromkeys([*(coverage.get("qualityFlags") or []),*(value["features"].get("qualityFlags") or [])]))},"features":_compact_features(value["features"],value["rules"],value["agent"]),"layers":{"structure":value["rules"]["structure"],"trend":value["rules"]["trend"],"agent":value["agent"]},"chartSetup":{"alwaysOn":["volume-profile","volume"],"recommended":recommended_indicators(value["features"])},"commentary":commentary,"buildContext":{"higherTf":value["higher"] or None,"flags":[]}}

    def _degraded_data_asset(self,symbol,interval,rows,coverage,input_digest):
        generated=utc_now_iso(); placeholder=rows[-1]["timestamp"] if rows else generated; window_rows=rows[-DISPLAY_BARS[interval]:]
        empty=_empty_agent_layer("data_insufficient")
        asset={"assetVersion":ASSET_VERSION,"kernelVersion":KERNEL_VERSION,"qualityPolicyVersion":QUALITY_POLICY_VERSION,"promptVersion":PROMPT_VERSION_V2,"modelPolicyVersion":MODEL_POLICY_VERSION,"symbol":symbol,"interval":interval,"asOf":placeholder,"generatedAt":generated,"status":"degraded","window":{"displayFrom":window_rows[0]["timestamp"] if window_rows else placeholder,"displayTo":placeholder,"displayBars":DISPLAY_BARS[interval],"lookbackBars":LOOKBACK_BARS[interval]},"coverage":coverage,"input":{"digest":input_digest,"canonicalDataVersion":CANONICAL_DATA_VERSION,"sessionPolicy":SESSION_POLICY,"adjustmentPolicy":ADJUSTMENT_POLICY,"candleContractVersion":CANDLE_CONTRACT_VERSION},"build":{"ruleDigest":_digest({}),"contextDigest":_digest({}),"buildIntentDigest":_digest({"data":"insufficient"}),"assetContentDigest":"sha256:"+"0"*64,"llmMode":"rule_only","agentPreservationPolicy":AGENT_PRESERVATION_POLICY,"agentOutcome":"not_requested_empty"},"quality":{"state":"stale_input" if "stale_input" in coverage.get("qualityFlags",[]) else "insufficient_data","score":0,"reasons":["data_degraded"],"penalties":coverage.get("qualityFlags",[])},"features":{"pivots":[],"levels":[],"trends":[],"events":[],"fibCandidates":[],"vp":{},"regime":{}},"layers":{"structure":_empty_rule_layer("data_insufficient"),"trend":_empty_rule_layer("data_insufficient"),"agent":empty},"chartSetup":{"alwaysOn":["volume-profile","volume"],"recommended":[]},"commentary":{"headline":"분석 데이터가 충분하지 않습니다.","regimeSummary":"","focusItems":[],"keyLevelsV2":[],"higherTimeframeContext":"","counterEvidence":[],"dataCaveats":coverage.get("qualityFlags",[]),"confidenceV2":{"selection":{"score":0,"reasons":["data_degraded"],"penalties":coverage.get("qualityFlags",[])},"marketDirection":{"score":None,"reasons":[],"penalties":[]}},"text":"분석 데이터 갱신 후 다시 확인하세요.","keyLevels":[],"invalidation":"데이터 갱신이 필요합니다.","confidence":0,"enrichment":None,"emptyState":"data_degraded","emptyReason":"data_insufficient"},"buildContext":{"higherTf":None,"flags":["data_insufficient"]}}
        asset["build"]["assetContentDigest"]=_asset_content_digest(asset); return asset

def _fast_noop(asset,input_digest,llm_mode,requested_model,pre_kernel_digest):
    if not asset or asset.get("assetVersion")!="v2" or asset.get("input",{}).get("digest")!=input_digest or asset.get("input",{}).get("candleContractVersion")!=CANDLE_CONTRACT_VERSION or asset.get("kernelVersion")!=KERNEL_VERSION or asset.get("qualityPolicyVersion")!=QUALITY_POLICY_VERSION:return False
    build=asset.get("build",{}); outcome=build.get("agentOutcome")
    eligible=outcome in ({"ready","ready_empty"} if llm_mode=="curate" else {"not_requested_empty","preserved"})
    return eligible and build.get("llmMode")==llm_mode and build.get("preKernelDigest")==pre_kernel_digest and (llm_mode!="curate" or build.get("requestedModel")==requested_model)
def _build_intent_digest(*,rule_digest,context_digest,requested_model,llm_mode):
    return _digest({"ruleDigest":rule_digest,"contextDigest":context_digest,"promptVersion":PROMPT_VERSION_V2,"modelPolicyVersion":MODEL_POLICY_VERSION,"requestedModel":requested_model,"assemblerVersion":ASSEMBLER_VERSION,"llmMode":llm_mode,"agentPreservationPolicy":AGENT_PRESERVATION_POLICY})
def _late_intent_noop(asset,intent_digest,llm_mode,input_digest):
    if not asset or asset.get("assetVersion")!="v2" or asset.get("kernelVersion")!=KERNEL_VERSION or asset.get("qualityPolicyVersion")!=QUALITY_POLICY_VERSION:return False
    if asset.get("input",{}).get("digest")!=input_digest or asset.get("input",{}).get("candleContractVersion")!=CANDLE_CONTRACT_VERSION:return False
    build=asset.get("build",{}); outcome=build.get("agentOutcome")
    eligible=outcome in ({"ready","ready_empty"} if llm_mode=="curate" else {"not_requested_empty","preserved"})
    return eligible and build.get("llmMode")==llm_mode and build.get("buildIntentDigest")==intent_digest
def _preserved_agent(asset,input_digest,palette):
    if not asset or asset.get("assetVersion")!="v2" or asset.get("input",{}).get("digest")!=input_digest or asset.get("input",{}).get("candleContractVersion")!=CANDLE_CONTRACT_VERSION or asset.get("kernelVersion")!=KERNEL_VERSION or asset.get("qualityPolicyVersion")!=QUALITY_POLICY_VERSION:return None
    agent=asset.get("layers",{}).get("agent") or {}; allowed={item["candidateId"] for item in palette.get("visualCandidates",[])}
    selected=agent.get("selected") or []
    if not selected or any(item.get("candidateId") not in allowed for item in selected):return None
    drawing_ids={item.get("id") for item in agent.get("drawings",[])}
    if not drawing_ids or any(not set(item.get("drawingIds") or []).issubset(drawing_ids) for item in selected):return None
    return json.loads(json.dumps(agent))
def _compact_features(features,rules,agent):
    selected=[item for layer in (*rules.values(),agent) for item in layer.get("selected",[])]
    refs={str(ref) for item in selected for ref in item.get("evidenceRefs",[])}
    candidate_ids={str(item.get("candidateId")) for item in selected if item.get("candidateId")}
    pivots=[_project(item,("id","timestamp","price","kind","grade","strength")) for item in features.get("pivots",[]) if item.get("id") in refs][:16]
    levels=[_project(item,("id","price","zoneLow","zoneHigh","score","touches","lastTestAt","lastTouchAgeBars","currentDistanceAtr","role","state","evidencePass","activePass","vpConfluence","memberPivotIds")) for item in features.get("levels",[]) if item.get("id") in candidate_ids][:4]
    trends=[_project(item,("id","kind","anchorPivotIds","touchPivotIds","touchCandleKeys","touches","reactionCount","slopePerBar","slopeAtrPerBar","currentDistanceAtr","lastTouchAgeBars","spanBars","medianResidualAtr","violationCount","activeInvalidation","adverseCloseRatio","containment","parallelSlopeError","rangeFrom","rangeTo","rangeHigh","rangeLow","score")) for item in features.get("trends",[]) if item.get("id") in candidate_ids][:2]
    events=[]
    for item in features.get("events",[]):
        if item.get("id") not in candidate_ids and item.get("id") not in refs:continue
        projected=_project(item,("id","timestamp","candleKey","kind","price","refIds","currentImpact","ageBars"))
        projected["state"]=(item.get("detail") or {}).get("state")
        events.append(projected)
    vp=features.get("vp") or {}
    return {"pivots":pivots,"levels":levels,"trends":trends,"events":events[:4],"fibCandidates":[],"vp":_project(vp,("poc","valueArea")),"regime":_project(features.get("regime") or {},("trend","emaSlope20","atr14","atrPercentile","bbSqueeze","bbBandwidthPercentile","macdState","rsi14","volumeZLast","pctFrom52wHigh")),"qualityFlags":features.get("qualityFlags",[])}
def _project(value,keys):return {key:value[key] for key in keys if key in value and value[key] is not None}
def _cross_timeframe(palettes):
    trends={interval:item.get("regime",{}).get("trend") for interval,item in palettes.items()}; values=set(trends.values()); alignment="aligned" if len(values)==1 else "mixed"
    relation=[]
    if len(trends)>1:relation=["MTF:"+"_".join(f"{key.lower()}_{trends[key]}" for key in sorted(trends))]
    return {"alignment":alignment,"relationIds":relation,"evidenceRefs":[]}
def _empty_rule_layer(reason):return {"drawings":[],"selected":[],"emptyReason":reason,"meta":{"candidateCount":0,"passedCount":0,"rejectedByReason":{}}}
def _empty_agent_layer(reason):return {"drawings":[],"selected":[],"emptyReason":reason,"meta":{"candidateCount":0,"passedCount":0,"rejectedByReason":{},"degraded":False}}
def _digest(value):return "sha256:"+hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),default=str).encode()).hexdigest()
def _asset_content_digest(asset):
    body={key:_without_audit_timestamps(asset[key]) for key in ("status","quality","features","layers","chartSetup","commentary","buildContext")}; body["assemblerVersion"]=asset["build"].get("assemblerVersion"); body["agentOutcome"]=asset["build"]["agentOutcome"]; body["resolvedModel"]=asset["build"].get("resolvedModel")
    # Presentation freshness is content, not audit metadata.  A newly closed
    # candle can leave rounded geometry unchanged; omitting this identity would
    # keep the old asOf/input forever and make the frontend reject it as stale.
    body["freshnessIdentity"]={"asOf":asset.get("asOf"),"window":asset.get("window"),"inputDigest":(asset.get("input") or {}).get("digest"),"candleContractVersion":(asset.get("input") or {}).get("candleContractVersion")}
    return _digest(body)
def _without_audit_timestamps(value):
    if isinstance(value,dict):return {key:_without_audit_timestamps(item) for key,item in value.items() if key not in {"createdAt","updatedAt","generatedAt","latencyMs"}}
    if isinstance(value,list):return [_without_audit_timestamps(item) for item in value]
    return value
def _item(symbol,interval,status,stage,error,elapsed_ms,warning=None,reason=None):
    result={"symbol":symbol,"interval":interval,"status":status,"stage":stage,"error":error,"elapsedMs":elapsed_ms}
    if warning:result["warning"]=warning
    if reason:result["reason"]=reason
    return result
def _elapsed(started):return max(0,int((time.monotonic()-started)*1000))


def _higher_summaries(assets, intervals):
    result = {}
    needed = set()
    requested = set(intervals)
    if "1D" in requested:
        needed.update({"1M", "1W"}.difference(requested))
    if "1W" in requested:
        needed.update({"1M"}.difference(requested))
    for source in needed:
        asset = assets.get(source)
        if asset and asset.get("assetVersion") == "v2" and asset.get("quality", {}).get("state") == "eligible":
            result[source] = {
                "asOf": asset.get("asOf"),
                "ruleDigest": asset.get("build", {}).get("ruleDigest"),
                "regime": asset.get("features", {}).get("regime", {}),
            }
    return result


def _asset_is_fresh(asset, hours):
    if not asset or hours <= 0:
        return False
    if asset.get("input", {}).get("candleContractVersion") != CANDLE_CONTRACT_VERSION:
        return False
    try:
        generated = datetime.fromisoformat(str(asset["generatedAt"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return False
    return (datetime.now(timezone.utc) - generated).total_seconds() < hours * 3600


def _entity_counts(asset):
    layers = (asset or {}).get("layers", {})
    counts = {
        name: len((layers.get(name) or {}).get("drawings") or [])
        for name in ("structure", "trend", "agent")
    }
    return {**counts, "total": sum(counts.values())}


def _asset_rejection_reasons(asset):
    counts = {}
    for name in ("structure", "trend", "agent"):
        layer = ((asset or {}).get("layers", {}).get(name) or {})
        for reason, count in (layer.get("meta", {}).get("rejectedByReason") or {}).items():
            counts[reason] = counts.get(reason, 0) + int(count or 0)
    return ",".join(f"{reason}:{count}" for reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:3])


def _preflight_log(symbol, interval, coverage, result):
    flags = ",".join(coverage.get("qualityFlags") or []) or "none"
    return (
        f"{symbol}:{interval} warning: {result}; "
        f"bars={coverage.get('actualBars', 0)}/{coverage.get('expectedBars', 0)} flags={flags}"
    )


def _empty_reasons(rules, agent):
    reasons = []
    for layer in (*rules.values(), agent):
        reason = layer.get("emptyReason")
        if reason and reason not in {"curator_selected_none", "llm_not_requested"}:
            reasons.append(str(reason))
    return list(dict.fromkeys(reasons)) or ["no_structural_evidence"]
