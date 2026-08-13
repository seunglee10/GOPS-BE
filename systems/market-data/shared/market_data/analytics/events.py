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
    # A well-evidenced level remains useful for detecting its own break even when
    # it is no longer eligible to be drawn as an active H-Line.
    eligible_levels = [item for item in levels if item.get("evidencePass", item.get("hardPass"))]
    events = _moving_average_cross_events(
        candles,
        interval=interval,
        display_start=display_start,
    )
    active_breaks = {}
    extreme_episodes = {"52wHigh": None, "52wLow": None}
    for index in range(max(1, display_start), len(candles)):
        row, previous = candles[index], candles[index - 1]
        local_atr = max(atr_values[index], 1e-12)
        previous_close, close = float(previous["close"]), float(row["close"])
        baseline = [float(item.get("volume") or 0) for item in candles[max(0, index-config.volume_baseline_bars):index]]
        relative_volume = float(row.get("volume") or 0) / max(statistics.median(baseline) if baseline else 1, 1)
        for level in eligible_levels:
            confirmed_index = level.get("evidenceConfirmedIndex")
            if confirmed_index is not None and index <= int(confirmed_index):
                continue
            low, high = float(level.get("zoneLow", level["price"])), float(level.get("zoneHigh", level["price"]))
            active = active_breaks.get(level["id"])
            if active and index > active[0]:
                active_direction = active[1]
                active_event = active[2]
                if (active_direction == "up" and close < high) or (active_direction == "down" and close > low):
                    active_event["detail"]["state"] = "invalidated" if active_event["detail"].get("confirmationPass") else "failed"
                    active_event["detail"]["confirmationPass"] = False
                    active_event["currentImpact"] = _impact(index, len(candles), close, float(level["price"]), local_atr, config, active_event["detail"]["state"])
                    active_breaks.pop(level["id"], None)
                elif not active_event["detail"].get("confirmationPass"):
                    if (float(row["low"]) <= high if active_direction == "up" else float(row["high"]) >= low):
                        held = close > high if active_direction == "up" else close < low
                        if held:
                            active_event["detail"]["state"] = "retest_confirmed"
                            active_event["detail"]["confirmationPass"] = True
                            events.append(_event(row, "retest", float(level["price"]), [level["id"]], {"direction": active_direction, "state": "confirmed", "breakEventId": active_event["id"]}, interval))
                    elif index == active[0] + 1:
                        held = close > high if active_direction == "up" else close < low
                        if held:
                            active_event["detail"]["state"] = "hold_confirmed"
                            active_event["detail"]["confirmationPass"] = True
                    else:
                        active_event["detail"]["state"] = "hold"
            direction = "up" if previous_close <= high and close > high + .25*local_atr else "down" if previous_close >= low and close < low - .25*local_atr else None
            if direction and level["id"] not in active_breaks:
                volume_confirmed = index == len(candles) - 1 and relative_volume >= 1.5
                event = _event(row, "breakout", close, [level["id"]], {
                    "direction": direction,
                    "state": "volume_confirmed" if volume_confirmed else "unresolved",
                    "relativeVolume": round(relative_volume, 3),
                    "participationPass": relative_volume >= 1.5,
                    "penetrationAtr": round((close - high if direction == "up" else low - close) / local_atr, 4),
                    "confirmationPass": volume_confirmed,
                }, interval)
                events.append(event); active_breaks[level["id"]] = (index, direction, event)
        lookback = candles[max(0,index-min(252,index)):index]
        for kind, value, is_extreme in (("52wHigh",float(row["high"]),lookback and float(row["high"])>max(float(item["high"]) for item in lookback)),("52wLow",float(row["low"]),lookback and float(row["low"])<min(float(item["low"]) for item in lookback))):
            if not is_extreme: continue
            active = extreme_episodes[kind]
            if active and index-active[0] <= config.extreme_episode_gap:
                active[1].update(_event(row, kind, value, [], {"episodeStart": active[1]["detail"]["episodeStart"]}, interval))
            else:
                event = _event(row, kind, value, [], {"episodeStart": row.get("candleKey") or row["timestamp"]}, interval)
                events.append(event); extreme_episodes[kind]=(index,event)
    breakout_by_id = {event["id"]: event for event in events if event["kind"] == "breakout"}
    for event in events:
        age = len(candles)-1-next((i for i,row in enumerate(candles) if row["timestamp"]==event["timestamp"]),0)
        distance = abs(float(candles[-1]["close"])-float(event["price"]))/max(atr_values[-1],1e-12)
        state = event["detail"].get("state", "unresolved")
        if event["kind"] == "movingAverageCross":
            event.setdefault("currentImpact", _moving_average_cross_impact(age, config))
        else:
            event.setdefault("currentImpact", _impact(len(candles)-1-age,len(candles),float(candles[-1]["close"]),float(event["price"]),max(atr_values[-1],1e-12),config,state))
        event["ageBars"] = age
        impact_pass = event["currentImpact"] in {"high", "medium"}
        current_distance = abs(float(candles[-1]["close"])-float(event["price"]))/max(atr_values[-1],1e-12)
        event["currentDistanceAtr"] = round(current_distance, 4)
        if event["kind"] == "breakout":
            evidence_pass = float(event["detail"].get("penetrationAtr") or 0) >= .25
            active_pass = (
                bool(event["detail"].get("confirmationPass"))
                and age <= max(3, int(.05 * config.display_bars))
                and current_distance <= 3
            )
        elif event["kind"] == "retest":
            parent = breakout_by_id.get(event["detail"].get("breakEventId"))
            evidence_pass = event["detail"].get("state") == "confirmed" and bool(parent and parent["detail"].get("confirmationPass"))
            active_pass = evidence_pass and age <= max(1, int(.10 * config.display_bars)) and current_distance <= 1.5
        elif event["kind"] in {"52wHigh", "52wLow"}:
            evidence_pass = True
            active_pass = event["currentImpact"] == "high"
        elif event["kind"] == "movingAverageCross":
            evidence_pass = True
            active_pass = age <= config.event_relevance_bars
        else:
            evidence_pass = True
            active_pass = impact_pass
        event["evidencePass"] = evidence_pass
        event["activePass"] = active_pass
        event["hardPass"] = evidence_pass and active_pass
        event["rejectReasons"] = _event_reject_reasons(event)
    ordered = sorted(_dedupe(events), key=lambda item:(item["timestamp"],item["kind"],item["id"]))
    passed = [item for item in ordered if item["hardPass"]]
    rejected = []
    for kind in sorted({item["kind"] for item in ordered}):
        rejected.extend(sorted(
            (item for item in ordered if not item["hardPass"] and item["kind"] == kind),
            key=lambda item: (int(item.get("ageBars") or 0), item["id"]),
        )[:8])
    return sorted([*passed, *rejected], key=lambda item:(item["timestamp"],item["kind"],item["id"]))


