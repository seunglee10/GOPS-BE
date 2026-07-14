import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(REPO_ROOT / "systems" / "market-data" / "shared"))

from gops_agents.orchestration.coach_point_in_time import StoreCoachPointInTimeContextProvider


class FakeRedisMarketProvider:
    def __init__(self, rows=None):
        self.rows = rows or {}
        self.calls = 0

    def live_trade(self, symbol):
        self.calls += 1
        return self.rows.get(symbol)


class FakeClickHouseProvider:
    def __init__(self, rows=None):
        self.rows = rows or {}
        self.calls = []

    def table(self, name):
        return f"market_data.{name}"

    def query_json_each_row(self, query, params):
        self.calls.append((query, dict(params)))
        for table_name, rows in self.rows.items():
            if f"market_data.{table_name}" in query:
                return list(rows)
        return []


class FakeOntologyProvider:
    def fetch(self, request):
        return [{
            "provider": "ontology",
            "status": "available",
            "title": f"{request.symbol} current graph",
            "summary": "current-only evidence",
            "observedAt": "2026-07-14T12:00:01Z",
            "raw": {"sector": "Technology"},
        }]


class BrokenClickHouseProvider:
    def __init__(self):
        self.calls = 0

    def table(self, name):
        return f"market_data.{name}"

    def query_json_each_row(self, query, params):
        self.calls += 1
        raise TimeoutError("clickhouse unavailable")


