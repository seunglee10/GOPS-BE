from __future__ import annotations

import copy
import hashlib
import json
import statistics
from typing import Any


PROMPT_VERSION_V2 = "prompt-v2"
MODEL_POLICY_VERSION = "chart-asset-model-v1"
SEMANTIC_TO_TOOL = {
    "price_zone": "horizontalParallelLines",
    "consolidation_base": "rangeBox",
    "retracement": "fibonacciRetracement",
    "event_window": "verticalParallelLines",
    "single_event": "flagMarker",
    "historical_structure": "trendLine",
}
EMPHASIS_CODES = {"STRUCTURE_FIRST", "EVENT_FIRST", "CONFLICT_FIRST", "BALANCED"}


def build_interval_palette(
    *, symbol: str, interval: str, input_digest: str, features: dict[str, Any],
    rule_layers: dict[str, Any], candles: list[dict[str, Any]], generated_at: str,
) -> dict[str, Any]:
    selected_rule_ids = {
        item.get("candidateId") for layer in rule_layers.values() if isinstance(layer, dict)
        for item in layer.get("selected", []) if item.get("candidateId")
    }
    facts: list[dict[str, Any]] = []
    conditions: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for layer_name in ("structure", "trend"):
        layer = rule_layers.get(layer_name) or {}
        for selected in layer.get("selected", []):
            candidate_id = str(selected.get("candidateId") or "")
            drawings = selected.get("drawingIds") or []
            if not candidate_id or not drawings: continue
            fact_id = f"{interval}:fact:{_suffix(candidate_id + ':rule')}"
            facts.append({"factId": fact_id, "ownerRef": candidate_id, "clauseCode": "ACTIVE_STRUCTURE", "renderedKo": _rule_fact(layer_name, selected)})
            findings.append({"findingId": candidate_id, "drawingIds": drawings, "evidenceRefs": selected.get("evidenceRefs") or [], "factIds": [fact_id], "quality": selected.get("quality") or {}})
    for level in features.get("levels", []):
        level_distance = float(level["currentDistanceAtr"]) if level.get("currentDistanceAtr") is not None else 99.0
        if not level.get("hardPass") or float(level.get("score") or 0) < .65 or level_distance > 2 or not level.get("vpConfluence"):
            continue
        if float(level.get("zoneHigh", level["price"])) - float(level.get("zoneLow", level["price"])) < .25 * float(features.get("regime", {}).get("atr14") or 0):
            continue
        evidence = list(level.get("memberPivotIds") or [])
        candidate = _candidate(symbol, interval, input_digest, "price_zone", evidence, level["id"], level["score"], level["currentDistanceAtr"], {
            "type": "horizontalParallelLines", "anchors": [{"price": float(level["zoneLow"])}, {"price": float(level["zoneHigh"])}], "label": "가격 구간 · 매물대",
        }, generated_at)
        if candidate["redundancyKey"] not in selected_rule_ids: candidates.append(candidate)
    for event in features.get("events", []):
        if not event.get("hardPass") or event.get("currentImpact") not in {"high", "medium"}: continue
        if event["id"] in selected_rule_ids: continue
        candidate = _candidate(symbol, interval, input_digest, "single_event", [event["id"]], event["id"], .7 if event["currentImpact"] == "high" else .65, 0, {
            "type": "flagMarker", "anchors": [{"timestamp": event["timestamp"], "price": float(event["price"])}], "label": _event_label(event),
        }, generated_at)
        candidates.append(candidate)
    for fib in features.get("fibCandidates", [])[:2]:
        pivots = {item["id"]: item for item in features.get("pivots", [])}
        first, second = pivots.get(fib.get("fromPivotId")), pivots.get(fib.get("toPivotId"))
        if (
            not first or not second or not fib.get("hardPass")
            or float(fib.get("quality") or 0) < .65
            or float(fib.get("impulseAtr") or 0) < 5
            or not fib.get("reactionAt")
            or (float(fib["currentDistanceAtr"]) if fib.get("currentDistanceAtr") is not None else 99.0) > 2
        ): continue
        candidate = _candidate(symbol, interval, input_digest, "retracement", [first["id"], second["id"]], f"{first['id']}:{second['id']}", fib["quality"], fib["currentDistanceAtr"], {
            "type": "fibonacciRetracement", "anchors": [{"timestamp": first["timestamp"], "price": float(first["price"])}, {"timestamp": second["timestamp"], "price": float(second["price"])}], "label": "검증된 되돌림 구간",
        }, generated_at)
        candidates.append(candidate)
    candidates = _dedupe_candidates(candidates)[:6]
    for candidate in candidates:
        fact_id = f"{interval}:fact:{_suffix(candidate['candidateId'])}"
        watch_id = f"{interval}:cond:{_suffix(candidate['candidateId'] + ':watch')}"
        invalid_id = f"{interval}:cond:{_suffix(candidate['candidateId'] + ':invalid')}"
        candidate["factIds"] = [fact_id]
        candidate["confirmationConditionRef"] = watch_id
        candidate["invalidationConditionRef"] = invalid_id
        facts.append({"factId": fact_id, "ownerRef": candidate["candidateId"], "clauseCode": _fact_code(candidate["semanticType"]), "renderedKo": _candidate_fact(candidate)})
        conditions.extend([
            {"conditionId": watch_id, "ownerRef": candidate["candidateId"], "code": "CONFIRM_CLOSE_HOLD", "renderedKo": "다음 확정봉에서 해당 구조를 지키는 종가가 이어지는지 확인하세요."},
            {"conditionId": invalid_id, "ownerRef": candidate["candidateId"], "code": "INVALIDATE_CLOSE_CROSS", "renderedKo": "확정 종가가 구조의 반대편으로 이탈하면 이 해석은 무효입니다."},
        ])
    return {
        "interval": interval, "inputDigest": input_digest, "asOf": candles[-1]["timestamp"],
        "quality": {"state": "eligible", "coverage": 1.0},
        "regime": _compact_regime(features.get("regime") or {}),
        "ruleFindings": findings, "visualCandidates": candidates,
        "narrativeFacts": facts, "conditions": conditions,
        "ruleDrawingCount": sum(len((rule_layers.get(key) or {}).get("drawings", [])) for key in ("structure", "trend")),
    }


