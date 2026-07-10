from __future__ import annotations

import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]


def load_yaml(relative_path: str):
    return yaml.safe_load((REPO_ROOT / relative_path).read_text(encoding="utf-8"))


class DeploymentContractsTest(unittest.TestCase):
    def test_kafka_uses_persistent_child_log_directory(self):
        kafka = load_yaml("infra/k8s/base/platform/kafka-statefulset.yaml")
        container = kafka["spec"]["template"]["spec"]["containers"][0]
        command = container["command"][2]
        mount = container["volumeMounts"][0]

        self.assertEqual(mount["mountPath"], "/var/lib/kafka/data")
        self.assertIn("mkdir -p /var/lib/kafka/data/data", command)
        self.assertIn("log.dirs=/var/lib/kafka/data/data", command)
        self.assertNotIn("rm -rf", command)

    def test_batch_nodepool_stays_warm_for_scheduled_jobs(self):
        dynamic_nodepool = load_yaml("infra/k8s/base/platform/nodepool-batch.yaml")
        warm_nodepool = load_yaml("infra/k8s/base/platform/nodepool-batch-warm.yaml")

        self.assertNotIn("replicas", dynamic_nodepool["spec"])
        self.assertEqual(warm_nodepool["metadata"]["name"], "batch-warm")
        self.assertEqual(warm_nodepool["spec"]["replicas"], 1)
        requirements = {
            item["key"]: item["values"]
            for item in warm_nodepool["spec"]["template"]["spec"]["requirements"]
        }
        self.assertEqual(requirements["eks.amazonaws.com/instance-cpu"], ["2"])
        self.assertEqual(requirements["eks.amazonaws.com/instance-memory"], ["8192"])

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
                    "batch-warm",
                )
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

    def test_order_workers_have_loop_heartbeat_probes(self):
        for path in (
            "infra/k8s/base/app/deployment-order-outbox-publisher.yaml",
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
            replicas[name] = next(item["value"] for item in operations if item["path"] == "/spec/replicas")

        self.assertEqual(replicas, {"alert-evaluator": 1, "recommendation-worker": 1})
        workflow = (REPO_ROOT / ".github/workflows/deploy-dev.yml").read_text(encoding="utf-8")
        self.assertNotIn("enable-ci-api-workers.sh", workflow)

    def test_deploy_waits_for_quality_gate(self):
        workflow = load_yaml(".github/workflows/deploy-dev.yml")
        workflow_text = (REPO_ROOT / ".github/workflows/deploy-dev.yml").read_text(encoding="utf-8")

        self.assertIn("quality", workflow["jobs"])
        self.assertEqual(workflow["jobs"]["deploy"]["needs"], "quality")
        self.assertIn("kubectl kustomize infra/k8s/base/platform", workflow_text)

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
