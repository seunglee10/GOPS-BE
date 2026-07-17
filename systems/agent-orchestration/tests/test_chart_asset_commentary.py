from __future__ import annotations

import copy
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "systems" / "market-data" / "shared", ROOT / "systems" / "agent-orchestration" / "shared"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from alfaka.analytics.analysis_candles import AnalysisCandleBundle  # noqa: E402
from gops_agents.chart_assets.builder import ChartAssetBuilder  # noqa: E402
from gops_agents.chart_assets.commentary import (  # noqa: E402
    ChartCommentaryGenerationError,
    ClickHouseChartCommentaryContextLoader,
    OpenAIChartCommentaryWriter,
    build_chart_commentary_fact_pack,
    generate_chart_commentary,
)
from gops_agents.chart_assets.envelope import ChartAssetBuildEnvelope  # noqa: E402
from gops_agents.chart_assets.progress import InMemoryChartAssetProgressStore  # noqa: E402
from gops_agents.chart_assets.storage import _validate_asset_schema  # noqa: E402


class ChartAssetCommentaryTest(unittest.TestCase):
    def test_fact_pack_is_deterministic_and_has_bounded_real_candles(self):
        rows = _rows(160)
        geometry = _geometry()
        context = {
            "news": [{
                "id": "news:NVDA:2025-06-10", "type": "news", "marketDate": "2025-06-10",
                "summary": "저장 뉴스", "keyPoints": [], "impactDirection": "neutral",
                "sentiment": "neutral", "articleCount": 1, "generatedAt": "2025-06-10T20:00:00.000Z",
            }],
            "earnings": [],
            "missingData": ["earnings"],
        }

        first = build_chart_commentary_fact_pack(
            symbol="NVDA", interval="1D", candles=rows, geometry=geometry,
            geometry_input_digest="sha256:geometry", context=context,
        )
        second = build_chart_commentary_fact_pack(
            symbol="NVDA", interval="1D", candles=copy.deepcopy(rows), geometry=copy.deepcopy(geometry),
            geometry_input_digest="sha256:geometry", context=copy.deepcopy(context),
        )

        self.assertEqual(first, second)
        self.assertLessEqual(len(first["majorCandles"]), 6)
        self.assertTrue(all(item["timestamp"] in {row["timestamp"] for row in rows} for item in first["majorCandles"]))
        self.assertEqual(first["indicators"]["rsi14"]["period"], 14)
        self.assertNotIn("requestedBy", first)
        self.assertNotIn("user", str(first).lower())
        self.assertNotIn("portfolio", str(first).lower())

    def test_pattern_proposal_facts_use_one_final_pattern_geometry(self):
        rows = _rows(160)
        geometry = _geometry()
        signal_at = rows[-2]["timestamp"]
        upper_id = "chart-asset:NVDA:1D:pattern-triangle-1-upper"
        lower_id = "chart-asset:NVDA:1D:pattern-triangle-1-lower"
        geometry.update({
            "drawings": [
                _trend_drawing(upper_id, rows, 115.0),
                _trend_drawing(lower_id, rows, 110.0),
            ],
            "patterns": [{
                "id": "triangle-1", "geometryHash": "triangle-1",
                "kind": "ascending_triangle", "state": "confirmed",
            }],
            "primaryPattern": {
                "id": "triangle-1", "geometryHash": "triangle-1",
                "kind": "ascending_triangle", "state": "confirmed",
            },
            "tradePlan": {
                "patternId": "triangle-1", "patternKind": "ascending_triangle",
                "patternState": "confirmed", "action": "buy_candidate", "direction": "long",
                "signalAt": signal_at, "entryTrigger": 115.0, "entryPrice": 115.0,
                "targetPrice": 120.0, "stopPrice": 110.0, "rewardRiskRatio": 1.0,
                "reasons": [],
            },
            "drawingGroups": {"levels": [], "trend": [], "pattern": [upper_id, lower_id]},
        })

        fact_pack = build_chart_commentary_fact_pack(
            symbol="NVDA", interval="1D", candles=rows, geometry=geometry,
            geometry_input_digest="sha256:geometry", context={},
        )

        proposal = fact_pack["geometry"]["proposal"]
        self.assertEqual((proposal["entryPrice"], proposal["targetPrice"], proposal["stopPrice"]), (115.0, 120.0, 110.0))
        self.assertEqual(proposal["sources"]["entry"]["drawingIds"], [upper_id])
        self.assertEqual(proposal["sources"]["stop"]["drawingIds"], [lower_id])
        self.assertEqual(set(proposal["sources"]["target"]["drawingIds"]), {upper_id, lower_id})

    def test_level_proposal_facts_require_three_final_h_lines(self):
        rows = _rows(160)
        geometry = _geometry()
        geometry.update(_level_geometry(rows, support_prices=[110.0], resistance_prices=[118.0, 125.0]))

        fact_pack = build_chart_commentary_fact_pack(
            symbol="NVDA", interval="1D", candles=rows, geometry=geometry,
            geometry_input_digest="sha256:geometry", context={},
        )

        proposal = fact_pack["geometry"]["proposal"]
        self.assertEqual((proposal["entryPrice"], proposal["targetPrice"], proposal["stopPrice"]), (118.0, 125.0, 110.0))
        self.assertEqual(proposal["sources"]["entry"]["label"], "저항선")
        self.assertEqual(proposal["sources"]["target"]["label"], "다음 저항선")
        self.assertEqual(proposal["sources"]["stop"]["label"], "지지선")

        insufficient = _geometry()
        insufficient.update(_level_geometry(rows, support_prices=[110.0], resistance_prices=[118.0]))
        insufficient_pack = build_chart_commentary_fact_pack(
            symbol="NVDA", interval="1D", candles=rows, geometry=insufficient,
            geometry_input_digest="sha256:geometry", context={},
        )
        self.assertIsNone(insufficient_pack["geometry"]["proposal"])

    def test_context_loader_excludes_sources_after_build_cutoff(self):
        rows = _rows(160)
        cutoff = "2025-06-10T21:00:00.000Z"
        provider = ContextProvider([
            {"date": "2025-06-10", "summary": "accepted", "generatedAt": "2025-06-10T20:00:00.000Z"},
            {"date": "2025-06-10", "summary": "future", "generatedAt": "2025-06-10T22:00:00.000Z"},
        ], [
            {"eventAt": "2025-06-01T12:00:00.000Z", "actualValue": 1.0, "sourceAsOf": "2025-06-10T20:00:00.000Z"},
            {"eventAt": "2025-06-08T12:00:00.000Z", "actualValue": 2.0, "sourceAsOf": "2025-06-10T22:00:00.000Z"},
        ])

        context = ClickHouseChartCommentaryContextLoader(provider).load(
            symbol="NVDA", interval="1D", candles=rows,
            as_of=rows[-1]["timestamp"], build_cutoff=cutoff,
        )

        self.assertEqual([item["summary"] for item in context["news"]], ["accepted"])
        self.assertEqual(len(context["earnings"]), 1)
        self.assertEqual(context["earnings"][0]["eps"]["actual"], 1.0)

    def test_output_validation_rejects_unknown_fact_and_personal_language(self):
        fact_pack = _fact_pack()
        writer = FixtureWriter()
        ready, _latency = generate_chart_commentary(
            fact_pack=fact_pack, writer=writer, generated_at="2025-06-11T00:00:00.000Z",
        )
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["sourceIdentity"]["contextDigest"], fact_pack["contextDigest"])
        self.assertLessEqual(sum(ref["type"] == "candle" for ref in ready["references"]), 3)

        bad_reference = FixtureWriter(mutation="reference")
        with self.assertRaisesRegex(ChartCommentaryGenerationError, "unknown evidence"):
            generate_chart_commentary(
                fact_pack=fact_pack, writer=bad_reference, generated_at="2025-06-11T00:00:00.000Z",
            )

        personal = FixtureWriter(mutation="personal")
        with self.assertRaisesRegex(ChartCommentaryGenerationError, "personal account"):
            generate_chart_commentary(
                fact_pack=fact_pack, writer=personal, generated_at="2025-06-11T00:00:00.000Z",
            )

        invented_number = FixtureWriter(mutation="number")
        with self.assertRaisesRegex(ChartCommentaryGenerationError, "unsupported numeric"):
            generate_chart_commentary(
                fact_pack=fact_pack, writer=invented_number, generated_at="2025-06-11T00:00:00.000Z",
            )

    def test_openai_writer_uses_store_false_and_deterministic_strict_request(self):
        fact_pack = _fact_pack()
        fixture_output = FixtureWriter().generate(fact_pack)
        requests = []
        writer = OpenAIChartCommentaryWriter(
            read_config=lambda key: {"CHART_COMMENTARY_MODEL": "fixture-openai-model"}.get(key),
            response_requester=lambda request: requests.append(copy.deepcopy(request)) or fixture_output,
        )

        self.assertEqual(writer.generate(fact_pack), fixture_output)
        self.assertEqual(writer.generate(copy.deepcopy(fact_pack)), fixture_output)

        self.assertEqual(requests[0], requests[1])
        self.assertEqual(requests[0]["model"], "fixture-openai-model")
        self.assertIs(requests[0]["store"], False)
        self.assertEqual(requests[0]["text"]["format"]["type"], "json_schema")
        self.assertIs(requests[0]["text"]["format"]["strict"], True)
        self.assertEqual(json.loads(requests[0]["input"]), fact_pack)

    def test_builder_required_commentary_failure_preserves_previous_asset(self):
        rows = _rows(160)
        existing = {"assetVersion": "geometry", "symbol": "NVDA", "interval": "1D", "marker": "previous"}
        storage = MemoryStorage(existing)
        builder = ChartAssetBuilder(
            candle_loader=Loader(rows), storage=storage, progress=InMemoryChartAssetProgressStore(),
            commentary_writer=FailingWriter(), commentary_context_loader=StaticContext(),
            commentary_required=True, concurrency=1,
        )

        state = builder.run(ChartAssetBuildEnvelope.create(
            requested_by="any-user", symbols=["NVDA"], intervals=["1D"], force=True,
        ))

        self.assertEqual(state["status"], "completed_with_errors")
        self.assertEqual(state["recentItems"][-1]["stage"], "commentary")
        self.assertEqual(state["recentItems"][-1]["reason"], "commentary_generation_failed")
        self.assertIs(storage.assets[("NVDA", "1D")], existing)
        self.assertEqual(storage.save_count, 0)
        failure_log = json.loads(state["logs"][-1])
        self.assertEqual(failure_log["event"], "chart_commentary_failed")
        self.assertEqual(failure_log["commentary"]["status"], "failed")
        self.assertEqual(failure_log["commentary"]["model"], "fixture-model")
        self.assertEqual(failure_log["commentary"]["promptVersion"], "chart-commentary.ko.v1")
        self.assertTrue(str(failure_log["commentary"]["contextDigest"]).startswith("sha256:"))
        self.assertEqual(set(failure_log["commentary"]), {
            "status", "model", "promptVersion", "contextDigest",
            "newsAsOf", "earningsAsOf", "latencyMs",
        })

    def test_builder_stores_injected_commentary_and_schema_accepts_it(self):
        rows = _rows(160)
        storage = MemoryStorage()
        builder = ChartAssetBuilder(
            candle_loader=Loader(rows), storage=storage, progress=InMemoryChartAssetProgressStore(),
            commentary_writer=FixtureWriter(), commentary_context_loader=StaticContext(),
            commentary_required=True, concurrency=1,
        )

        state = builder.run(ChartAssetBuildEnvelope.create(
            requested_by="first-user", symbols=["NVDA"], intervals=["1D"], force=True,
        ))

        self.assertEqual(state["recentItems"][-1]["status"], "saved")
        asset = storage.assets[("NVDA", "1D")]
        self.assertEqual(asset["commentary"]["version"], "chart-commentary.v1")
        self.assertNotIn("first-user", str(asset))
        _validate_asset_schema(asset)


