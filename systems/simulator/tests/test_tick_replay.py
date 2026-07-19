from __future__ import annotations

import sys
import unittest
import urllib.parse
import urllib.error
import gzip
import hashlib
import json
import tempfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from threading import Event
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[3]
SIMULATOR_ROOT = REPO_ROOT / "systems" / "simulator"
MARKET_DATA_SHARED_ROOT = REPO_ROOT / "systems" / "market-data" / "shared"
for source_root in (SIMULATOR_ROOT, MARKET_DATA_SHARED_ROOT):
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

from gops_simul.dataset import (
    ALLOWED_SPEEDS,
    DATASET_END,
    DATASET_ID,
    DATASET_S3_PREFIX,
    DATASET_START,
    COMPANY_BY_SYMBOL,
    EXPECTED_SYMBOL_COUNT,
    FEED_SEGMENTS,
    REPLAY_SYMBOL_SET,
    REPLAY_SYMBOLS,
    UNIVERSE_SYMBOLS_SHA256,
    dataset_manifest_template,
    in_half_open_window,
)
from gops_simul import env as simulator_env
from gops_simul.tick_replay import InMemoryReplayEventSource, ReplayController, ReplayEvent
from gops_simul.order_flow import ReplayOrderFlowProjection
from gops_simul.clickhouse import ClickHouseHttpClient, ClickHouseReplayEventSource
from gops_simul.tools import import_alpaca
from gops_simul.tools.import_alpaca import fetch_kind


class ManualClock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value


class MemoryStateStore:
    def __init__(self) -> None:
        self.snapshot = None

    def load_active(self):
        return self.snapshot

    def save(self, run_id, payload):
        self.snapshot = {**payload, "runId": run_id}

    def delete(self, run_id):
        if self.snapshot and self.snapshot.get("runId") == run_id:
            self.snapshot = None


def quote(sequence: int, seconds: float, symbol: str, bid: float, ask: float) -> ReplayEvent:
    timestamp = DATASET_START + timedelta(seconds=seconds)
    return ReplayEvent(
        sequence=sequence,
        timestamp=timestamp,
        feed="sip",
        payload={"T": "q", "S": symbol, "bp": bid, "ap": ask, "bs": 10, "as": 10, "t": timestamp.isoformat()},
    )


def trade(sequence: int, seconds: float, symbol: str, price: float) -> ReplayEvent:
    timestamp = DATASET_START + timedelta(seconds=seconds)
    return ReplayEvent(
        sequence=sequence,
        timestamp=timestamp,
        feed="sip",
        payload={"T": "t", "S": symbol, "p": price, "s": 1, "t": timestamp.isoformat()},
    )