def build_symbol_bundle(symbol: str, interval_palettes: list[dict[str, Any]], cross_timeframe: dict[str, Any] | None = None) -> dict[str, Any]:
    palettes = sorted(interval_palettes, key=lambda item: {"1M": 0, "1W": 1, "1D": 2}[item["interval"]])
    digest = "sha256:" + hashlib.sha256(_stable_json({"symbol": symbol, "intervals": palettes, "cross": cross_timeframe or {}}).encode()).hexdigest()
    compact = [_compact_palette(item) for item in palettes]
    allowed: set[str] = set()
    for rank in range(6):
        for palette in compact:
            if rank < len(palette["visualCandidates"]) and len(allowed) < 10:
                allowed.add(palette["visualCandidates"][rank]["candidateId"])
    for palette in compact:
        palette["visualCandidates"] = [item for item in palette["visualCandidates"] if item["candidateId"] in allowed]
    return {"symbol": symbol, "symbolBundleDigest": digest, "intervals": compact, "crossTimeframe": cross_timeframe or {"alignment": "unknown", "relationIds": [], "evidenceRefs": []}}


def validate_curation_output(value: Any, bundle: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"intervalSelections"} or not isinstance(value["intervalSelections"], list):
        raise ValueError("invalid curation output")
    palettes = {item["interval"]: item for item in bundle["intervals"]}
    relation_ids = set(bundle.get("crossTimeframe", {}).get("relationIds") or [])
    seen_intervals = set()
    total = 0
    for selection in value["intervalSelections"]:
        required = {"interval", "selectedCandidateIds", "headlineFactIds", "focusNarratives", "counterEvidenceRefs", "higherTimeframeRelationIds", "emphasisCode"}
        if not isinstance(selection, dict) or set(selection) != required: raise ValueError("invalid interval selection")
        interval = selection["interval"]
        if interval not in palettes or interval in seen_intervals: raise ValueError("invalid interval reference")
        for key in ("selectedCandidateIds", "headlineFactIds", "focusNarratives", "counterEvidenceRefs", "higherTimeframeRelationIds"):
            if not isinstance(selection[key], list): raise ValueError(f"invalid {key}")
        for key in ("selectedCandidateIds", "headlineFactIds", "counterEvidenceRefs", "higherTimeframeRelationIds"):
            if any(not isinstance(item, str) for item in selection[key]): raise ValueError(f"invalid {key}")
        seen_intervals.add(interval); palette = palettes[interval]
        candidates = {item["candidateId"]: item for item in palette["visualCandidates"]}
        findings = {item["findingId"]: item for item in palette["ruleFindings"]}
        fact_owners = {fact_id: item["candidateId"] for item in palette["visualCandidates"] for fact_id in item.get("factIds", [])}
        fact_owners.update({fact_id: item["findingId"] for item in palette["ruleFindings"] for fact_id in item.get("factIds", [])})
        conditions = {item["confirmationConditionRef"]: item["candidateId"] for item in palette["visualCandidates"]}
        evidence_refs = {
            ref
            for item in (*palette["visualCandidates"], *palette["ruleFindings"])
            for ref in item.get("evidenceRefs", [])
        }
        evidence_refs.update(bundle.get("crossTimeframe", {}).get("evidenceRefs") or [])
        selected = selection["selectedCandidateIds"]
        if len(selected) > 2 or len(selected) != len(set(selected)) or any(item not in candidates for item in selected): raise ValueError("invalid candidate selection")
        total += len(selected)
        for focus in selection["focusNarratives"]:
            if set(focus) != {"refType","refId","factIds","watchConditionRef","priority"}: raise ValueError("invalid focus narrative")
            if focus["refType"] not in {"visualCandidate", "ruleFinding"}: raise ValueError("invalid focus ref type")
            if not isinstance(focus["refId"], str) or not isinstance(focus["watchConditionRef"], str): raise ValueError("invalid focus reference")
            if not isinstance(focus["factIds"], list) or any(not isinstance(item, str) for item in focus["factIds"]): raise ValueError("invalid focus fact")
            if type(focus["priority"]) is not int or not 1 <= focus["priority"] <= 9: raise ValueError("invalid focus priority")
            owner = focus["refId"]
            allowed_owner = owner in candidates if focus["refType"] == "visualCandidate" else owner in findings
            if not allowed_owner or any(fact_owners.get(fact) != owner for fact in focus["factIds"]): raise ValueError("invalid focus fact")
            if focus["refType"] == "visualCandidate" and conditions.get(focus["watchConditionRef"]) != owner: raise ValueError("invalid focus condition")
            if focus["refType"] == "ruleFinding" and focus["watchConditionRef"]: raise ValueError("rule finding cannot invent condition")
        focus_ids = [item["refId"] for item in selection["focusNarratives"] if item["refType"] == "visualCandidate"]
        if sorted(focus_ids) != sorted(selected): raise ValueError("focus candidate mismatch")
        if len(selection["headlineFactIds"]) != len(set(selection["headlineFactIds"])) or any(item not in fact_owners for item in selection["headlineFactIds"]): raise ValueError("invalid headline fact")
        if len(selection["counterEvidenceRefs"]) != len(set(selection["counterEvidenceRefs"])) or any(item not in evidence_refs for item in selection["counterEvidenceRefs"]): raise ValueError("invalid counter evidence")
        if len(selection["higherTimeframeRelationIds"]) != len(set(selection["higherTimeframeRelationIds"])) or any(item not in relation_ids for item in selection["higherTimeframeRelationIds"]): raise ValueError("invalid relation")
        if selection["emphasisCode"] not in EMPHASIS_CODES: raise ValueError("invalid emphasis")
    if total > 6: raise ValueError("symbol visual budget exceeded")
    return value


