from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SIMULATOR_ROOT = REPO_ROOT / "systems" / "simulator"
if str(SIMULATOR_ROOT) not in sys.path:
    sys.path.insert(0, str(SIMULATOR_ROOT))

from gops_simul.demo import DemoScenarioController, load_demo_scenario
from gops_simul.replay import demo_stream_payload


SCENARIO_ROOT = SIMULATOR_ROOT / "data" / "scenarios" / "saturday-demo-amd-iff-oke"


class ManualClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


class SaturdayDemoScenarioTests(unittest.TestCase):
    def test_manifest_contains_the_full_operator_runbook(self):
        manifest = json.loads((SCENARIO_ROOT / "scenario.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["scenarioId"], "saturday-demo-amd-iff-oke")
        self.assertEqual(manifest["symbols"], ["AMD", "IFF", "OKE"])
        self.assertEqual(
            [phase["id"] for phase in manifest["phases"]],
            [
                "market-overview",
                "recommendation",
                "company-research",
                "chart-analysis",
                "order-ready",
                "market-open",
                "breaking-event",
                "market-close",
            ],
        )
        self.assertEqual(manifest["breakingNewsAtSeconds"], 210)

    def test_scenario_streams_matching_trades_and_quotes_with_the_expected_rotation(self):
        manifest = json.loads((SCENARIO_ROOT / "scenario.json").read_text(encoding="utf-8"))
        rows = [
            json.loads(line)
            for line in (SCENARIO_ROOT / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

        for symbol in manifest["symbols"]:
            trades = [row for row in rows if row["payload"]["T"] == "t" and row["payload"]["S"] == symbol]
            quotes = [row for row in rows if row["payload"]["T"] == "q" and row["payload"]["S"] == symbol]
            self.assertGreaterEqual(len(trades), 300, symbol)
            self.assertEqual(len(quotes), len(trades), symbol)
            for trade, quote in zip(trades, quotes, strict=True):
                self.assertEqual(trade["atSeconds"], quote["atSeconds"])
                self.assertLessEqual(quote["payload"]["bp"], trade["payload"]["p"])
                self.assertGreaterEqual(quote["payload"]["ap"], trade["payload"]["p"])

        breaking_at = float(manifest["breakingNewsAtSeconds"])
        first_prices = manifest["seedPrices"]
        final_prices = {
            symbol: [
                row["payload"]["p"]
                for row in rows
                if row["payload"]["T"] == "t" and row["payload"]["S"] == symbol and row["atSeconds"] >= breaking_at
            ][-1]
            for symbol in manifest["symbols"]
        }
        self.assertLess(final_prices["AMD"], first_prices["AMD"] * 0.94)
        self.assertGreater(final_prices["OKE"], first_prices["OKE"] * 1.05)

    def test_operator_can_jump_to_each_phase_without_waiting_for_wall_clock(self):
        clock = ManualClock()
        controller = DemoScenarioController(load_demo_scenario(SCENARIO_ROOT), clock=clock)
        controller.set_mode("simulation")

        status = controller.set_phase("breaking-event")

        self.assertEqual(status["phase"], "breaking-event")
        self.assertEqual(status["phaseIndex"], 6)
        self.assertEqual(status["elapsedSeconds"], 210)
        self.assertTrue(status["breakingNewsReleased"])
        self.assertEqual(status["nextPhase"], "market-close")
        self.assertEqual(len(status["phases"]), 8)

    def test_replayed_messages_are_tagged_as_regular_session_simulation_data(self):
        rendered = demo_stream_payload(
            {"T": "q", "S": "AMD", "bp": 99.9, "ap": 100.1, "t": "2026-07-10T19:30:00Z"},
            status={
                "scenarioId": "saturday-demo-amd-iff-oke",
                "runId": "sim-test",
                "phase": "market-open",
                "elapsedSeconds": 180,
            },
            sequence=12,
        )

        self.assertEqual(rendered["simulator"]["marketSession"], "regular")
        self.assertEqual(rendered["simulator"]["source"], "gops-simulator")
        self.assertEqual(rendered["simulator"]["runId"], "sim-test")


if __name__ == "__main__":
    unittest.main()