class CoachPointInTimeProviderTests(unittest.TestCase):
    def test_clickhouse_outage_opens_one_request_circuit(self):
        cutoff = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
        clickhouse = BrokenClickHouseProvider()
        provider = StoreCoachPointInTimeContextProvider(
            clickhouse_provider=clickhouse,
            redis_market_provider=FakeRedisMarketProvider(),
            ontology_provider=FakeOntologyProvider(),
            heatmap_seed_path="/missing/heatmap.json",
            now_provider=lambda: cutoff,
        )

        result = provider.load(
            fills=[{"fillId": "fill-1", "symbol": "NVDA", "filledAt": "2026-07-14T10:00:00Z"}],
            requested_at=cutoff,
            current_fill_ids={"fill-1"},
        )

        self.assertEqual(clickhouse.calls, 1)
        self.assertTrue(any(item["code"] == "clickhouse_quote_unavailable" for item in result["missingData"]))

    def test_snapshot_never_reads_redis_and_uses_clickhouse_tick(self):
        cutoff = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
        clickhouse = FakeClickHouseProvider({
            "trade_ticks": [{"symbol": "NVDA", "price": 194.72, "sourceAsOf": "2026-07-14T11:59:00Z"}],
        })
        redis = FakeRedisMarketProvider({
            "NVDA": {
                "symbol": "NVDA",
                "price": "999.00",
                "timestamp": "2026-07-14T11:59:00Z",
                "receivedAt": "2026-07-14T11:59:00Z",
                "source": "redis.live_trade",
            }
        })
        provider = StoreCoachPointInTimeContextProvider(
            clickhouse_provider=clickhouse,
            redis_market_provider=redis,
            ontology_provider=FakeOntologyProvider(),
            heatmap_seed_path="/missing/heatmap.json",
            now_provider=lambda: cutoff,
        )

        result = provider.load(
            fills=[{"fillId": "fill-1", "symbol": "NVDA", "filledAt": "2026-07-14T10:00:00Z"}],
            requested_at=cutoff,
            current_fill_ids={"fill-1"},
        )

        self.assertEqual(result["fillEnrichmentById"]["fill-1"]["currentPrice"], 194.72)
        self.assertEqual(redis.calls, 0)
        self.assertEqual(
            result["fillEnrichmentById"]["fill-1"]["currentPriceSource"],
            "clickhouse.trade_ticks",
        )
        tick_query = next(query for query, _ in clickhouse.calls if "market_data.trade_ticks" in query)
        self.assertIn("event_time <=", tick_query)
        self.assertIn("received_at", tick_query)
        self.assertIn("inserted_at <=", tick_query)
        self.assertIn("tuple(event_time, inserted_at", tick_query)
        self.assertEqual(result["sourceAsOf"]["market"], "2026-07-14T11:59:00Z")

    def test_metadata_and_heatmap_fallback_respect_the_fill_cutoff(self):
        cutoff = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
        clickhouse = FakeClickHouseProvider({
            "symbols": [{
                "symbol": "NVDA",
                "companyName": "Future symbol name",
                "source": "alpaca.assets",
                "sourceAsOf": "2026-07-11T00:00:00Z",
            }],
            "sec_company_tickers": [{
                "symbol": "NVDA",
                "companyName": "NVIDIA SEC",
                "source": "sec_company_tickers",
                "sourceAsOf": "2026-07-09T00:00:00Z",
            }],
        })
        with tempfile.TemporaryDirectory() as directory:
            seed_path = Path(directory) / "heatmap.json"
            seed_path.write_text(json.dumps({
                "sourceRetrievedAt": "2026-07-12",
                "items": [{"symbol": "NVDA", "companyName": "Future seed", "sector": "Technology"}],
            }), encoding="utf-8")
            provider = StoreCoachPointInTimeContextProvider(
                clickhouse_provider=clickhouse,
                redis_market_provider=FakeRedisMarketProvider(),
                ontology_provider=FakeOntologyProvider(),
                heatmap_seed_path=seed_path,
                now_provider=lambda: cutoff,
            )
            result = provider.load(
                fills=[{"fillId": "fill-1", "symbol": "NVDA", "filledAt": "2026-07-10T15:00:00Z"}],
                requested_at=cutoff,
                current_fill_ids=set(),
            )

        enrichment = result["fillEnrichmentById"]["fill-1"]
        self.assertEqual(enrichment["companyName"], "NVIDIA SEC")
        self.assertIsNone(enrichment["sector"])

    def test_fill_context_excludes_evidence_available_after_entry(self):
        cutoff = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
        clickhouse = FakeClickHouseProvider({
            "trade_ticks": [{"symbol": "NVDA", "price": 194.72, "sourceAsOf": "2026-07-14T11:59:00Z"}],
            "symbols": [{
                "symbol": "NVDA",
                "companyName": "NVIDIA Corporation",
                "exchange": "NASDAQ",
                "source": "alpaca.assets",
                "sourceAsOf": "2026-07-01T00:00:00Z",
            }],
            "news_articles": [
                {
                    "symbol": "NVDA",
                    "articleId": "outside-fill-lookback",
                    "headline": "Too old for the entry window",
                    "source": "wire",
                    "publishedAt": "2026-05-01T09:00:00Z",
                    "receivedAt": "2026-05-01T09:01:00Z",
                    "insertedAt": "2026-05-01T09:01:01Z",
                    "availableAt": "2026-05-01T09:01:01Z",
                },
                {
                    "symbol": "NVDA",
                    "articleId": "known-at-entry",
                    "headline": "Known before entry",
                    "source": "wire",
                    "publishedAt": "2026-07-09T09:00:00Z",
                    "receivedAt": "2026-07-09T09:01:00Z",
                    "insertedAt": "2026-07-09T09:01:01Z",
                    "availableAt": "2026-07-09T09:01:01Z",
                },
                {
                    "symbol": "NVDA",
                    "articleId": "after-entry",
                    "headline": "Published after entry",
                    "source": "wire",
                    "publishedAt": "2026-07-11T09:00:00Z",
                    "receivedAt": "2026-07-11T09:01:00Z",
                    "insertedAt": "2026-07-11T09:01:01Z",
                    "availableAt": "2026-07-11T09:01:01Z",
                },
            ],
            "sec_financial_facts": [
                {
                    "symbol": "NVDA",
                    "metric": "revenue",
                    "value": 100.0,
                    "unit": "USD",
                    "fiscalYear": 2026,
                    "fiscalPeriod": "Q1",
                    "periodEnd": "2026-04-30",
                    "filedAt": "2026-07-08",
                    "versionFiledAt": "2026-07-08",
                    "insertedAt": "2026-07-08T20:00:00Z",
                    "quality": "available",
                    "rowType": "fact",
                },
                {
                    "symbol": "NVDA",
                    "metric": "same_day_unknown",
                    "value": 200.0,
                    "unit": "USD",
                    "fiscalYear": 2026,
                    "fiscalPeriod": "Q1",
                    "periodEnd": "2026-04-30",
                    "filedAt": "2026-07-10",
                    "versionFiledAt": "2026-07-10",
                    "insertedAt": "2026-07-10T10:00:00Z",
                    "quality": "available",
                    "rowType": "fact",
                },
                {
                    "symbol": "NVDA",
                    "metric": "future_metric",
                    "value": 300.0,
                    "unit": "USD",
                    "fiscalYear": 2026,
                    "fiscalPeriod": "Q1",
                    "periodEnd": "2026-04-30",
                    "filedAt": "2026-07-12",
                    "versionFiledAt": "2026-07-12",
                    "insertedAt": "2026-07-12T10:00:00Z",
                    "quality": "available",
                    "rowType": "fact",
                },
            ],
            "yahoo_earnings_estimates": [
                {
                    "symbol": "NVDA",
                    "metric": "eps",
                    "average": 1.23,
                    "low": 1.1,
                    "high": 1.4,
                    "analystCount": 25,
                    "collectedAt": "2026-07-13T01:00:00Z",
                    "insertedAt": "2026-07-13T01:01:00Z",
                    "raw": json.dumps({"sourceFrame": "earnings_dates", "date": "2026-07-20T20:00:00Z"}),
                },
                {
                    "symbol": "NVDA",
                    "metric": "eps",
                    "average": 9.99,
                    "collectedAt": "2026-07-15T01:00:00Z",
                    "insertedAt": "2026-07-15T01:01:00Z",
                    "raw": json.dumps({"sourceFrame": "earnings_dates", "date": "2026-08-20T20:00:00Z"}),
                },
            ],
        })
        with tempfile.TemporaryDirectory() as directory:
            seed_path = Path(directory) / "heatmap.json"
            seed_path.write_text(json.dumps({
                "sourceRetrievedAt": "2026-06-30",
                "items": [{
                    "symbol": "NVDA",
                    "companyName": "NVIDIA",
                    "sector": "Technology",
                    "industry": "Semiconductors",
                }],
            }), encoding="utf-8")
            provider = StoreCoachPointInTimeContextProvider(
                clickhouse_provider=clickhouse,
                redis_market_provider=FakeRedisMarketProvider(),
                ontology_provider=FakeOntologyProvider(),
                heatmap_seed_path=seed_path,
                now_provider=lambda: cutoff,
            )
            result = provider.load(
                fills=[{"fillId": "fill-1", "symbol": "NVDA", "filledAt": "2026-07-10T15:00:00Z"}],
                requested_at=cutoff,
                current_fill_ids={"fill-1"},
            )

        enrichment = result["fillEnrichmentById"]["fill-1"]
        self.assertEqual(enrichment["companyName"], "NVIDIA Corporation")
        self.assertEqual(enrichment["sector"], "Technology")
        self.assertEqual(enrichment["industry"], "Semiconductors")

        news = result["newsContext"]["byFillId"]["fill-1"]["items"]
        self.assertEqual([item["articleId"] for item in news], ["known-at-entry"])
        self.assertLessEqual(news[0]["availableAt"], "2026-07-10T15:00:00Z")

        fundamentals = result["fundamentalsContext"]["byFillId"]["fill-1"]["items"]
        self.assertEqual([item["metric"] for item in fundamentals], ["revenue"])
        self.assertEqual(result["fundamentalsContext"]["availabilityPrecision"], "date")

        earnings = result["earningsContext"]["NVDA"]
        self.assertEqual(earnings["earningsAt"], "2026-07-20T20:00:00Z")
        self.assertEqual(earnings["earningsDaysRemaining"], 6)
        self.assertFalse(earnings["historicalRevisionAvailable"])

        ontology = result["ontologyContext"]
        self.assertEqual(ontology["temporalScope"], "current-only")
        self.assertFalse(ontology["historicalSimilarityEligible"])
        self.assertIsNone(result["sourceAsOf"]["ontology"])

        news_query = next(query for query, _ in clickhouse.calls if "market_data.news_articles" in query)
        self.assertIn("greatest(published_at", news_query)
        facts_query = next(query for query, _ in clickhouse.calls if "market_data.sec_financial_facts" in query)
        self.assertIn("filed_at <=", facts_query)
        self.assertIn("version_filed_at <=", facts_query)
        yahoo_query = next(query for query, _ in clickhouse.calls if "market_data.yahoo_earnings_estimates" in query)
        self.assertIn("collected_at <=", yahoo_query)
        self.assertIn("inserted_at <=", yahoo_query)

    def test_decision_cutoff_excludes_news_that_arrived_before_fill_but_after_order(self):
        cutoff = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
        clickhouse = FakeClickHouseProvider({
            "news_articles": [
                {
                    "symbol": "NVDA",
                    "articleId": "before-order",
                    "headline": "Known before order",
                    "source": "wire",
                    "publishedAt": "2026-07-10T13:00:00Z",
                    "receivedAt": "2026-07-10T13:00:01Z",
                    "insertedAt": "2026-07-10T13:00:02Z",
                    "availableAt": "2026-07-10T13:00:02Z",
                },
                {
                    "symbol": "NVDA",
                    "articleId": "after-order-before-fill",
                    "headline": "Not known when order was submitted",
                    "source": "wire",
                    "publishedAt": "2026-07-10T14:30:00Z",
                    "receivedAt": "2026-07-10T14:30:01Z",
                    "insertedAt": "2026-07-10T14:30:02Z",
                    "availableAt": "2026-07-10T14:30:02Z",
                },
            ],
        })
        provider = StoreCoachPointInTimeContextProvider(
            clickhouse_provider=clickhouse,
            redis_market_provider=FakeRedisMarketProvider(),
            ontology_provider=FakeOntologyProvider(),
            heatmap_seed_path="/missing/heatmap.json",
            now_provider=lambda: cutoff,
        )

        result = provider.load(
            fills=[{
                "fillId": "kis:order-1",
                "symbol": "NVDA",
                "decisionAt": "2026-07-10T14:00:00Z",
                "filledAt": "2026-07-10T15:00:00Z",
            }],
            requested_at=cutoff,
            current_fill_ids=set(),
        )

        context = result["newsContext"]["byFillId"]["kis:order-1"]
        self.assertEqual(context["asOf"], "2026-07-10T14:00:00Z")
        self.assertEqual([item["articleId"] for item in context["items"]], ["before-order"])

    def test_stale_quotes_are_reported_missing_instead_of_used_as_current_price(self):
        cutoff = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
        clickhouse = FakeClickHouseProvider({
            "trade_ticks": [{"symbol": "NVDA", "price": 190.0, "sourceAsOf": "2026-07-10T00:00:00Z"}],
            "chart_candles": [{"symbol": "NVDA", "price": 191.0, "sourceAsOf": "2026-07-10T00:00:00Z"}],
        })
        provider = StoreCoachPointInTimeContextProvider(
            clickhouse_provider=clickhouse,
            redis_market_provider=FakeRedisMarketProvider(),
            ontology_provider=FakeOntologyProvider(),
            heatmap_seed_path="/missing/heatmap.json",
            now_provider=lambda: cutoff,
        )

        with patch.dict("os.environ", {"COACH_CURRENT_QUOTE_MAX_AGE_MINUTES": "60"}):
            result = provider.load(
                fills=[{"fillId": "fill-1", "symbol": "NVDA", "filledAt": "2026-07-14T10:00:00Z"}],
                requested_at=cutoff,
                current_fill_ids={"fill-1"},
            )

        self.assertIsNone(result["fillEnrichmentById"]["fill-1"]["currentPrice"])
        self.assertTrue(any(item["code"] == "current_quote_missing" for item in result["missingData"]))

    def test_pre_fill_quote_is_not_used_for_current_return(self):
        cutoff = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
        provider = StoreCoachPointInTimeContextProvider(
            clickhouse_provider=FakeClickHouseProvider({
                "trade_ticks": [{"symbol": "NVDA", "price": 194.72, "sourceAsOf": "2026-07-14T09:59:00Z"}],
            }),
            redis_market_provider=FakeRedisMarketProvider(),
            ontology_provider=FakeOntologyProvider(),
            heatmap_seed_path="/missing/heatmap.json",
            now_provider=lambda: cutoff,
        )

        result = provider.load(
            fills=[{"fillId": "fill-1", "symbol": "NVDA", "filledAt": "2026-07-14T10:00:00Z"}],
            requested_at=cutoff,
            current_fill_ids={"fill-1"},
        )

        self.assertIsNone(result["fillEnrichmentById"]["fill-1"]["currentPrice"])
        self.assertTrue(any(item["code"] == "current_quote_missing" for item in result["missingData"]))


if __name__ == "__main__":
    unittest.main()
