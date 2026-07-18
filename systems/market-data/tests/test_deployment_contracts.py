from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]


def load_yaml(relative_path: str):
    return yaml.safe_load((REPO_ROOT / relative_path).read_text(encoding="utf-8"))


class DeploymentContractsTest(unittest.TestCase):
    def test_chart_asset_runtime_is_postgres_alpaca_only(self):
        base = load_yaml("infra/k8s/base/app/configmap.yaml")["data"]
        self.assertEqual(base["CHART_ASSET_REPAIR_ALPACA_ENABLED"], "true")
        self.assertNotIn("CHART_ASSET_REPAIR_S3_TIMEOUT_SECONDS", base)
        self.assertNotIn("CHART_ASSET_STORAGE_MODE", base)
        self.assertNotIn("CHART_ASSET_BUILD_REQUESTS_TOPIC", base)
        self.assertEqual(base["CHART_ASSET_STORAGE_MAINTENANCE"], "false")

        compose_builder = load_yaml("docker-compose.yml")["services"]["chart-asset-builder"]
        compose_env = compose_builder["environment"]
        self.assertEqual(compose_env["CHART_ASSET_REPAIR_ALPACA_ENABLED"], "${CHART_ASSET_REPAIR_ALPACA_ENABLED:-true}")
        for name in ("REDIS_URL", "KAFKA_BOOTSTRAP_SERVERS", "S3_BUCKET"):
            self.assertNotIn(name, compose_env)
        self.assertEqual(compose_env["OPENAI_API_KEY"], "${OPENAI_API_KEY:-}")
        self.assertEqual(compose_env["CHART_COMMENTARY_PROVIDER"], "${CHART_COMMENTARY_PROVIDER:-disabled}")

        completed = subprocess.run(
            ["kubectl", "kustomize", "infra/k8s/overlays/aws-incluster-app-ci"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        resources = [item for item in yaml.safe_load_all(completed.stdout) if item]
        config = next(
            item
            for item in resources
            if item.get("kind") == "ConfigMap"
            and item.get("metadata", {}).get("name") == "alfaka-market-data-config"
        )
        data = config["data"]

        self.assertEqual(data["CHART_ASSET_REPAIR_ENABLED"], "true")
        self.assertEqual(data["CHART_ASSET_REPAIR_ALPACA_ENABLED"], "true")
        self.assertEqual(data["CHART_ASSET_REPAIR_CONCURRENCY"], "2")
        self.assertEqual(data["CHART_ASSET_REPAIR_MAX_RANGES"], "8")
        self.assertNotIn("CHART_ASSET_REPAIR_S3_TIMEOUT_SECONDS", data)

    def test_chart_asset_postgres_migration_is_one_shot_and_builder_waits_for_it(self):
        base_resources = load_yaml("infra/k8s/base/kustomization.yaml")["resources"]
        self.assertNotIn("job-chart-asset-migrations.yaml", base_resources)

        deployment = load_yaml("infra/k8s/base/app/deployment-chart-asset-builder.yaml")
        builder = deployment["spec"]["template"]["spec"]["containers"][0]
        env_from = builder["envFrom"]
        secret_names = {
            item["secretRef"]["name"]
            for item in env_from
            if "secretRef" in item
        }
        self.assertIn("alfaka-order-db-secret", secret_names)
        self.assertEqual(builder["resources"]["requests"]["memory"], "512Mi")
        self.assertEqual(builder["resources"]["limits"]["memory"], "1Gi")

        base_config = load_yaml("infra/k8s/base/app/configmap.yaml")["data"]
        aws_config = load_yaml("infra/k8s/overlays/aws/configmap-aws-patch.yaml")["data"]
        self.assertEqual(base_config["CHART_ASSET_BUILD_CONCURRENCY"], "2")
        self.assertEqual(aws_config["CHART_ASSET_BUILD_CONCURRENCY"], "2")

        migration = load_yaml("infra/k8s/base/job-chart-asset-migrations.yaml")
        container = migration["spec"]["template"]["spec"]["containers"][0]
        self.assertIn("chart-asset-migrations/main.py", " ".join(container["command"]))
        self.assertEqual(
            migration["spec"]["template"]["spec"]["nodeSelector"]["karpenter.sh/nodepool"],
            "batch",
        )
        self.assertEqual(
            migration["spec"]["template"]["spec"]["tolerations"][0]["value"],
            "batch",
        )
        required_secrets = {
            item["secretRef"]["name"]
            for item in container["envFrom"]
            if "secretRef" in item and item["secretRef"].get("optional") is False
        }
        self.assertEqual(required_secrets, {"alfaka-order-db-secret"})

        runner = (REPO_ROOT / "scripts/aws/run-chart-asset-migrations-job.sh").read_text(encoding="utf-8")
        self.assertIn("name: ${JOB_NAME}", runner)
        self.assertIn("namespace: ${K8S_NAMESPACE}", runner)
        self.assertNotIn("sync", runner)
        self.assertNotIn("verify", runner)

        compose = load_yaml("docker-compose.yml")
        compose_migration = compose["services"]["chart-asset-migrations"]
        self.assertNotIn("profiles", compose_migration)
        self.assertEqual(
            compose_migration["environment"]["CHART_ASSET_STORAGE_MAINTENANCE"],
            "${CHART_ASSET_STORAGE_MAINTENANCE:-false}",
        )
        self.assertEqual(compose_migration["restart"], "no")
        self.assertEqual(
            compose["services"]["chart-asset-builder"]["depends_on"]["chart-asset-migrations"]["condition"],
            "service_completed_successfully",
        )

    def test_chart_geometry_schedule_and_manual_job_use_operational_intervals(self):
        cron = load_yaml("infra/k8s/overlays/aws/scheduled/cronjob-chart-geometry-build.yaml")
        self.assertEqual(cron["spec"]["schedule"], "40 8 * * 1-5")
        self.assertEqual(cron["spec"]["timeZone"], "Asia/Seoul")
        self.assertEqual(cron["spec"]["concurrencyPolicy"], "Forbid")
        self.assertFalse(cron["spec"]["suspend"])
        container = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        intervals = next(item["value"] for item in container["env"] if item["name"] == "CHART_ASSET_INTERVALS")
        self.assertEqual(intervals, "1m,1D")
        manual = (REPO_ROOT / "scripts/aws/run-chart-geometry-build-job.sh").read_text(encoding="utf-8")
        self.assertIn('INTERVALS="${INTERVALS:-1m,1D}"', manual)

    def test_aws_overlay_preserves_chart_builder_memory_contract(self):
        completed = subprocess.run(
            ["kubectl", "kustomize", "infra/k8s/overlays/aws-incluster-app-ci"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        resources = [item for item in yaml.safe_load_all(completed.stdout) if item]
        deployment = next(
            item
            for item in resources
            if item.get("kind") == "Deployment"
            and item.get("metadata", {}).get("name") == "chart-asset-builder"
        )
        builder_resources = deployment["spec"]["template"]["spec"]["containers"][0]["resources"]

        self.assertEqual(builder_resources["requests"]["memory"], "512Mi")
        self.assertEqual(builder_resources["limits"]["memory"], "1Gi")

    def test_agent_shared_changes_rebuild_agent_and_backend_images(self):
        detector = (REPO_ROOT / "scripts/aws/detect-changed-services.sh").read_text(encoding="utf-8")
        dependency_case = (
            "systems/agent-orchestration/shared/* | systems/agent-orchestration/config/*)"
        )
        start = detector.index(dependency_case)
        end = detector.index(";;", start)
        branch = detector[start:end]
        self.assertIn("add_service backend", branch)
        self.assertIn("add_service agent-orchestrator", branch)

        image_lib = (REPO_ROOT / "scripts/aws/lib-gops-images.sh").read_text(encoding="utf-8")
        deployments = image_lib[image_lib.index("gops_deployments_for_service()") :]
        agent_case = deployments[deployments.index("agent-orchestrator)"):]
        agent_case = agent_case[:agent_case.index(";;")]
        self.assertIn("chart-asset-builder", agent_case)

    def test_market_shared_changes_rebuild_paper_order_matcher_image(self):
        detector = (REPO_ROOT / "scripts/aws/detect-changed-services.sh").read_text(encoding="utf-8")
        start = detector.index("systems/market-data/shared/*)")
        branch = detector[start:detector.index(";;", start)]
        self.assertIn("add_service order-worker", branch)

    def test_chart_asset_migration_runner_renders_custom_name(self):
        runner = REPO_ROOT / "scripts/aws/run-chart-asset-migrations-job.sh"
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_kubectl = temp / "kubectl"
            fake_kubectl.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "apply" ]]; then
  cp "$3" "${APPLIED_MANIFEST}"
fi
""",
                encoding="utf-8",
            )
            fake_kubectl.chmod(0o755)
            base_env = {
                **os.environ,
                "PATH": f"{temp}{os.pathsep}{os.environ['PATH']}",
                "IMAGE_TAG": "test-image",
                "ECR_AGENT_ORCHESTRATOR_REPO": "example.invalid/gops-agent",
                "APPLIED_MANIFEST": str(temp / "applied.yaml"),
            }

            completed = subprocess.run(
                [str(runner)],
                cwd=REPO_ROOT,
                env={
                    **base_env,
                    "CHART_ASSET_MIGRATIONS_JOB_NAME": "chart-assets-verify",
                    "K8S_NAMESPACE": "chart-assets-test",
                },
                check=True,
                capture_output=True,
                text=True,
            )
            applied = yaml.safe_load((temp / "applied.yaml").read_text(encoding="utf-8"))
            self.assertEqual(applied["metadata"]["name"], "chart-assets-verify")
            self.assertEqual(applied["metadata"]["namespace"], "chart-assets-test")
            self.assertNotIn("CHART_ASSET_MIGRATION_ACTION", str(applied))

    def test_kafka_uses_persistent_child_log_directory(self):
        kafka = load_yaml("infra/k8s/base/platform/kafka-statefulset.yaml")
        container = kafka["spec"]["template"]["spec"]["containers"][0]
        command = container["command"][2]
        mount = container["volumeMounts"][0]

        self.assertEqual(mount["mountPath"], "/var/lib/kafka/data")
        self.assertIn("mkdir -p /var/lib/kafka/data/data", command)
        self.assertIn("log.dirs=/var/lib/kafka/data/data", command)
        self.assertNotIn("rm -rf", command)

    def test_batch_nodepool_scales_from_zero_for_scheduled_jobs(self):
        dynamic_nodepool = load_yaml("infra/k8s/base/platform/nodepool-batch.yaml")
        platform_resources = load_yaml("infra/k8s/base/platform/kustomization.yaml")["resources"]

        self.assertNotIn("replicas", dynamic_nodepool["spec"])
        self.assertNotIn("nodepool-batch-warm.yaml", platform_resources)

        topic_init = load_yaml("infra/k8s/base/platform/kafka-topic-init-job.yaml")
        self.assertEqual(
            topic_init["spec"]["template"]["spec"]["nodeSelector"]["karpenter.sh/nodepool"],
            "batch",
        )
        order_migrations = load_yaml("infra/k8s/base/job-order-migrations.yaml")
        self.assertEqual(
            order_migrations["spec"]["template"]["spec"]["nodeSelector"]["karpenter.sh/nodepool"],
            "batch",
        )
        geometry_cron = load_yaml("infra/k8s/overlays/aws/scheduled/cronjob-chart-geometry-build.yaml")
        geometry_pod = geometry_cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        self.assertEqual(geometry_pod["nodeSelector"]["karpenter.sh/nodepool"], "batch")
        self.assertEqual(geometry_pod["tolerations"][0]["value"], "batch")

        reminder = load_yaml("infra/k8s/overlays/aws/scheduled/cronjob-notification-schedules.yaml")
        reminder_pod = reminder["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        self.assertEqual(reminder_pod["nodeSelector"]["karpenter.sh/nodepool"], "app-agent")
        self.assertEqual(reminder_pod["tolerations"][0]["value"], "app-agent")

    def test_interactive_company_journal_processing_reuses_app_capacity(self):
        manifest_path = (
            REPO_ROOT / "infra/k8s/overlays/aws/scheduled/cronjob-company-journal-worker.yaml"
        )
        resources = [
            item
            for item in yaml.safe_load_all(manifest_path.read_text(encoding="utf-8"))
            if item
        ]
        process_template = next(
            item
            for item in resources
            if item.get("kind") == "CronJob"
            and item.get("metadata", {}).get("name") == "gops-company-journal-process-template"
        )
        job_spec = process_template["spec"]["jobTemplate"]["spec"]
        processor_pod = job_spec["template"]["spec"]

        self.assertEqual(processor_pod["nodeSelector"]["karpenter.sh/nodepool"], "app-agent")
        self.assertEqual(processor_pod["tolerations"][0]["value"], "app-agent")
        self.assertGreaterEqual(job_spec["ttlSecondsAfterFinished"], 7 * 24 * 60 * 60)

        post_market = load_yaml("infra/k8s/overlays/aws/scheduled/cronjob-company-journal-post-market.yaml")
        post_market_pod = post_market["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        self.assertEqual(post_market_pod["nodeSelector"]["karpenter.sh/nodepool"], "batch")
        self.assertEqual(post_market_pod["tolerations"][0]["value"], "batch")

    def test_right_sized_nodeclasses_and_stateful_pools(self):
        app_nodeclass = load_yaml("infra/k8s/base/platform/nodeclass-app-agent.yaml")
        state_nodeclass = load_yaml("infra/k8s/base/platform/nodeclass-stateful.yaml")
        self.assertEqual(app_nodeclass["spec"]["ephemeralStorage"]["size"], "50Gi")
        self.assertEqual(state_nodeclass["spec"]["ephemeralStorage"]["size"], "20Gi")

        expected = {
            "cache-db": (["r5a"], ["2"], ["16384"]),
            "graphdb": (["r5a"], ["2"], ["16384"]),
            "streaming": (["m5a", "m6a"], ["2"], ["8192"]),
            "clickhouse": (["m5a", "m6a"], ["4"], ["16384"]),
        }
        for pool, (families, cpu, memory) in expected.items():
            with self.subTest(pool=pool):
                nodepool = load_yaml(f"infra/k8s/base/platform/nodepool-{pool}.yaml")
                template = nodepool["spec"]["template"]["spec"]
                self.assertEqual(template["nodeClassRef"]["name"], "gops-stateful-20")
                requirements = {item["key"]: item["values"] for item in template["requirements"]}
                self.assertEqual(requirements["eks.amazonaws.com/instance-family"], families)
                self.assertEqual(requirements["eks.amazonaws.com/instance-cpu"], cpu)
                self.assertEqual(requirements["eks.amazonaws.com/instance-memory"], memory)

        clickhouse = load_yaml("infra/k8s/base/platform/clickhouse-statefulset.yaml")
        resources = clickhouse["spec"]["template"]["spec"]["containers"][0]["resources"]
        self.assertEqual(resources["requests"]["cpu"], "3500m")
        self.assertEqual(resources["limits"]["cpu"], "4")

    def test_scheduled_jobs_have_resources_and_retain_failure_evidence(self):
        for path in (
            "infra/k8s/overlays/aws/scheduled/cronjob-order-flow-daily-rollup.yaml",
            "infra/k8s/overlays/aws/scheduled/cronjob-sec-fundamentals-sync.yaml",
        ):
            with self.subTest(path=path):
                cronjob = load_yaml(path)
                job_spec = cronjob["spec"]["jobTemplate"]["spec"]
                container = job_spec["template"]["spec"]["containers"][0]
                self.assertGreaterEqual(cronjob["spec"]["failedJobsHistoryLimit"], 7)
                self.assertGreaterEqual(job_spec["ttlSecondsAfterFinished"], 7 * 24 * 60 * 60)
                self.assertTrue(container["resources"]["requests"]["cpu"])
                self.assertTrue(container["resources"]["requests"]["memory"])
                self.assertTrue(container["resources"]["limits"]["memory"])
                self.assertEqual(
                    job_spec["template"]["spec"]["nodeSelector"]["karpenter.sh/nodepool"],
                    "batch",
                )
                if cronjob["metadata"]["name"] == "alfaka-order-flow-daily-rollup":
                    self.assertTrue(cronjob["spec"]["suspend"])
                if cronjob["metadata"]["name"] == "alfaka-sec-fundamentals-sync":
                    self.assertEqual(container["resources"]["limits"], {"cpu": "2", "memory": "6Gi"})

        app_overlay = load_yaml("infra/k8s/overlays/aws-incluster-app/kustomization.yaml")
        self.assertIn("../aws/scheduled", app_overlay["resources"])

    def test_replicated_market_processors_have_strict_spread_and_health_probes(self):
        for path in (
            "infra/k8s/base/app/deployment-market-processor.yaml",
            "infra/k8s/base/app/deployment-market-quote-processor.yaml",
        ):
            with self.subTest(path=path):
                deployment = load_yaml(path)
                pod_spec = deployment["spec"]["template"]["spec"]
                label = deployment["spec"]["selector"]["matchLabels"]["app"]
                constraint = pod_spec["topologySpreadConstraints"][0]
                container = pod_spec["containers"][0]
                self.assertEqual(constraint["maxSkew"], 1)
                self.assertEqual(constraint["minDomains"], 3)
                self.assertEqual(constraint["whenUnsatisfiable"], "DoNotSchedule")
                self.assertEqual(constraint["labelSelector"]["matchLabels"]["app"], label)
                self.assertIn("startupProbe", container)
                self.assertIn("readinessProbe", container)
                self.assertIn("livenessProbe", container)

        overlay = load_yaml("infra/k8s/overlays/aws-incluster-app/kustomization.yaml")
        processor_names = {"alfaka-market-processor", "alfaka-market-quote-processor"}
        constraints = {}
        for patch in overlay["patches"]:
            target = patch.get("target") or {}
            name = target.get("name")
            if name not in processor_names:
                continue
            operations = yaml.safe_load(patch["patch"])
            operation = next(
                item for item in operations
                if item["path"] == "/spec/template/spec/topologySpreadConstraints"
            )
            constraints[name] = operation["value"][0]

        self.assertEqual(set(constraints), processor_names)
        for constraint in constraints.values():
            self.assertEqual(constraint["minDomains"], 3)
            self.assertEqual(constraint["whenUnsatisfiable"], "DoNotSchedule")

    def test_market_processors_receive_clickhouse_credentials(self):
        processor_names = {"alfaka-market-processor", "alfaka-market-quote-processor"}
        for path in (
            "infra/k8s/base/app/deployment-market-processor.yaml",
            "infra/k8s/base/app/deployment-market-quote-processor.yaml",
        ):
            with self.subTest(path=path):
                deployment = load_yaml(path)
                container = deployment["spec"]["template"]["spec"]["containers"][0]
                clickhouse_secret = next(
                    (
                        item["secretRef"]
                        for item in container["envFrom"]
                        if item.get("secretRef", {}).get("name") == "alfaka-clickhouse-secret"
                    ),
                    None,
                )
                self.assertIsNotNone(clickhouse_secret)
                self.assertTrue(clickhouse_secret["optional"])

        overlay = load_yaml("infra/k8s/overlays/aws-incluster-app/kustomization.yaml")
        required_secret_patches = set()
        for patch in overlay["patches"]:
            target = patch.get("target") or {}
            name = target.get("name")
            if name not in processor_names:
                continue
            operations = yaml.safe_load(patch["patch"])
            if any(
                operation["path"]
                == "/spec/template/spec/containers/0/envFrom/1/secretRef/optional"
                and operation["value"] is False
                for operation in operations
            ):
                required_secret_patches.add(name)

        self.assertEqual(required_secret_patches, processor_names)

    def test_order_workers_have_loop_heartbeat_probes(self):
        for path in (
            "infra/k8s/base/app/deployment-order-outbox-publisher.yaml",
            "infra/k8s/base/app/deployment-paper-order-matcher.yaml",
            "infra/k8s/base/app/deployment-kis-broker-adapter.yaml",
        ):
            with self.subTest(path=path):
                container = load_yaml(path)["spec"]["template"]["spec"]["containers"][0]
                self.assertIn("startupProbe", container)
                self.assertIn("readinessProbe", container)
                self.assertIn("livenessProbe", container)

    def test_git_declares_api_workers_instead_of_copying_live_replicas(self):
        overlay_path = REPO_ROOT / "infra/k8s/overlays/aws-incluster-app/kustomization.yaml"
        overlay = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
        replicas = {}
        for patch in overlay["patches"]:
            target = patch.get("target") or {}
            name = target.get("name")
            if name not in {"alert-evaluator", "recommendation-worker"}:
                continue
            operations = yaml.safe_load(patch["patch"])
            for operation in operations:
                if operation["path"] == "/spec/replicas":
                    replicas[name] = operation["value"]

        self.assertEqual(replicas, {"alert-evaluator": 1, "recommendation-worker": 1})
        workflow = (REPO_ROOT / ".github/workflows/deploy-dev.yml").read_text(encoding="utf-8")
        self.assertNotIn("enable-ci-api-workers.sh", workflow)

    def test_deploy_waits_for_quality_gate(self):
        workflow = load_yaml(".github/workflows/deploy-dev.yml")
        workflow_text = (REPO_ROOT / ".github/workflows/deploy-dev.yml").read_text(encoding="utf-8")

        self.assertIn("quality", workflow["jobs"])
        self.assertEqual(workflow["jobs"]["deploy"]["needs"], "quality")
        self.assertIn("kubectl kustomize infra/k8s/base/platform", workflow_text)

    def test_dev_deploy_automatically_migrates_chart_assets_before_app_rollout(self):
        workflow = (REPO_ROOT / ".github/workflows/deploy-dev.yml").read_text(encoding="utf-8")

        self.assertIn("run_chart_asset_migrations:", workflow)
        self.assertIn("run-chart-asset-migrations-job.sh", workflow)
        self.assertIn(
            "contains(steps.changes.outputs.services, 'agent-orchestrator')",
            workflow,
        )
        self.assertIn(
            "run_chart_asset_migrations=true requires services to include agent-orchestrator.",
            workflow,
        )
        self.assertLess(
            workflow.index("run-chart-asset-migrations-job.sh"),
            workflow.index("name: Deploy app workloads"),
        )

    def test_local_dev_deploy_automatically_migrates_chart_assets_before_app_rollout(self):
        script = (REPO_ROOT / "scripts/aws/deploy-dev-local.sh").read_text(encoding="utf-8")

        self.assertIn('RUN_CHART_ASSET_MIGRATIONS="${RUN_CHART_ASSET_MIGRATIONS:-false}"', script)
        self.assertIn("REMOTE_BRANCH=branch-name", script)
        self.assertIn(
            "RUN_CHART_ASSET_MIGRATIONS=true requires agent-orchestrator to be selected.",
            script,
        )
        self.assertIn(
            'service_selected "agent-orchestrator" && ! is_true "${CHART_INTERPRETATION_ONLY}"',
            script,
        )
        self.assertIn("run-chart-asset-migrations-job.sh", script)
        main = script[script.index("main()") :]
        self.assertLess(
            main.index("run_migrations_if_requested"),
            main.index("deploy_app_workloads"),
        )

    def test_terraform_covers_all_current_images_with_immutable_tags(self):
        terraform = (REPO_ROOT / "infra/aws/terraform/main.tf").read_text(encoding="utf-8")

        self.assertIn('agent_orchestrator = "gops-agent-orchestrator"', terraform)
        self.assertIn('image_tag_mutability = "IMMUTABLE"', terraform)

    def test_retired_namespace_cleanup_is_review_first(self):
        script = (REPO_ROOT / "scripts/aws/cleanup-retired-gops-dev.sh").read_text(encoding="utf-8")

        self.assertIn("--apply", script)
        self.assertIn("dry-run", script)


if __name__ == "__main__":
    unittest.main()
