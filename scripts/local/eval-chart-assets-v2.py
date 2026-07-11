#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "systems/market-data/shared", ROOT / "systems/order/shared", ROOT / "systems/agent-orchestration/shared", ROOT / "systems/api-server/pods/api-server/gops-backend"):
    if str(path) not in sys.path: sys.path.insert(0, str(path))

from alfaka.analytics import DISPLAY_BARS, compute_feature_pack  # noqa: E402
from alfaka.analytics.analysis_candles import (  # noqa: E402
    aggregate_analysis_candle_bundle,
    aggregate_analysis_candles,
    analysis_input_digest,
)
from gops_agents.chart_assets.commentary_v2 import assemble_commentary_v2  # noqa: E402
from gops_agents.chart_assets.compilers import compile_rule_layers  # noqa: E402
from gops_agents.chart_assets.curation import build_interval_palette, build_symbol_bundle, materialize_curation  # noqa: E402
from gops_agents.chart_assets.llm import ChartAssetLLMService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="systems/market-data/tests/fixtures/chart_assets_v2/manifest.json")
    parser.add_argument("--symbols")
    parser.add_argument("--episode-ids", help="comma-separated exact episode IDs")
    parser.add_argument("--intervals", default="1M,1W,1D")
    parser.add_argument("--mode", choices=("rules", "integration", "llm-canary"), required=True)
    parser.add_argument("--stratified-limit", type=int, default=0)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--env-file")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.env_file: load_env(ROOT / args.env_file)
    manifest_path = ROOT / args.manifest
    if not manifest_path.exists():
        print(f"manifest not found: {manifest_path}", file=sys.stderr); return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if args.mode == "integration":
        return run_integration(args)
    episodes = list(manifest.get("episodes") or [])
    if args.symbols:
        selected = {item.strip().upper() for item in args.symbols.split(",") if item.strip()}
        episodes = [item for item in episodes if item["symbol"] in selected]
    if args.episode_ids:
        selected_episode_ids = {item.strip() for item in args.episode_ids.split(",") if item.strip()}
        episodes = [item for item in episodes if item["episodeId"] in selected_episode_ids]
        missing_episode_ids = selected_episode_ids.difference(item["episodeId"] for item in episodes)
        if missing_episode_ids:
            print(f"unknown episode IDs: {','.join(sorted(missing_episode_ids))}", file=sys.stderr)
            return 2
    if args.stratified_limit: episodes = stratified(episodes, args.stratified_limit)
    intervals = tuple(item for item in args.intervals.split(",") if item in {"1D","1W","1M"})
    results=[]; invariant_failures=[]; stability_failures=[]; latencies=[]; bundle_compute_latencies=[]; calls=0; interval_latencies=[]; review_index=[]; drawing_reviews=[]
    interval_latency_samples={interval:[] for interval in intervals}; aggregation_latencies=[]
    llm = ChartAssetLLMService() if args.mode == "llm-canary" else None
    for episode in episodes:
        expected_by_interval = normalize_expected_by_interval(episode)
        raw = json.loads((manifest_path.parent / episode["series"]).read_text(encoding="utf-8"))
        raw = [row for row in raw if row["timestamp"] <= episode["asOf"]]
        episode_started=time.perf_counter()
        aggregation_started=time.perf_counter()
        aggregated = aggregate_analysis_candle_bundle(
            raw,
            intervals,
            now=_after_asof(episode["asOf"]),
        )
        aggregation_share_ms = (
            (time.perf_counter() - aggregation_started) * 1000 / max(1, len(intervals))
        )
        aggregation_latencies.append(aggregation_share_ms * len(intervals))
        palettes={}; rules_by_interval={}; rows_by_interval={}
        interval_states = {
            interval: {"evaluated": False, "qualityState": "unavailable", "emptyReasons": []}
            for interval in intervals
        }
        for interval in intervals:
            interval_started=time.perf_counter()
            rows=aggregated[interval]
            if len(rows)<20: continue
            features=compute_feature_pack(rows,interval); generated="2026-07-11T00:00:00.000Z"
            rules=compile_rule_layers(symbol=episode["symbol"],interval=interval,features=features,candles=rows,generated_at=generated)
            digest=analysis_input_digest(episode["symbol"],interval,rows)
            palette=build_interval_palette(symbol=episode["symbol"],interval=interval,input_digest=digest,features=features,rule_layers=rules,candles=rows,generated_at=generated)
            palettes[interval]=palette; rules_by_interval[interval]=rules; rows_by_interval[interval]=rows
            interval_states[interval] = interval_evaluation_state(rules)
            invariant_failures.extend(check_rule_invariants(episode["episodeId"],rows,rules,features))
            interval_elapsed=(time.perf_counter()-interval_started)*1000 + aggregation_share_ms
            interval_latencies.append(interval_elapsed)
            interval_latency_samples[interval].append(interval_elapsed)
        bundle=build_symbol_bundle(episode["symbol"],list(palettes.values()))
        bundle_compute_latencies.append((time.perf_counter()-episode_started)*1000)
        call_result=None; drawing_count=sum(len(layer["drawings"]) for rules in rules_by_interval.values() for layer in rules.values())
        if llm and palettes:
            repeated=[llm.curate_symbol(bundle) for _ in range(max(1,args.repeat))]; calls+=len(repeated);call_result=repeated[0]
            visual_signatures=[_visual_selection_signature(item) for item in repeated]
            stable=len(set(visual_signatures))==1 and not any(item.get("degraded") for item in repeated)
            if not stable:stability_failures.append({"episodeId":episode["episodeId"],"reason":"visual_selection_unstable","repeatCount":len(repeated)})
            layers=materialize_curation(symbol=episode["symbol"],palettes=palettes,output=call_result["output"],generated_at="2026-07-11T00:00:00.000Z",model=call_result.get("model"))
            for interval,palette in palettes.items():
                selection=next((item for item in call_result["output"]["intervalSelections"] if item["interval"]==interval),None)
                commentary=assemble_commentary_v2(interval=interval,palette=palette,rule_layers=rules_by_interval[interval],agent_layer=layers[interval],curation_selection=selection)
                drawing_count+=len(layers[interval]["drawings"])
                invariant_failures.extend(check_focus_invariant(episode["episodeId"],rules_by_interval[interval],layers[interval],commentary))
        for interval,rules in rules_by_interval.items():
            commentary=assemble_commentary_v2(interval=interval,palette=palettes[interval],rule_layers=rules,agent_layer=_empty_agent_layer(),curation_selection=None)
            card,reviews=build_review_card(episode,interval,rows_by_interval[interval],rules,commentary)
            review_index.append(card); drawing_reviews.extend(reviews)
        elapsed=(time.perf_counter()-episode_started)*1000; latencies.append(elapsed)
        per_interval={interval:sum(len(layer["drawings"]) for layer in rules_by_interval[interval].values()) for interval in rules_by_interval}
        results.append({"episodeId":episode["episodeId"],"symbol":episode["symbol"],"asOf":episode["asOf"],"split":episode["split"],"evaluationRound":episode.get("evaluationRound"),"category":episode["category"],"expectation":episode["expectation"],"expectedSemanticTypes":list(episode.get("expectedSemanticTypes") or []),"expectedByInterval":expected_by_interval,"drawingCount":drawing_count,"drawingCounts":per_interval,"drawingSemantics":{interval:sorted({drawing_semantic_type(drawing) for layer in rules_by_interval[interval].values() for drawing in layer["drawings"]}) for interval in rules_by_interval},"topRejectionReasons":{interval:_top_rejection_reasons(rules_by_interval[interval]) for interval in rules_by_interval},"intervalStates":interval_states,"intervals":sorted(palettes),"candidateCount":sum(len(item["visualCandidates"]) for item in palettes.values()),"llm":{"degraded":call_result.get("degraded"),"reason":call_result.get("reason"),"model":call_result.get("model"),"usage":call_result.get("usage"),"latencyMs":call_result.get("latencyMs"),"repeatCount":max(1,args.repeat),"stable":stable,"selected":{item["interval"]:item["selectedCandidateIds"] for item in call_result["output"]["intervalSelections"]}} if call_result else None,"kernelMs":round(elapsed,3)})
    drawing_counts=[count for item in results for count in item["drawingCounts"].values()]
    quality=quality_summary(drawing_reviews,results,intervals)
    benchmark=benchmark_kernels(manifest_path,manifest,intervals) if args.mode=="rules" and not args.stratified_limit else None
    llm_latencies=[float(item["llm"]["latencyMs"]) for item in results if item.get("llm") and item["llm"].get("latencyMs") is not None]
    metrics={"kernelP50Ms":round(statistics.median(interval_latencies),3) if interval_latencies else None,"kernelP95Ms":round(percentile(interval_latencies,.95),3) if interval_latencies else None,"kernelP95ByIntervalMs":{interval:round(percentile(values,.95),3) if values else None for interval,values in interval_latency_samples.items()},"aggregationP95Ms":round(percentile(aggregation_latencies,.95),3) if aggregation_latencies else None,"symbolBundleP95Ms":round(percentile(bundle_compute_latencies,.95),3) if bundle_compute_latencies else None,"endToEndP95Ms":round(percentile(latencies,.95),3) if latencies else None,"llmLatencyP95Ms":round(percentile(llm_latencies,.95),3) if llm_latencies else None,"drawingMedian":statistics.median(drawing_counts) if drawing_counts else None,"drawingP95":percentile(drawing_counts,.95) if drawing_counts else None}
    metric_gates=metric_quality_gates(metrics)
    report={"mode":args.mode,"fixtureVersion":manifest.get("fixtureVersion"),"episodeCount":len(results),"llmCalls":calls,"invariantFailures":invariant_failures,"stabilityFailures":stability_failures,"environment":{"python":platform.python_version(),"machine":platform.machine(),"processor":platform.processor() or "unknown","singleProcess":True,"singleThreadBenchmark":True},"metrics":{**metrics,"gates":metric_gates,"passed":all(metric_gates.values())},"benchmark":benchmark,"quality":{"label":"automated reviewer estimate","humanGate":"pending",**quality},"reviewIndex":{"blinding":"implementation version is omitted from review cards","reviewers":["evidence-gate-v1","recency-conservative-v1"],"cards":review_index},"results":results}
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({key:report[key] for key in ("mode","episodeCount","llmCalls","metrics")},ensure_ascii=False))
    if invariant_failures:
        print(f"invariant failures: {len(invariant_failures)}",file=sys.stderr); return 1
    if args.mode=="llm-canary" and any(item["llm"] and item["llm"]["degraded"] for item in results): return 1
    if stability_failures:return 1
    return 0


