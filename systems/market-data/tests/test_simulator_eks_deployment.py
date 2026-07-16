import os
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SIMULATOR_ROOT = REPO_ROOT / "systems" / "simulator"


class SimulatorEksDeploymentContractTests(unittest.TestCase):
    def test_simulator_uses_fixed_replay_dataset_and_no_ttl_tables(self):
        dataset_source = (SIMULATOR_ROOT / "gops_simul" / "dataset.py").read_text(encoding="utf-8")
        schema = (REPO_ROOT / "infra" / "clickhouse" / "initdb" / "01-market-data.sql").read_text(encoding="utf-8")

        self.assertIn('DATASET_ID: Final = "sp500-top20-20260715-kst-v1"', dataset_source)
        self.assertIn("simulation_replay_events", schema)
        self.assertIn("simulation_replay_candles_1m", schema)
        replay_schema = schema.split("CREATE TABLE IF NOT EXISTS market_data.trade_ticks", 1)[0]
        self.assertNotIn("TTL", replay_schema)

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
        self.assertIn("cpu: 250m", rendered)
        self.assertIn("memory: 256Mi", rendered)
        self.assertIn("memory: 512Mi", rendered)
        self.assertIn("SIM_REPLAY_DATASET_ID", rendered)
        self.assertIn("CLICKHOUSE_URL", rendered)
        self.assertIn("REDIS_URL", rendered)
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

    def test_on_demand_scripts_only_switch_global_mode_and_preserve_live_pipeline(self):
        start_script = (REPO_ROOT / "scripts" / "aws" / "start-dev-simulator.sh").read_text(
            encoding="utf-8"
        )
        stop_script = (REPO_ROOT / "scripts" / "aws" / "stop-dev-simulator.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("gops-simulator --replicas=1", start_script)
        self.assertIn("GOPS_SIMULATOR_URL=http://gops-simulator:8765", start_script)
        self.assertIn("simulation_replay_datasets", start_script)
        self.assertIn('"READY"', start_script)
        self.assertIn("/api/control/mode", start_script)
        self.assertIn("set_simulator_mode simulation", start_script)
        self.assertNotIn("alfaka-alpaca-ingestor", start_script)
        self.assertNotIn("alfaka-market-processor", start_script)
        self.assertNotIn("trade-condition-executor", start_script)

        self.assertIn("GOPS_SIMULATOR_URL-", stop_script)
        self.assertIn("gops-simulator --replicas=0", stop_script)
        self.assertIn("simulator:replay:active-run", stop_script)
        self.assertNotIn("alfaka-alpaca-ingestor", stop_script)
        self.assertNotIn("alfaka-market-processor", stop_script)
        self.assertNotIn("trade-condition-executor", stop_script)

        for script in ("start-dev-simulator.sh", "stop-dev-simulator.sh", "run-simulator-replay-import.sh"):
            subprocess.run(
                ["bash", "-n", f"scripts/aws/{script}"],
                cwd=REPO_ROOT,
                check=True,
                env=os.environ.copy(),
            )

    def test_replay_import_job_is_suspended_and_uses_existing_secrets(self):
        manifest = (REPO_ROOT / "infra" / "k8s" / "base" / "job-simulator-replay-import.yaml").read_text(
            encoding="utf-8"
        )
        runner = (REPO_ROOT / "scripts" / "aws" / "run-simulator-replay-import.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("suspend: true", manifest)
        self.assertIn("serviceAccountName: alfaka-market-data-sa", manifest)
        self.assertIn("name: alfaka-alpaca-secret", manifest)
        self.assertIn("name: alfaka-clickhouse-secret", manifest)
        self.assertIn("--fixed-dataset", manifest)
        self.assertIn("suspend\":false", runner)
        self.assertIn("condition=Ready", runner)
        self.assertLess(runner.index("condition=Ready"), runner.index("kubectl logs -f"))
        self.assertIn("--local -o yaml", runner)
        self.assertNotIn('kubectl set image "job/${JOB_NAME}"', runner)
        self.assertIn("condition=complete", runner)
        self.assertIn("simulation_replay_datasets", runner)

    def test_local_deploy_can_target_a_committed_local_ref(self):
        deploy_script = (REPO_ROOT / "scripts" / "aws" / "deploy-dev-local.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('LOCAL_REF="${LOCAL_REF:-}"', deploy_script)
        self.assertIn('DEPLOY_TARGET_REF="local:${LOCAL_REF}"', deploy_script)
        self.assertIn('git rev-parse --verify "${LOCAL_REF}^{commit}"', deploy_script)


if __name__ == "__main__":
    unittest.main()
