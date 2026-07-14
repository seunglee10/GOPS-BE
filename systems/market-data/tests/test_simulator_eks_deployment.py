import json
import os
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SIMULATOR_ROOT = REPO_ROOT / "systems" / "simulator"


class SimulatorEksDeploymentContractTests(unittest.TestCase):
    def test_simulator_image_contains_the_operator_controlled_saturday_scenario(self):
        scenario_path = (
            SIMULATOR_ROOT
            / "data"
            / "scenarios"
            / "saturday-demo-amd-iff-oke"
            / "scenario.json"
        )
        events_path = scenario_path.with_name("events.jsonl")

        scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
        event_types = {
            json.loads(line)["payload"]["T"]
            for line in events_path.read_text(encoding="utf-8").splitlines()
            if line
        }

        self.assertEqual(scenario["symbols"], ["AMD", "IFF", "OKE"])
        self.assertEqual(event_types, {"t", "q"})
        self.assertEqual(len(scenario["phases"]), 8)

    def test_simulator_image_contains_the_five_minute_demo_scenario(self):
        scenario_path = (
            SIMULATOR_ROOT
            / "data"
            / "scenarios"
            / "iran-ceasefire-collapse-2026-07-08"
            / "scenario.json"
        )
        events_path = scenario_path.with_name("events.jsonl")

        scenario = json.loads(scenario_path.read_text(encoding="utf-8"))

        self.assertEqual(scenario["durationSeconds"], 300)
        self.assertEqual(scenario["breakingNewsAtSeconds"], 5)
        self.assertEqual(
            scenario["symbols"],
            ["NVDA", "AMD", "AVGO", "MU", "TSM", "XOM", "CVX", "COP"],
        )
        self.assertEqual(
            sum(1 for line in events_path.read_text(encoding="utf-8").splitlines() if line),
            4_076,
        )

    def test_kustomize_renders_an_internal_scale_to_zero_simulator(self):
        completed = subprocess.run(
            ["kubectl", "kustomize", "infra/k8s/overlays/aws-incluster-app-ci"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        rendered = completed.stdout

        self.assertIn("name: gops-simulator", rendered)
        self.assertIn("replicas: 0", rendered)
        self.assertIn("cpu: 50m", rendered)
        self.assertIn("memory: 64Mi", rendered)
        self.assertIn("memory: 128Mi", rendered)
        self.assertIn("type: ClusterIP", rendered)
        self.assertNotIn("host: simulator.", rendered)

    def test_image_and_change_detection_scripts_recognize_simulator(self):
        completed = subprocess.run(
            [
                "bash",
                "-c",
                "source scripts/aws/lib-gops-images.sh; "
                "gops_normalize_service_key gops-simulator; "
                "gops_deployments_for_service simulator; "
                "gops_primary_deployment_for_service simulator",
            ],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            completed.stdout.splitlines(),
            ["simulator", "gops-simulator", "gops-simulator"],
        )

        detector = (REPO_ROOT / "scripts" / "aws" / "detect-changed-services.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("systems/simulator/*", detector)
        self.assertIn("Dockerfile.gops-simulator", detector)

    def test_on_demand_scripts_switch_only_the_sip_feed_and_restore_it(self):
        start_script = (REPO_ROOT / "scripts" / "aws" / "start-dev-simulator.sh").read_text(
            encoding="utf-8"
        )
        stop_script = (REPO_ROOT / "scripts" / "aws" / "stop-dev-simulator.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("gops-simulator --replicas=1", start_script)
        self.assertIn("GOPS_SIMULATOR_URL=http://gops-simulator:8765", start_script)
        self.assertIn("ALPACA_STREAM_BASE_URL=ws://gops-simulator:8765", start_script)
        self.assertIn("/api/control/mode", start_script)
        self.assertIn('{"mode":"live"}', start_script)
        self.assertIn("AMD,IFF,OKE", start_script)
        self.assertIn("ALPACA_CHANNELS=trades,quotes", start_script)
        self.assertIn("ORDER_FLOW_PINNED_SYMBOLS=AMD,IFF,OKE", start_script)
        self.assertIn("TRADE_CONDITION_EXECUTION_MODE=paper", start_script)
        self.assertIn("simulator_state_snapshot capture", start_script)
        self.assertIn("alfaka-alpaca-ingestor-sip", start_script)
        self.assertNotIn("alfaka-alpaca-ingestor-boats", start_script)
        self.assertNotIn("alfaka-alpaca-ingestor-crypto", start_script)

        self.assertIn("GOPS_SIMULATOR_URL-", stop_script)
        self.assertIn("ALPACA_STREAM_BASE_URL-", stop_script)
        self.assertIn(
            "ALPACA_ACTIVE_CHANNELS=bars,updatedBars,dailyBars,trades,quotes",
            stop_script,
        )
        self.assertNotIn("ALPACA_ACTIVE_CHANNELS-", stop_script)
        self.assertIn("gops-simulator --replicas=0", stop_script)
        self.assertIn("TRADE_CONDITION_EXECUTION_MODE=demo", stop_script)
        self.assertIn("simulator_state_snapshot restore", stop_script)

        for script in ("start-dev-simulator.sh", "stop-dev-simulator.sh"):
            subprocess.run(
                ["bash", "-n", f"scripts/aws/{script}"],
                cwd=REPO_ROOT,
                check=True,
                env=os.environ.copy(),
            )


if __name__ == "__main__":
    unittest.main()
