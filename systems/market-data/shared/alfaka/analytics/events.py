from __future__ import annotations

import hashlib
import statistics
from typing import Any

from .atr import atr_series
from .config import QUALITY_CONFIG


def compute_events(candles, levels, *, atr, display_from, interval):
    config = QUALITY_CONFIG[interval]
    atr_values = [float(value or 0) for value in atr_series(candles)]
    display_start = next((index for index, row in enumerate(candles) if row["timestamp"] >= display_from), len(candles))
    eligible_levels = [item for item in levels if item.get("hardPass")]
    events = []
    active_breaks = {}
    gaps = []
    extreme_episodes = {"52wHigh": None, "52wLow": None}
    for index in range(max(1, display_start), len(candles)):
        row, previous = candles[index], candles[index - 1]
        local_atr = max(atr_values[index], 1e-12)
        previous_close, close = float(previous["close"]), float(row["close"])
        baseline = [float(item.get("volume") or 0) for item in candles[max(0, index-config.volume_baseline_bars):index]]
        relative_volume = float(row.get("volume") or 0) / max(statistics.median(baseline) if baseline else 1, 1)
        for level in eligible_levels:
            low, high = float(level.get("zoneLow", level["price"])), float(level.get("zoneHigh", level["price"]))
            direction = "up" if previous_close <= high and close > high + .25*local_atr else "down" if previous_close >= low and close < low - .25*local_atr else None
            if direction:
                event = _event(row, "breakout", close, [level["id"]], {"direction": direction, "state": "unresolved", "relativeVolume": round(relative_volume, 3), "participationPass": relative_volume >= 1.2}, interval)
                events.append(event); active_breaks[level["id"]] = (index, direction, event)
            active = active_breaks.get(level["id"])
            if not active or index <= active[0]: continue
            direction = active[1]
            if (direction == "up" and close < high) or (direction == "down" and close > low):
                active[2]["detail"]["state"] = "failed"
                active[2]["currentImpact"] = _impact(index, len(candles), close, float(level["price"]), local_atr, config, "failed")
                active_breaks.pop(level["id"], None)
            elif (float(row["low"]) <= high if direction == "up" else float(row["high"]) >= low):
                held = close > high if direction == "up" else close < low
                if held:
                    active[2]["detail"]["state"] = "retest_confirmed"
                    events.append(_event(row, "retest", float(level["price"]), [level["id"]], {"direction": direction, "state": "confirmed", "breakEventId": active[2]["id"]}, interval))
                    active_breaks.pop(level["id"], None)
            else:
                active[2]["detail"]["state"] = "hold"
        gap_distance = abs(float(row["open"]) - previous_close)
        if gap_distance >= local_atr:
            direction = "up" if float(row["open"]) > previous_close else "down"
            event = _event(row, "gap", float(row["open"]), [], {"direction": direction, "state": "unfilled", "gapFrom": previous_close, "gapTo": float(row["open"])}, interval)
            gaps.append(event); events.append(event)
        for gap in gaps:
            if gap["detail"]["state"] != "unfilled" or gap["timestamp"] == row["timestamp"]: continue
            lower, upper = sorted((gap["detail"]["gapFrom"], gap["detail"]["gapTo"]))
            if float(row["low"]) <= lower and float(row["high"]) >= upper:
                gap["detail"]["state"] = "filled"; gap["detail"]["filledAt"] = row["timestamp"]
        lookback = candles[max(0,index-min(252,index)):index]
        for kind, value, is_extreme in (("52wHigh",float(row["high"]),lookback and float(row["high"])>max(float(item["high"]) for item in lookback)),("52wLow",float(row["low"]),lookback and float(row["low"])<min(float(item["low"]) for item in lookback))):
            if not is_extreme: continue
            active = extreme_episodes[kind]
            if active and index-active[0] <= config.extreme_episode_gap:
                active[1].update(_event(row, kind, value, [], {"episodeStart": active[1]["detail"]["episodeStart"]}, interval))
            else:
                event = _event(row, kind, value, [], {"episodeStart": row.get("candleKey") or row["timestamp"]}, interval)
                events.append(event); extreme_episodes[kind]=(index,event)
    for event in events:
        age = len(candles)-1-next((i for i,row in enumerate(candles) if row["timestamp"]==event["timestamp"]),0)
        distance = abs(float(candles[-1]["close"])-float(event["price"]))/max(atr_values[-1],1e-12)
        state = event["detail"].get("state", "unresolved")
        event.setdefault("currentImpact", _impact(len(candles)-1-age,len(candles),float(candles[-1]["close"]),float(event["price"]),max(atr_values[-1],1e-12),config,state))
        event["ageBars"] = age
        event["hardPass"] = event["currentImpact"] == "high" if event["kind"] in {"52wHigh", "52wLow"} else event["currentImpact"] in {"high","medium"}
    return sorted(_dedupe(events), key=lambda item:(item["timestamp"],item["kind"],item["id"]))


def _event(row, kind, price, refs, detail, interval):
    identity = f"{interval}|{kind}|{row.get('candleKey') or row['timestamp']}|{','.join(refs)}|{detail.get('episodeStart','')}"
    return {"id":f"{interval}:event:{hashlib.sha256(identity.encode()).hexdigest()[:10]}","timestamp":row["timestamp"],"candleKey":row.get("candleKey"),"kind":kind,"price":round(price,4),"refIds":refs,"detail":detail}


def _impact(index,total,current,price,atr,config,state):
    age=total-1-index; distance=abs(current-price)/atr
    if (state in {"retest_confirmed","failed","unfilled"} and distance<=1.5) or age<=max(1,int(.1*config.display_bars)): return "high"
    if state in {"unresolved","hold"} and distance<=3 and age<=config.event_relevance_bars: return "medium"
    return "low"


def _dedupe(events):
    result={}
    for event in events:
        key=(event["kind"],tuple(event["refIds"]),event["detail"].get("episodeStart") or event["detail"].get("breakEventId") or event["id"])
        current=result.get(key)
        if current is None or (event["currentImpact"] in {"high","medium"},event["timestamp"],event["id"])>(current["currentImpact"] in {"high","medium"},current["timestamp"],current["id"]): result[key]=event
    return list(result.values())