def materialize_curation(*, symbol: str, palettes: dict[str, dict[str, Any]], output: dict[str, Any], generated_at: str, model: str | None) -> dict[str, dict[str, Any]]:
    results = {}
    selections = {item["interval"]: item for item in output["intervalSelections"]}
    accepted_candidates: list[dict[str, Any]] = []
    for interval in sorted(palettes, key=lambda item: {"1M": 0, "1W": 1, "1D": 2}[item]):
        palette = palettes[interval]
        selection = selections.get(interval) or {"selectedCandidateIds": [], "focusNarratives": []}
        candidates = {item["candidateId"]: item for item in palette["visualCandidates"]}
        drawings, selected_meta = [], []
        rejected_by_reason: dict[str, int] = {}
        available_slots = max(0, 5 - int(palette.get("ruleDrawingCount") or 0))
        for candidate_id in selection["selectedCandidateIds"][:available_slots]:
            candidate = candidates[candidate_id]
            if any(_cross_tf_redundant(candidate, accepted) for accepted in accepted_candidates):
                rejected_by_reason["mtf_redundant"] = rejected_by_reason.get("mtf_redundant", 0) + 1
                continue
            drawing = copy.deepcopy(candidate["drawingTemplate"])
            suffix = _suffix(candidate_id)
            drawing.update({"id": f"ca-{symbol}-{interval}-agent-{suffix}", "sourceInterval": interval, "createdBy": "llm", "sourceProposalId": f"chart-asset:{symbol}:{interval}:agent", "createdAt": generated_at, "updatedAt": generated_at, "locked": False, "visible": True})
            drawings.append(drawing)
            selected_meta.append({"candidateId": candidate_id, "drawingIds": [drawing["id"]], "evidenceRefs": candidate["evidenceRefs"], "quality": candidate["quality"], "geometryBy": "kernel", "selectedBy": "llm"})
            accepted_candidates.append(candidate)
        results[interval] = {"drawings": drawings, "selected": selected_meta, "emptyReason": None if drawings else "curator_selected_none", "meta": {"candidateCount": len(candidates), "passedCount": len(candidates), "rejectedByReason": rejected_by_reason, "model": model, "degraded": False, "selection": selection}}
    return results