def check_rule_invariants(episode_id,rows,rules,features):
    times={row["timestamp"] for row in rows}; failures=[]
    for layer in rules.values():
        for drawing in layer["drawings"]:
            for anchor in drawing.get("anchors",[]):
                if anchor.get("timestamp") and anchor["timestamp"] not in times:failures.append({"episodeId":episode_id,"reason":"anchor_not_exact","drawingId":drawing["id"]})
            if drawing["type"]=="horizontalLine" and any(token in str(drawing.get("label") or "") for token in price_tokens(drawing["anchors"][0]["price"])):failures.append({"episodeId":episode_id,"reason":"hline_price_label","drawingId":drawing["id"]})
    for trend in features.get("trends",[]):
        if trend.get("hardPass") and trend.get("kind") in {"up","down","channel"} and int(trend.get("touches") or 0)<3:failures.append({"episodeId":episode_id,"reason":"trend_touch_gate","candidateId":trend["id"]})
    return failures


def check_focus_invariant(episode_id,rules,agent,commentary):
    accepted={drawing["id"] for layer in (*rules.values(),agent) for drawing in layer["drawings"]}; focused={drawing for item in commentary["focusItems"] for drawing in item["drawingIds"]}
    return [] if accepted==focused else [{"episodeId":episode_id,"reason":"focus_coverage","missing":sorted(accepted-focused),"unknown":sorted(focused-accepted)}]


