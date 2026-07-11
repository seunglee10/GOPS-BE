from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from types import SimpleNamespace
from typing import Any

from alfaka.analytics import DISPLAY_BARS, KERNEL_VERSION, LOOKBACK_BARS, QUALITY_POLICY_VERSION, compute_feature_pack
from alfaka.analytics.analysis_candles import (
    ADJUSTMENT_POLICY, CANONICAL_DATA_VERSION, CANDLE_CONTRACT_VERSION, SESSION_POLICY,
    analysis_input_digest,
)

from .candles import ChartAssetCandleLoader
from .commentary_v2 import assemble_commentary_v2
from .compilers import compile_rule_layers_v2, recommended_indicators
from .curation import (
    MODEL_POLICY_VERSION, PROMPT_VERSION_V2, build_interval_palette, build_symbol_bundle,
    deterministic_curation, materialize_curation,
)
from .envelope import BUILD_INTERVAL_ORDER, ChartAssetBuildEnvelope, utc_now_iso
from .llm import ChartAssetLLMService
from .progress import InMemoryChartAssetProgressStore, build_progress_store_from_env
from .storage import ChartAssetStorage


ASSET_VERSION = "v2"
ASSEMBLER_VERSION = "chart-asset-assembler-v2.1"
AGENT_PRESERVATION_POLICY = "preserve_valid_same_input"
MAX_ASSET_BYTES = 20 * 1024


