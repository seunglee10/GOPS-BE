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
    def test_manifest_contains_the_streamlined_operator_runbook(self):
        manifest = json.loads((SCENARIO_ROOT / "scenario.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["scenarioId"], "saturday-demo-amd-iff-oke")
        self.assertEqual(manifest["symbols"], ["AMD", "OKE"])
        self.assertEqual(
            [phase["id"] for phase in manifest["phases"]],
            [
                "market-overview",
                "breaking-event",
                "market-close",
            ],
        )
        self.assertEqual(manifest["breakingNewsAtSeconds"], 210)
        self.assertEqual(
            manifest["seedPrices"],
            {"AMD": 565.0, "OKE": 90.0},
        )

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
        post_event_prices = {
            symbol: [
                row["payload"]["p"]
                for row in rows
                if row["payload"]["T"] == "t" and row["payload"]["S"] == symbol and row["atSeconds"] >= breaking_at
            ]
            for symbol in manifest["symbols"]
        }
        final_prices = {symbol: prices[-1] for symbol, prices in post_event_prices.items()}
        amd_decline_percent = (first_prices["AMD"] - final_prices["AMD"]) / first_prices["AMD"] * 100
        self.assertGreaterEqual(amd_decline_percent, 6.5)
        self.assertLessEqual(amd_decline_percent, 7.5)
        self.assertGreater(final_prices["OKE"], first_prices["OKE"] * 1.05)
        self.assertGreaterEqual(final_prices["AMD"], 524.0)
        self.assertLessEqual(final_prices["AMD"], 527.0)
        self.assertGreaterEqual(final_prices["OKE"], 95.0)
        self.assertLessEqual(final_prices["OKE"], 96.0)
        amd_prices = post_event_prices["AMD"]
        early_decline = amd_prices[0] - amd_prices[25]
        middle_decline = amd_prices[25] - amd_prices[65]
        late_decline = amd_prices[65] - amd_prices[-1]
        self.assertLessEqual(early_decline, 3.0)
        self.assertGreaterEqual(middle_decline, 25.0)
        self.assertLessEqual(late_decline, 6.0)
        self.assertGreater(middle_decline, early_decline * 8)
        for symbol, prices in post_event_prices.items():
            largest_tick_jump = max(abs(current - previous) for previous, current in zip(prices, prices[1:]))
            max_allowed_jump = 1.4 if symbol == "AMD" else 0.5
            self.assertLessEqual(largest_tick_jump, max_allowed_jump, symbol)

    def test_first_next_action_starts_the_breaking_event_immediately(self):
        clock = ManualClock()
        controller = DemoScenarioController(load_demo_scenario(SCENARIO_ROOT), clock=clock)
        started = controller.set_mode("simulation")

        self.assertEqual(started["phase"], "market-overview")
        self.assertEqual(started["nextPhase"], "breaking-event")
        status = controller.set_phase(str(started["nextPhase"]))

        self.assertEqual(status["phase"], "breaking-event")
        self.assertEqual(status["phaseIndex"], 1)
        self.assertEqual(status["elapsedSeconds"], 210)
        self.assertTrue(status["breakingNewsReleased"])
        self.assertEqual(status["nextPhase"], "market-close")
        self.assertEqual(len(status["phases"]), 3)

    def test_simulation_starts_natural_amd_and_oke_data_before_the_event(self):
        clock = ManualClock()
        controller = DemoScenarioController(load_demo_scenario(SCENARIO_ROOT), clock=clock)
        controller.set_mode("simulation")

        clock.value += 12
        running = controller.status()

        self.assertEqual(running["phase"], "market-overview")
        self.assertEqual(running["nextPhase"], "breaking-event")
        self.assertGreater(running["eventCount"], 0)
        self.assertFalse(running["breakingNewsReleased"])
        symbols = {item["symbol"]: item for item in running["symbols"]}
        self.assertEqual(set(symbols), {"AMD", "OKE"})
        self.assertNotEqual(symbols["AMD"]["price"], symbols["AMD"]["seedPrice"])
        self.assertNotEqual(symbols["OKE"]["price"], symbols["OKE"]["seedPrice"])

        clock.value += 999
        waiting = controller.status()
        self.assertEqual(waiting["phase"], "market-overview")
        self.assertLess(waiting["elapsedSeconds"], 210)
        self.assertFalse(waiting["breakingNewsReleased"])

    def test_replayed_messages_are_tagged_as_regular_session_simulation_data(self):
        rendered = demo_stream_payload(
            {"T": "q", "S": "AMD", "bp": 99.9, "ap": 100.1, "t": "2026-07-10T19:30:00Z"},
            status={
                "scenarioId": "saturday-demo-amd-iff-oke",
                "runId": "sim-test",
                "phase": "market-overview",
                "elapsedSeconds": 180,
            },
            sequence=12,
        )

        self.assertEqual(rendered["simulator"]["marketSession"], "regular")
        self.assertEqual(rendered["simulator"]["source"], "gops-simulator")
        self.assertEqual(rendered["simulator"]["runId"], "sim-test")

    def test_jumping_back_starts_a_fresh_run_so_stream_clients_can_replay_again(self):
        clock = ManualClock()
        controller = DemoScenarioController(load_demo_scenario(SCENARIO_ROOT), clock=clock)
        first = controller.set_mode("simulation")
        controller.set_phase("breaking-event")

        rewound = controller.set_phase("market-overview")

        self.assertNotEqual(rewound["runId"], first["runId"])
        self.assertEqual(rewound["phase"], "market-overview")
        self.assertEqual(rewound["elapsedSeconds"], 0)


if __name__ == "__main__":
    unittest.main()