class DatasetContractTests(unittest.TestCase):
    def test_clickhouse_materialization_uses_bounded_contiguous_windows(self):
        windows = list(import_alpaca._materialization_windows())

        self.assertEqual(len(windows), 96)
        self.assertEqual(windows[0][0], DATASET_START)
        self.assertEqual(windows[-1][1], DATASET_END)
        self.assertTrue(all(end - start == timedelta(minutes=15) for start, end in windows))
        self.assertTrue(all(left[1] == right[0] for left, right in zip(windows, windows[1:])))

    def test_clickhouse_materialization_carries_sequence_offset_between_windows(self):
        class FakeClickHouseClient:
            def __init__(self):
                self.count_index = 0
                self.executed = []

            def query_rows(self, _sql):
                values = [(3, 2), (2, 0)]
                events, trades = values[self.count_index] if self.count_index < len(values) else (0, 0)
                self.count_index += 1
                return [{"events": events, "trades": trades}]

            def execute(self, sql):
                self.executed.append(sql)

        client = FakeClickHouseClient()
        with patch("builtins.print"):
            import_alpaca._materialize_clickhouse(client)

        event_queries = [sql for sql in client.executed if "simulation_replay_events" in sql]
        candle_queries = [sql for sql in client.executed if "simulation_replay_candles_1m" in sql]
        self.assertEqual(len(event_queries), 2)
        self.assertIn("toUInt64(0) +", event_queries[0])
        self.assertIn("toUInt64(3) +", event_queries[1])
        self.assertTrue(all("event_time >=" in sql and "event_time <" in sql for sql in event_queries))
        self.assertEqual(len(candle_queries), 1)

    def test_clickhouse_http_error_includes_server_detail(self):
        error = urllib.error.HTTPError(
            "http://clickhouse:8123",
            500,
            "Internal Server Error",
            {},
            BytesIO(b"MEMORY_LIMIT_EXCEEDED"),
        )
        client = ClickHouseHttpClient("http://clickhouse:8123")

        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "MEMORY_LIMIT_EXCEEDED"):
                client.execute("SELECT 1")

    def test_failed_import_can_restore_verified_source_file_from_s3(self):
        rows = [
            {"T": "t", "S": "MMM", "p": 100.0, "s": 1, "t": "2026-07-14T15:00:01Z"},
            {"T": "t", "S": "MMM", "p": 100.1, "s": 2, "t": "2026-07-14T15:00:02Z"},
        ]
        compressed = gzip.compress(
            ("\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n").encode()
        )
        digest = hashlib.sha256(compressed).hexdigest()

        class FakeS3Client:
            def head_object(self, **_kwargs):
                return {
                    "ContentLength": len(compressed),
                    "Metadata": {"sha256": digest, "row-count": str(len(rows))},
                }

            def download_file(self, _bucket, _key, filename, **_kwargs):
                Path(filename).write_bytes(compressed)

        class FakeStagingWriter:
            def __init__(self):
                self.rows = []

            def add(self, row):
                self.rows.append(row)

        writer = FakeStagingWriter()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            import_alpaca, "_s3_client", return_value=FakeS3Client()
        ):
            entry, row_count = import_alpaca._restore_fixed_file_from_s3(
                root=Path(directory),
                file_ordinal=0,
                segment_index=1,
                segment=FEED_SEGMENTS[0],
                symbol="MMM",
                kind="trades",
                base_url="https://data.alpaca.markets",
                headers={},
                limit=10_000,
                max_pages=None,
                s3_prefix="s3://example/replay",
                staging_writer=writer,
                local_only=False,
                stop_event=Event(),
            )

        self.assertEqual(row_count, 2)
        self.assertEqual(entry["sha256"], digest)
        self.assertEqual([row["source_sequence"] for row in writer.rows], [1, 2])
        self.assertEqual([row["event_type"] for row in writer.rows], ["trade", "trade"])

    def test_clickhouse_import_batches_are_large_enough_for_full_tick_volume(self):
        self.assertGreaterEqual(import_alpaca.CLICKHOUSE_INSERT_BATCH_SIZE, 250_000)

    def test_clickhouse_staging_writer_combines_rows_across_symbol_files(self):
        class FakeClickHouseClient:
            def __init__(self):
                self.batches = []

            def insert_json_each_row(self, table, rows):
                self.batches.append((table, list(rows)))

        client = FakeClickHouseClient()
        writer = import_alpaca.ClickHouseStagingWriter(client, batch_size=3)

        writer.add({"symbol": "AAPL"})
        writer.add({"symbol": "AMD"})
        self.assertEqual(client.batches, [])
        writer.add({"symbol": "OKE"})
        writer.add({"symbol": "WMT"})
        writer.flush()

        self.assertEqual([len(rows) for _table, rows in client.batches], [3, 1])
        self.assertTrue(all(table == "market_data.simulation_replay_staging" for table, _rows in client.batches))

    def test_parallel_import_uses_deterministic_file_and_row_sequence(self):
        self.assertGreaterEqual(import_alpaca.DEFAULT_IMPORT_WORKERS, 4)
        self.assertEqual(import_alpaca.deterministic_source_sequence(0, 1), 1)
        self.assertGreater(
            import_alpaca.deterministic_source_sequence(1, 1),
            import_alpaca.deterministic_source_sequence(0, 999_999_999),
        )

    def test_clickhouse_http_client_accepts_iso8601_event_timestamps(self):
        request = ClickHouseHttpClient("http://clickhouse:8123")._request(b"SELECT 1")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        self.assertEqual(query["date_time_input_format"], ["best_effort"])

    def test_clickhouse_http_client_returns_timezone_aware_iso_timestamps(self):
        request = ClickHouseHttpClient("http://clickhouse:8123")._request(b"SELECT now64()")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        self.assertEqual(query["date_time_output_format"], ["iso"])

    def test_clickhouse_previous_close_snapshot_selects_all_replay_symbols_from_canonical_daily_rows(self):
        class FakeClickHouseClient:
            def __init__(self):
                self.queries = []

            def query_rows(self, sql):
                self.queries.append(sql)
                if "simulation_replay_datasets" in sql:
                    return [{"status": "READY", "total_events": 2}]
                return [
                    {"symbol": symbol, "previous_close": 100.0 + index}
                    for index, symbol in enumerate(REPLAY_SYMBOLS)
                ]

        client = FakeClickHouseClient()
        source = ClickHouseReplayEventSource(client, DATASET_ID)

        previous_closes = source.previous_close_snapshot()

        self.assertEqual(len(previous_closes), EXPECTED_SYMBOL_COUNT)
        self.assertEqual(previous_closes[REPLAY_SYMBOLS[0]], 100.0)
        query = client.queries[-1]
        self.assertIn("argMax(close", query)
        self.assertIn("canonical_version = 'v2'", query)
        self.assertIn("price_adjustment = 'split'", query)
        self.assertIn("market_session = 'regular'", query)
        self.assertIn("2026-07-13", query)

    def test_clickhouse_previous_close_snapshot_rejects_an_incomplete_baseline(self):
        class FakeClickHouseClient:
            def query_rows(self, sql):
                if "simulation_replay_datasets" in sql:
                    return [{"status": "READY", "total_events": 2}]
                return [{"symbol": "NVDA", "previous_close": 100.0}]

        source = ClickHouseReplayEventSource(FakeClickHouseClient(), DATASET_ID)

        with self.assertRaisesRegex(RuntimeError, "previous close baseline is incomplete"):
            source.previous_close_snapshot()

    def test_replay_sequence_query_does_not_scan_by_virtual_time(self):
        class FakeClickHouseClient:
            def __init__(self):
                self.queries = []

            def query_rows(self, sql):
                self.queries.append(sql)
                if "simulation_replay_datasets" in sql:
                    return [{"status": "READY", "total_events": 2}]
                return [
                    {
                        "sequence": 1,
                        "event_time": (DATASET_START + timedelta(seconds=1)).isoformat(),
                        "feed": "sip",
                        "payload": json.dumps({"T": "q", "S": "NVDA", "bp": 99, "ap": 100}),
                    },
                    {
                        "sequence": 2,
                        "event_time": (DATASET_START + timedelta(seconds=3)).isoformat(),
                        "feed": "sip",
                        "payload": json.dumps({"T": "t", "S": "NVDA", "p": 100, "s": 1}),
                    },
                ]

        client = FakeClickHouseClient()
        source = ClickHouseReplayEventSource(client, DATASET_ID)

        events = source.events_after(0, DATASET_START + timedelta(seconds=2), 50_000)

        self.assertEqual([event.sequence for event in events], [1])
        replay_query = client.queries[-1]
        self.assertIn("sequence > 0", replay_query)
        self.assertIn("ORDER BY sequence LIMIT 50000", replay_query)
        self.assertNotIn("event_time <=", replay_query)

    def test_daily_replay_candles_use_new_york_market_midnight_and_stay_live(self):
        class FakeClickHouseClient:
            def __init__(self):
                self.queries = []

            def query_rows(self, sql):
                self.queries.append(sql)
                if "simulation_replay_datasets" in sql:
                    return [{"status": "READY", "total_events": 2}]
                return [{
                    "market_date": "2026-07-14",
                    "open": 170.0,
                    "high": 171.0,
                    "low": 169.5,
                    "close": 170.5,
                    "volume": 1000,
                    "trade_count": 2,
                }]

        client = FakeClickHouseClient()
        source = ClickHouseReplayEventSource(client)

        payload = source.candle_snapshot(
            "NVDA",
            "1D",
            datetime(2026, 7, 14, 15, 1, tzinfo=UTC),
            20,
        )

        self.assertIn("America/New_York", client.queries[-1])
        self.assertEqual(payload["candles"][0]["timestamp"], "2026-07-14T04:00:00.000Z")
        self.assertFalse(payload["candles"][0]["isClosed"])

    def test_daily_replay_closes_only_the_previous_new_york_market_day(self):
        class FakeClickHouseClient:
            def query_rows(self, sql):
                if "simulation_replay_datasets" in sql:
                    return [{"status": "READY", "total_events": 4}]
                return [
                    {"market_date": "2026-07-15", "open": 172, "high": 173, "low": 171, "close": 172.5, "volume": 20, "trade_count": 2},
                    {"market_date": "2026-07-14", "open": 170, "high": 172, "low": 169, "close": 171.5, "volume": 30, "trade_count": 2},
                ]

        source = ClickHouseReplayEventSource(FakeClickHouseClient())

        payload = source.candle_snapshot(
            "NVDA",
            "1D",
            datetime(2026, 7, 15, 14, 30, tzinfo=UTC),
            20,
        )

        self.assertEqual([item["timestamp"] for item in payload["candles"]], [
            "2026-07-14T04:00:00.000Z",
            "2026-07-15T04:00:00.000Z",
        ])
        self.assertEqual([item["isClosed"] for item in payload["candles"]], [True, False])

    def test_installed_layout_uses_the_application_root_env_candidate(self):
        self.assertEqual(
            simulator_env.repository_env_path(Path("/app/gops_simul/env.py")),
            Path("/app/.env"),
        )

    def test_dataset_is_the_fixed_kst_day_with_the_pinned_sp500_universe(self):
        canonical = json.loads(
            (REPO_ROOT / "systems" / "market-data" / "config" / "sp500-universe.json").read_text()
        )
        self.assertEqual(DATASET_ID, "sp500-full-20260715-kst-v3")
        self.assertEqual(DATASET_S3_PREFIX, "simulator/replay/v3/dataset=sp500-full-20260715-kst")
        self.assertEqual(DATASET_START, datetime(2026, 7, 14, 15, 0, tzinfo=UTC))
        self.assertEqual(DATASET_END, datetime(2026, 7, 15, 15, 0, tzinfo=UTC))
        self.assertEqual(EXPECTED_SYMBOL_COUNT, 502)
        self.assertEqual(REPLAY_SYMBOLS, tuple(canonical["symbols"]))
        self.assertEqual(REPLAY_SYMBOL_SET, frozenset(REPLAY_SYMBOLS))
        self.assertEqual(UNIVERSE_SYMBOLS_SHA256, "c1e72d49557182d11cd64d33bba16778f7b4184e5dfd58b921f2b46fe0d10cef")
        self.assertTrue({"AMD", "MU", "OKE"}.issubset(REPLAY_SYMBOL_SET))
        self.assertEqual(COMPANY_BY_SYMBOL["AMD"], "Advanced Micro Devices")
        self.assertEqual(COMPANY_BY_SYMBOL["MU"], "Micron Technology")
        self.assertEqual(COMPANY_BY_SYMBOL["CAG"], "CAG")
        self.assertEqual(len(COMPANY_BY_SYMBOL), EXPECTED_SYMBOL_COUNT)
        self.assertEqual([segment.feed for segment in FEED_SEGMENTS], ["sip", "boats", "sip"])
        self.assertEqual(ALLOWED_SPEEDS, (1, 2, 5, 10))

        manifest = dataset_manifest_template()
        self.assertEqual(manifest["universe"]["symbolCount"], EXPECTED_SYMBOL_COUNT)
        self.assertEqual(manifest["universe"]["symbolsSha256"], UNIVERSE_SYMBOLS_SHA256)

    def test_alpaca_rate_limit_is_retried_with_backoff(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"trades": {}}'

        rate_limited = urllib.error.HTTPError(
            "https://example.test",
            429,
            "rate limited",
            {"Retry-After": "0"},
            None,
        )
        with patch(
            "gops_simul.tools.import_alpaca.urllib.request.urlopen",
            side_effect=[rate_limited, FakeResponse()],
        ), patch("gops_simul.tools.import_alpaca.time_module.sleep") as sleep:
            payload = import_alpaca.fetch_json("https://example.test", {})

        self.assertEqual(payload, {"trades": {}})
        sleep.assert_called_once_with(import_alpaca.ALPACA_RETRY_BASE_SECONDS)

    def test_import_result_requires_every_file_for_all_502_symbols(self):
        manifest = {}
        counts = {"events": 123}
        completed = {
            symbol: len(FEED_SEGMENTS) * 2
            for symbol in REPLAY_SYMBOLS
        }
        completed[REPLAY_SYMBOLS[-1]] -= 1

        import_alpaca._update_import_result(
            manifest,
            counts,
            completed,
            {REPLAY_SYMBOLS[-1]},
        )

        self.assertEqual(manifest["importResult"]["requestedSymbolCount"], 502)
        self.assertEqual(manifest["importResult"]["successfulSymbolCount"], 501)
        self.assertEqual(manifest["importResult"]["storedRowCount"], 123)
        self.assertEqual(manifest["importResult"]["errorSymbols"], [REPLAY_SYMBOLS[-1]])

    def test_half_open_filter_rejects_the_exact_end_boundary(self):
        self.assertTrue(in_half_open_window(DATASET_START, DATASET_START, DATASET_END))
        self.assertTrue(in_half_open_window(DATASET_END - timedelta(microseconds=1), DATASET_START, DATASET_END))
        self.assertFalse(in_half_open_window(DATASET_END, DATASET_START, DATASET_END))

    def test_importer_follows_every_page_and_filters_end_boundary(self):
        pages = {
            None: {
                "trades": {"NVDA": [{"t": "2026-07-14T15:00:00Z", "p": 100}]},
                "next_page_token": "p2",
            },
            "p2": {
                "trades": {"NVDA": [{"t": "2026-07-14T15:00:01Z", "p": 101}]},
                "next_page_token": "p3",
            },
            "p3": {
                "trades": {"NVDA": [{"t": "2026-07-15T00:00:00Z", "p": 102}]},
                "next_page_token": None,
            },
        }

        def fake_fetch(url, _headers):
            token = None
            if "page_token=p2" in url:
                token = "p2"
            elif "page_token=p3" in url:
                token = "p3"
            return pages[token]

        with patch("gops_simul.tools.import_alpaca.fetch_json", side_effect=fake_fetch):
            rows = fetch_kind(
                kind="trades",
                base_url="https://example.test",
                symbol="NVDA",
                feed="sip",
                start="2026-07-14T15:00:00Z",
                end="2026-07-15T00:00:00Z",
                limit=1,
                headers={},
            )

        self.assertEqual([row["p"] for row in rows], [100, 101])

    def test_importer_rejects_a_repeated_page_token(self):
        payload = {
            "quotes": {"NVDA": [{"t": "2026-07-14T15:00:00Z", "bp": 99, "ap": 100}]},
            "next_page_token": "same-token",
        }
        with patch("gops_simul.tools.import_alpaca.fetch_json", return_value=payload):
            with self.assertRaisesRegex(RuntimeError, "repeated next_page_token"):
                fetch_kind(
                    kind="quotes",
                    base_url="https://example.test",
                    symbol="NVDA",
                    feed="sip",
                    start="2026-07-14T15:00:00Z",
                    end="2026-07-15T00:00:00Z",
                    limit=1,
                    headers={},
                )


