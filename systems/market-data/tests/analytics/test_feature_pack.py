from __future__ import annotations

import json
import math
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
if str(MARKET_SHARED) not in sys.path:
    sys.path.insert(0, str(MARKET_SHARED))

from market_data.analytics import KERNEL_VERSION, compute_feature_pack, normalize_candles  # noqa: E402
from market_data.analytics.analysis_candles import aggregate_analysis_candles  # noqa: E402
from market_data.analytics.events import _impact, compute_events  # noqa: E402
from market_data.analytics.levels import _role_state  # noqa: E402
from market_data.analytics.config import QUALITY_CONFIG  # noqa: E402
from market_data.analytics.trends import _independent_boundary_touches, compute_trends  # noqa: E402


FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "scenarios.json").read_text(encoding="utf-8"))


def scenario_candles(spec: dict) -> list[dict]:
    rows = []
    start = datetime(2025, 1, 2, tzinfo=timezone.utc)
    missing = set(spec.get("missingIndexes", []))
    previous_close = float(spec["base"])
    for index in range(int(spec["count"])):
        if index in missing:
            continue
        wave = float(spec["amplitude"]) * math.sin(index * math.pi / 6)
        close = float(spec["base"]) + float(spec["drift"]) * index + wave
        open_price = previous_close
        if index == spec.get("gapIndex"):
            open_price = previous_close + 8
            close = open_price + 1.5
        rows.append({
            "timestamp": (start + timedelta(days=index)).isoformat().replace("+00:00", "Z"),
            "open": round(open_price, 4),
            "high": round(max(open_price, close) + 1.2, 4),
            "low": round(min(open_price, close) - 1.2, 4),
            "close": round(close, 4),
            "volume": 8000 if index == spec.get("gapIndex") else 1000 + (index % 7) * 20,
            "isClosed": True,
        })
        previous_close = close
    return rows