class FixtureWriter:
    model = "fixture-model"

    def __init__(self, mutation: str | None = None):
        self.mutation = mutation

    def generate(self, fact_pack):
        references = fact_pack["references"]
        candle_reference = next(item["id"] for item in references if item["type"] == "candle")
        drawing_reference = next((item["id"] for item in references if item["type"] == "drawing"), candle_reference)
        event_reference = next((item["id"] for item in references if item["type"] in {"news", "earnings"}), candle_reference)
        sentence = (
            "저장된 사실과 최종 작도를 함께 읽으면 현재 구조의 위치와 변화 조건을 한 화면에서 차분하게 구분할 수 있으며 "
            "각 근거는 분석 시점에 확정된 완료 봉과 연결되어 이후 정보가 섞이지 않은 관찰 맥락을 제공합니다."
        )
        texts = [sentence + " " + sentence, sentence, sentence + " " + sentence, sentence, sentence]
        kinds = ("overview", "drawing_guide", "indicator_context", "event_context", "watch_next")
        reference_by_kind = {
            "overview": candle_reference,
            "drawing_guide": drawing_reference,
            "indicator_context": candle_reference,
            "event_context": event_reference,
            "watch_next": candle_reference,
        }
        blocks = [{
            "id": f"block-{index}", "kind": kind, "text": texts[index],
            "referenceIds": [reference_by_kind[kind]],
        } for index, kind in enumerate(kinds)]
        if self.mutation == "reference":
            blocks[0]["referenceIds"] = ["missing-reference"]
        if self.mutation == "personal":
            blocks[0]["text"] = blocks[0]["text"].replace("저장된 사실", "사용자 계좌")
        if self.mutation == "number":
            blocks[0]["text"] = blocks[0]["text"].replace("저장된 사실", "저장된 사실과 9999")
        return {"blocks": blocks, "indicatorRecommendations": [], "limitations": []}