class ChartAssetBuilder:
    def __init__(self, *, candle_loader=None, storage=None, progress=None, llm_service=None, concurrency=None):
        self.candle_loader = candle_loader or ChartAssetCandleLoader(); self.storage = storage or ChartAssetStorage()
        self.progress = progress or build_progress_store_from_env(); self.llm_service = llm_service if llm_service is not None else ChartAssetLLMService()
        self.concurrency = max(1, concurrency or int(os.getenv("CHART_ASSET_BUILD_CONCURRENCY", "4")))

    def process_message(self, message):
        from .envelope import envelope_from_dict
        return self.run(envelope_from_dict(message))

    def run(self, envelope: ChartAssetBuildEnvelope):
        if self.progress.get(envelope.job_id) is None: self.progress.initialize(envelope)
        self.progress.set_status(envelope.job_id, "running", startedAt=utc_now_iso())
        self.progress.add_log(envelope.job_id, f"build started: symbols={len(envelope.symbols)} intervals={','.join(envelope.intervals)}")
        errors = warnings = 0
        try:
            with ThreadPoolExecutor(max_workers=min(self.concurrency, len(envelope.symbols))) as executor:
                futures = [executor.submit(self._process_symbol, envelope, symbol) for symbol in envelope.symbols]
                for future in as_completed(futures):
                    current_errors, current_warnings = future.result(); errors += current_errors; warnings += current_warnings
        except Exception as exc:
            self.progress.add_log(envelope.job_id, f"job failed: {exc.__class__.__name__}: {exc}")
            return self.progress.set_status(envelope.job_id, "failed", finishedAt=utc_now_iso()) or {}
        state = self.progress.get(envelope.job_id) or {}
        status = "canceled" if state.get("cancelRequested") else "completed_with_errors" if errors or state.get("progress",{}).get("failed") else "completed_with_warnings" if warnings or state.get("progress",{}).get("warnings") else "completed"
        self.progress.add_log(envelope.job_id, f"build finished: status={status}")
        return self.progress.set_status(envelope.job_id, status, finishedAt=utc_now_iso()) or {}

    def _process_symbol(self, envelope, symbol):
        requested = [item for item in BUILD_INTERVAL_ORDER if item in envelope.intervals]
        if self.progress.is_cancel_requested(envelope.job_id):
            for interval in requested: self.progress.record_item(envelope.job_id, _item(symbol, interval, "skipped", "cancel", None, 0))
            return 0, 0
        existing = {interval: self.storage.get(symbol, interval) for interval in requested}
        if not envelope.force and envelope.skip_fresh_hours and all(self.storage.is_fresh(symbol, interval, envelope.skip_fresh_hours) for interval in requested):
            for interval in requested: self.progress.record_item(envelope.job_id, _item(symbol, interval, "skipped", "fresh", None, 0))
            return 0, 0
        started = time.monotonic()
        try:
            bundle = self._load_symbol(symbol, requested)
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"
            for interval in requested: self.progress.record_item(envelope.job_id, _item(symbol, interval, "failed", "candle", error, _elapsed(started)))
            return len(requested), 0
        eligible = []
        for interval in requested:
            coverage = bundle.coverage[interval]
            if coverage.get("renderable") and len(bundle.rows[interval]) >= 20: eligible.append(interval)
            elif existing[interval]:
                self.progress.record_item(envelope.job_id, _item(symbol, interval, "skipped", "preflight", None, _elapsed(started), warning="input_insufficient_existing_asset_preserved"))
            else:
                asset = self._degraded_data_asset(symbol, interval, bundle.rows[interval], coverage, bundle.digests[interval])
                self.storage.save(asset); self.progress.record_item(envelope.job_id, _item(symbol, interval, "saved_with_warning", "preflight", None, _elapsed(started), warning="input_insufficient"))
        if not eligible: return 0, len(requested)
        llm_mode = "curate" if envelope.llm_enabled else "rule_only"
        requested_model = getattr(self.llm_service, "model", None) if envelope.llm_enabled else None
        pre_kernel_digest = _digest({"inputs": [[item, bundle.digests[item]] for item in eligible], "versions": [KERNEL_VERSION, QUALITY_POLICY_VERSION, PROMPT_VERSION_V2, MODEL_POLICY_VERSION, ASSEMBLER_VERSION], "llmMode": llm_mode, "requestedModel": requested_model, "higher": self._stored_higher_summaries(symbol, eligible)})
        if not envelope.force and all(_fast_noop(existing[interval], bundle.digests[interval], llm_mode, requested_model, pre_kernel_digest) for interval in eligible):
            for interval in eligible: self.progress.record_item(envelope.job_id, _item(symbol, interval, "unchanged", "digest", None, _elapsed(started), reason="build_intent_unchanged"))
            return 0, 0
        generated_at = utc_now_iso(); features_by_interval={}; rules_by_interval={}; palettes={}; rule_digests={}
        for interval in eligible:
            rows=bundle.rows[interval]; features=compute_feature_pack(rows, interval)
            if "abnormal_true_range" in features.get("qualityFlags",[]):
                features["trends"]=[]; features["levels"]=[]
            rules=compile_rule_layers_v2(symbol=symbol, interval=interval, features=features, candles=rows, generated_at=generated_at)
            rule_digest=_digest({"input":bundle.digests[interval],"kernel":KERNEL_VERSION,"quality":QUALITY_POLICY_VERSION,"selected":[item.get("candidateId") for layer in rules.values() for item in layer.get("selected",[])]})
            palette=build_interval_palette(symbol=symbol,interval=interval,input_digest=bundle.digests[interval],features=features,rule_layers=rules,candles=rows,generated_at=generated_at)
            palette["ruleDigest"]=rule_digest; features_by_interval[interval]=features; rules_by_interval[interval]=rules; palettes[interval]=palette; rule_digests[interval]=rule_digest
        context_digest=_digest({"symbol":symbol,"rules":[[interval,rule_digests[interval]] for interval in eligible],"higher":self._stored_higher_summaries(symbol, eligible)})
        symbol_bundle=build_symbol_bundle(symbol,list(palettes.values()),_cross_timeframe(palettes))
        intent_digests={interval:_build_intent_digest(rule_digest=rule_digests[interval],context_digest=context_digest,requested_model=requested_model,llm_mode=llm_mode) for interval in eligible}
        if not envelope.force and all(_late_intent_noop(existing[interval],intent_digests[interval],llm_mode) for interval in eligible):
            for interval in eligible: self.progress.record_item(envelope.job_id,_item(symbol,interval,"unchanged","digest",None,_elapsed(started),reason="late_intent_unchanged"))
            return 0,0
        if envelope.llm_enabled:
            try: curation=self.llm_service.curate_symbol(symbol_bundle)
            except Exception as exc: curation={"output":deterministic_curation(symbol_bundle),"degraded":True,"reason":f"llm_{exc.__class__.__name__}","model":requested_model,"usage":{}}
        else:
            curation={"output":deterministic_curation(symbol_bundle),"degraded":False,"reason":None,"model":None,"usage":{}}
        if envelope.llm_enabled:
            agent_layers=materialize_curation(symbol=symbol,palettes=palettes,output=curation["output"],generated_at=generated_at,model=curation.get("model"))
        else:
            agent_layers={interval:_preserved_agent(existing[interval],bundle.digests[interval],palettes[interval]) or _empty_agent_layer("llm_not_requested") for interval in eligible}
        selections={item["interval"]:item for item in curation["output"].get("intervalSelections",[])}
        warnings=0
        for position, interval in enumerate(eligible):
            if self.progress.is_cancel_requested(envelope.job_id):
                for remaining in eligible[position:]: self.progress.record_item(envelope.job_id, _item(symbol, remaining, "skipped", "cancel", None, _elapsed(started)))
                break
            outcome="degraded" if curation.get("degraded") and envelope.llm_enabled else "ready" if envelope.llm_enabled and agent_layers[interval]["drawings"] else "ready_empty" if envelope.llm_enabled else "preserved" if agent_layers[interval]["drawings"] else "not_requested_empty"
            intent_digest=intent_digests[interval]
            asset=self._assemble_asset(symbol=symbol,interval=interval,rows=bundle.rows[interval],coverage=bundle.coverage[interval],input_digest=bundle.digests[interval],features=features_by_interval[interval],rules=rules_by_interval[interval],agent=agent_layers[interval],palette=palettes[interval],selection=selections.get(interval),generated_at=generated_at,rule_digest=rule_digests[interval],context_digest=context_digest,intent_digest=intent_digest,pre_kernel_digest=pre_kernel_digest,llm_mode=llm_mode,outcome=outcome,curation=curation)
            higher = self._stored_higher_summaries(symbol, [interval])
            required_higher = {"1W": {"1M"}, "1D": {"1M", "1W"}}.get(interval, set())
            asset["buildContext"] = {"higherTf": higher or None, "flags": ["no_higher_tf_context"] if not required_higher.issubset(higher) else []}
            content_digest=_asset_content_digest(asset); asset["build"]["assetContentDigest"]=content_digest
            encoded=json.dumps(asset,ensure_ascii=False,sort_keys=True,separators=(",",":"))
            if len(encoded.encode())>MAX_ASSET_BYTES: raise ValueError(f"chart asset payload exceeds {MAX_ASSET_BYTES} bytes")
            current=existing[interval]
            if current and current.get("build",{}).get("assetContentDigest")==content_digest:
                status="unchanged"; reason="unchanged_after_force" if envelope.force else "content_unchanged"
            else:
                self.storage.save(asset); status="saved_with_warning" if outcome=="degraded" else "saved"; reason=None
            warning=curation.get("reason") if outcome=="degraded" else None
            if warning: warnings+=1; self.progress.add_log(envelope.job_id,f"{symbol}:{interval} saved with warning: {warning}")
            self.progress.record_item(envelope.job_id,_item(symbol,interval,status,"save",None,_elapsed(started),warning=warning,reason=reason))
        return 0,warnings

    def _load_symbol(self,symbol,intervals):
        if hasattr(self.candle_loader,"load_symbol"): return self.candle_loader.load_symbol(symbol,intervals)
        rows={item:self.candle_loader.load(symbol,item) for item in intervals}; coverage={item:{"expectedBars":len(rows[item]),"actualBars":len(rows[item]),"missingBars":0,"coverageRatio":1.0,"recentContiguousBars":len(rows[item]),"largestGapBars":0,"lastExpectedClosedAt":rows[item][-1]["timestamp"] if rows[item] else None,"lastActualClosedAt":rows[item][-1]["timestamp"] if rows[item] else None,"renderable":True,"qualityFlags":[]} for item in intervals}
        return SimpleNamespace(rows=rows,coverage=coverage,digests={item:analysis_input_digest(symbol,item,rows[item]) for item in intervals})

    def _assemble_asset(self,**value):
        symbol=value["symbol"]; interval=value["interval"]; rows=value["rows"]; coverage=value["coverage"]; display=rows[-DISPLAY_BARS[interval]:]
        commentary=assemble_commentary_v2(interval=interval,palette=value["palette"],rule_layers=value["rules"],agent_layer=value["agent"],curation_selection=value["selection"],coverage=coverage)
        curation=value["curation"]; status="degraded" if value["outcome"]=="degraded" else "ready"
        return {"assetVersion":ASSET_VERSION,"kernelVersion":KERNEL_VERSION,"qualityPolicyVersion":QUALITY_POLICY_VERSION,"promptVersion":PROMPT_VERSION_V2,"modelPolicyVersion":MODEL_POLICY_VERSION,"symbol":symbol,"interval":interval,"asOf":rows[-1]["timestamp"],"generatedAt":value["generated_at"],"status":status,"window":{"displayFrom":display[0]["timestamp"],"displayTo":display[-1]["timestamp"],"displayBars":DISPLAY_BARS[interval],"lookbackBars":LOOKBACK_BARS[interval]},"coverage":coverage,"input":{"digest":value["input_digest"],"canonicalDataVersion":CANONICAL_DATA_VERSION,"sessionPolicy":SESSION_POLICY,"adjustmentPolicy":ADJUSTMENT_POLICY,"candleContractVersion":CANDLE_CONTRACT_VERSION},"build":{"ruleDigest":value["rule_digest"],"contextDigest":value["context_digest"],"preKernelDigest":value["pre_kernel_digest"],"buildIntentDigest":value["intent_digest"],"assetContentDigest":"sha256:"+"0"*64,"assemblerVersion":ASSEMBLER_VERSION,"llmMode":value["llm_mode"],"agentPreservationPolicy":AGENT_PRESERVATION_POLICY,"agentOutcome":value["outcome"],"requestedModel":getattr(self.llm_service,"model",None) if value["llm_mode"]=="curate" else None,"resolvedModel":curation.get("model"),"usage":curation.get("usage") or {},"latencyMs":curation.get("latencyMs")},"quality":{"state":"eligible","score":round(.7+.3*float(coverage.get("coverageRatio",0)),4),"reasons":["recent_contiguous_history","exact_anchor_membership"],"penalties":list(coverage.get("qualityFlags") or [])},"features":_compact_features(value["features"],value["rules"],value["agent"]),"layers":{"structure":value["rules"]["structure"],"trend":value["rules"]["trend"],"agent":value["agent"]},"chartSetup":{"alwaysOn":["volume-profile","volume"],"recommended":recommended_indicators(value["features"])},"commentary":commentary,"buildContext":{"higherTf":self._stored_higher_summaries(symbol,[interval]) or None,"flags":[]}}

    def _degraded_data_asset(self,symbol,interval,rows,coverage,input_digest):
        generated=utc_now_iso(); placeholder=rows[-1]["timestamp"] if rows else generated; window_rows=rows[-DISPLAY_BARS[interval]:]
        empty=_empty_agent_layer("data_insufficient")
        asset={"assetVersion":ASSET_VERSION,"kernelVersion":KERNEL_VERSION,"qualityPolicyVersion":QUALITY_POLICY_VERSION,"promptVersion":PROMPT_VERSION_V2,"modelPolicyVersion":MODEL_POLICY_VERSION,"symbol":symbol,"interval":interval,"asOf":placeholder,"generatedAt":generated,"status":"degraded","window":{"displayFrom":window_rows[0]["timestamp"] if window_rows else placeholder,"displayTo":placeholder,"displayBars":DISPLAY_BARS[interval],"lookbackBars":LOOKBACK_BARS[interval]},"coverage":coverage,"input":{"digest":input_digest,"canonicalDataVersion":CANONICAL_DATA_VERSION,"sessionPolicy":SESSION_POLICY,"adjustmentPolicy":ADJUSTMENT_POLICY,"candleContractVersion":CANDLE_CONTRACT_VERSION},"build":{"ruleDigest":_digest({}),"contextDigest":_digest({}),"buildIntentDigest":_digest({"data":"insufficient"}),"assetContentDigest":"sha256:"+"0"*64,"llmMode":"rule_only","agentPreservationPolicy":AGENT_PRESERVATION_POLICY,"agentOutcome":"not_requested_empty"},"quality":{"state":"stale_input" if "stale_input" in coverage.get("qualityFlags",[]) else "insufficient_data","score":0,"reasons":[],"penalties":coverage.get("qualityFlags",[])},"features":{"pivots":[],"levels":[],"trends":[],"events":[],"fibCandidates":[],"vp":{},"regime":{}},"layers":{"structure":_empty_rule_layer("data_insufficient"),"trend":_empty_rule_layer("data_insufficient"),"agent":empty},"chartSetup":{"alwaysOn":["volume-profile","volume"],"recommended":[]},"commentary":{"headline":"분석 데이터가 충분하지 않습니다.","regimeSummary":"","focusItems":[],"keyLevelsV2":[],"higherTimeframeContext":"","counterEvidence":[],"dataCaveats":coverage.get("qualityFlags",[]),"confidenceV2":{"selection":{"score":0,"reasons":[],"penalties":coverage.get("qualityFlags",[])},"marketDirection":{"score":None,"reasons":[],"penalties":[]}},"text":"분석 데이터 갱신 후 다시 확인하세요.","keyLevels":[],"invalidation":"데이터 갱신이 필요합니다.","confidence":0,"enrichment":None},"buildContext":{"higherTf":None,"flags":["data_insufficient"]}}
        asset["build"]["assetContentDigest"]=_asset_content_digest(asset); return asset

    def _stored_higher_summaries(self,symbol,intervals):
        result={}
        needed=set();
        requested=set(intervals)
        if "1D" in requested:needed.update({"1M","1W"}.difference(requested))
        if "1W" in requested:needed.update({"1M"}.difference(requested))
        for source in needed:
            asset=self.storage.get(symbol,source)
            if asset and asset.get("assetVersion")=="v2" and asset.get("quality",{}).get("state")=="eligible":result[source]={"asOf":asset.get("asOf"),"ruleDigest":asset.get("build",{}).get("ruleDigest"),"regime":asset.get("features",{}).get("regime",{})}
        return result