def deterministic_curation(bundle: dict[str, Any]) -> dict[str, Any]:
    return {"intervalSelections": [{"interval": palette["interval"], "selectedCandidateIds": [], "headlineFactIds": [fact_id for item in palette["ruleFindings"][:2] for fact_id in item.get("factIds", [])][:2], "focusNarratives": [], "counterEvidenceRefs": [], "higherTimeframeRelationIds": [], "emphasisCode": "STRUCTURE_FIRST"} for palette in bundle["intervals"]]}


def curation_output_schema() -> dict[str, Any]:
    focus = {"type":"object","additionalProperties":False,"properties":{"refType":{"enum":["visualCandidate","ruleFinding"]},"refId":{"type":"string"},"factIds":{"type":"array","items":{"type":"string"},"maxItems":3},"watchConditionRef":{"type":"string"},"priority":{"type":"integer","minimum":1,"maximum":9}},"required":["refType","refId","factIds","watchConditionRef","priority"]}
    selection = {"type":"object","additionalProperties":False,"properties":{"interval":{"enum":["1D","1W","1M"]},"selectedCandidateIds":{"type":"array","items":{"type":"string"},"maxItems":2},"headlineFactIds":{"type":"array","items":{"type":"string"},"maxItems":3},"focusNarratives":{"type":"array","items":focus,"maxItems":5},"counterEvidenceRefs":{"type":"array","items":{"type":"string"},"maxItems":3},"higherTimeframeRelationIds":{"type":"array","items":{"type":"string"},"maxItems":3},"emphasisCode":{"enum":sorted(EMPHASIS_CODES)}},"required":["interval","selectedCandidateIds","headlineFactIds","focusNarratives","counterEvidenceRefs","higherTimeframeRelationIds","emphasisCode"]}
    return {"type":"object","additionalProperties":False,"properties":{"intervalSelections":{"type":"array","items":selection,"maxItems":3}},"required":["intervalSelections"]}


def _candidate(symbol, interval, digest, semantic, evidence, redundancy, score, distance, template, generated_at):
    candidate_id = f"{interval}:vc-{_suffix('|'.join([digest, semantic, *sorted(evidence), 'candidate-v1']))}"
    template = {**template, "style": {"colorToken": "insight-primary", "lineWidth": 2}, "sourceInterval": interval}
    return {"candidateId":candidate_id,"interval":interval,"semanticType":semantic,"drawingTemplate":template,"evidenceRefs":evidence,"counterEvidenceRefs":[],"redundancyKey":redundancy,"quality":{"hardPass":True,"score":round(float(score),4),"currentDistanceAtr":round(float(distance),4)},"qualityBand":"high" if score>=.8 else "medium","currentRelevance":"near" if distance<=1.5 else "actionable"}
