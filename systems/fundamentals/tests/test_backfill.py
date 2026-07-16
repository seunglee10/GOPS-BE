import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))

from fundamentals.backfill import (
    BackfillStats,
    CompanyTicker,
    FundamentalsBackfillConfig,
    add_synthetic_q4_rows,
    build_summary_payload,
    default_frame_periods,
    derive_metric_rows,
    effective_source,
    fetch_sec_frame_api_rows,
    iter_companyfacts_api_payloads,
    iter_companyfacts_payloads,
    normalize_companyfacts_payload,
    parse_company_tickers_exchange,
    run_companyfacts_backfill,
    write_redis_peer_summaries,
)


class FakeRedis:
    def __init__(self):
        self.values = {}

    def set(self, key, value):
        self.values[key] = value

    def setex(self, key, _ttl, value):
        self.values[key] = value


class FakeSecClient:
    def __init__(self):
        self.requests = []

    def frame(self, taxonomy, concept_name, unit, frame_period):
        self.requests.append((taxonomy, concept_name, unit, frame_period))
        return {
            "taxonomy": taxonomy,
            "tag": concept_name,
            "uom": unit,
            "ccp": frame_period,
            "data": [
                {
                    "cik": 320193,
                    "val": 100,
                    "accn": f"{concept_name}-{frame_period}",
                    "start": "2025-10-01",
                    "end": "2025-12-31",
                    "entityName": "Apple Inc.",
                    "fy": 2025,
                    "fp": "Q4",
                    "form": "10-Q",
                }
            ],
        }


class FakeCompanyFactsClient:
    def __init__(self, payloads, errors=()):
        self.payloads = payloads
        self.errors = set(errors)
        self.requests = []

    def companyfacts(self, cik):
        self.requests.append(cik)
        if cik in self.errors:
            raise RuntimeError("boom")
        return self.payloads[cik]


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body, **_kwargs):
        self.objects[(Bucket, Key)] = Body


def sec_fact(value, *, fy=2024, fp="FY", end="2024-12-31", filed="2025-02-01", accn=None, form="10-K"):
    return {
        "val": value,
        "fy": fy,
        "fp": fp,
        "form": form,
        "filed": filed,
        "end": end,
        "accn": accn or f"{fy}-{fp}",
    }


def concept(units):
    return {"units": units}