class ReplayOrderFlowProjectionTests(unittest.TestCase):
    def test_classification_uses_replay_quote_age_and_cent_bins(self):
        source = InMemoryReplayEventSource([
            quote(1, 1, "NVDA", 100.0, 101.0),
            ReplayEvent(2, DATASET_START + timedelta(seconds=1.1), "sip", {"T": "t", "S": "NVDA", "p": 101.0, "s": 2}),
            ReplayEvent(3, DATASET_START + timedelta(seconds=4), "sip", {"T": "t", "S": "NVDA", "p": 101.0, "s": 3}),
            quote(4, 5, "NVDA", 100.0, 102.0),
            ReplayEvent(5, DATASET_START + timedelta(seconds=5.5), "sip", {"T": "t", "S": "NVDA", "p": 101.5, "s": 1}),
            ReplayEvent(6, DATASET_START + timedelta(seconds=6), "sip", {"T": "t", "S": "NVDA", "p": 100.5, "s": 1}),
        ])
        payload = ReplayOrderFlowProjection(source, page_size=2).snapshot(
            "NVDA", through=DATASET_START + timedelta(seconds=7), run_id="run-1"
        )

        self.assertEqual(payload["classificationVersion"], "orderflow-estimated-v2")
        self.assertEqual(payload["dataStatus"], "ready")
        levels = {level["priceBin"]: level for level in payload["minutes"][0]["bins"]}
        self.assertEqual(levels[101.0]["askVolume"], 2.0)
        self.assertEqual(levels[101.0]["unknownVolume"], 3.0)
        self.assertEqual(levels[101.0]["askTradeCount"], 1)
        self.assertEqual(levels[101.0]["unknownTradeCount"], 1)
        self.assertEqual(levels[101.5]["askTradeCount"], 1)
        self.assertEqual(levels[100.5]["bidTradeCount"], 1)

    def test_snapshot_never_reads_after_virtual_cursor(self):
        source = InMemoryReplayEventSource([
            quote(1, 1, "NVDA", 100.0, 101.0),
            trade(2, 2, "NVDA", 101.0),
            quote(3, 3.5, "NVDA", 100.0, 101.0),
            trade(4, 4, "NVDA", 101.0),
        ])
        projection = ReplayOrderFlowProjection(source)

        first = projection.snapshot("NVDA", through=DATASET_START + timedelta(seconds=3), run_id="run-1")
        second = projection.snapshot("NVDA", through=DATASET_START + timedelta(seconds=5), run_id="run-1")
        delta = projection.snapshot(
            "NVDA",
            through=DATASET_START + timedelta(seconds=5),
            run_id="run-1",
            after_sequence=first["nextSequence"],
        )

        self.assertEqual(first["minutes"][0]["bins"][0]["askTradeCount"], 1)
        self.assertEqual(second["minutes"][0]["bins"][0]["askTradeCount"], 2)
        self.assertEqual(delta["minutes"][0]["bins"][0]["askTradeCount"], 2)
        self.assertEqual(delta["nextSequence"], 4)

    def test_after_hours_keeps_previous_regular_profile_until_next_regular_trade(self):
        next_regular_seconds = 22 * 60 * 60 + 30 * 60
        source = InMemoryReplayEventSource([
            quote(1, 1, "NVDA", 100.0, 101.0),
            trade(2, 2, "NVDA", 101.0),
            quote(3, 6 * 60 * 60, "NVDA", 200.0, 201.0),
            trade(4, 6 * 60 * 60 + 1, "NVDA", 201.0),
            quote(5, next_regular_seconds, "NVDA", 300.0, 301.0),
            trade(6, next_regular_seconds + 1, "NVDA", 301.0),
        ])
        projection = ReplayOrderFlowProjection(source)

        after_hours = projection.snapshot("NVDA", through=DATASET_START + timedelta(hours=7), run_id="run-1")
        next_session = projection.snapshot(
            "NVDA", through=DATASET_START + timedelta(seconds=next_regular_seconds + 2), run_id="run-1"
        )

        self.assertEqual(after_hours["sessionDate"], "2026-07-14")
        self.assertEqual(after_hours["liveQuote"]["bidPrice"], 200.0)
        self.assertEqual(len(after_hours["minutes"]), 1)
        self.assertEqual(next_session["sessionDate"], "2026-07-15")
        self.assertEqual(len(next_session["minutes"]), 1)
        self.assertEqual(next_session["minutes"][0]["bins"][0]["priceBin"], 301.0)

    def test_projection_supports_all_replay_symbols_with_bounded_lru(self):
        events = []
        sequence = 1
        for index, symbol in enumerate(REPLAY_SYMBOLS):
            seconds = index + 1
            events.extend([
                quote(sequence, seconds, symbol, 100.0 + index, 101.0 + index),
                trade(sequence + 1, seconds + 0.1, symbol, 101.0 + index),
            ])
            sequence += 2
        projection = ReplayOrderFlowProjection(InMemoryReplayEventSource(events), cache_symbol_limit=8)

        for symbol in REPLAY_SYMBOLS:
            payload = projection.snapshot(symbol, through=DATASET_START + timedelta(minutes=10), run_id="run-1")
            self.assertEqual(payload["dataStatus"], "ready")

        self.assertEqual(payload["supportedSymbols"], list(REPLAY_SYMBOLS))
        self.assertLessEqual(len(projection._states), 8)