class FailingWriter:
    model = "fixture-model"

    def generate(self, _fact_pack):
        raise ChartCommentaryGenerationError("injected failure")


class StaticContext:
    def load(self, **_kwargs):
        return {"news": [], "earnings": [], "missingData": ["news", "earnings"]}


class ContextProvider:
    def __init__(self, news, earnings):
        self.news = news
        self.earnings = earnings

    def company_daily_news_summaries_between(self, *_args, **_kwargs):
        return self.news

    def earnings_events(self, *_args, **_kwargs):
        return self.earnings


class Loader:
    def __init__(self, rows):
        self.rows = rows

    def load_symbol(self, _symbol, intervals):
        interval = intervals[0]
        return AnalysisCandleBundle(
            rows={interval: self.rows},
            coverage={interval: {
                "coverageState": "partial", "recentContiguousBars": len(self.rows),
                "missingBars": 380 - len(self.rows),
            }},
            digests={interval: "sha256:geometry"},
        )


class MemoryStorage:
    def __init__(self, existing=None):
        self.assets = {("NVDA", "1D"): existing} if existing else {}
        self.save_count = 0

    def get(self, symbol, interval):
        return self.assets.get((symbol, interval))

    def save(self, asset):
        self.save_count += 1
        self.assets[(asset["symbol"], asset["interval"])] = asset
        return True