class FundamentalsBackfillTests(unittest.TestCase):
    def test_parse_company_tickers_exchange_keeps_dot_and_dash_aliases(self):
        payload = {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [[1067983, "Berkshire Hathaway Inc.", "BRK-B", "NYSE"]],
        }

        mapping = parse_company_tickers_exchange(payload)

        self.assertEqual(mapping["BRK-B"].cik, "0001067983")
        self.assertEqual(mapping["BRK.B"].cik, "0001067983")

    def test_iter_companyfacts_payloads_keeps_multiple_symbols_for_same_cik(self):
        company_map = {
            "GOOG": CompanyTicker(symbol="GOOG", cik="0001652044"),
            "GOOGL": CompanyTicker(symbol="GOOGL", cik="0001652044"),
        }
        payload = {"facts": {"us-gaap": {}}}
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = Path(tmpdir) / "companyfacts.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("CIK0001652044.json", json.dumps(payload))

            rows = list(iter_companyfacts_payloads(zip_path, company_map))

        self.assertEqual([company.symbol for company, _payload in rows], ["GOOG", "GOOGL"])

    def test_normalize_companyfacts_and_derive_metrics(self):
        company = CompanyTicker(symbol="AAPL", cik="0000320193", company_name="Apple Inc.", exchange="Nasdaq")
        payload = {
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": concept({
                        "USD": [
                            sec_fact(100, accn="fy24"),
                            sec_fact(20, fp="Q1", end="2024-03-31", filed="2024-05-01", accn="q1"),
                            sec_fact(25, fp="Q2", end="2024-06-30", filed="2024-08-01", accn="q2"),
                            sec_fact(30, fp="Q3", end="2024-09-30", filed="2024-11-01", accn="q3"),
                            sec_fact(80, fy=2023, end="2023-12-31", filed="2024-02-01", accn="fy23"),
                        ]
                    }),
                    "NetIncomeLoss": concept({"USD": [sec_fact(10), sec_fact(8, fy=2023, end="2023-12-31", filed="2024-02-01")]}),
                    "OperatingIncomeLoss": concept({"USD": [sec_fact(20), sec_fact(16, fy=2023, end="2023-12-31", filed="2024-02-01")]}),
                    "Assets": concept({"USD": [sec_fact(200)]}),
                    "Liabilities": concept({"USD": [sec_fact(100)]}),
                    "StockholdersEquity": concept({"USD": [sec_fact(50)]}),
                    "AssetsCurrent": concept({"USD": [sec_fact(60)]}),
                    "LiabilitiesCurrent": concept({"USD": [sec_fact(30)]}),
                    "NetCashProvidedByUsedInOperatingActivities": concept({"USD": [sec_fact(30)]}),
                    "PaymentsToAcquirePropertyPlantAndEquipment": concept({"USD": [sec_fact(5)]}),
                    "InterestExpense": concept({"USD": [sec_fact(-4)]}),
                    "CashAndCashEquivalentsAtCarryingValue": concept({"USD": [sec_fact(8)]}),
                    "EarningsPerShareDiluted": concept({"USD/shares": [sec_fact(2)]}),
                    "DebtCurrent": concept({"USD": [sec_fact(10, accn="debt-current")]}),
                    "ShortTermBorrowings": concept({"USD": [sec_fact(999, accn="component-ignored")]}),
                    "LongTermDebtNoncurrent": concept({"USD": [sec_fact(20, accn="debt-noncurrent")]}),
                },
                "dei": {
                    "EntityCommonStockSharesOutstanding": concept({
                        "shares": [sec_fact(1000, accn="shares", form="10-K")]
                    })
                }
            }
        }

        fact_rows = add_synthetic_q4_rows(normalize_companyfacts_payload(company, payload))
        derived_rows = derive_metric_rows(company, fact_rows)

        q4_revenue = [row for row in fact_rows if row["metric"] == "revenue" and row["fiscal_period"] == "Q4"][0]
        self.assertEqual(q4_revenue["value"], 25.0)
        self.assertEqual(json.loads(q4_revenue["raw"])["quality"], "synthetic_q4")
        shares_row = [row for row in fact_rows if row["metric"] == "shares_outstanding"][0]
        self.assertEqual(shares_row["taxonomy"], "dei")
        self.assertEqual(shares_row["concept"], "EntityCommonStockSharesOutstanding")
        self.assertEqual(shares_row["unit"], "shares")

        fy_metrics = {
            row["metric"]: row
            for row in derived_rows
            if row["fiscal_year"] == 2024 and row["fiscal_period"] == "FY" and row["value"] is not None
        }
        self.assertEqual(fy_metrics["net_margin"]["value"], 0.1)
        self.assertEqual(fy_metrics["current_ratio"]["value"], 2.0)
        self.assertEqual(fy_metrics["free_cash_flow"]["value"], 25.0)
        self.assertEqual(fy_metrics["interest_coverage"]["value"], 5.0)
        self.assertEqual(fy_metrics["current_liabilities_to_equity"]["value"], 0.6)
        self.assertEqual(fy_metrics["noncurrent_liabilities_to_equity"]["value"], 1.4)
        self.assertEqual(fy_metrics["financial_cost_burden_ratio"]["value"], 0.04)
        self.assertEqual(fy_metrics["total_debt"]["value"], 30.0)
        self.assertEqual(fy_metrics["net_debt"]["value"], 22.0)
        self.assertEqual(json.loads(fy_metrics["total_debt"]["raw"])["debt_composition"]["current_sources"], ["DebtCurrent"])

        summary = build_summary_payload("AAPL", fact_rows, derived_rows)
        self.assertEqual(summary["symbol"], "AAPL")
        self.assertEqual(summary["cik"], "0000320193")
        self.assertEqual(summary["source"], "sec_companyfacts")
        self.assertEqual(summary["as_of"], "2024-12-31")
        self.assertTrue(summary["metrics"])
        summary_metrics = {item["metric"]: item for item in summary["metrics"]}
        self.assertEqual(summary_metrics["revenue"]["kind"], "fact")
        self.assertEqual(summary_metrics["shares_outstanding"]["value"], 1000.0)
        self.assertEqual(summary_metrics["shares_outstanding"]["taxonomy"], "dei")
        self.assertEqual(summary_metrics["shares_outstanding"]["source"], "sec_companyfacts")

    def test_summary_payload_keeps_metric_latest_when_shares_has_separate_period_end(self):
        fact_rows = [
            {
                "symbol": "NVDA",
                "cik": "0001045810",
                "metric": "assets",
                "value": 100,
                "fiscal_year": 2027,
                "fiscal_period": "Q1",
                "period_end": "2026-04-26",
                "filed_at": "2026-05-20",
                "version_filed_at": "2026-05-20",
                "taxonomy": "us-gaap",
                "concept": "Assets",
                "unit": "USD",
                "quality": "available",
                "raw": "{}",
            },
            {
                "symbol": "NVDA",
                "cik": "0001045810",
                "metric": "shares_outstanding",
                "value": 24200000000,
                "fiscal_year": 2027,
                "fiscal_period": "Q1",
                "period_end": "2026-05-15",
                "filed_at": "2026-05-20",
                "version_filed_at": "2026-05-20",
                "taxonomy": "dei",
                "concept": "EntityCommonStockSharesOutstanding",
                "unit": "shares",
                "quality": "available",
                "raw": "{}",
            },
        ]

        summary = build_summary_payload("NVDA", fact_rows, [])
        summary_metrics = {item["metric"]: item for item in summary["metrics"]}

        self.assertEqual(summary["as_of"], "2026-04-26")
        self.assertEqual(summary_metrics["assets"]["value"], 100)
        self.assertEqual(summary_metrics["shares_outstanding"]["value"], 24200000000)

    def test_write_redis_peer_summary_writes_group_for_each_symbol(self):
        redis = FakeRedis()
        frame_rows = [
            {"frame_period": "CY2025Q4", "concept": "Revenues", "unit": "USD", "symbol": "AAPL", "value": 100, "quality": "frame_as_reported"},
            {"frame_period": "CY2025Q4", "concept": "Revenues", "unit": "USD", "symbol": "MSFT", "value": 90, "quality": "frame_as_reported"},
        ]

        written = write_redis_peer_summaries(redis, frame_rows)

        self.assertEqual(written, 2)
        aapl_payload = json.loads(redis.values["gops:fundamentals:peer:v1:AAPL:latest"])
        self.assertEqual(aapl_payload["frame_period"], "CY2025Q4")
        self.assertEqual([item["symbol"] for item in aapl_payload["peers"]], ["AAPL", "MSFT"])
        self.assertIn("gops:fundamentals:peer:v1:MSFT:CY2025Q4", redis.values)

    def test_write_redis_peer_summary_keeps_multiple_concepts_in_one_payload(self):
        redis = FakeRedis()
        frame_rows = [
            {"frame_period": "CY2025Q4", "concept": "Revenues", "unit": "USD", "symbol": "AAPL", "value": 100, "quality": "frame_as_reported"},
            {"frame_period": "CY2025Q4", "concept": "Revenues", "unit": "USD", "symbol": "MSFT", "value": 90, "quality": "frame_as_reported"},
            {"frame_period": "CY2025Q4I", "concept": "Assets", "unit": "USD", "symbol": "AAPL", "value": 500, "quality": "frame_as_reported"},
            {"frame_period": "CY2025Q4I", "concept": "Assets", "unit": "USD", "symbol": "MSFT", "value": 400, "quality": "frame_as_reported"},
        ]

        written = write_redis_peer_summaries(redis, frame_rows)

        self.assertEqual(written, 2)
        aapl_payload = json.loads(redis.values["gops:fundamentals:peer:v1:AAPL:latest"])
        self.assertEqual(aapl_payload["frame_period"], "CY2025Q4")
        self.assertEqual([frame["concept"] for frame in aapl_payload["frames"]], ["Revenues", "Assets"])
        self.assertEqual(aapl_payload["frames"][1]["display_period"], "CY2025Q4")

    def test_write_redis_peer_summary_uses_symbol_latest_when_global_latest_missing_symbol(self):
        redis = FakeRedis()
        frame_rows = [
            {"frame_period": "CY2025Q4", "concept": "Revenues", "unit": "USD", "symbol": "AAPL", "value": 100, "quality": "frame_as_reported"},
            {"frame_period": "CY2025Q4", "concept": "Revenues", "unit": "USD", "symbol": "MSFT", "value": 90, "quality": "frame_as_reported"},
            {"frame_period": "CY2025Q3", "concept": "Revenues", "unit": "USD", "symbol": "XYZ", "value": 80, "quality": "frame_as_reported"},
        ]

        written = write_redis_peer_summaries(redis, frame_rows)

        self.assertEqual(written, 3)
        xyz_payload = json.loads(redis.values["gops:fundamentals:peer:v1:XYZ:latest"])
        self.assertEqual(xyz_payload["frame_period"], "CY2025Q3")
        self.assertEqual(xyz_payload["peers"][0]["symbol"], "XYZ")

    def test_default_frame_periods_skips_freshly_closed_quarter(self):
        periods = default_frame_periods(datetime(2026, 7, 5, tzinfo=timezone.utc), count=3)

        self.assertEqual(periods, ["CY2026Q1", "CY2025Q4", "CY2025Q3"])

    def test_fetch_sec_frame_api_rows_uses_instant_period_suffix(self):
        client = FakeSecClient()
        company_map = {"AAPL": CompanyTicker(symbol="AAPL", cik="0000320193")}

        rows = list(fetch_sec_frame_api_rows(client, company_map, {"Assets", "Revenues"}, ["CY2025Q4"]))

        self.assertEqual(len(rows), 2)
        self.assertIn(("us-gaap", "Assets", "USD", "CY2025Q4I"), client.requests)
        self.assertIn(("us-gaap", "Revenues", "USD", "CY2025Q4"), client.requests)
        flattened = [row for batch in rows for row in batch]
        self.assertEqual({row["symbol"] for row in flattened}, {"AAPL"})

    def test_effective_source_defaults_to_api_and_falls_back_to_zip_for_explicit_paths(self):
        self.assertEqual(effective_source(FundamentalsBackfillConfig()), "api")
        self.assertEqual(effective_source(FundamentalsBackfillConfig(source="zip")), "zip")
        self.assertEqual(effective_source(FundamentalsBackfillConfig(source="api", local_zip_path="/tmp/companyfacts.zip")), "zip")
        self.assertEqual(effective_source(FundamentalsBackfillConfig(source="api", s3_zip_key="fundamentals/sec/companyfacts/2026-07-10/companyfacts.zip")), "zip")
        self.assertEqual(effective_source(FundamentalsBackfillConfig(source="unknown")), "api")

    def test_iter_companyfacts_api_payloads_uploads_json_and_counts_failures(self):
        company_map = {
            "GOOG": CompanyTicker(symbol="GOOG", cik="0001652044"),
            "GOOGL": CompanyTicker(symbol="GOOGL", cik="0001652044"),
            "AAPL": CompanyTicker(symbol="AAPL", cik="0000320193"),
        }
        client = FakeCompanyFactsClient(
            payloads={"0001652044": {"facts": {"us-gaap": {}}}},
            errors={"0000320193"},
        )
        s3 = FakeS3()
        config = FundamentalsBackfillConfig(dry_run=False, s3_bucket="test-bucket")
        stats = BackfillStats(run_id="test", dry_run=False)

        with redirect_stdout(StringIO()):
            rows = list(iter_companyfacts_api_payloads(client, company_map, config=config, stats=stats, s3_client=s3))

        self.assertEqual(sorted(company.symbol for company, _payload in rows), ["GOOG", "GOOGL"])
        self.assertEqual(stats.companies_failed, 1)
        self.assertEqual(client.requests, ["0000320193", "0001652044"])
        self.assertIn(("test-bucket", "fundamentals/sec/companyfacts/api/CIK0001652044.json"), s3.objects)
        self.assertNotIn(("test-bucket", "fundamentals/sec/companyfacts/api/CIK0000320193.json"), s3.objects)

    def test_iter_companyfacts_api_payloads_skips_upload_in_dry_run(self):
        company_map = {"GOOG": CompanyTicker(symbol="GOOG", cik="0001652044")}
        client = FakeCompanyFactsClient(payloads={"0001652044": {"facts": {"us-gaap": {}}}})
        s3 = FakeS3()
        config = FundamentalsBackfillConfig(dry_run=True, s3_bucket="test-bucket")

        rows = list(iter_companyfacts_api_payloads(client, company_map, config=config, s3_client=s3))

        self.assertEqual(len(rows), 1)
        self.assertEqual(s3.objects, {})

    def test_dry_run_without_download_returns_without_network_clients(self):
        config = FundamentalsBackfillConfig(dry_run=True, symbols=["AAPL"], download_in_dry_run=False)

        with redirect_stdout(StringIO()):
            stats = run_companyfacts_backfill(config)

        self.assertIsInstance(stats, BackfillStats)
        self.assertEqual(stats.companies_requested, 1)
        self.assertEqual(stats.companies_loaded, 0)


if __name__ == "__main__":
    unittest.main()