class ReplayControllerTests(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock()
        self.source = InMemoryReplayEventSource(
            [
                quote(1, 1, "NVDA", 99.0, 100.0),
                trade(2, 1, "NVDA", 99.5),
                quote(3, 3, "NVDA", 101.0, 102.0),
                trade(4, 3, "NVDA", 101.5),
            ],
            previous_closes={"NVDA": 100.0},
        )
        self.controller = ReplayController(self.source, clock=self.clock, default_speed=1)

    def test_entering_simulation_is_ready_and_resume_advances_source_time(self):
        ready = self.controller.set_mode("simulation")
        self.assertEqual(ready["state"], "ready")
        self.assertEqual(ready["virtualTime"], "2026-07-15T00:00:00+09:00")
        self.assertEqual(ready["requestedSpeed"], 1)

        self.controller.resume()
        self.clock.value += 1.1
        running = self.controller.status()

        self.assertEqual(running["processedEventCount"], 2)
        self.assertEqual(self.controller.latest_quote("NVDA"), {"bid": 99.0, "ask": 100.0})
        self.assertEqual(self.controller.emitted_events()[0].payload["t"], "2026-07-14T15:00:01+00:00")

    def test_start_from_live_creates_a_run_and_immediately_plays(self):
        started = self.controller.start()

        self.assertEqual(started["mode"], "simulation")
        self.assertEqual(started["state"], "running")
        self.assertIsNotNone(started["runId"])
        self.assertEqual(started["virtualTime"], "2026-07-15T00:00:00+09:00")

        self.clock.value += 1.1
        running = self.controller.status()

        self.assertEqual(running["processedEventCount"], 2)
        self.assertEqual(self.controller.latest_quote("NVDA"), {"bid": 99.0, "ask": 100.0})

    def test_status_exposes_change_from_the_previous_regular_session_close(self):
        self.controller.set_mode("simulation")
        self.controller.resume()
        self.clock.value += 4

        status = self.controller.status()
        nvda = next(item for item in status["symbols"] if item["symbol"] == "NVDA")
        msft = next(item for item in status["symbols"] if item["symbol"] == "MSFT")

        self.assertEqual(nvda["price"], 101.5)
        self.assertEqual(nvda["previousClose"], 100.0)
        self.assertEqual(nvda["changePercent"], 1.5)
        self.assertIsNone(msft["price"])
        self.assertIsNone(msft["previousClose"])
        self.assertIsNone(msft["changePercent"])

    def test_status_uses_the_previous_regular_session_close_instead_of_the_first_replay_trade(self):
        controller = ReplayController(
            InMemoryReplayEventSource(
                [
                    trade(1, 1, "NVDA", 99.5),
                    trade(2, 3, "NVDA", 101.5),
                ],
                previous_closes={"NVDA": 100.0},
            ),
            clock=self.clock,
        )
        controller.set_mode("simulation")
        controller.resume()
        self.clock.value += 4

        nvda = next(item for item in controller.status()["symbols"] if item["symbol"] == "NVDA")

        self.assertEqual(nvda["price"], 101.5)
        self.assertEqual(nvda["previousClose"], 100.0)
        self.assertEqual(nvda["changePercent"], 1.5)

    def test_status_does_not_fall_back_to_the_first_trade_when_previous_close_is_missing(self):
        controller = ReplayController(
            InMemoryReplayEventSource([
                trade(1, 1, "NVDA", 99.5),
                trade(2, 3, "NVDA", 101.5),
            ]),
            clock=self.clock,
        )
        controller.set_mode("simulation")
        controller.resume()
        self.clock.value += 4

        nvda = next(item for item in controller.status()["symbols"] if item["symbol"] == "NVDA")

        self.assertEqual(nvda["price"], 101.5)
        self.assertIsNone(nvda["previousClose"])
        self.assertIsNone(nvda["changePercent"])

    def test_start_rejects_an_empty_dataset(self):
        controller = ReplayController(InMemoryReplayEventSource([]), clock=self.clock)

        with self.assertRaisesRegex(ValueError, "dataset is not READY"):
            controller.start()

    def test_daily_snapshot_is_built_from_processed_ticks_without_a_clickhouse_rescan(self):
        class NoDailyRescanSource(InMemoryReplayEventSource):
            def candle_snapshot(self, symbol, interval, through, limit):
                if interval in {"1D", "1d"}:
                    raise AssertionError("daily replay must not rescan ClickHouse")
                return super().candle_snapshot(symbol, interval, through, limit)

        controller = ReplayController(
            NoDailyRescanSource([
                trade(1, 1, "NVDA", 99.5),
                trade(2, 3, "NVDA", 101.5),
            ]),
            clock=self.clock,
        )
        controller.set_mode("simulation")
        controller.resume()
        self.clock.value += 4

        payload = controller.candle_snapshot("NVDA", "1D", 20)

        self.assertEqual(len(payload["candles"]), 1)
        self.assertEqual(payload["candles"][0]["timestamp"], "2026-07-14T04:00:00.000Z")
        self.assertEqual(payload["candles"][0]["open"], 99.5)
        self.assertEqual(payload["candles"][0]["close"], 101.5)
        self.assertFalse(payload["candles"][0]["isClosed"])

    def test_daily_snapshot_survives_controller_restore(self):
        store = MemoryStateStore()
        source = InMemoryReplayEventSource(
            [
                trade(1, 1, "NVDA", 99.5),
                trade(2, 3, "NVDA", 101.5),
            ],
            previous_closes={"NVDA": 100.0},
        )
        controller = ReplayController(source, clock=self.clock, state_store=store)
        controller.set_mode("simulation")
        controller.resume()
        self.clock.value += 4
        controller.status()

        restored = ReplayController(source, clock=self.clock, state_store=store)
        payload = restored.candle_snapshot("NVDA", "1D", 20)

        self.assertEqual(restored.state, "paused")
        self.assertEqual(payload["candles"][0]["open"], 99.5)
        self.assertEqual(payload["candles"][0]["close"], 101.5)
        self.assertFalse(payload["candles"][0]["isClosed"])
        nvda = next(item for item in restored.status()["symbols"] if item["symbol"] == "NVDA")
        self.assertEqual(nvda["previousClose"], 100.0)
        self.assertEqual(nvda["changePercent"], 1.5)

    def test_speed_can_change_mid_run_without_dropping_events(self):
        self.controller.set_mode("simulation")
        self.controller.resume()
        changed = self.controller.set_speed(2)
        self.assertEqual(changed["requestedSpeed"], 2)
        self.controller.set_speed(10)
        self.clock.value += 1

        completed = self.controller.status()

        self.assertEqual(completed["requestedSpeed"], 10)
        self.assertEqual(completed["processedEventCount"], 4)
        self.assertEqual([event.sequence for event in self.controller.emitted_events()], [1, 2, 3, 4])
        with self.assertRaises(ValueError):
            self.controller.set_speed(20)

    def test_legacy_speed_is_migrated_when_configuration_or_state_is_restored(self):
        configured = ReplayController(self.source, clock=self.clock, default_speed=60)
        self.assertEqual(configured.default_speed, 10)

        store = MemoryStateStore()
        store.snapshot = {
            "mode": "simulation",
            "state": "paused",
            "runId": "legacy-run",
            "virtualTime": DATASET_START.isoformat(),
            "requestedSpeed": 300,
            "processedEventCount": 0,
        }

        restored = ReplayController(self.source, clock=self.clock, state_store=store)

        self.assertEqual(restored.status()["requestedSpeed"], 10)
        self.assertEqual(store.snapshot["requestedSpeed"], 10)

    def test_execution_events_page_in_sequence_and_resume_from_checkpoint(self):
        self.controller.set_mode("simulation")
        self.controller.resume()
        self.clock.value += 4
        self.controller.status()

        first = self.controller.execution_events(after_sequence=0, limit=2)
        second = self.controller.execution_events(after_sequence=first["nextSequence"], limit=2)
        duplicate = self.controller.execution_events(after_sequence=second["nextSequence"], limit=300)

        self.assertEqual([item["sequence"] for item in first["quotes"]], [1])
        self.assertEqual([item["sequence"] for item in second["quotes"]], [3])
        self.assertEqual(first["nextSequence"], 2)
        self.assertEqual(second["nextSequence"], 4)
        self.assertTrue(second["caughtUp"])
        self.assertEqual(duplicate["quotes"], [])
        self.assertEqual(duplicate["nextSequence"], 4)

    def test_execution_events_reads_the_processed_sequence_window_without_repumping(self):
        class TrackingSource(InMemoryReplayEventSource):
            def __init__(self, events):
                super().__init__(events)
                self.pump_reads = 0
                self.window_reads = 0

            def events_after(self, sequence, through, limit):
                self.pump_reads += 1
                return super().events_after(sequence, through, limit)

            def events_between(self, after_sequence, through_sequence, limit):
                self.window_reads += 1
                return [
                    event for event in self._events
                    if after_sequence < event.sequence <= through_sequence
                ][:limit]

        source = TrackingSource([
            quote(1, 1, "NVDA", 99.0, 100.0),
            trade(2, 1, "NVDA", 99.5),
        ])
        controller = ReplayController(source, clock=self.clock)
        controller.start()
        self.clock.value += 2
        controller.status()
        source.pump_reads = 0

        page = controller.execution_events(after_sequence=0, limit=50_000)

        self.assertEqual(page["nextSequence"], 2)
        self.assertEqual(source.pump_reads, 0)
        self.assertEqual(source.window_reads, 1)

    def test_raw_crossed_quote_is_replayed_without_modification(self):
        controller = ReplayController(
            InMemoryReplayEventSource([quote(1, 1, "NVDA", 101.0, 100.0)]),
            clock=self.clock,
        )
        controller.set_mode("simulation")
        controller.resume()
        self.clock.value += 2

        status = controller.status()

        self.assertEqual(status["processedEventCount"], 1)
        self.assertEqual(controller.latest_quote("NVDA"), {"bid": 101.0, "ask": 100.0})

    def test_zero_sided_quote_is_processed_and_invalidates_the_executable_quote(self):
        controller = ReplayController(
            InMemoryReplayEventSource([
                quote(1, 1, "NVDA", 99.0, 100.0),
                quote(2, 2, "NVDA", 99.0, 0.0),
            ]),
            clock=self.clock,
        )
        controller.set_mode("simulation")
        controller.resume()
        self.clock.value += 3

        status = controller.status()

        self.assertEqual(status["processedEventCount"], 2)
        self.assertIsNone(controller.latest_quote("NVDA"))

    def test_restart_changes_run_without_resetting_an_account_ledger(self):
        self.controller.set_mode("simulation")
        self.controller.resume()
        self.clock.value += 2
        before_restart = self.controller.order_flow_snapshot("NVDA")
        first = self.controller.set_mode("simulation")
        restarted = self.controller.restart()

        self.assertEqual(before_restart["dataStatus"], "ready")
        self.assertNotEqual(first["runId"], restarted["runId"])
        self.assertEqual(restarted["state"], "ready")
        self.assertEqual(self.controller.order_flow_snapshot("NVDA")["dataStatus"], "empty")
        self.assertNotIn("accounts", self.controller.state_store.snapshot if self.controller.state_store else {})

    def test_persisted_state_contains_replay_state_only(self):
        store = MemoryStateStore()
        controller = ReplayController(self.source, clock=self.clock, state_store=store)
        controller.set_mode("simulation")
        controller.resume()
        self.clock.value += 1.1
        controller.status()

        self.assertNotIn("accounts", store.snapshot)
        self.assertNotIn("orderFlow", store.snapshot)
        restored = ReplayController(self.source, clock=self.clock, state_store=store)
        self.assertEqual(restored.status()["state"], "paused")
        self.assertEqual(restored.status()["processedEventCount"], 2)
        self.assertEqual(restored.order_flow_snapshot("NVDA")["dataStatus"], "ready")

if __name__ == "__main__":
    unittest.main()
