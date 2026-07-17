from __future__ import annotations

import copy
import io
import json
import sys
import unittest
import urllib.error
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
        self.assertEqual(ready["version"], "chart-commentary.v2")
        self.assertEqual(len(ready["paragraphs"]), 3)
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

        wrong_type = FixtureWriter(mutation="type")
        with self.assertRaisesRegex(ChartCommentaryGenerationError, "type does not match"):
            generate_chart_commentary(
                fact_pack=fact_pack, writer=wrong_type, generated_at="2025-06-11T00:00:00.000Z",
            )

        markup = FixtureWriter(mutation="markup")
        with self.assertRaisesRegex(ChartCommentaryGenerationError, "continuous prose"):
            generate_chart_commentary(
                fact_pack=fact_pack, writer=markup, generated_at="2025-06-11T00:00:00.000Z",
            )

    def test_v2_inline_links_trace_final_drawing_candle_indicator_and_event(self):
        rows = _rows(160)
        geometry = _geometry()
        geometry.update(_level_geometry(rows, support_prices=[110.0], resistance_prices=[118.0, 125.0]))
        fact_pack = build_chart_commentary_fact_pack(
            symbol="NVDA", interval="1D", candles=rows, geometry=geometry,
            geometry_input_digest="sha256:geometry",
            context={
                "news": [{
                    "id": "news:NVDA:2025-06-10", "type": "news", "marketDate": "2025-06-10",
                    "summary": "저장 뉴스", "keyPoints": [], "impactDirection": "neutral",
                    "sentiment": "neutral", "articleCount": 1, "generatedAt": "2025-06-10T20:00:00.000Z",
                }],
                "earnings": [],
                "missingData": ["earnings"],
            },
        )

        ready, _latency = generate_chart_commentary(
            fact_pack=fact_pack, writer=FixtureWriter(), generated_at="2025-06-11T00:00:00.000Z",
        )

        links = [
            segment["link"]
            for paragraph in ready["paragraphs"]
            for segment in paragraph["segments"]
            if segment.get("link")
        ]
        self.assertEqual({link["kind"] for link in links}, {"drawing", "candle", "indicator", "news"})
        self.assertEqual(ready["indicatorRecommendations"][0]["layer"], "rsi:14")
        self.assertEqual(len({reference["id"] for reference in ready["references"]}), len(ready["references"]))

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
        self.assertEqual(requests[0]["text"]["format"]["name"], "chart_commentary_ko_v2")
        self.assertEqual(json.loads(requests[0]["input"]), fact_pack)

    def test_openai_transport_retries_rate_limit_once(self):
        fact_pack = _fact_pack()
        fixture_output = FixtureWriter().generate(fact_pack)
        attempts = []
        responses = [
            urllib.error.HTTPError(
                "https://api.openai.com/v1/responses",
                429,
                "rate limited",
                {"x-request-id": "req-safe-1"},
                io.BytesIO(json.dumps({
                    "error": {"type": "rate_limit_error", "code": "rate_limit_exceeded", "param": None},
                }).encode("utf-8")),
            ),
            FakeOpenAIResponse({"status": "completed", "output_text": json.dumps(fixture_output)}),
        ]

        def urlopen(_request, *, timeout):
            attempts.append(timeout)
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        sleeps = []
        writer = OpenAIChartCommentaryWriter(
            read_config=lambda key: {
                "OPENAI_API_KEY": "secret-not-logged",
                "CHART_COMMENTARY_MODEL": "fixture-openai-model",
                "CHART_COMMENTARY_TIMEOUT_SECONDS": "12",
            }.get(key),
            urlopen=urlopen,
            sleep=sleeps.append,
        )

        self.assertEqual(writer.generate(fact_pack), fixture_output)
        self.assertEqual(attempts, [12.0, 12.0])
        self.assertEqual(sleeps, [0.5])

    def test_openai_transport_does_not_retry_auth_failure(self):
        attempts = []

        def urlopen(_request, *, timeout):
            attempts.append(timeout)
            raise urllib.error.HTTPError(
                "https://api.openai.com/v1/responses",
                401,
                "unauthorized",
                {"x-request-id": "req-auth-1"},
                io.BytesIO(json.dumps({
                    "error": {"type": "invalid_request_error", "code": "invalid_api_key", "param": None},
                }).encode("utf-8")),
            )

        writer = OpenAIChartCommentaryWriter(
            read_config=lambda key: {"OPENAI_API_KEY": "invalid", "CHART_COMMENTARY_MODEL": "fixture"}.get(key),
            urlopen=urlopen,
            sleep=lambda _seconds: self.fail("auth failure must not retry"),
        )
        with self.assertRaises(ChartCommentaryGenerationError) as caught:
            writer.generate(_fact_pack())
        self.assertEqual(caught.exception.code, "provider_auth")
        self.assertEqual(caught.exception.attempts, 1)
        self.assertEqual(caught.exception.details["requestId"], "req-auth-1")
        self.assertEqual(len(attempts), 1)

    def test_post_validation_failure_gets_one_repair_attempt(self):
        writer = RepairingFixtureWriter()

        ready, _latency = generate_chart_commentary(
            fact_pack=_fact_pack(),
            writer=writer,
            generated_at="2025-06-11T00:00:00.000Z",
        )

        self.assertEqual(ready["version"], "chart-commentary.v2")
        self.assertEqual(writer.repair_calls, 1)

    def test_required_builder_fails_fast_when_openai_key_is_missing(self):
        writer = OpenAIChartCommentaryWriter(
            read_config=lambda key: {"CHART_COMMENTARY_MODEL": "fixture-openai-model"}.get(key),
            response_requester=lambda _request: {},
        )
        with self.assertRaises(ChartCommentaryGenerationError) as caught:
            ChartAssetBuilder(
                candle_loader=Loader(_rows(160)),
                storage=MemoryStorage(),
                progress=InMemoryChartAssetProgressStore(),
                commentary_writer=writer,
                commentary_context_loader=StaticContext(),
                commentary_required=True,
                concurrency=1,
            )
        self.assertEqual(caught.exception.code, "provider_config")

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
        self.assertEqual(failure_log["commentary"]["promptVersion"], "chart-commentary.ko.v2")
        self.assertTrue(str(failure_log["commentary"]["contextDigest"]).startswith("sha256:"))
        self.assertEqual(set(failure_log["commentary"]), {
            "status", "failureCode", "retryable", "attempts", "model", "promptVersion",
            "contextDigest", "newsAsOf", "earningsAsOf", "latencyMs", "requestId",
        })
        self.assertEqual(failure_log["commentary"]["failureCode"], "provider_timeout")
        self.assertEqual(failure_log["commentary"]["requestId"], "req-fixture-safe")
        self.assertNotIn("unsafe", str(failure_log))
        self.assertIn("[provider_timeout]", state["recentItems"][-1]["error"])

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
        self.assertEqual(asset["commentary"]["version"], "chart-commentary.v2")
        self.assertNotIn("first-user", str(asset))
        _validate_asset_schema(asset)