def _compact_palette(item):
    compact = {key:item[key] for key in ("interval","regime")}
    compact["ruleFindings"] = [{key:finding[key] for key in ("findingId","factIds","evidenceRefs")} for finding in item["ruleFindings"][:2]]
    compact["visualCandidates"] = [{key:candidate[key] for key in ("candidateId","semanticType","factIds","evidenceRefs","qualityBand","currentRelevance","confirmationConditionRef","invalidationConditionRef")} for candidate in item["visualCandidates"]]
    return compact
def _compact_regime(regime): return {"trend":regime.get("trend","range"),"volatility":"high" if float(regime.get("atrPercentile") or 0)>.8 else "low" if float(regime.get("atrPercentile") or 0)<.2 else "normal","momentum":"slowing" if regime.get("macdState")=="diverging" else "stable"}
def _dedupe_candidates(items):
    result={}
    for item in items:
        key=item["redundancyKey"]; current=result.get(key)
        if current is None or (item["quality"]["score"],item["candidateId"])>(current["quality"]["score"],current["candidateId"]):result[key]=item
    return sorted(result.values(),key=lambda item:(-item["quality"]["score"],item["quality"]["currentDistanceAtr"],item["candidateId"]))


def _cross_tf_redundant(candidate, accepted):
    if candidate.get("semanticType") != accepted.get("semanticType"):
        return False
    left = candidate.get("drawingTemplate") or {}
    right = accepted.get("drawingTemplate") or {}
    if left.get("type") != right.get("type"):
        return False
    left_anchors, right_anchors = left.get("anchors") or [], right.get("anchors") or []
    if left.get("type") == "horizontalParallelLines" and len(left_anchors) == len(right_anchors) == 2:
        left_prices = sorted(float(item["price"]) for item in left_anchors)
        right_prices = sorted(float(item["price"]) for item in right_anchors)
        left_mid, right_mid = statistics.mean(left_prices), statistics.mean(right_prices)
        tolerance = max(left_prices[1] - left_prices[0], right_prices[1] - right_prices[0], .005 * max(left_mid, right_mid))
        return abs(left_mid - right_mid) <= tolerance
    if left.get("type") == "fibonacciRetracement" and len(left_anchors) == len(right_anchors) == 2:
        left_prices = [float(item["price"]) for item in left_anchors]
        right_prices = [float(item["price"]) for item in right_anchors]
        scale = max(*(abs(item) for item in (*left_prices, *right_prices)), 1.0)
        return max(abs(a - b) for a, b in zip(left_prices, right_prices)) <= .01 * scale
    if left.get("type") == "flagMarker" and left_anchors and right_anchors:
        return str(left_anchors[0].get("timestamp"))[:10] == str(right_anchors[0].get("timestamp"))[:10]
    return False
def _suffix(value): return hashlib.sha256(value.encode()).hexdigest()[:10]
def _stable_json(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),default=str)
def _rule_fact(layer, selected): return "현재 가격과 가까운 검증된 가격 구조가 있습니다." if layer=="structure" else "세 번 이상 독립적으로 확인된 현재 관련 추세 구조가 있습니다."
def _candidate_fact(candidate): return {"price_zone":"폭과 매물대 근거가 함께 확인된 가격 구간입니다.","retracement":"검증된 충격파의 주요 되돌림 구간이 현재와 가깝습니다.","single_event":"최근 상태 변화가 현재 구조에 영향을 주고 있습니다."}.get(candidate["semanticType"],"현재 구조를 설명하는 추가 근거입니다.")
def _fact_code(semantic): return {"price_zone":"ACTIVE_ZONE_NEAR","retracement":"VALID_RETRACEMENT_NEAR","single_event":"CURRENT_EVENT_IMPACT"}.get(semantic,"ADDITIONAL_STRUCTURE")
def _event_label(event):
    if event.get("kind")=="breakout" and (event.get("detail") or {}).get("state")=="failed":return "구조 이탈 실패"
    return {"breakout":"구조 이탈","retest":"리테스트 확인","gap":"갭","52wHigh":"52주 신고가","52wLow":"52주 신저가"}.get(event.get("kind"),"주요 이벤트")
