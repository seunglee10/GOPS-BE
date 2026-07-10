import unittest
from pathlib import Path

from alfaka.orderflow.verify import build_order_flow_verification_report
from alfaka.serving.time_utils import parse_utc_time


REPO_ROOT = Path(__file__).resolve().parents[3]


class OrderFlowVerifyTest(unittest.TestCase):
    def test_report_marks_large_matching_asof_and_live_skew_as_market_like(self):
        client = FakeClickHouseClient(
            trades=sample_trades(),
            quotes=sample_quotes(),
            daily_rows=[
                daily_row("2026-06-25", 100.0, ask=10, bid=4, unknown=2),
                daily_row("2026-06-25", 100.3, ask=5),
            ],
        )

        report = build_order_flow_verification_report(
            client,
            "NVDA",
            "2026-06-25",
            price_bin_size=0.01,
            live_payload=live_payload([
                minute("2026-06-25T13:30:00.000Z", [
                    level(100.0, ask=10, bid=4, unknown=2),
                ]),
                minute("2026-06-25T13:31:00.000Z", [
                    level(100.3, ask=5),
                ]),
            ]),
            include_minutes=True,
        )

        self.assertEqual(report["sources"]["asofTicks"]["status"], "ready")
        self.assertEqual(report["sources"]["dailyRow"]["status"], "ready")
        self.assertAlmostEqual(report["sources"]["asofTicks"]["metrics"]["askVolume"], 15)
        self.assertAlmostEqual(report["comparisons"]["liveVsAsofTicks"]["deltaSkew"], 0)
        self.assertEqual(report["comparisons"]["liveVsAsofTicks"]["signMismatchRate"], 0)
        self.assertEqual(report["verdict"]["primary"], "A")
        self.assertGreater(report["quoteCoverage"]["quoteGapMinuteCount"], 0)

    def test_report_marks_live_asof_divergence_as_artifact_candidate(self):
        client = FakeClickHouseClient(trades=sample_trades(), quotes=sample_quotes())

        report = build_order_flow_verification_report(
            client,
            "NVDA",
            "2026-06-25",
            price_bin_size=0.01,
            live_payload=live_payload([
                minute("2026-06-25T13:30:00.000Z", [level(100.0, bid=16)]),
                minute("2026-06-25T13:31:00.000Z", [level(100.3, bid=5)]),
            ]),
        )

        self.assertEqual(report["verdict"]["primary"], "B")
        self.assertGreater(report["comparisons"]["liveVsAsofTicks"]["deltaSkew"], 0.15)

    def test_report_marks_high_unknown_ratio_as_quote_coverage_issue(self):
        client = FakeClickHouseClient(
            trades=[
                trade("2026-06-25T13:30:01.000Z", 100.10, 10, "t-1"),
                trade("2026-06-25T13:30:02.000Z", 100.00, 4, "t-2"),
            ],
            quotes=[],
        )

        report = build_order_flow_verification_report(
            client,
            "NVDA",
            "2026-06-25",
            price_bin_size=0.01,
            live_payload=live_payload([]),
        )

        self.assertEqual(report["verdict"]["primary"], "C")
        self.assertEqual(report["sources"]["asofTicks"]["metrics"]["unknownRatio"], 1.0)

    def test_cli_script_exists_as_operator_entrypoint(self):
        script = REPO_ROOT / "scripts" / "local" / "orderflow_verify.py"
        self.assertTrue(script.exists())
        self.assertIn("build_order_flow_verification_report", script.read_text())


class FakeClickHouseClient:
    def __init__(self, trades=None, quotes=None, daily_rows=None):
        self.trades = trades or []
        self.quotes = quotes or []
        self.daily_rows = daily_rows or []
        self.queries = []

    def query_json_each_row(self, query, parameters=None):
        parameters = parameters or {}
        self.queries.append((query, parameters))
        if "market_data.trade_ticks" in query:
            return self._filter_ticks(self.trades, parameters)
        if "market_data.quote_ticks" in query:
            return self._filter_ticks(self.quotes, parameters)
        if "order_flow_profile_daily" in query:
            return [
                dict(row)
                for row in self.daily_rows
                if row["sessionDate"] == parameters.get("sessionDate")
                and row["symbol"] == parameters.get("symbol")
            ]
        return []

    def _filter_ticks(self, rows, parameters):
        symbol = parameters.get("symbol")
        start = parse_utc_time(parameters.get("fromTime"))
        end = parse_utc_time(parameters.get("toTime"))
        output = []
        for row in rows:
            row_time = parse_utc_time(row["event_time"])
            if row.get("symbol") == symbol and start <= row_time < end:
                output.append(dict(row))
        return output


def sample_trades():
    return [
        trade("2026-06-25T13:30:01.000Z", 100.10, 10, "t-ask"),
        trade("2026-06-25T13:30:02.000Z", 100.00, 4, "t-bid"),
        trade("2026-06-25T13:30:03.000Z", 100.05, 2, "t-unknown"),
        trade("2026-06-25T13:31:02.000Z", 100.31, 5, "t-ask-2"),
    ]


def sample_quotes():
    return [
        quote("2026-06-25T13:30:00.000Z", 100.0, 100.1, "q-1"),
        quote("2026-06-25T13:31:00.000Z", 100.2, 100.3, "q-2"),
    ]


def live_payload(minutes):
    return {
        "symbol": "NVDA",
        "sessionDate": "2026-06-25",
        "dataStatus": "ready" if minutes else "empty",
        "priceBinSize": 0.01,
        "minutes": minutes,
    }


def minute(event_minute, bins):
    return {"eventMinute": event_minute, "bins": bins}


def level(price, *, ask=0, bid=0, unknown=0):
    return {
        "priceBin": price,
        "askVolume": ask,
        "bidVolume": bid,
        "unknownVolume": unknown,
        "askTradeCount": 1 if ask else 0,
        "bidTradeCount": 1 if bid else 0,
        "unknownTradeCount": 1 if unknown else 0,
    }


def daily_row(session_date, price, *, ask=0, bid=0, unknown=0):
    return {
        "symbol": "NVDA",
        "sessionDate": session_date,
        "priceBin": price,
        "priceBinSize": 0.01,
        "askVolume": ask,
        "bidVolume": bid,
        "unknownVolume": unknown,
        "askTradeCount": 1 if ask else 0,
        "bidTradeCount": 1 if bid else 0,
        "unknownTradeCount": 1 if unknown else 0,
        "tradeCount": (1 if ask else 0) + (1 if bid else 0) + (1 if unknown else 0),
        "volume": ask + bid + unknown,
    }


def trade(timestamp, price, size, source_event_id):
    return {
        "event_time": timestamp,
        "symbol": "NVDA",
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
        "symbol": "NVDA",
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
