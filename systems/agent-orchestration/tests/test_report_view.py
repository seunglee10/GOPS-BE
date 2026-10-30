"""D16 리포트 view 매퍼. 기록해 둔 샘플 리포트로 결과가 늘 같은지 확인한다."""

import json
import re
import unittest
from pathlib import Path

from gops_agents.contracts.view import build_report_view, clean_text

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agent_reports"
VIEW_KEYS = {"conclusion", "sections", "kv", "steps", "confidence", "suggestions"}
FORECAST_OR_ADVICE = re.compile(r"(매수하세요|매도하세요|사세요|파세요|오를 것|내릴 것|목표가|전망|예상)")


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class ReportViewTest(unittest.TestCase):
    def assert_view_shape(self, view):
        self.assertEqual(set(view), VIEW_KEYS)
        self.assertIn(view["confidence"], {"높음", "보통", "낮음"})
        for section in view["sections"]:
            self.assertEqual(set(section), {"title", "body", "evidence"})
            for item in section["evidence"]:
                self.assertIn(item["kind"], {"news", "chart", "fin"})
                self.assertTrue(item["ref"])
        for row in view["kv"]:
            self.assertEqual(set(row), {"k", "v"})
        for suggestion in view["suggestions"]:
            self.assertIn(suggestion["kind"], {"rule", "compare", "watch"})
            self.assertTrue(suggestion["label"])
            self.assertIsInstance(suggestion["payload"], dict)
        text = json.dumps(view, ensure_ascii=False)
        self.assertNotRegex(text, r"<[a-zA-Z/]")
        self.assertNotIn("http", text)
        self.assertNotIn("password", text)

    def test_rich_report_maps_sections_evidence_kv_steps_and_suggestions(self):
        report = load("rich_price_move")
        view = build_report_view(report)

        self.assert_view_shape(view)
        self.assertEqual(view, build_report_view(load("rich_price_move")))
        self.assertEqual(view["conclusion"], "거래량이 평소의 두 배를 넘으며 올랐고, 같은 날 데이터센터 기사가 이어졌어요.")
        self.assertEqual([section["title"] for section in view["sections"]], ["차트", "뉴스", "재무", "기업 관계"])
        chart, news, fin, relation = view["sections"]
        self.assertEqual(chart["body"], "거래량이 20일 평균의 **2.1배**로 늘었어요.")
        self.assertEqual(chart["evidence"], [{"kind": "chart", "ref": "volume"}])
        self.assertEqual(news["body"], "[[NVDA]] 데이터센터 매출 기사 4건이 이어졌어요.")
        self.assertEqual([item["ref"] for item in news["evidence"]], ["n_121", "n_122", "n_123", "n_124"])
        self.assertEqual(fin["evidence"], [{"kind": "fin", "ref": "revenue"}])
        self.assertEqual(relation["evidence"], [])
        self.assertEqual(view["kv"][:3], [
            {"k": "20일 평균 대비 거래량", "v": "2.1배"},
            {"k": "당일 등락", "v": "+3.42%"},
            {"k": "확인한 뉴스", "v": "4건"},
        ])
        self.assertEqual(view["steps"], ["질문 이해", "차트 해석", "뉴스 4건 확인", "재무 자료 확인", "기업 관계 확인", "답변 정리"])
        self.assertEqual(view["confidence"], "높음")
        rule, compare, watch = view["suggestions"]
        self.assertEqual(rule["label"], "137.70 아래로 가면 알리기")
        self.assertEqual(rule["payload"]["direction"], "below")
        self.assertEqual(compare["symbols"], ["NVDA", "AMD"])
        self.assertEqual(watch["payload"], {"symbol": "NVDA"})
        # 리포트의 bullish 포인트·stance 는 view 에 옮기지 않는다.
        self.assertNotIn("상승 여력", json.dumps(view, ensure_ascii=False))
        self.assertIsNone(FORECAST_OR_ADVICE.search(json.dumps(view, ensure_ascii=False)))

    def test_offline_no_data_report_hides_connection_errors(self):
        report = load("offline_news_no_data")
        self.assertIn("HTTPConnectionPool", report["summary"])
        view = build_report_view(report)

        self.assert_view_shape(view)
        self.assertEqual(view["conclusion"], "뉴스 저장 데이터 데이터 미연결 또는 조회 실패")
        self.assertEqual(view["sections"], [{"title": "뉴스", "body": "NVDA 관련 저장 뉴스가 없습니다.", "evidence": []}])
        self.assertEqual(view["steps"], ["질문 이해", "뉴스 확인(관련 기사 없음)", "답변 정리"])
        self.assertEqual(view["kv"], [{"k": "자료 상태", "v": "일부 자료 없음"}])
        self.assertEqual(view["confidence"], "낮음")
        self.assertEqual([item["kind"] for item in view["suggestions"]], ["watch"])

    def test_offline_partial_report_skips_internal_roles(self):
        view = build_report_view(load("offline_price_move_partial"))

        self.assert_view_shape(view)
        titles = [section["title"] for section in view["sections"]]
        self.assertEqual(titles, ["차트", "뉴스", "시장 흐름", "기업 관계"])
        self.assertNotIn("guardrail", json.dumps(view, ensure_ascii=False))
        self.assertEqual(view["confidence"], "낮음")

    def test_falls_back_to_final_answer_sections_and_handles_empty_reports(self):
        view = build_report_view({
            "symbol": "MARKET",
            "summary": "요약",
            "finalAnswer": {"summary": "", "sections": [{"title": "핵심 판단", "bullets": ["첫째", "<i>둘째</i>"]}]},
            "findings": [],
        })
        self.assertEqual(view["conclusion"], "요약")
        self.assertEqual(view["sections"], [{"title": "핵심 판단", "body": "첫째\n둘째", "evidence": []}])
        self.assertEqual(view["suggestions"], [])
        self.assertEqual(view["confidence"], "낮음")
        self.assertIsNone(build_report_view({"analysisId": "a", "status": "queued"}))
        self.assertIsNone(build_report_view(None))

    def test_confidence_thresholds_and_partial_cap(self):
        def confidence(value, partial=False):
            return build_report_view({"summary": "x", "finalResponse": {"confidence": value, "partial_data_used": partial}})["confidence"]

        self.assertEqual([confidence(0.7), confidence(0.45), confidence(0.44)], ["높음", "보통", "낮음"])
        self.assertEqual(confidence(0.9, partial=True), "보통")

    def test_clean_text_strips_html_urls_and_error_tails(self):
        self.assertEqual(clean_text("<b>굵게</b> **유지** [[AAPL]] https://x.y/z"), "굵게 **유지** [[AAPL]]")
        self.assertEqual(clean_text("재무 데이터 조회에 실패했습니다: ConnectionError"), "재무 데이터 조회에 실패했습니다")
        self.assertEqual(len(clean_text("가" * 700)), 600)


if __name__ == "__main__":
    unittest.main()
