import argparse
import contextlib
import importlib.util
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "aws" / "regenerate-s3-candles-from-clickhouse.py"
SPEC = importlib.util.spec_from_file_location("regenerate_s3_candles_from_clickhouse", SCRIPT_PATH)
recovery = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(recovery)


class S3CandleRecoveryToolTest(unittest.TestCase):
    def test_sql_filter_is_bounded_and_escapes_contract_values(self):
        args = _args(
            symbol=["aapl", "O'Reilly"],
            interval=["1m", "1D"],
            start="2026-07-01T00:00:00Z",
            end="2026-07-02T00:00:00Z",
        )

        where_sql = recovery.candle_where_sql(args)

        self.assertIn("is_closed = 1", where_sql)
        self.assertIn("price_adjustment = 'split'", where_sql)
        self.assertIn("canonical_version = 'v2'", where_sql)
        self.assertIn("symbol IN ('AAPL','O\\'REILLY')", where_sql)
        self.assertIn("interval IN ('1m','1D')", where_sql)
        self.assertIn("event_time >= parseDateTime64BestEffort('2026-07-01T00:00:00Z'", where_sql)
        self.assertIn("event_time < parseDateTime64BestEffort('2026-07-02T00:00:00Z'", where_sql)

    def test_recovery_partition_is_deterministic_for_group_and_run_id(self):
        group = {
            "symbol": "AAPL",
            "interval": "1m",
            "feed": "sip",
            "feed_profile": "sip",
            "day": "2026-07-08",
        }

        first = recovery.recovery_partition_key("final", group, "recovery-42")
        second = recovery.recovery_partition_key("final", dict(reversed(list(group.items()))), "recovery-42")

        self.assertEqual(first, second)
        self.assertEqual(
            first,
            "final/candles/feed=sip/interval=1m/symbol=AAPL/year=2026/month=07/day=08/backfill_request=recovery-42",
        )

    def test_dry_run_without_row_verification_never_creates_s3_client(self):
        args = _args(dry_run=True, verify_rows=False)
        groups = [{
            "symbol": "AAPL",
            "interval": "1m",
            "feed": "sip",
            "feed_profile": "sip",
            "day": "2026-07-08",
            "row_count": 2,
        }]
        output = io.StringIO()

        with mock.patch.dict(os.environ, {"S3_BUCKET": "bucket"}, clear=False):
            with mock.patch.object(recovery, "parse_args", return_value=args):
                with mock.patch.object(recovery, "ClickHouseHttpClient", return_value=object()):
                    with mock.patch.object(recovery, "select_candle_groups", return_value=groups):
                        with mock.patch.object(recovery, "create_s3_client", side_effect=AssertionError("dry-run must not create S3")):
                            with mock.patch.object(recovery, "select_candle_rows", side_effect=AssertionError("dry-run must not query rows")):
                                with contextlib.redirect_stdout(output):
                                    recovery.main()

        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([record["status"] for record in records], ["planned", "complete"])
        self.assertEqual(records[-1]["sourceRowCount"], 2)
        self.assertEqual(records[-1]["writtenObjectCount"], 0)


def _args(**overrides):
    values = {
        "dry_run": False,
        "symbol": [],
        "interval": [],
        "start": None,
        "end": None,
        "price_adjustment": "split",
        "canonical_version": "v2",
        "limit_groups": 0,
        "run_id": "recovery-test",
        "force": False,
        "output_format": "jsonl",
        "manifest_layout": "compact",
        "progress_every": 0,
        "verify_rows": False,
        "include_groups": False,
        "verbose_flush": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


if __name__ == "__main__":
    unittest.main()