def _visual_selection_signature(result):
    selections=result.get("output",{}).get("intervalSelections",[])
    return json.dumps([[item.get("interval"),sorted(item.get("selectedCandidateIds") or [])] for item in sorted(selections,key=lambda value:value.get("interval", ""))],separators=(",",":"))


def _empty_agent_layer():
    return {"drawings": [], "selected": [], "emptyReason": "rules_evaluation", "meta": {}}


def normalize_expected_by_interval(episode: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate optional interval labels without deriving them from legacy labels."""
    raw = episode.get("expectedByInterval")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{episode.get('episodeId')}: expectedByInterval must be an object")
    normalized: dict[str, dict[str, Any]] = {}
    for interval, label in raw.items():
        if interval not in {"1D", "1W", "1M"}:
            raise ValueError(f"{episode.get('episodeId')}: unsupported expected interval {interval!r}")
        if not isinstance(label, dict):
            raise ValueError(f"{episode.get('episodeId')}:{interval}: expected label must be an object")
        expectation = label.get("expectation")
        if expectation not in {"must_draw", "must_not_draw", "may_draw"}:
            raise ValueError(f"{episode.get('episodeId')}:{interval}: invalid expectation {expectation!r}")
        semantics = label.get("semanticTypes")
        alias = label.get("expectedSemanticTypes")
        if semantics is not None and alias is not None and semantics != alias:
            raise ValueError(f"{episode.get('episodeId')}:{interval}: semantic label aliases disagree")
        semantics = semantics if semantics is not None else alias
        if semantics is None:
            semantics = []
        if not isinstance(semantics, list) or any(not isinstance(item, str) or not item for item in semantics):
            raise ValueError(f"{episode.get('episodeId')}:{interval}: semanticTypes must be a string array")
        if expectation == "must_not_draw" and semantics:
            raise ValueError(f"{episode.get('episodeId')}:{interval}: must_not_draw cannot require semantics")
        normalized[interval] = {
            "expectation": expectation,
            "semanticTypes": sorted(set(semantics)),
        }
    return normalized


def interval_evaluation_state(rules: dict[str, Any]) -> dict[str, Any]:
    empty_reasons = sorted({
        str(layer["emptyReason"])
        for layer in rules.values()
        if layer.get("emptyReason")
    })
    degraded = any(
        (layer.get("meta") or {}).get("qualityState") == "data_degraded"
        or layer.get("emptyReason") == "data_quality_blocked"
        for layer in rules.values()
    )
    return {
        "evaluated": True,
        "qualityState": "data_degraded" if degraded else "ready",
        "emptyReasons": empty_reasons,
    }


def drawing_semantic_type(drawing: dict[str, Any]) -> str:
    drawing_type = drawing.get("type")
    if drawing_type == "horizontalLine":
        return "level"
    if drawing_type == "flagMarker":
        return "event"
    if drawing_type in {"trendLine", "trendParallelLines"}:
        return "trend"
    if drawing_type == "rangeBox":
        return "range"
    if drawing_type == "fibonacciRetracement":
        return "retracement"
    return "other"


def _top_rejection_reasons(rules: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for layer in rules.values():
        for reason, count in ((layer.get("meta") or {}).get("rejectedByReason") or {}).items():
            counts[str(reason)] = counts.get(str(reason), 0) + int(count or 0)
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:8])


def metric_quality_gates(metrics: dict[str, Any]) -> dict[str, bool]:
    return {
        "drawingMedianAtMost2": metrics.get("drawingMedian") is not None and metrics["drawingMedian"] <= 2,
        "drawingP95AtMost4": metrics.get("drawingP95") is not None and metrics["drawingP95"] <= 4,
        "kernelP95AtMost130Ms": metrics.get("kernelP95Ms") is not None and metrics["kernelP95Ms"] <= 130,
        "symbolBundleP95AtMost350Ms": metrics.get("symbolBundleP95Ms") is not None and metrics["symbolBundleP95Ms"] <= 350,
    }


def build_review_card(episode, interval, rows, rules, commentary):
    display_from = rows[-min(DISPLAY_BARS[interval], len(rows))]["timestamp"]
    focus = {drawing_id: item for item in commentary["focusItems"] for drawing_id in item["drawingIds"]}
    selected = {
        drawing_id: item
        for layer in rules.values()
        for item in layer.get("selected", [])
        for drawing_id in item.get("drawingIds", [])
    }
    drawings = [drawing for layer in rules.values() for drawing in layer["drawings"]]
    reviews = []
    scored = []
    for drawing in drawings:
        evidence = selected.get(drawing["id"], {})
        scores = automated_scores(drawing, evidence, focus.get(drawing["id"]), rows)
        offscreen = any(anchor.get("timestamp", display_from) < display_from for anchor in drawing.get("anchors", []))
        meaningful = all(
            review["structureEvidence"] >= 4 and review["currentRelevance"] >= 4 and review["geometryAccuracy"] >= 4
            for review in scores.values()
        )
        clearly_meaningless = any(min(review.values()) == 1 for review in scores.values())
        record = {
            "episodeId": episode["episodeId"], "split": episode["split"], "evaluationRound": episode.get("evaluationRound"), "expectation": episode["expectation"],
            "interval": interval, "drawingId": drawing["id"], "drawingType": drawing["type"],
            "semanticType": drawing_semantic_type(drawing),
            "offscreen": offscreen, "meaningful": meaningful, "clearlyMeaningless": clearly_meaningless,
            "scores": scores,
        }
        reviews.append(record); scored.append(record)
    return ({
        "reviewId": "review-" + hashlib.sha256(f"{episode['episodeId']}|{interval}".encode()).hexdigest()[:12],
        "episodeId": episode["episodeId"], "symbol": episode["symbol"], "interval": interval,
        "asOf": episode["asOf"], "expectation": episode["expectation"], "displayFrom": display_from,
        "variants": [_review_variant("candidate", drawings, scored)],
        "focusItems": commentary["focusItems"],
    }, reviews)


def _review_variant(name, drawings, scores):
    score_by_id = {item["drawingId"]: item for item in scores}
    return {
        "variantId": name,
        "drawings": [{"id": item["id"], "type": item["type"], "anchors": item.get("anchors", []), "label": item.get("label")} for item in drawings],
        "automatedScores": [score_by_id[item["id"]] for item in drawings if item["id"] in score_by_id],
    }


def automated_scores(drawing, selected, focus, rows):
    quality = selected.get("quality") or {}
    touches = int(quality.get("touchEpisodes") or quality.get("touches") or 0)
    distance = float(quality.get("currentDistanceAtr") if quality.get("currentDistanceAtr") is not None else 0)
    age = int(quality.get("lastTouchAgeBars") or quality.get("ageBars") or 0)
    kind = drawing.get("type")
    exact = all(not anchor.get("timestamp") or any(row["timestamp"] == anchor["timestamp"] for row in rows) for anchor in drawing.get("anchors", []))
    if kind == "horizontalLine":
        structure_a = 5 if touches >= 5 else 4 if touches >= 3 else 3
        structure_b = 5 if touches >= 6 else 4 if touches >= 3 else 3
        relevance_a = 5 if distance <= 1.5 else 4 if distance <= 3 else 3
        relevance_b = 5 if distance <= 1 else 4 if distance <= 2 else 3
    elif kind in {"trendLine", "trendParallelLines"}:
        structure_a = 5 if touches >= 4 else 4 if touches >= 3 else 1
        structure_b = 5 if touches >= 5 else 4 if touches >= 3 else 1
        relevance_a = 5 if distance <= 1 else 4 if distance <= 2.5 and age <= 24 else 3
        relevance_b = 5 if distance <= .75 else 4 if distance <= 2.25 and age <= 18 else 3
    elif kind == "rangeBox":
        structure_a = 5 if touches >= 8 else 4 if touches >= 4 else 3
        structure_b = 5 if touches >= 10 else 4 if touches >= 6 else 3
        relevance_a = 5 if distance <= .5 else 4 if distance <= 1.5 else 3
        relevance_b = 5 if distance <= .25 else 4 if distance <= 1 else 3
    elif kind == "flagMarker":
        structure_a = structure_b = 4
        if quality.get("currentImpact") == "high":
            relevance_a = relevance_b = 5
        else:
            relevance_a = 5 if age <= 5 else 4 if age <= 15 else 3
            relevance_b = 5 if age <= 3 else 4 if age <= 10 else 3
    else:
        structure_a = structure_b = 4 if selected else 2
        relevance_a = relevance_b = 4 if selected else 2
    geometry = 5 if exact else 1
    commentary = 5 if focus and focus.get("confirmation") and focus.get("invalidation") else 2
    return {
        "evidence-gate-v1": {"structureEvidence": structure_a, "currentRelevance": relevance_a, "geometryAccuracy": geometry, "nonDuplication": 5, "commentaryUsefulness": commentary},
        "recency-conservative-v1": {"structureEvidence": structure_b, "currentRelevance": relevance_b, "geometryAccuracy": geometry, "nonDuplication": 5, "commentaryUsefulness": commentary},
    }


def quality_summary(drawing_reviews, results, requested_intervals=("1M", "1W", "1D")):
    rounds = [item.get("evaluationRound") for item in results if item.get("evaluationRound")]
    active_round = rounds[-1] if rounds else None
    holdout = [item for item in drawing_reviews if item["split"] == "holdout" and (not active_round or item.get("evaluationRound") == active_round)]
    active_results = [
        item for item in results
        if item["split"] == "holdout" and (not active_round or item.get("evaluationRound") == active_round)
    ]
    meaningful = [item for item in holdout if item["meaningful"]]
    meaningless = [item for item in holdout if item["clearlyMeaningless"]]
    offscreen = [item for item in holdout if item["offscreen"]]
    offscreen_relevant = [item for item in offscreen if all(score["currentRelevance"] >= 4 for score in item["scores"].values())]
    by_episode = {}
    for item in holdout: by_episode.setdefault(item["episodeId"], []).append(item)
    must_draw = [item for item in active_results if item["expectation"] == "must_draw"]
    must_not = [item for item in active_results if item["expectation"] == "must_not_draw"]
    recalled = sum(any(review["meaningful"] for review in by_episode.get(item["episodeId"], [])) for item in must_draw)
    unnecessary = sum(any(not review["meaningful"] for review in by_episode.get(item["episodeId"], [])) for item in must_not)
    precision = _ratio(len(meaningful), len(holdout))
    meaningless_rate = _ratio(len(meaningless), len(holdout))
    offscreen_rate = _ratio(len(offscreen_relevant), len(offscreen))
    recall = _ratio(recalled, len(must_draw))
    unnecessary_rate = _ratio(unnecessary, len(must_not))

    meaningful_by_episode_interval: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for review in meaningful:
        meaningful_by_episode_interval.setdefault((review["episodeId"], review["interval"]), []).append(review)

    requested = tuple(dict.fromkeys(requested_intervals))
    requested_set = set(requested)
    explicit_labels: list[dict[str, Any]] = []
    out_of_scope_labels = 0
    for result in active_results:
        for interval, expected in (result.get("expectedByInterval") or {}).items():
            if interval not in requested_set:
                out_of_scope_labels += 1
                continue
            current_reviews = meaningful_by_episode_interval.get((result["episodeId"], interval), [])
            state = (result.get("intervalStates") or {}).get(interval) or {
                "evaluated": False, "qualityState": "unavailable",
            }
            explicit_labels.append({
                "episodeId": result["episodeId"],
                "interval": interval,
                "expectation": expected["expectation"],
                "semanticTypes": list(expected.get("semanticTypes") or []),
                "recalled": bool(current_reviews),
                "meaningfulSemantics": {review["semanticType"] for review in current_reviews},
                "drawingCount": int((result.get("drawingCounts") or {}).get(interval, 0)),
                "qualityState": state.get("qualityState") or "unavailable",
            })

    explicit_must_draw = [item for item in explicit_labels if item["expectation"] == "must_draw"]
    explicit_must_not = [item for item in explicit_labels if item["expectation"] == "must_not_draw"]
    interval_recall = _ratio(sum(item["recalled"] for item in explicit_must_draw), len(explicit_must_draw))
    interval_recall_by_interval = {
        interval: _ratio(
            sum(item["recalled"] for item in explicit_must_draw if item["interval"] == interval),
            sum(item["interval"] == interval for item in explicit_must_draw),
        )
        for interval in requested
    }
    ready_must_draw = [item for item in explicit_must_draw if item["qualityState"] == "ready"]
    false_zero = [item for item in ready_must_draw if item["drawingCount"] == 0]
    false_zero_rate = _ratio(len(false_zero), len(ready_must_draw))
    false_zero_by_interval = {
        interval: _ratio(
            sum(item["drawingCount"] == 0 for item in ready_must_draw if item["interval"] == interval),
            sum(item["interval"] == interval for item in ready_must_draw),
        )
        for interval in requested
    }
    explicit_unnecessary_rate = _ratio(
        sum(item["drawingCount"] > 0 for item in explicit_must_not),
        len(explicit_must_not),
    )
    explicit_unnecessary_by_interval = {
        interval: _ratio(
            sum(item["drawingCount"] > 0 for item in explicit_must_not if item["interval"] == interval),
            sum(item["interval"] == interval for item in explicit_must_not),
        )
        for interval in requested
    }

    semantic_labels = [
        {
            "interval": item["interval"],
            "semanticType": semantic,
            "recalled": semantic in item["meaningfulSemantics"],
        }
        for item in explicit_must_draw
        for semantic in item["semanticTypes"]
    ]
    semantic_recall = _ratio(sum(item["recalled"] for item in semantic_labels), len(semantic_labels))
    semantic_recall_by_interval = {
        interval: _ratio(
            sum(item["recalled"] for item in semantic_labels if item["interval"] == interval),
            sum(item["interval"] == interval for item in semantic_labels),
        )
        for interval in requested
    }
    semantic_names = sorted({item["semanticType"] for item in semantic_labels})
    semantic_recall_by_semantic = {
        semantic: _ratio(
            sum(item["recalled"] for item in semantic_labels if item["semanticType"] == semantic),
            sum(item["semanticType"] == semantic for item in semantic_labels),
        )
        for semantic in semantic_names
    }
    interval_semantic_recall: dict[str, dict[str, Any]] = {}
    for interval in requested:
        current_semantics = sorted({item["semanticType"] for item in semantic_labels if item["interval"] == interval})
        interval_semantic_recall[interval] = {
            semantic: _ratio(
                sum(item["recalled"] for item in semantic_labels if item["interval"] == interval and item["semanticType"] == semantic),
                sum(item["interval"] == interval and item["semanticType"] == semantic for item in semantic_labels),
            )
            for semantic in current_semantics
        }

    legacy_semantic_labels = [
        {
            "semanticType": semantic,
            "recalled": any(
                review["meaningful"] and review["semanticType"] == semantic
                for review in by_episode.get(result["episodeId"], [])
            ),
        }
        for result in must_draw
        for semantic in result.get("expectedSemanticTypes") or []
    ]
    legacy_semantic_names = sorted({item["semanticType"] for item in legacy_semantic_labels})
    legacy_semantic_recall = {
        "overall": _ratio(sum(item["recalled"] for item in legacy_semantic_labels), len(legacy_semantic_labels)),
        "bySemantic": {
            semantic: _ratio(
                sum(item["recalled"] for item in legacy_semantic_labels if item["semanticType"] == semantic),
                sum(item["semanticType"] == semantic for item in legacy_semantic_labels),
            )
            for semantic in legacy_semantic_names
        },
    }

    interval_label_coverage = bool(requested) and all(
        interval_recall_by_interval[interval]["denominator"] > 0 for interval in requested
    )
    semantic_label_coverage = bool(requested) and all(
        semantic_recall_by_interval[interval]["denominator"] > 0 for interval in requested
    )
    interval_semantic_cells = [
        ratio
        for current in interval_semantic_recall.values()
        for ratio in current.values()
    ]
    denominators = {
        "mustDrawEpisodes":len(must_draw),"mustNotDrawEpisodes":len(must_not),"finalDrawings":len(holdout),"offscreenDrawings":len(offscreen),
        "explicitIntervalLabels": len(explicit_labels),
        "explicitMustDrawIntervalLabels": len(explicit_must_draw),
        "explicitMustNotDrawIntervalLabels": len(explicit_must_not),
        "explicitSemanticLabels": len(semantic_labels),
        "readyMustDrawIntervalLabels": len(ready_must_draw),
        "unavailableMustDrawIntervalLabels": sum(item["qualityState"] == "unavailable" for item in explicit_must_draw),
        "dataDegradedMustDrawIntervalLabels": sum(item["qualityState"] == "data_degraded" for item in explicit_must_draw),
        "outOfScopeIntervalLabels": out_of_scope_labels,
    }
    gates = {
        "minimumDenominators": denominators["explicitMustDrawIntervalLabels"]>=30 and denominators["explicitMustNotDrawIntervalLabels"]>=20 and denominators["finalDrawings"]>=40 and denominators["offscreenDrawings"]>=20,
        "precision85": precision["point"] is not None and precision["point"]>=.85,
        "clearlyMeaningless5": meaningless_rate["point"] is not None and meaningless_rate["point"]<=.05,
        "offscreenRelevant95": len(offscreen)>=20 and offscreen_rate["point"]>=.95,
        "mustNotDraw10": explicit_unnecessary_rate["point"] is not None and explicit_unnecessary_rate["point"]<=.10,
        "mustDrawRecall60": recall["point"] is not None and recall["point"]>=.60,
        "mustDrawRecall85": recall["point"] is not None and recall["point"]>=.85,
        "explicitIntervalLabelCoverage": interval_label_coverage,
        "explicitSemanticLabelCoverage": semantic_label_coverage,
        "explicitIntervalRecall85": interval_recall["point"] is not None and interval_recall["point"]>=.85,
        "perIntervalRecall80": interval_label_coverage and all(
            item["point"] is not None and item["point"] >= .80
            for item in interval_recall_by_interval.values()
        ),
        "explicitSemanticRecall85": semantic_recall["point"] is not None and semantic_recall["point"]>=.85,
        "intervalSemanticRecall80": semantic_label_coverage and bool(interval_semantic_cells) and all(
            item["point"] is not None and item["point"] >= .80
            for item in interval_semantic_cells
        ),
        "readyFalseZero15": false_zero_rate["point"] is not None and false_zero_rate["point"]<=.15,
    }
    return {
        "evaluationRound":active_round,"denominators":denominators,"precision":precision,"clearlyMeaninglessRate":meaningless_rate,"offscreenCurrentRelevance":offscreen_rate,"mustNotDrawUnnecessaryRate":unnecessary_rate,"mustDrawRecall":recall,
        "explicitIntervalEvaluation": {
            "labelContract": "expectedByInterval only; legacy expectation is never inferred per interval",
            "requestedIntervals": list(requested),
            "overallRecall": interval_recall,
            "recallByInterval": interval_recall_by_interval,
            "readyFalseZeroRate": false_zero_rate,
            "readyFalseZeroRateByInterval": false_zero_by_interval,
            "mustNotDrawUnnecessaryRate": explicit_unnecessary_rate,
            "mustNotDrawUnnecessaryRateByInterval": explicit_unnecessary_by_interval,
        },
        "explicitSemanticEvaluation": {
            "labelContract": "semanticTypes (or expectedSemanticTypes alias) inside expectedByInterval",
            "overallRecall": semantic_recall,
            "recallByInterval": semantic_recall_by_interval,
            "recallBySemantic": semantic_recall_by_semantic,
            "recallByIntervalSemantic": interval_semantic_recall,
        },
        "legacyExpectedSemanticEvaluation": legacy_semantic_recall,
        "labelCoverage": {
            "allRequestedIntervalsHaveMustDrawLabels": interval_label_coverage,
            "allRequestedIntervalsHaveSemanticLabels": semantic_label_coverage,
        },
        "gates":gates,"passed":all(gates.values()),
    }


def _ratio(numerator, denominator):
    if not denominator: return {"numerator":numerator,"denominator":denominator,"point":None,"wilson95":None}
    point=numerator/denominator; z=1.959963984540054; scale=1+z*z/denominator
    center=(point+z*z/(2*denominator))/scale
    margin=z*math.sqrt(point*(1-point)/denominator+z*z/(4*denominator*denominator))/scale
    return {"numerator":numerator,"denominator":denominator,"point":round(point,6),"wilson95":[round(max(0,center-margin),6),round(min(1,center+margin),6)]}


def benchmark_kernels(manifest_path, manifest, intervals):
    series = max(manifest["series"], key=lambda item:item["bars"])
    raw = json.loads((manifest_path.parent / series["file"]).read_text(encoding="utf-8"))[-500:]
    interval = "1D" if "1D" in intervals else intervals[-1]
    rows = aggregate_analysis_candles(raw, interval, now=_after_asof(raw[-1]["timestamp"]))
    generated = "2026-07-11T00:00:00.000Z"
    def run():
        features=compute_feature_pack(rows,interval)
        compile_rule_layers(symbol=series["symbol"],interval=interval,features=features,candles=rows,generated_at=generated)
    for _ in range(5): run()
    values=[]
    for _ in range(30):
        started=time.perf_counter();run();values.append((time.perf_counter()-started)*1000)
    return {"current":{"p50Ms":round(statistics.median(values),3),"p95Ms":round(percentile(values,.95),3),"runs":30},"baselineScope":"canonical current compiler; compare reports across commits instead of retaining a runtime legacy compiler"}


def run_integration(args):
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from app.main import create_app
    from gops_agents.chart_assets import builder as builder_module
    from gops_agents.chart_assets.builder import ChartAssetBuilder
    from gops_agents.chart_assets.candles import ChartAssetCandleLoader
    from gops_agents.chart_assets.envelope import ChartAssetBuildEnvelope
    from gops_agents.chart_assets.progress import RedisChartAssetProgressStore, STATUS_KEY_PREFIX
    from gops_agents.chart_assets.storage import ChartAssetStorage

    symbols=tuple(item.strip().upper() for item in (args.symbols or "NVDA,AAPL,MSFT,SPY").split(",") if item.strip())
    intervals=tuple(item for item in args.intervals.split(",") if item in {"1D","1W","1M"})
    os.environ.setdefault("CLICKHOUSE_HTTP_URL","http://127.0.0.1:8123")
    os.environ.setdefault("CLICKHOUSE_DATABASE","market_data")
    os.environ.setdefault("CLICKHOUSE_USER","alfaka")
    os.environ.setdefault("CLICKHOUSE_PASSWORD","alfaka")
    os.environ.setdefault("REDIS_URL","redis://127.0.0.1:6379/0")
    os.environ["AUTH_ENABLED"]="false"

    class CountingLoader:
        def __init__(self): self.delegate=ChartAssetCandleLoader();self.calls=[]
        def load_symbol(self,symbol,current_intervals):self.calls.append(symbol);return self.delegate.load_symbol(symbol,current_intervals)
    class CountingStorage:
        def __init__(self):self.delegate=ChartAssetStorage();self.save_calls=0
        def save(self,asset):self.save_calls+=1;return self.delegate.save(asset)
        def __getattr__(self,name):return getattr(self.delegate,name)

    loader=CountingLoader();storage=CountingStorage();progress=RedisChartAssetProgressStore()
    kernel_calls=0;original_compute=builder_module.compute_feature_pack
    def counted_compute(*current_args,**current_kwargs):
        nonlocal kernel_calls
        kernel_calls+=1
        return original_compute(*current_args,**current_kwargs)
    builder_module.compute_feature_pack=counted_compute
    runs=[]
    try:
        for index in range(2):
            before={"queries":len(loader.calls),"kernel":kernel_calls,"writes":storage.save_calls}
            envelope=ChartAssetBuildEnvelope.create(requested_by="chart-assets-v2-eval",symbols=symbols,intervals=intervals,llm_enabled=False,force=False)
            state=ChartAssetBuilder(candle_loader=loader,storage=storage,progress=progress,llm_service=None,concurrency=1).run(envelope)
            runs.append({
                "jobId":envelope.job_id,"status":state.get("status"),
                "canonicalQueries":len(loader.calls)-before["queries"],"kernelCalls":kernel_calls-before["kernel"],
                "llmCalls":0,"insertCalls":storage.save_calls-before["writes"],
                "itemStatuses":sorted(item.get("status") for item in state.get("recentItems",[])),
                "itemReasons":sorted({item.get("reason") for item in state.get("recentItems",[]) if item.get("reason")}),
                "items":state.get("recentItems",[]),
            })
    finally:
        builder_module.compute_feature_pack=original_compute

    assets={symbol:storage.get_symbol_assets(symbol) for symbol in symbols}
    payload_sizes=[len(json.dumps(asset,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()) for current in assets.values() for asset in current.values() if asset and asset.get("assetVersion")=="v2"]
    redis_client=progress.redis
    redis_keys=sorted(str(key) for key in redis_client.scan_iter(match="gops:chart-assets:*"))
    redis_types={key:str(redis_client.type(key)) for key in redis_keys}
    served={};serving_samples={"v1":[],"v2":[]}
    with patch("app.routes.chart_assets.chart_asset_storage",return_value=storage):
        client=TestClient(create_app())
        for symbol in symbols:
            response=client.get("/api/charts/analysis-assets",params={"symbol":symbol});served[symbol]={"status":response.status_code,"versions":sorted({item.get("assetVersion") for item in response.json().get("assets",{}).values() if item})}
        for _ in range(5):
            client.get("/api/charts/analysis-assets",params={"symbol":"META"});client.get("/api/charts/analysis-assets",params={"symbol":symbols[0]})
        for version,symbol in (("v1","META"),("v2",symbols[0])):
            for _ in range(30):
                started=time.perf_counter();response=client.get("/api/charts/analysis-assets",params={"symbol":symbol});serving_samples[version].append((time.perf_counter()-started)*1000)
                if response.status_code!=200:raise RuntimeError(f"serving API returned {response.status_code}")
    serving={version:{"p50Ms":round(statistics.median(values),3),"p95Ms":round(percentile(values,.95),3),"requests":30} for version,values in serving_samples.items()}
    serving["relativeP95"]=round(serving["v2"]["p95Ms"]/max(serving["v1"]["p95Ms"],1e-9),4)
    checks={
        "firstBuildCompleted":runs[0]["status"] in {"completed","completed_with_warnings"},
        "queryAtMostOncePerSymbolPerBuild":all(run["canonicalQueries"]<=len(symbols) for run in runs),
        "secondRunKernelSkippedOrContentRevalidated":runs[1]["kernelCalls"]==0 or (runs[0]["insertCalls"]==0 and runs[1]["insertCalls"]==0 and "content_unchanged" in runs[1]["itemReasons"]),
        "secondRunLlmSkipped":runs[1]["llmCalls"]==0,
        "secondRunInsertSkipped":runs[1]["insertCalls"]==0,
        "secondRunUnchangedOrInsufficientPreserved":set(runs[1]["itemStatuses"]).issubset({"unchanged","skipped"}) and "unchanged" in runs[1]["itemStatuses"],
        "allSymbolsServedCompatible":all(item["status"]==200 and "v2" in item["versions"] and set(item["versions"]).issubset({"v1","v2"}) for item in served.values()),
        "assetHardCap20KiB":bool(payload_sizes) and max(payload_sizes)<=20*1024,
        "assetP95AtMost12KiB":bool(payload_sizes) and percentile(payload_sizes,.95)<=12*1024,
        "redisOnlyJobStatusKeys":all(key.startswith(STATUS_KEY_PREFIX+":") and redis_types[key]=="string" for key in redis_keys),
        "servingAbsoluteP95AtMost100Ms":serving["v2"]["p95Ms"]<=100,
        "servingRegressionAtMost10Percent":serving["relativeP95"]<=1.10,
    }
    report={"mode":"integration","symbols":list(symbols),"intervals":list(intervals),"runs":runs,"payloadBytes":{"count":len(payload_sizes),"max":max(payload_sizes) if payload_sizes else None,"p95":round(percentile(payload_sizes,.95),2) if payload_sizes else None},"redis":{"keyCount":len(redis_keys),"keyTypes":redis_types,"channelsPersistedAsKeys":False},"serving":serving,"served":served,"checks":checks,"passed":all(checks.values())}
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"mode":"integration","runs":runs,"payloadBytes":report["payloadBytes"],"serving":serving,"checks":checks,"passed":report["passed"]},ensure_ascii=False))
    return 0 if report["passed"] else 1


def load_env(path):
    for raw in path.read_text(encoding="utf-8").splitlines():
        line=raw.strip()
        if not line or line.startswith("#") or "=" not in line:continue
        name,value=line.split("=",1); os.environ.setdefault(name.strip(),value.strip().strip("\"'"))


def stratified(episodes,limit):
    selected=[]; categories=[]
    for item in episodes:
        if item["category"] not in categories:categories.append(item["category"])
    while len(selected)<min(limit,len(episodes)):
        changed=False
        for category in categories:
            item=next((entry for entry in episodes if entry["category"]==category and entry not in selected),None)
            if item and len(selected)<limit:selected.append(item);changed=True
        if not changed:break
    return selected


def _after_asof(value):
    from datetime import datetime,timedelta,timezone
    # The fixture timestamp identifies a completed NYSE daily session.  Advance
    # only to the following UTC midnight so week/month aggregation applies the
    # same completion contract as production.  A large look-ahead would make a
    # partial week or month appear complete even though no later candles exist.
    return datetime.fromisoformat(value.replace("Z","+00:00")).astimezone(timezone.utc)+timedelta(days=1)
def price_tokens(value):
    price=float(value);return {str(price),f"{price:.2f}",f"{price:g}"}
def percentile(values,q):
    if not values:return 0
    ordered=sorted(values); position=(len(ordered)-1)*q; lower=int(position); upper=min(len(ordered)-1,lower+1); return ordered[lower]+(ordered[upper]-ordered[lower])*(position-lower)


if __name__=="__main__": raise SystemExit(main())
