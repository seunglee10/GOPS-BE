from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from gops_agents.runtime.coach_snapshot_archive import CoachSnapshotArchive


REPO_ROOT = Path(__file__).resolve().parents[3]


def load_yaml(relative_path: str):
    return yaml.safe_load((REPO_ROOT / relative_path).read_text(encoding="utf-8"))


def render(relative_path: str) -> list[dict]:
    if shutil.which("kubectl") is None:
        raise unittest.SkipTest("kubectl is required for Kustomize contract validation")
    completed = subprocess.run(
        ["kubectl", "kustomize", relative_path],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [item for item in yaml.safe_load_all(completed.stdout) if isinstance(item, dict)]


class CoachAwsContractTests(unittest.TestCase):
    def test_archive_uses_one_conditional_encrypted_put(self):
        class FakeS3:
            def __init__(self):
                self.calls = []

            def put_object(self, **kwargs):
                self.calls.append(kwargs)

        client = FakeS3()
        snapshot = {
            "schemaVersion": "coach-input.v1",
            "request": {
                "analysisId": "analysis-1",
                "requestedAt": "2026-07-14T01:02:03Z",
            },
            "fills": [],
        }
        with patch.dict(
            "os.environ",
            {
                "AI_COACH_SNAPSHOT_ARCHIVE_ENABLED": "true",
                "AI_COACH_SNAPSHOT_ARCHIVE_REQUIRED": "false",
            },
            clear=False,
        ):
            result = CoachSnapshotArchive(
                client=client,
                bucket="coach-bucket",
                prefix="ai-coach/snapshots",
            ).put_once(snapshot, "analysis-1")

        self.assertEqual(result["status"], "stored")
        self.assertEqual(len(client.calls), 1)
        request = client.calls[0]
        self.assertEqual(
            request["Key"],
            "ai-coach/snapshots/v1/date=2026-07-14/analysis-1.json",
        )
        self.assertEqual(request["IfNoneMatch"], "*")
        self.assertEqual(request["ServerSideEncryption"], "AES256")
        self.assertEqual(request["Metadata"]["sha256"], result["sha256"])

    def test_local_defaults_do_not_require_aws_and_aws_overlays_fail_closed(self):
        base = load_yaml("infra/k8s/base/app/configmap.yaml")["data"]
        self.assertEqual(base["AGENT_ANALYSIS_QUEUE_BACKEND"], "auto")
        self.assertEqual(base["AGENT_REPORT_STORE_BACKEND"], "auto")
        self.assertEqual(base["AI_COACH_SNAPSHOT_ARCHIVE_ENABLED"], "false")
        self.assertEqual(base["AGENT_OUTPUT_KAFKA_REQUIRED"], "false")

        for overlay in (
            "infra/k8s/overlays/aws",
            "infra/k8s/overlays/aws-incluster-app",
            "infra/k8s/overlays/aws-incluster-app-ci",
        ):
            resources = render(overlay)
            config = next(
                item
                for item in resources
                if item.get("kind") == "ConfigMap"
                and item.get("metadata", {}).get("name") == "alfaka-market-data-config"
            )["data"]
            self.assertEqual(config["AGENT_ANALYSIS_QUEUE_BACKEND"], "kafka")
            self.assertEqual(config["AGENT_REPORT_STORE_BACKEND"], "redis")
            self.assertEqual(config["AGENT_OUTPUT_KAFKA_REQUIRED"], "true")
            self.assertEqual(config["AI_COACH_SNAPSHOT_ARCHIVE_ENABLED"], "true")
            self.assertEqual(config["AI_COACH_SNAPSHOT_ARCHIVE_REQUIRED"], "true")
            self.assertTrue(config["AI_COACH_SNAPSHOT_S3_BUCKET"])
            self.assertEqual(config["AI_COACH_SNAPSHOT_S3_PREFIX"], "ai-coach/snapshots")

    def test_analysis_worker_has_dedicated_irsa_and_required_account_secret(self):
        resources = render("infra/k8s/overlays/aws-incluster-app-ci")
        service_account = next(
            item
            for item in resources
            if item.get("kind") == "ServiceAccount"
            and item.get("metadata", {}).get("name") == "ai-coach-worker-sa"
        )
        role_arn = service_account["metadata"]["annotations"]["eks.amazonaws.com/role-arn"]
        self.assertTrue(role_arn.endswith(":role/alfaka-dev-ai-coach-worker-irsa"))

        deployment = next(
            item
            for item in resources
            if item.get("kind") == "Deployment"
            and item.get("metadata", {}).get("name") == "agent-analysis-worker"
        )
        pod = deployment["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "ai-coach-worker-sa")
        secret_refs = {
            item["secretRef"]["name"]: item["secretRef"].get("optional")
            for item in pod["containers"][0]["envFrom"]
            if "secretRef" in item
        }
        self.assertEqual(secret_refs["alfaka-clickhouse-secret"], False)
        self.assertEqual(secret_refs["alfaka-order-db-secret"], False)
        self.assertEqual(secret_refs["alfaka-openai-secret"], False)

        config = next(
            item
            for item in resources
            if item.get("kind") == "ConfigMap"
            and item.get("metadata", {}).get("name") == "alfaka-market-data-config"
        )["data"]
        self.assertTrue(config["REDIS_URL"])
        self.assertTrue(config["CLICKHOUSE_HTTP_URL"])
        self.assertTrue(config["GRAPHDB_SPARQL_URL"])
        self.assertTrue(config["AI_COACH_SNAPSHOT_S3_BUCKET"])

    def test_default_app_overlay_keeps_earnings_estimates_collector(self):
        resources = render("infra/k8s/overlays/aws-incluster-app-ci")
        cronjob = next(
            item
            for item in resources
            if item.get("kind") == "CronJob"
            and item.get("metadata", {}).get("name") == "alfaka-yahoo-estimates-sync"
        )
        container = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        env = {item["name"]: item.get("value") for item in container.get("env", [])}
        self.assertEqual(env["YAHOO_ESTIMATES_DRY_RUN"], "false")
        self.assertIn("systems/fundamentals/jobs/yahoo-estimates-sync/main.py", container["command"])

    def test_image_and_terraform_contain_snapshot_runtime(self):
        dockerfile = (REPO_ROOT / "infra/docker/Dockerfile.gops-agent-orchestrator").read_text(
            encoding="utf-8"
        )
        requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
        terraform = (REPO_ROOT / "infra/aws/terraform/main.tf").read_text(encoding="utf-8")
        variables = (REPO_ROOT / "infra/aws/terraform/variables.tf").read_text(encoding="utf-8")
        outputs = (REPO_ROOT / "infra/aws/terraform/outputs.tf").read_text(encoding="utf-8")

        self.assertIn("COPY systems/agent-orchestration", dockerfile)
        self.assertIn(
            "COPY systems/market-data/config/sp500-heatmap-seed.json",
            dockerfile,
        )
        self.assertIn("boto3", requirements)
        self.assertIn("psycopg", requirements)
        self.assertIn('resource "aws_s3_bucket" "ai_coach_snapshots"', terraform)
        self.assertIn('resource "aws_iam_role" "ai_coach_worker_irsa"', terraform)
        self.assertIn('"${aws_s3_bucket.ai_coach_snapshots.arn}/ai-coach/snapshots/*"', terraform)
        coach_policy = terraform[terraform.index('resource "aws_iam_policy" "ai_coach_worker"') :]
        coach_policy = coach_policy[: coach_policy.index('resource "aws_iam_role" "ai_coach_worker_irsa"')]
        self.assertIn('Action = ["s3:GetObject"]', coach_policy)
        self.assertIn('Action = ["s3:PutObject"]', coach_policy)
        self.assertIn("s3:GetObject", coach_policy)
        self.assertIn('"${aws_s3_bucket.ai_coach_snapshots.arn}/ai-coach/input/*"', coach_policy)
        self.assertIn('"${aws_s3_bucket.ai_coach_snapshots.arn}/ai-coach/reports/*"', coach_policy)
        self.assertNotIn("s3:ListBucket", coach_policy)
        self.assertIn(
            "noncurrent_days = var.ai_coach_snapshot_noncurrent_retention_days",
            terraform,
        )
        noncurrent_variable = variables[
            variables.index('variable "ai_coach_snapshot_noncurrent_retention_days"') :
        ]
        noncurrent_variable = noncurrent_variable[: noncurrent_variable.index("}\n") + 2]
        self.assertIn("default     = 1", noncurrent_variable)
        self.assertIn("<= 7", noncurrent_variable)
        self.assertIn('output "agent_orchestrator_ecr_repository_url"', outputs)
        self.assertIn('output "ai_coach_snapshot_s3_bucket"', outputs)
        self.assertIn('output "ai_coach_worker_irsa_role_arn"', outputs)

    def test_order_migration_is_automatic_for_agent_deploys(self):
        migration = (REPO_ROOT / "systems/order/shared/kis_trader/migrations/0006_ai_coach.sql").read_text(
            encoding="utf-8"
        )
        detector = (REPO_ROOT / "scripts/aws/detect-changed-services.sh").read_text(encoding="utf-8")
        workflow = (REPO_ROOT / ".github/workflows/deploy-dev.yml").read_text(encoding="utf-8")
        local_deploy = (REPO_ROOT / "scripts/aws/deploy-dev-local.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("ADD COLUMN IF NOT EXISTS user_sub", migration)
        self.assertIn("user_portfolio_snapshot_history", migration)
        self.assertIn("trade_decision_check_events", migration)
        order_branch = detector[detector.index("systems/order/shared/*)") :]
        order_branch = order_branch[: order_branch.index(";;")]
        self.assertIn("add_service order-worker", order_branch)
        self.assertIn("run_order_migrations", workflow)
        self.assertIn("run-order-migrations-job.sh", workflow)
        self.assertIn("steps.changes.outputs.order_migrations_required == 'true'", workflow)
        self.assertLess(
            workflow.index("run-order-migrations-job.sh"),
            workflow.index("name: Deploy app workloads"),
        )

        self.assertIn('if [[ "${key}" == "agent-orchestrator" ]]', detector)
        self.assertIn("add_service order-worker", detector)
        self.assertIn('write_output "order_migrations_required"', detector)
        migration_runner = local_deploy[local_deploy.index("run_migrations_if_requested()") :]
        migration_runner = migration_runner[: migration_runner.index("deploy_app_workloads()")]
        self.assertIn('service_selected "order-worker"', migration_runner)
        local_main = local_deploy[local_deploy.index("main()") :]
        self.assertLess(
            local_main.index("run_migrations_if_requested"),
            local_main.index("deploy_app_workloads"),
        )

        self.assertNotIn("declare -A", detector)
        completed = subprocess.run(
            ["bash", "scripts/aws/detect-changed-services.sh"],
            cwd=REPO_ROOT,
            env={**os.environ, "REQUESTED_SERVICES": "agent-orchestrator"},
            check=True,
            capture_output=True,
            text=True,
        )
        outputs = dict(
            line.split("=", 1)
            for line in completed.stdout.splitlines()
            if "=" in line
        )
        self.assertIn("agent-orchestrator", outputs["services"].split())
        self.assertIn("order-worker", outputs["services"].split())
        self.assertEqual(outputs["order_migrations_required"], "true")

    def test_image_tag_updater_runs_on_system_bash_without_associative_arrays(self):
        updater = (REPO_ROOT / "scripts/aws/update-ci-image-tags.sh").read_text(encoding="utf-8")
        self.assertNotIn("declare -A", updater)
        with tempfile.TemporaryDirectory() as temporary_dir:
            overlay = Path(temporary_dir) / "overlay"
            shutil.copytree(REPO_ROOT / "infra/k8s/overlays/aws-incluster-app-ci", overlay)
            completed = subprocess.run(
                ["bash", "scripts/aws/update-ci-image-tags.sh"],
                cwd=REPO_ROOT,
                env={
                    **os.environ,
                    "AWS_ACCOUNT_ID": "<aws-account-id>",
                    "IMAGE_TAG": "bash3-contract",
                    "KUSTOMIZE_OVERLAY": str(overlay),
                    "SERVICES": (
                        "frontend backend market-ingestor market-processor market-storage "
                        "order-worker kis-adapter agent-orchestrator simulator"
                    ),
                },
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("agent-orchestrator", completed.stdout)
            rendered = load_yaml(str(overlay / "kustomization.yaml"))
            self.assertTrue(rendered["images"])
            self.assertTrue(all(item["newTag"] == "bash3-contract" for item in rendered["images"]))

    def test_scheduled_aws_resources_trigger_their_runtime_service(self):
        detector = (REPO_ROOT / "scripts/aws/detect-changed-services.sh").read_text(encoding="utf-8")
        expected = {
            "cronjob-chart-geometry-build.yaml": "add_service agent-orchestrator",
            "cronjob-order-flow-daily-rollup.yaml": "add_service market-processor",
            "cronjob-notification-schedules.yaml": "add_service backend",
            "cronjob-sec-fundamentals-sync.yaml": "add_service market-storage",
            "cronjob-yahoo-estimates-sync.yaml": "add_service market-storage",
        }
        for filename, service_call in expected.items():
            branch = detector[detector.index(f"infra/k8s/overlays/aws/scheduled/{filename}") :]
            branch = branch[: branch.index(";;")]
            self.assertIn(service_call, branch)

    def test_market_config_changes_rebuild_agent_image_with_metadata_seed(self):
        detector = (REPO_ROOT / "scripts/aws/detect-changed-services.sh").read_text(encoding="utf-8")
        branch = detector[detector.index("systems/market-data/config/*)") :]
        branch = branch[: branch.index(";;")]
        self.assertIn("add_service agent-orchestrator", branch)

    def test_deploy_has_fail_closed_worker_irsa_snapshot_gate(self):
        workflow = (REPO_ROOT / ".github/workflows/deploy-dev.yml").read_text(encoding="utf-8")
        verifier = (REPO_ROOT / "scripts/aws/verify-ai-coach-snapshot-s3.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("Verify AI coach snapshot archive through worker IRSA", workflow)
        self.assertIn("verify-ai-coach-snapshot-s3.sh", workflow)
        self.assertIn(
            'EXPECTED_SERVICE_ACCOUNT="${AI_COACH_WORKER_SERVICE_ACCOUNT:-ai-coach-worker-sa}"',
            verifier,
        )
        self.assertIn('IfNoneMatch="*"', verifier)
        self.assertIn('ServerSideEncryption="AES256"', verifier)
        self.assertIn("get_object", verifier)
        self.assertIn("S3 GetObject digest verification failed", verifier)
        self.assertNotIn("delete_object", verifier)
        local_deploy = (REPO_ROOT / "scripts/aws/deploy-dev-local.sh").read_text(
            encoding="utf-8"
        )
        local_main = local_deploy[local_deploy.index("main()") :]
        self.assertLess(
            local_main.index("deploy_app_workloads"),
            local_main.index("verify_ai_coach_snapshot_archive"),
        )

    def test_portfolio_history_insert_is_atomic_and_change_only(self):
        repository = (
            REPO_ROOT
            / "systems/api-server/pods/api-server/gops-backend/app/recommendations/repository.py"
        ).read_text(encoding="utf-8")
        method = repository[repository.index("    def upsert_portfolio_snapshot", repository.index("class Postgres")) :]
        method = method[: method.index("    def latest_run")]

        self.assertIn("pg_advisory_xact_lock", method)
        self.assertIn("WITH previous AS", method)
        self.assertIn(
            "previous.payload - 'asOf' - 'sourceAsOf'",
            method,
        )
        self.assertIn("INSERT INTO user_portfolio_snapshot_history", method)
        self.assertIn("FROM upserted", method)


if __name__ == "__main__":
    unittest.main()