def _rows(count: int):
    start = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)
    return [{
        "candleKey": (start + timedelta(days=index)).date().isoformat(),
        "timestamp": (start + timedelta(days=index)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "barIndex": index,
        "open": 100 + index * 0.1,
        "high": 101 + index * 0.1 + (index % 7) * 0.04,
        "low": 99 + index * 0.1 - (index % 5) * 0.03,
        "close": 100.2 + index * 0.1,
        "volume": 1_000_000 + (index % 11) * 20_000,
        "isClosed": True,
        "interval": "1D",
    } for index in range(count)]


def _geometry():
    return {
        "drawings": [], "supports": [], "resistances": [], "patterns": [],
        "primaryPattern": None, "tradePlan": None, "primaryTriangle": None,
        "historicalTriangle": None, "evidence": [],
        "drawingGroups": {"levels": [], "trend": [], "pattern": []},
        "analysisTrace": {
            "version": "geometry-analysis-trace-v2", "pivots": [],
            "levelCandidates": [], "trendCandidates": [], "patternCandidates": [],
            "selections": {"levelCandidateIds": [], "trendCandidateIds": [], "patternCandidateIds": []},
            "omittedCounts": {},
            "completeness": {
                "complete": True,
                "detected": {"levels": 0, "trends": 0, "patterns": 0},
                "stored": {"levels": 0, "trends": 0, "patterns": 0},
            },
        },
    }


def _trend_drawing(drawing_id: str, rows: list[dict], price: float):
    return {
        "id": drawing_id,
        "type": "trendLine",
        "createdBy": "system",
        "anchors": [
            {"timestamp": rows[120]["timestamp"], "logicalIndex": 120, "price": price},
            {"timestamp": rows[150]["timestamp"], "logicalIndex": 150, "price": price},
        ],
    }


def _level_geometry(rows: list[dict], *, support_prices: list[float], resistance_prices: list[float]):
    supports = [{"id": f"support-{index}", "price": price} for index, price in enumerate(support_prices)]
    resistances = [{"id": f"resistance-{index}", "price": price} for index, price in enumerate(resistance_prices)]
    levels = [*supports, *resistances]
    drawings = [{
        "id": f"chart-asset:NVDA:1D:{level['id']}",
        "type": "horizontalLine",
        "createdBy": "system",
        "anchors": [{"timestamp": rows[-1]["timestamp"], "logicalIndex": len(rows) - 1, "price": level["price"]}],
    } for level in levels]
    return {
        "drawings": drawings,
        "supports": supports,
        "resistances": resistances,
        "drawingGroups": {"levels": [drawing["id"] for drawing in drawings], "trend": [], "pattern": []},
    }


def _fact_pack():
    return build_chart_commentary_fact_pack(
        symbol="NVDA", interval="1D", candles=_rows(160), geometry=_geometry(),
        geometry_input_digest="sha256:geometry",
        context={"news": [], "earnings": [], "missingData": ["news", "earnings"]},
    )


if __name__ == "__main__":
    unittest.main()