class FeaturePackGoldenTest(unittest.TestCase):
    def test_kernel_version_is_fixed(self):
        self.assertEqual(KERNEL_VERSION, "kernel-v7")

    def test_golden_trend_scenarios(self):
        for name in ("uptrend", "downtrend", "range"):
            with self.subTest(name=name):
                spec = FIXTURES[name]
                features = compute_feature_pack(scenario_candles(spec), "1D")
                self.assertEqual(features["regime"]["trend"], spec["expectedRegime"])
                confirmed = [trend for trend in features["trends"] if trend["hardPass"]]
                kinds = {trend.get("direction") or trend["kind"] for trend in confirmed}
                if spec["expectedTrend"] == "range":
                    self.assertEqual(kinds, {"range"})
                else:
                    self.assertIn(spec["expectedTrend"], kinds)
                self.assertGreaterEqual(len(features["pivots"]), 4)
                self.assertTrue(confirmed)
                self.assertTrue(all(trend["touches"] >= 3 for trend in confirmed))
                self.assertTrue(all(level["touches"] >= 3 for level in features["levels"] if level["hardPass"]))
                self.assertTrue(all(sum(episode["outcome"] == "reaction" for episode in level["touchEpisodes"]) >= 2 for level in features["levels"] if level["hardPass"]))

    def test_gap_events_are_not_emitted(self):
        spec = FIXTURES["gap"]
        features = compute_feature_pack(scenario_candles(spec), "1D")
        self.assertNotIn("gap", {event["kind"] for event in features["events"]})

    def test_daily_ma60_ma120_crosses_are_emitted_at_the_cross_candle(self):
        for direction in ("golden", "dead"):
            with self.subTest(direction=direction):
                rows = _ma_cross_rows(direction)

                events = compute_events(
                    rows,
                    [],
                    atr=1,
                    display_from=rows[-120]["timestamp"],
                    interval="1D",
                )

                cross = next(item for item in events if item["kind"] == "movingAverageCross")
                self.assertEqual(cross["timestamp"], rows[157]["timestamp"])
                self.assertEqual(cross["candleKey"], rows[157]["candleKey"])
                self.assertEqual(cross["detail"]["direction"], direction)
                self.assertEqual(cross["detail"]["shortPeriod"], 60)
                self.assertEqual(cross["detail"]["longPeriod"], 120)
                self.assertTrue(cross["hardPass"])
                self.assertEqual(cross["ageBars"], 0)

    def test_ma60_ma120_crosses_require_121_daily_candles(self):
        rows = _ma_cross_rows("golden")

        insufficient = compute_events(
            rows[:120], [], atr=1, display_from=rows[0]["timestamp"], interval="1D",
        )
        weekly = compute_events(
            rows, [], atr=1, display_from=rows[-120]["timestamp"], interval="1W",
        )

        self.assertFalse(any(item["kind"] == "movingAverageCross" for item in insufficient))
        self.assertFalse(any(item["kind"] == "movingAverageCross" for item in weekly))

    def test_short_and_missing_history_are_deterministic(self):
        for name in ("short_history", "missing_bars"):
            with self.subTest(name=name):
                rows = scenario_candles(FIXTURES[name])
                self.assertEqual(len(normalize_candles(rows, "1D")), FIXTURES[name]["expectedCount"])
                first = compute_feature_pack(rows, "1D")
                second = compute_feature_pack(rows, "1D")
                self.assertEqual(
                    json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    json.dumps(second, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                )

    def test_feature_invariants(self):
        features = compute_feature_pack(scenario_candles(FIXTURES["uptrend"]), "1D")
        ids = [item["id"] for group in ("pivots", "levels", "trends", "events") for item in features[group]]
        self.assertEqual(len(ids), len(set(ids)))
        for pivot in features["pivots"]:
            self.assertGreaterEqual(pivot["confirmedAt"], pivot["timestamp"])
        for level in features["levels"]:
            self.assertEqual(level["price"], round(level["price"], 2))

    def test_full_lookback_is_bounded_and_range_uses_display_tail(self):
        spec = {**FIXTURES["range"], "count": 540}
        rows = normalize_candles(scenario_candles(spec), "1D")
        self.assertEqual(len(rows), 380)
        display = rows[-260:]

        trends = compute_trends(rows, [], display_from=display[0]["timestamp"], atr=2.0)

        self.assertEqual(trends[0]["kind"], "range")
        self.assertGreaterEqual(trends[0]["rangeFrom"], display[0]["timestamp"])
        self.assertEqual(trends[0]["rangeTo"], display[-1]["timestamp"])

    def test_two_point_or_currently_irrelevant_line_is_not_emitted(self):
        rows = normalize_candles(scenario_candles(FIXTURES["range"]), "1D")
        pivots = [
            {"id": "1D:pivot:a", "timestamp": rows[5]["timestamp"], "barIndex": 5, "price": rows[5]["low"], "kind": "L", "grade": "structural", "strength": .9},
            {"id": "1D:pivot:b", "timestamp": rows[25]["timestamp"], "barIndex": 25, "price": rows[25]["low"] + 1, "kind": "L", "grade": "structural", "strength": .9},
        ]
        trends = compute_trends(rows, pivots, display_from=rows[0]["timestamp"], atr=2.0)
        self.assertFalse(any(item["hardPass"] and item["kind"] in {"up", "down", "channel"} for item in trends))

    def test_one_wide_bar_cannot_confirm_both_range_boundaries(self):
        lower, upper, ambiguous = _independent_boundary_touches([2, 7, 12], [4, 7, 14])
        self.assertEqual(lower, [2, 12])
        self.assertEqual(upper, [4, 14])
        self.assertEqual(ambiguous, [7])

    def test_raw_price_contact_can_confirm_structural_anchor_line(self):
        rows = _line_rows(touches=(20, 60, 110))
        pivots = [
            _structural_pivot(rows, 20, "L"),
            _structural_pivot(rows, 60, "L"),
        ]

        trends = compute_trends(rows, pivots, display_from=rows[0]["timestamp"], atr=1.2)

        confirmed = next(item for item in trends if item["hardPass"] and item["kind"] == "up")
        self.assertGreaterEqual(confirmed["touches"], 3)
        self.assertGreaterEqual(confirmed["reactionCount"], 2)
        self.assertIn(rows[110]["candleKey"], confirmed["touchCandleKeys"])
        self.assertLessEqual(confirmed["medianResidualAtr"], .35)

    def test_parallel_opposite_pivots_promote_a_confirmed_channel(self):
        rows = _line_rows(touches=(20, 60, 110))
        pivots = [
            _structural_pivot(rows, 20, "L"),
            _structural_pivot(rows, 60, "L"),
            *[_structural_pivot(rows, index, "H") for index in (30, 70, 100)],
        ]

        trends = compute_trends(
            rows, pivots, display_from=rows[0]["timestamp"], atr=1.2,
        )

        channel = next(item for item in trends if item["kind"] == "channel" and item["hardPass"])
        self.assertEqual(channel["direction"], "up")
        self.assertEqual(len(channel["anchorPivotIds"]), 3)
        self.assertLessEqual(channel["parallelSlopeError"], .20)
        self.assertGreaterEqual(channel["containment"], .80)

    def test_historical_breach_revalidates_but_latest_breach_blocks(self):
        rows = _line_rows(touches=(20, 60, 110), breach=80)
        pivots = [_structural_pivot(rows, 20, "L"), _structural_pivot(rows, 60, "L")]
        revalidated = compute_trends(rows, pivots, display_from=rows[0]["timestamp"], atr=1.2)
        confirmed = next(item for item in revalidated if item["kind"] == "up" and item["hardPass"])
        self.assertFalse(confirmed["activeInvalidation"])

        latest_breach = _line_rows(touches=(20, 60, 95), breach=118)
        pivots = [_structural_pivot(latest_breach, 20, "L"), _structural_pivot(latest_breach, 60, "L")]
        rejected = compute_trends(latest_breach, pivots, display_from=latest_breach[0]["timestamp"], atr=1.2)
        line = next(item for item in rejected if item["kind"] == "up")
        self.assertFalse(line["hardPass"])
        self.assertIn("active_invalidation", line["rejectReasons"])

    def test_historical_outlier_is_trimmed_and_recent_outlier_blocks_geometry(self):
        historical = scenario_candles({**FIXTURES["uptrend"], "count": 260})
        historical[20]["high"] = historical[20]["close"] + 1000
        features = compute_feature_pack(historical, "1D")
        self.assertIn("abnormal_true_range_trimmed", features["qualityFlags"])
        self.assertNotIn("data_quality_blocked", features["qualityFlags"])

        recent = scenario_candles({**FIXTURES["uptrend"], "count": 220})
        recent[100]["high"] = recent[100]["close"] + 1000
        blocked = compute_feature_pack(recent, "1D")
        self.assertIn("data_quality_blocked", blocked["qualityFlags"])
        self.assertEqual(blocked["levels"], [])
        self.assertEqual(blocked["trends"], [])

        latest = scenario_candles({**FIXTURES["uptrend"], "count": 200})
        latest[-1]["high"] = latest[-1]["close"] + 1000
        latest_blocked = compute_feature_pack(latest, "1D")
        self.assertIn("data_quality_blocked", latest_blocked["qualityFlags"])
        self.assertEqual(latest_blocked["trends"], [])

    def test_invalid_ohlcv_trims_only_sufficient_history_and_blocks_latest(self):
        historical = scenario_candles({**FIXTURES["uptrend"], "count": 260})
        historical[20]["close"] = historical[20]["high"] + 1
        trimmed = compute_feature_pack(historical, "1D")
        self.assertIn("invalid_ohlcv_trimmed", trimmed["qualityFlags"])
        self.assertNotIn("data_quality_blocked", trimmed["qualityFlags"])

        latest = scenario_candles({**FIXTURES["uptrend"], "count": 220})
        latest[-1]["close"] = float("nan")
        blocked = compute_feature_pack(latest, "1D")
        self.assertIn("invalid_ohlcv", blocked["qualityFlags"])
        self.assertIn("data_quality_blocked", blocked["qualityFlags"])
        self.assertEqual(blocked["trends"], [])
        self.assertEqual(blocked["levels"], [])

    def test_breakout_requires_follow_through_or_latest_relative_volume(self):
        rows = _event_rows()
        level = {"id": "1D:level:confirmed", "price": 100, "zoneLow": 99.8, "zoneHigh": 100.2, "evidencePass": True}
        low_volume = compute_events(rows[:-1], [level], atr=1, display_from=rows[0]["timestamp"], interval="1D")
        self.assertFalse(any(item["kind"] == "breakout" and item["hardPass"] for item in low_volume))

        volume_confirmed = compute_events(rows, [level], atr=1, display_from=rows[0]["timestamp"], interval="1D")
        confirmed = next(item for item in volume_confirmed if item["kind"] == "breakout" and item["hardPass"])
        self.assertEqual(confirmed["rejectReasons"], [])

        invalidated_rows = [dict(item) for item in rows]
        invalidated_rows.append({
            **invalidated_rows[-1],
            "timestamp": "2025-01-26T00:00:00.000Z",
            "candleKey": "2025-01-26",
            "open": 100.0, "high": 100.1, "low": 99.0, "close": 99.7,
            "volume": 1000,
        })
        invalidated = compute_events(invalidated_rows, [level], atr=1, display_from=rows[0]["timestamp"], interval="1D")
        prior_break = next(item for item in invalidated if item["kind"] == "breakout" and item["detail"].get("state") == "invalidated")
        self.assertFalse(prior_break["hardPass"])
        self.assertIn("breakout_unconfirmed", prior_break["rejectReasons"])
        self.assertFalse(any(item["kind"] == "retest" and item["hardPass"] for item in invalidated))

        future_confirmed = {**level, "evidenceConfirmedIndex": len(rows) - 1}
        no_lookahead = compute_events(rows, [future_confirmed], atr=1, display_from=rows[0]["timestamp"], interval="1D")
        self.assertFalse(any(item["kind"] == "breakout" for item in no_lookahead))

        failed_rows = [dict(item) for item in rows]
        failed_rows[-1].update({"open": 100.0, "high": 100.1, "low": 99.0, "close": 99.7})
        failed = compute_events(failed_rows, [level], atr=1, display_from=rows[0]["timestamp"], interval="1D")
        failed_break = next(item for item in failed if item["kind"] == "breakout" and item["detail"].get("state") == "failed")
        self.assertFalse(failed_break["hardPass"])
        self.assertIn("breakout_unconfirmed", failed_break["rejectReasons"])

    def test_confirmed_level_role_does_not_require_a_second_hidden_reaction_gate(self):
        rows = [
            {"close": 101.0, "low": 99.8, "high": 101.2},
            {"close": 101.1, "low": 100.7, "high": 101.3},
        ]
        episodes = [{
            "startIndex": 0,
            "endIndex": 0,
            "approach": "above",
            "outcome": "reaction",
            "mfeAtr": 0.8,
        }]

        state = _role_state(
            rows, 99.8, 100.2, [1.0, 1.0], episodes, QUALITY_CONFIG["1D"],
        )

        self.assertEqual(state, "support_active")

    def test_level_role_recovers_after_false_break_closes_back_inside(self):
        resistance_rows = [
            {"close": 99.0, "low": 98.8, "high": 100.2},
            {"close": 101.0, "low": 100.4, "high": 101.2},
            {"close": 100.1, "low": 99.9, "high": 100.5},
        ]
        resistance_episode = [{
            "startIndex": 0, "endIndex": 0, "approach": "below",
            "outcome": "reaction", "mfeAtr": 0.8,
        }]
        support_rows = [
            {"close": 101.0, "low": 99.8, "high": 101.2},
            {"close": 99.0, "low": 98.8, "high": 99.6},
            {"close": 99.9, "low": 99.5, "high": 100.1},
        ]
        support_episode = [{
            "startIndex": 0, "endIndex": 0, "approach": "above",
            "outcome": "reaction", "mfeAtr": 0.8,
        }]

        self.assertEqual(
            _role_state(
                resistance_rows, 99.8, 100.2, [1.0] * 3,
                resistance_episode, QUALITY_CONFIG["1D"],
            ),
            "resistance_active",
        )
        self.assertEqual(
            _role_state(
                support_rows, 99.8, 100.2, [1.0] * 3,
                support_episode, QUALITY_CONFIG["1D"],
            ),
            "support_active",
        )

    def test_stale_medium_52_week_extreme_is_not_hard_passed(self):
        path = ROOT / "systems/market-data/tests/fixtures/chart_assets_v2/meta-1d.json"
        rows = [row for row in json.loads(path.read_text(encoding="utf-8")) if row["timestamp"] <= "2025-05-28T00:00:00.000Z"]
        weekly = aggregate_analysis_candles(rows, "1W")

        features = compute_feature_pack(weekly, "1W")

        stale_extremes = [event for event in features["events"] if event["kind"] in {"52wHigh", "52wLow"} and event["currentImpact"] == "medium"]
        self.assertTrue(stale_extremes)
        self.assertTrue(all(not event["hardPass"] for event in stale_extremes))

    def test_recent_but_distant_event_is_not_marked_high_impact(self):
        self.assertEqual(
            _impact(99, 100, 80, 100, 1, QUALITY_CONFIG["1D"], "unresolved"),
            "low",
        )


def _line_rows(*, touches, breach=None):
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(121):
        line = 100 + .10 * index
        low = line if index in touches else line + 2.0
        close = line + 1.2
        high = line + 2.4
        if breach == index:
            close, low, high = line - 2.0, line - 2.2, line + .2
        rows.append({
            "timestamp": (start + timedelta(days=index)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "candleKey": (start + timedelta(days=index)).date().isoformat(),
            "barIndex": index, "open": close, "high": high, "low": low,
            "close": close, "volume": 1000, "isClosed": True,
        })
    return rows


def _ma_cross_rows(direction: str) -> list[dict]:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(158):
        base = 120 - .4 * index if index < 100 else 80 + .5 * (index - 100)
        close = base if direction == "golden" else 200 - base
        timestamp = (start + timedelta(days=index)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        rows.append({
            "timestamp": timestamp,
            "candleKey": timestamp[:10],
            "barIndex": index,
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1000,
            "isClosed": True,
        })
    return rows


def _structural_pivot(rows, index, kind):
    return {"id": f"1D:pivot:{index}", "timestamp": rows[index]["timestamp"], "candleKey": rows[index]["candleKey"], "barIndex": index, "price": rows[index]["low" if kind == "L" else "high"], "kind": kind, "grade": "structural", "strength": .9}


def _event_rows():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(25):
        close = 99.5 if index < 23 else 100.8 if index == 23 else 101.0
        rows.append({
            "timestamp": (start + timedelta(days=index)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "candleKey": (start + timedelta(days=index)).date().isoformat(),
            "barIndex": index, "open": close, "high": close + .5, "low": close - .5,
            "close": close, "volume": 2000 if index == 24 else 1000, "isClosed": True,
        })
    return rows


if __name__ == "__main__":
    unittest.main()
