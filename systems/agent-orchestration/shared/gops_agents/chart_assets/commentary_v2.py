from __future__ import annotations

import statistics
from typing import Any


def assemble_commentary_v2(*, interval: str, palette: dict[str, Any], rule_layers: dict[str, Any], agent_layer: dict[str, Any], curation_selection: dict[str, Any] | None, coverage: dict[str, Any] | None = None) -> dict[str, Any]:
    facts = {item["factId"]: item for item in palette.get("narrativeFacts", [])}
    conditions = {item["conditionId"]: item for item in palette.get("conditions", [])}
    candidates = {item["candidateId"]: item for item in palette.get("visualCandidates", [])}
    selected_agent = {item["candidateId"]: item for item in agent_layer.get("selected", [])}
    all_drawings = [drawing for layer in (rule_layers.get("structure") or {}, rule_layers.get("trend") or {}, agent_layer) for drawing in layer.get("drawings", [])]
    focus = []
    for layer_name in ("structure", "trend"):
        for item in (rule_layers.get(layer_name) or {}).get("selected", []):
            if not item.get("drawingIds"): continue
            focus.append({"drawingIds":item["drawingIds"],"candidateId":item.get("candidateId"),"featureIds":item.get("evidenceRefs") or [],"whatItShows":_rule_what(layer_name),"whyItMatters":"현재 가격과 연결된 검증 근거만 표시했습니다.","whatToWatch":"다음 확정봉에서 구조 반응이 이어지는지 확인하세요.","confirmation":None,"invalidation":None,"horizon":_horizon(interval)})
    selection = curation_selection or {}
    for narrative in sorted(selection.get("focusNarratives") or [], key=lambda item:item.get("priority",9)):
        candidate_id=narrative.get("refId"); selected=selected_agent.get(candidate_id); candidate=candidates.get(candidate_id)
        if not selected or not candidate: continue
        rendered=[facts[item]["renderedKo"] for item in narrative.get("factIds",[]) if item in facts]
        watch=conditions.get(narrative.get("watchConditionRef"))
        invalid=conditions.get(candidate.get("invalidationConditionRef"))
        focus.append({"drawingIds":selected["drawingIds"],"candidateId":candidate_id,"featureIds":candidate["evidenceRefs"],"whatItShows":" ".join(rendered) or "검증된 추가 구조입니다.","whyItMatters":"현재 구조 판단에 추가 정보를 제공합니다.","whatToWatch":watch["renderedKo"] if watch else "다음 확정봉의 반응을 확인하세요.","confirmation":watch["conditionId"] if watch else None,"invalidation":invalid["conditionId"] if invalid else None,"horizon":_horizon(interval)})
    covered={drawing_id for item in focus for drawing_id in item["drawingIds"]}
    for drawing in all_drawings:
        if drawing["id"] not in covered:
            focus.append({"drawingIds":[drawing["id"]],"candidateId":None,"featureIds":[],"whatItShows":drawing.get("label") or "검증된 차트 구조입니다.","whyItMatters":"현재 차트에서 우선 확인할 구조입니다.","whatToWatch":"다음 확정봉의 반응을 확인하세요.","confirmation":None,"invalidation":None,"horizon":_horizon(interval)})
    headline = _headline(palette, bool(all_drawings))
    key_levels = _key_levels(rule_layers)
    scores=[float(item.get("quality",{}).get("score") or 0) for layer in (rule_layers.get("structure") or {},rule_layers.get("trend") or {},agent_layer) for item in layer.get("selected",[])]
    coverage_ratio=float((coverage or {}).get("coverageRatio",1)); confidence=min(1,max(0,.65*(statistics.mean(scores) if scores else .6)+.35*coverage_ratio))
    invalidation=next((item["whatToWatch"] for item in focus if item.get("whatToWatch")),"새 구조가 확인되면 현재 해석을 다시 평가하세요.")
    text=" ".join([headline,*[f"주요 관찰: {item['whatItShows']} {item['whatToWatch']}" for item in focus[:3]],f"무효화 판단: {invalidation}"])
    return {"headline":headline,"regimeSummary":_regime_summary(palette),"focusItems":focus,"keyLevelsV2":key_levels,"higherTimeframeContext":"상위 주기 구조는 동일 bundle의 검증된 관계만 반영합니다.","counterEvidence":[],"dataCaveats":list((coverage or {}).get("qualityFlags") or []),"confidenceV2":{"selection":{"score":round(confidence,4),"reasons":["verified_geometry","current_relevance"],"penalties":list((coverage or {}).get("qualityFlags") or [])},"marketDirection":{"score":None,"reasons":[],"penalties":[]}},"text":text,"keyLevels":[f"{item['role']} {item['price']:.2f} · {item['reason']}" for item in key_levels],"invalidation":invalidation,"confidence":round(confidence,4),"enrichment":None}


def _headline(palette,drawn):
    trend=palette.get("regime",{}).get("trend","range"); label={"up":"상승","down":"하락","range":"횡보"}.get(trend,"혼조")
    return f"{label} 구조에서 현재와 연결된 핵심 근거를 표시했습니다." if drawn else "현재 품질 기준을 통과한 작도는 표시하지 않았습니다."
def _regime_summary(palette): return f"현재 국면은 {palette.get('regime',{}).get('trend','range')}이며 변동성은 {palette.get('regime',{}).get('volatility','normal')}입니다."
def _rule_what(layer): return "검증된 지지·저항 구조입니다." if layer=="structure" else "세 번 이상 독립 접점이 확인된 추세·범위 구조입니다."
def _horizon(interval): return {"1D":"weeks","1W":"months","1M":"years"}[interval]
def _key_levels(rule_layers):
    result=[]
    for drawing in (rule_layers.get("structure") or {}).get("drawings",[]):
        if drawing.get("type")!="horizontalLine":continue
        anchor=(drawing.get("anchors") or [{}])[0]
        if anchor.get("price") is not None:result.append({"drawingId":drawing["id"],"role":"support" if "지지" in str(drawing.get("label")) else "resistance","price":float(anchor["price"]),"reason":drawing.get("label") or "검증된 가격 구조"})
    return result[:3]
