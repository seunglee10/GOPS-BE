from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "systems" / "agent-orchestration" / "shared", ROOT / "systems" / "market-data" / "shared"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

MODULE_PATH = ROOT / "systems" / "agent-orchestration" / "pods" / "chart-asset-builder" / "main.py"
SPEC = importlib.util.spec_from_file_location("chart_asset_builder_main", MODULE_PATH)
assert SPEC and SPEC.loader
chart_asset_builder_main = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(chart_asset_builder_main)


class Message:
    def __init__(self, value):
        self.value = value


class FakeConsumer(list):
    def __init__(self, values):
        super().__init__(Message(value) for value in values)
        self.commits = 0

    def commit(self):
        self.commits += 1


class RecordingBuilder:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.envelopes = []

    def run(self, envelope):
        if self.error:
            raise self.error
        self.envelopes.append(envelope)


class ChartAssetWorkerTest(unittest.TestCase):
    def test_invalid_envelope_is_committed_and_valid_message_continues(self):
        consumer = FakeConsumer([
            {"jobId": "bad", "symbols": []},
            {"jobId": "bad-type", "symbols": 7},
            valid_message(),
        ])
        builder = RecordingBuilder()

        chart_asset_builder_main.consume_messages(consumer, builder)

        self.assertEqual(consumer.commits, 3)
        self.assertEqual([envelope.job_id for envelope in builder.envelopes], ["cab-12345678-worker"])

    def test_valid_job_runtime_failure_remains_uncommitted(self):
        consumer = FakeConsumer([valid_message()])
        builder = RecordingBuilder(RuntimeError("retryable runtime failure"))

        with self.assertRaises(RuntimeError):
            chart_asset_builder_main.consume_messages(consumer, builder)

        self.assertEqual(consumer.commits, 0)


def valid_message() -> dict:
    return {
        "jobId": "cab-12345678-worker",
        "requestedBy": "test",
        "submittedAt": "2026-07-11T00:00:00.000Z",
        "symbols": ["NVDA"],
        "intervals": ["1D"],
        "llmEnabled": False,
        "skipFreshHours": 0,
    }


if __name__ == "__main__":
    unittest.main()