def _fast_noop(asset,input_digest,llm_mode,requested_model,pre_kernel_digest):
    if not asset or asset.get("assetVersion")!="v2" or asset.get("input",{}).get("digest")!=input_digest or asset.get("kernelVersion")!=KERNEL_VERSION or asset.get("qualityPolicyVersion")!=QUALITY_POLICY_VERSION:return False
    build=asset.get("build",{}); outcome=build.get("agentOutcome")
    eligible=outcome in ({"ready","ready_empty"} if llm_mode=="curate" else {"not_requested_empty","preserved"})
    return eligible and build.get("llmMode")==llm_mode and build.get("preKernelDigest")==pre_kernel_digest and (llm_mode!="curate" or build.get("requestedModel")==requested_model)
def _build_intent_digest(*,rule_digest,context_digest,requested_model,llm_mode):
    return _digest({"ruleDigest":rule_digest,"contextDigest":context_digest,"promptVersion":PROMPT_VERSION_V2,"modelPolicyVersion":MODEL_POLICY_VERSION,"requestedModel":requested_model,"assemblerVersion":ASSEMBLER_VERSION,"llmMode":llm_mode,"agentPreservationPolicy":AGENT_PRESERVATION_POLICY})
def _late_intent_noop(asset,intent_digest,llm_mode):
    if not asset or asset.get("assetVersion")!="v2":return False
    build=asset.get("build",{}); outcome=build.get("agentOutcome")
    eligible=outcome in ({"ready","ready_empty"} if llm_mode=="curate" else {"not_requested_empty","preserved"})
    return eligible and build.get("llmMode")==llm_mode and build.get("buildIntentDigest")==intent_digest
def _preserved_agent(asset,input_digest,palette):
    if not asset or asset.get("assetVersion")!="v2" or asset.get("input",{}).get("digest")!=input_digest or asset.get("kernelVersion")!=KERNEL_VERSION:return None
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
    levels=[_project(item,("id","price","zoneLow","zoneHigh","score","touches","lastTestAt","lastTouchAgeBars","currentDistanceAtr","role","state","vpConfluence","memberPivotIds")) for item in features.get("levels",[]) if item.get("id") in candidate_ids][:4]
    trends=[_project(item,("id","kind","anchorPivotIds","touchPivotIds","touches","slopePerBar","slopeAtrPerBar","currentDistanceAtr","lastTouchAgeBars","spanBars","medianResidualAtr","violationCount","rangeFrom","rangeTo","rangeHigh","rangeLow","score")) for item in features.get("trends",[]) if item.get("id") in candidate_ids][:2]
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
