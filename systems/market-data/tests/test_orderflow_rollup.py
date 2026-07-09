import unittest
import inspect
import os
from pathlib import Path
from unittest import mock

from alfaka.orderflow.rollup import fetch_alpaca_rows, iso, regular_session_bounds_utc, rollup_session
from alfaka.serving.time_utils import parse_utc_time


REPO_ROOT = Path(__file__).resolve().parents[3]


class OrderFlowRollupTest(unittest.TestCase):
    def test_rollup_session_aggregates_side_split_rows_and_dedupes_trade_ids(self):
        client = FakeClickHouseClient(
            trades=[
                trade("2026-06-25T13:30:01.000Z", 100.10, 10, "t-ask"),
                trade("2026-06-25T13:30:02.000Z", 100.00, 4, "t-bid"),
                trade("2026-06-25T13:30:02.000Z", 100.00, 4, "t-bid"),
                trade("2026-06-25T13:30:03.000Z", 100.05, 2, "t-unknown"),
                trade("2026-06-25T13:31:02.000Z", 100.31, 5, "t-ask-2"),
            ],
            quotes=[
                quote("2026-06-25T13:30:00.000Z", 100.0, 100.1, "q-1"),
                quote("2026-06-25T13:31:00.000Z", 100.2, 100.3, "q-2"),
            ],
        )

        summary = rollup_session(client, "AAPL", "2026-06-25")

        self.assertEqual(summary["status"], "ready")
        self.assertEqual(summary["tradeCount"], 4)
        self.assertEqual(summary["quoteCount"], 2)
        self.assertEqual(summary["duplicateCount"], 1)
        self.assertEqual(summary["insertedRows"], 4)
        self.assertEqual(len(client.inserts), 1)
        rows = client.inserts[0][1]
        self.assertEqual(sum(row["ask_volume"] for row in rows), 15)
        self.assertEqual(sum(row["bid_volume"] for row in rows), 4)
        self.assertEqual(sum(row["unknown_volume"] for row in rows), 2)
        self.assertTrue(all(row["market_session"] == "regular" for row in rows))
        self.assertTrue(all(row["classification_version"] == "orderflow-estimated-v2" for row in rows))

    def test_hourly_window_carries_quote_into_next_window(self):
        client = FakeClickHouseClient(
            trades=[trade("2026-06-25T14:30:01.000Z", 101.19, 3, "t-1")],
            quotes=[quote("2026-06-25T14:29:59.000Z", 101.0, 101.2, "q-1")],
        )

        summary = rollup_session(client, "AAPL", "2026-06-25")

        self.assertEqual(sum(row["ask_volume"] for row in summary["rows"]), 3)
        self.assertEqual(summary["sideDistribution"], {"ask": 1})

    def test_closed_date_exits_ok_without_insert(self):
        client = FakeClickHouseClient()

        summary = rollup_session(client, "AAPL", "2026-07-03")

        self.assertEqual(summary["status"], "closed")
        self.assertEqual(summary["rows"], [])
        self.assertEqual(client.inserts, [])

    def test_dry_run_inserts_nothing_and_rerun_rows_are_idempotent(self):
        rows = [trade("2026-06-25T13:30:01.000Z", 100.10, 10, "t-ask")]
        quotes = [quote("2026-06-25T13:30:00.000Z", 100.0, 100.1, "q-1")]
        dry_client = FakeClickHouseClient(trades=rows, quotes=quotes)
        write_client = FakeClickHouseClient(trades=rows, quotes=quotes)

        dry = rollup_session(dry_client, "AAPL", "2026-06-25", dry_run=True)
        first = rollup_session(write_client, "AAPL", "2026-06-25")
        second = rollup_session(write_client, "AAPL", "2026-06-25")

        self.assertEqual(dry["insertedRows"], 0)
        self.assertEqual(dry_client.inserts, [])
        self.assertEqual(first["rows"], second["rows"])

    def test_alpaca_rollup_streams_pages_and_matches_ticks_path(self):
        ticks_client = FakeClickHouseClient(
            trades=[
                trade("2026-06-25T13:30:01.000Z", 100.10, 10, "t-ask"),
                trade("2026-06-25T13:30:02.000Z", 100.00, 4, "t-bid"),
                trade("2026-06-25T13:30:03.000Z", 100.05, 2, "t-unknown"),
            ],
            quotes=[
                quote("2026-06-25T13:30:00.000Z", 100.0, 100.1, "q-1"),
            ],
        )
        alpaca_client = FakeClickHouseClient()
        fake_get = FakeAlpacaGet(
            trades=[
                [
                    {"t": "2026-06-25T13:30:01.000Z", "p": 100.10, "s": 10, "i": "t-ask"},
                    {"t": "2026-06-25T13:30:02.000Z", "p": 100.00, "s": 4, "i": "t-bid"},
                ],
                [
                    {"t": "2026-06-25T13:30:03.000Z", "p": 100.05, "s": 2, "i": "t-unknown"},
                ],
            ],
            quotes=[
                [{"t": "2026-06-25T13:30:00.000Z", "bp": 100.0, "ap": 100.1, "i": "q-1"}],
            ],
        )

        with mock.patch.dict(os.environ, {"APCA_API_KEY_ID": "key", "APCA_API_SECRET_KEY": "secret"}):
            with mock.patch("requests.get", side_effect=fake_get):
                ticks = rollup_session(ticks_client, "AAPL", "2026-06-25")
                alpaca = rollup_session(alpaca_client, "AAPL", "2026-06-25", source="alpaca")

        self.assertEqual(alpaca["rows"], ticks["rows"])
        self.assertEqual(fake_get.page_calls["trades"], ["first", "page-1"])
        self.assertEqual(fake_get.page_calls["quotes"], ["first"])

    def test_fetch_alpaca_rows_is_generator_and_requests_next_page_lazily(self):
        bounds = regular_session_bounds_utc(parse_utc_time("2026-06-25T13:30:00.000Z").date())
        start, end, _warmup = bounds
        fake_get = FakeAlpacaGet(
            trades=[
                [
                    {"t": "2026-06-25T13:30:01.000Z", "p": 100.10, "s": 10, "i": "t-1"},
                    {"t": "2026-06-25T13:30:02.000Z", "p": 100.11, "s": 2, "i": "t-2"},
                ],
                [
                    {"t": "2026-06-25T13:30:03.000Z", "p": 100.12, "s": 3, "i": "t-3"},
                ],
            ],
            quotes=[],
        )

        with mock.patch.dict(os.environ, {"APCA_API_KEY_ID": "key", "APCA_API_SECRET_KEY": "secret"}):
            with mock.patch("requests.get", side_effect=fake_get):
                rows = fetch_alpaca_rows("AAPL", "trades", start, end)
                self.assertTrue(inspect.isgenerator(rows))
                self.assertEqual(next(rows)["price"], 100.10)
                self.assertEqual(fake_get.page_calls["trades"], ["first"])
                self.assertEqual(next(rows)["price"], 100.11)
                self.assertEqual(fake_get.page_calls["trades"], ["first"])
                self.assertEqual(next(rows)["price"], 100.12)
                self.assertEqual(fake_get.page_calls["trades"], ["first", "page-1"])

    def test_order_flow_daily_ddl_exists_in_both_initdb_copies(self):
        for path in (
            REPO_ROOT / "infra/clickhouse/initdb/01-market-data.sql",
            REPO_ROOT / "infra/k8s/base/platform/clickhouse-initdb/01-market-data.sql",
        ):
            sql = path.read_text()
            self.assertIn("CREATE TABLE IF NOT EXISTS market_data.order_flow_profile_daily", sql)
            self.assertIn("ORDER BY (symbol, session_date, price_bin_size, price_bin)", sql)
            self.assertIn("No TTL", sql)


