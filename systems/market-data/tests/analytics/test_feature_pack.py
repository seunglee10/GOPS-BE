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

from alfaka.analytics import KERNEL_VERSION, compute_feature_pack, normalize_candles  # noqa: E402
from alfaka.analytics.analysis_candles import aggregate_analysis_candles  # noqa: E402
from alfaka.analytics.trends import compute_trends  # noqa: E402


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
        self.assertEqual(KERNEL_VERSION, "kernel-v2")

    def test_golden_trend_scenarios(self):
        for name in ("uptrend", "downtrend", "range"):
            with self.subTest(name=name):
                spec = FIXTURES[name]
                features = compute_feature_pack(scenario_candles(spec), "1D")
                self.assertEqual(features["regime"]["trend"], spec["expectedRegime"])
                kinds = {trend["kind"] for trend in features["trends"]}
                if spec["expectedTrend"] == "range":
                    self.assertEqual(kinds, {"range"})
                else:
                    self.assertIn(spec["expectedTrend"], kinds)
                self.assertGreaterEqual(len(features["pivots"]), 4)
                self.assertTrue(all(trend["hardPass"] for trend in features["trends"]))
                self.assertTrue(all(trend["touches"] >= 3 for trend in features["trends"]))
                self.assertTrue(all(level["touches"] >= 3 for level in features["levels"] if level["hardPass"]))
                self.assertTrue(all(sum(episode["outcome"] == "reaction" for episode in level["touchEpisodes"]) >= 2 for level in features["levels"] if level["hardPass"]))

    def test_gap_event_golden(self):
        spec = FIXTURES["gap"]
        features = compute_feature_pack(scenario_candles(spec), "1D")
        self.assertIn(spec["expectedEvent"], {event["kind"] for event in features["events"]})

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
        self.assertEqual(len(rows), 500)
        display = rows[-120:]

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
        self.assertFalse(any(item["kind"] in {"up", "down", "channel"} for item in trends))

    def test_stale_medium_52_week_extreme_is_not_hard_passed(self):
        path = ROOT / "systems/market-data/tests/fixtures/chart_assets_v2/meta-1d.json"
        rows = [row for row in json.loads(path.read_text(encoding="utf-8")) if row["timestamp"] <= "2025-05-28T00:00:00.000Z"]
        weekly = aggregate_analysis_candles(rows, "1W")

        features = compute_feature_pack(weekly, "1W")

        stale_extremes = [event for event in features["events"] if event["kind"] in {"52wHigh", "52wLow"} and event["currentImpact"] == "medium"]
        self.assertTrue(stale_extremes)
        self.assertTrue(all(not event["hardPass"] for event in stale_extremes))


if __name__ == "__main__":
    unittest.main()
