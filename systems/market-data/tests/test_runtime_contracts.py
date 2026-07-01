from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def read_repo_file(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def compose_service_block(compose_text: str, service_name: str) -> str:
    lines = compose_text.splitlines()
    header = f"  {service_name}:"
    for index, line in enumerate(lines):
        if line != header:
            continue
        block = [line]
        for next_line in lines[index + 1:]:
            if next_line.startswith("  ") and not next_line.startswith("    ") and next_line.strip().endswith(":"):
                break
            block.append(next_line)
        return "\n".join(block)
    raise AssertionError(f"docker-compose service not found: {service_name}")


class RuntimeContractTests(unittest.TestCase):
    def test_local_compose_keeps_alpaca_services_profile_gated(self) -> None:
        compose_text = read_repo_file("docker-compose.yml")

        for service in (
            "alpaca-ingestor",
            "alpaca-ingestor-iex",
            "alpaca-ingestor-boats",
            "alpaca-news-ingestor",
            "symbol-registry-sync",
        ):
            block = compose_service_block(compose_text, service)
            self.assertIn("profiles:", block, service)

    def test_runtime_configs_do_not_reintroduce_s3_sink_or_preload_jobs(self) -> None:
        checked_paths = [
            "docker-compose.yml",
            "infra/docker/Dockerfile.gops-market-storage",
            "infra/k8s/base/kustomization.yaml",
        ]
        forbidden = (
            "s3-sink",
            "raw-s3",
            "raw_s3",
            "initial-load",
            "coverage-repair",
            "s3_materializer",
        )

        for path in checked_paths:
            text = read_repo_file(path)
            for token in forbidden:
                self.assertNotIn(token, text, f"{path} must not contain {token}")

    def test_backfill_archive_env_contract_is_exposed_in_runtime_configs(self) -> None:
        required = (
            "BACKFILL_S3_ARCHIVE_ENABLED",
            "S3_BACKFILL_MANIFEST_PREFIX",
            "S3_BACKFILL_PROCESSED_PREFIX",
            "S3_BACKFILL_ARCHIVE_ROWS_PER_OBJECT",
        )
        checked_paths = [
            ".env.example",
            "docker-compose.yml",
            "infra/k8s/base/configmap.yaml",
            "infra/k8s/overlays/aws/configmap-aws-patch.yaml",
            "infra/k8s/overlays/aws-incluster-app/configmap-incluster-patch.yaml",
            "docs/ENVIRONMENT.md",
        ]

        for path in checked_paths:
            text = read_repo_file(path)
            for token in required:
                self.assertIn(token, text, f"{path} must document or expose {token}")

    def test_pipeline_required_components_env_is_exposed_in_runtime_configs(self) -> None:
        required = "PIPELINE_REQUIRED_COMPONENTS"
        checked_paths = [
            ".env.example",
            "systems/api-server/.env.example",
            "docker-compose.yml",
            "infra/k8s/base/configmap.yaml",
            "infra/k8s/overlays/aws/configmap-aws-patch.yaml",
            "docs/ENVIRONMENT.md",
        ]

        for path in checked_paths:
            text = read_repo_file(path)
            self.assertIn(required, text, f"{path} must document or expose {required}")

    def test_backfill_smoke_uses_half_open_clickhouse_range_contract(self) -> None:
        text = read_repo_file("scripts/local/smoke-backfill-missing-data.sh")

        self.assertIn("event_time < parseDateTimeBestEffort('${SMOKE_END}')", text)
        self.assertNotIn("event_time <= parseDateTimeBestEffort('${SMOKE_END}')", text)

    def test_request_config_does_not_expose_stale_live_s3_sink_contract(self) -> None:
        text = read_repo_file("systems/market-data/config/market-data-request.json")

        self.assertNotIn("livePrefix", text)
        self.assertNotIn("processedKafkaFormat", text)
        self.assertIn("clickhouseArchiveFormat", text)


if __name__ == "__main__":
    unittest.main()