class FakeClickHouseClient:
    def __init__(self, trades=None, quotes=None):
        self.trades = trades or []
        self.quotes = quotes or []
        self.queries = []
        self.inserts = []

    def query_json_each_row(self, query, parameters=None):
        parameters = parameters or {}
        self.queries.append((query, parameters))
        if "count()" in query:
            return [{"n": len(self._filter(self.trades, parameters))}]
        if "market_data.trade_ticks" in query:
            return self._filter(self.trades, parameters)
        if "market_data.quote_ticks" in query:
            return self._filter(self.quotes, parameters)
        return []

    def insert_json_each_row(self, table, rows):
        self.inserts.append((table, list(rows)))

    def _filter(self, rows, parameters):
        symbol = parameters.get("symbol")
        start = parse_utc_time(parameters.get("fromTime"))
        end = parse_utc_time(parameters.get("toTime"))
        output = []
        for row in rows:
            row_time = parse_utc_time(row["event_time"])
            if row.get("symbol") == symbol and start <= row_time < end:
                output.append(dict(row))
        return output


class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeAlpacaGet:
    def __init__(self, *, trades, quotes):
        self.pages = {"trades": trades, "quotes": quotes}
        self.page_calls = {"trades": [], "quotes": []}

    def __call__(self, url, headers=None, params=None, timeout=None):
        kind = "trades" if url.rstrip("/").endswith("/trades") else "quotes"
        token = (params or {}).get("page_token")
        index = int(str(token).replace("page-", "")) if token else 0
        self.page_calls[kind].append(token or "first")
        page = self.pages[kind][index] if index < len(self.pages[kind]) else []
        next_token = f"page-{index + 1}" if index + 1 < len(self.pages[kind]) else None
        return FakeResponse({kind: page, "next_page_token": next_token})


def trade(timestamp, price, size, source_event_id):
    return {
        "event_time": timestamp,
        "symbol": "AAPL",
        "price": price,
        "size": size,
        "exchange": "V",
        "conditions": [],
        "tape": "A",
        "source": "clickhouse",
        "feed": "sip",
        "feed_profile": "sip",
        "market_session": "regular",
        "source_event_id": source_event_id,
    }


def quote(timestamp, bid, ask, source_event_id):
    return {
        "event_time": timestamp,
        "symbol": "AAPL",
        "bid_price": bid,
        "bid_size": 1,
        "ask_price": ask,
        "ask_size": 1,
        "source": "clickhouse",
        "feed": "sip",
        "feed_profile": "sip",
        "market_session": "regular",
        "source_event_id": source_event_id,
    }


if __name__ == "__main__":
    unittest.main()