class FixtureWriter:
    model = "fixture-model"

    def __init__(self, mutation: str | None = None):
        self.mutation = mutation

    def generate(self, fact_pack):
        references = fact_pack["references"]
        candle_references = [item["id"] for item in references if item["type"] == "candle"]
        candle_reference = candle_references[0]
        indicator_reference = candle_references[1]
        drawing_reference = next((item["id"] for item in references if item["type"] == "drawing"), None)
        event = next((item for item in references if item["type"] in {"news", "earnings"}), None)
        paragraphs = [
            {
                "id": "structure",
                "segments": [
                    {"id": "structure-open", "text": "저장된 완료 봉과 구조 지표를 함께 놓고 보면 현재 가격은 단기 변동 하나보다 분석 시점까지 축적된 방향과 경계의 관계로 읽는 편이 타당합니다. ", "link": None},
                    *([{"id": "structure-drawing", "text": "최종 작도", "link": {"kind": "drawing", "referenceIds": [drawing_reference]}}] if drawing_reference else []),
                    {"id": "structure-close", "text": "는 가격이 어느 구간에서 반응을 반복했는지 보여 주며, 한 선의 돌파만 단독 신호로 보지 않고 반대 경계와 최근 접촉 이력을 함께 확인하게 합니다. 이 구조는 결론을 서두르기보다 완료 봉이 경계 안팎에서 자리를 잡는지 관찰하는 기준으로 기능합니다.", "link": None},
                ],
            },
            {
                "id": "confirmation",
                "segments": [
                    {"id": "confirmation-open", "text": "그 판단을 점검할 때에는 ", "link": None},
                    {"id": "confirmation-candle", "text": "주요 완료 봉", "link": {"kind": "candle", "referenceId": candle_reference}},
                    {"id": "confirmation-middle", "text": "의 몸통과 꼬리, 거래량이 경계 부근에서 남긴 반응을 먼저 살피는 것이 좋으며, 같은 위치에서 되돌림이 반복되는지도 중요합니다. 여기에 ", "link": None},
                    {"id": "confirmation-indicator", "text": "상대강도지수", "link": {"kind": "indicator", "layer": "rsi:14", "referenceIds": [indicator_reference]}},
                    {"id": "confirmation-close", "text": "를 겹쳐 보면 가격이 경계를 시험하는 과정에서 움직임의 힘이 확장되는지 둔화되는지를 분리해 볼 수 있습니다. 지표는 작도를 대신하는 결론이 아니라 가격 반응의 질을 확인하는 보조 근거로만 사용합니다.", "link": None},
                ],
            },
            {
                "id": "context",
                "segments": [
                    {"id": "context-open", "text": "외부 맥락은 가격 움직임의 원인으로 단정하지 않고 분석 시점에 시장이 함께 소화하던 정보의 범위로만 읽습니다. ", "link": None},
                    *([{"id": "context-event", "text": "저장된 이벤트 맥락", "link": {"kind": event["type"], "referenceId": event["id"]}}] if event else []),
                    {"id": "context-close", "text": "이 제한적이라면 차트 구조와 완료 봉이 제공하는 사실을 우선하고 확인되지 않은 서사를 빈칸에 채우지 않습니다. 다음 관찰에서는 가격이 최종 경계를 지킨 채 반응을 이어 가는지, 또는 반대편 경계를 넘어 기존 해석을 재검토해야 하는지를 새 완료 봉과 같은 기준으로 비교합니다.", "link": None},
                ],
            },
        ]
        if self.mutation == "reference":
            paragraphs[1]["segments"][1]["link"]["referenceId"] = "missing-reference"
        if self.mutation == "personal":
            paragraphs[0]["segments"][0]["text"] = paragraphs[0]["segments"][0]["text"].replace("저장된 완료 봉", "사용자 계좌")
        if self.mutation == "number":
            paragraphs[0]["segments"][0]["text"] = paragraphs[0]["segments"][0]["text"].replace("저장된 완료 봉", "저장된 완료 봉과 9999")
        if self.mutation == "type":
            paragraphs[1]["segments"][1]["link"]["kind"] = "news"
        if self.mutation == "markup":
            paragraphs[0]["segments"][0]["text"] = "<strong>" + paragraphs[0]["segments"][0]["text"]
        return {
            "paragraphs": paragraphs,
            "indicatorRecommendations": [{
                "layer": "rsi:14", "label": "상대강도지수",
                "reason": "가격 움직임의 힘이 확장되는지 둔화되는지 함께 확인합니다.",
                "referenceIds": [indicator_reference],
            }],
            "limitations": [],
        }


class FailingWriter:
    model = "fixture-model"

    def generate(self, _fact_pack):
        raise ChartCommentaryGenerationError(
            "injected failure",
            code="provider_timeout",
            retryable=True,
            details={"requestId": "req-fixture-safe", "providerMessage": "unsafe raw response"},
        )


class RepairingFixtureWriter(FixtureWriter):
    def __init__(self):
        super().__init__(mutation="number")
        self.repair_calls = 0

    def repair(self, fact_pack, previous_output, validation_error):
        self.repair_calls += 1
        if "9999" not in str(previous_output):
            raise AssertionError("repair must receive the rejected structured output")
        if "unsupported numeric" not in str(validation_error):
            raise AssertionError("repair must receive the validation failure")
        return FixtureWriter().generate(fact_pack)


class FakeOpenAIResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


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