def _moving_average_cross_events(candles, *, interval, display_start):
    if interval != "1D" or len(candles) < 121:
        return []
    closes = [float(row["close"]) for row in candles]
    prefix = [0.0]
    for close in closes:
        prefix.append(prefix[-1] + close)

    def average(end_index, period):
        return (prefix[end_index + 1] - prefix[end_index + 1 - period]) / period

    events = []
    for index in range(max(120, display_start), len(candles)):
        previous_short = average(index - 1, 60)
        current_short = average(index, 60)
        previous_long = average(index - 1, 120)
        current_long = average(index, 120)
        previous_difference = previous_short - previous_long
        current_difference = current_short - current_long
        if previous_difference <= 0 < current_difference:
            direction = "golden"
        elif previous_difference >= 0 > current_difference:
            direction = "dead"
        else:
            continue
        difference_change = current_difference - previous_difference
        fraction = max(0.0, min(1.0, -previous_difference / difference_change))
        short_cross = previous_short + fraction * (current_short - previous_short)
        long_cross = previous_long + fraction * (current_long - previous_long)
        row = candles[index]
        events.append(_event(row, "movingAverageCross", (short_cross + long_cross) / 2, [], {
            "direction": direction,
            "state": "crossed",
            "shortPeriod": 60,
            "longPeriod": 120,
            "previousShort": round(previous_short, 6),
            "previousLong": round(previous_long, 6),
            "currentShort": round(current_short, 6),
            "currentLong": round(current_long, 6),
        }, interval))
    return events


def _moving_average_cross_impact(age, config):
    if age <= max(1, int(.05 * config.display_bars)):
        return "high"
    if age <= config.event_relevance_bars:
        return "medium"
    return "low"


def _event(row, kind, price, refs, detail, interval):
    identity = f"{interval}|{kind}|{row.get('candleKey') or row['timestamp']}|{','.join(refs)}|{detail.get('episodeStart','')}"
    return {"id":f"{interval}:event:{hashlib.sha256(identity.encode()).hexdigest()[:10]}","timestamp":row["timestamp"],"candleKey":row.get("candleKey"),"kind":kind,"price":round(price,4),"refIds":refs,"detail":detail}


def _impact(index,total,current,price,atr,config,state):
    age=total-1-index; distance=abs(current-price)/atr
    if state in {"retest_confirmed","hold_confirmed","volume_confirmed","confirmed","failed","unfilled"} and distance<=1.5:return "high"
    if age<=max(1,int(.1*config.display_bars)) and distance<=1.5:return "high"
    if distance<=3 and age<=config.event_relevance_bars:return "medium"
    return "low"


def _dedupe(events):
    result={}
    for event in events:
        key=(event["kind"],tuple(event["refIds"]),event["detail"].get("episodeStart") or event["detail"].get("breakEventId") or event["id"])
        current=result.get(key)
        if current is None or (event["currentImpact"] in {"high","medium"},event["timestamp"],event["id"])>(current["currentImpact"] in {"high","medium"},current["timestamp"],current["id"]): result[key]=event
    return list(result.values())


def _event_reject_reasons(event):
    reasons = []
    if event.get("kind") == "breakout" and not event.get("detail", {}).get("confirmationPass"):
        reasons.append("breakout_unconfirmed")
    if not event.get("evidencePass"):
        reasons.append("no_structural_evidence")
    if event.get("currentImpact") not in {"high", "medium"}:
        reasons.append("not_currently_actionable")
    if not event.get("activePass") and event.get("evidencePass"):
        reasons.append("not_currently_actionable")
    if event.get("kind") in {"52wHigh", "52wLow"} and event.get("currentImpact") != "high":
        reasons.append("not_currently_actionable")
    return list(dict.fromkeys(reasons))
