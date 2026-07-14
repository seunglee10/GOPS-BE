from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "systems" / "agent-orchestration" / "shared"))

from gops_agents.orchestration.coach_analytics import (  # noqa: E402
    CoachInputSnapshot,
    _build_action_center,
    build_coach_report,
    calculate_outcomes,
    select_similar_cases,
)
from gops_agents.contracts import AnalysisReport  # noqa: E402
from gops_agents.runtime.report_store import deserialize_report, serialize_report  # noqa: E402


class CoachAnalyticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.current = {
            "fillId": "today", "symbol": "NVDA", "side": "buy", "filledAt": "2026-07-10T14:00:00Z",
            "averageFillPrice": 100, "industry": "semiconductor", "sector": "technology",
            "marketRegime": "up", "trend": "up", "momentum": "overheated", "volumeState": "low",
            "eventState": "earnings-near", "concentrationBand": "high", "cashBand": "low",
            "rsiBand": "overbought", "macdState": "weakening",
        }

    def candidate(self, case_id: str, date: str, score_variant: str = "same") -> dict:
        row = {**self.current, "caseId": case_id, "fillId": case_id, "tradeDate": date, "entryAt": date,
               "featureAsOf": date,
               "entryPrice": 100, "exitPrice": 105, "series": [
                   {"relativeDay": 0, "open": 100, "high": 103, "low": 98, "close": 101},
                   {"relativeDay": 1, "open": 101, "high": 108, "low": 99, "close": 105},
               ]}
        if score_variant != "same": row["trend"] = "down"
        return row

    def test_similarity_is_deterministic_excludes_self_future_and_limits_six(self) -> None:
        rows = [self.candidate(f"case-{i}", f"2026-06-{i + 1:02d}T14:00:00Z") for i in range(8)]
        lookahead = self.candidate("lookahead", "2026-06-20T14:00:00Z")
        lookahead["featureAsOf"] = "2026-06-21T14:00:00Z"
        missing_as_of = self.candidate("missing-as-of", "2026-06-19T14:00:00Z")
        missing_as_of.pop("featureAsOf")
        rows += [
            self.candidate("today", "2026-06-20T14:00:00Z"),
            self.candidate("future", "2026-08-01T14:00:00Z"),
            lookahead,
            missing_as_of,
        ]
        first = select_similar_cases(self.current, rows)
        second = select_similar_cases(self.current, list(reversed(rows)))
        self.assertEqual(first, second)
        self.assertEqual(len(first), 6)
        self.assertNotIn("today", [item["caseId"] for item in first])
        self.assertNotIn("future", [item["caseId"] for item in first])
        self.assertNotIn("lookahead", [item["caseId"] for item in first])
        self.assertNotIn("missing-as-of", [item["caseId"] for item in first])

    def test_outcomes_calculate_mfe_mae_and_return(self) -> None:
        result = calculate_outcomes(100, 105, [{"high": 108, "low": 97}, {"high": 106, "low": 99}], "buy")
        self.assertEqual(result, {"returnPercent": 5.0, "mfePercent": 8.0, "maePercent": -3.0})

        short_result = calculate_outcomes(100, 90, [{"high": 107, "low": 88}, {"high": 102, "low": 91}], "sell")
        self.assertEqual(short_result, {"returnPercent": 10.0, "mfePercent": 12.0, "maePercent": -7.0})

    def test_daily_entry_candle_is_not_used_for_intraday_mfe_or_mae(self) -> None:
        candidate = self.candidate("daily-case", "2026-06-20T14:00:00Z")
        candidate.update({
            "seriesInterval": "1D",
            "exitPrice": 105,
            "series": [
                {"relativeDay": 0, "time": "2026-06-20T00:00:00Z", "high": 150, "low": 50, "close": 101},
                {"relativeDay": 1, "time": "2026-06-21T00:00:00Z", "high": 110, "low": 95, "close": 105},
            ],
        })

        result = select_similar_cases(self.current, [candidate])[0]

        self.assertEqual(result["returnPercent"], 5.0)
        self.assertEqual(result["mfePercent"], 10.0)
        self.assertEqual(result["maePercent"], -5.0)

    def test_page2_periods_keep_entry_exit_and_portfolio_samples_point_in_time(self) -> None:
        cases = [
            self.candidate("buy-10", "2026-06-30T14:00:00Z"),
            {**self.candidate("sell-20", "2026-06-20T14:00:00Z"), "side": "sell"},
            self.candidate("buy-40", "2026-05-31T14:00:00Z"),
            {**self.candidate("sell-80", "2026-04-21T14:00:00Z"), "side": "sell"},
            self.candidate("buy-200", "2025-12-22T14:00:00Z"),
            self.candidate("future", "2026-07-11T14:00:00Z"),
        ]
        history = [
            self._portfolio_snapshot("2026-06-30T20:00:00Z", 60),
            self._portfolio_snapshot("2026-05-31T20:00:00Z", 65),
            self._portfolio_snapshot("2025-12-22T20:00:00Z", 70),
            self._portfolio_snapshot("2026-07-11T20:00:00Z", 90),
        ]
        report = build_coach_report({
            "request": {"requestedAt": "2026-07-10T15:00:00Z"},
            "chartContext": {"historicalCases": cases},
            "portfolioBefore": {"history": history},
            "fills": [],
        }, "analysis-periods", generated_at="2026-07-10T15:00:00Z")

        assert report is not None and report["page2"] is not None
        periods = report["page2"]["reportsByPeriod"]
        self.assertEqual(
            {stage: periods["30d"][stage]["sampleSize"] for stage in ("entry", "exit", "portfolio")},
            {"entry": 1, "exit": 1, "portfolio": 1},
        )
        self.assertEqual(
            {stage: periods["90d"][stage]["sampleSize"] for stage in ("entry", "exit", "portfolio")},
            {"entry": 2, "exit": 2, "portfolio": 2},
        )
        self.assertEqual(
            {stage: periods["1y"][stage]["sampleSize"] for stage in ("entry", "exit", "portfolio")},
            {"entry": 3, "exit": 2, "portfolio": 3},
        )

    def test_portfolio_impact_supports_gops_foreign_account_contract(self) -> None:
        before = {
            "account": {"cashForeign": 200, "totalValueForeign": 1000},
            "positions": [
                {"symbol": "NVDA", "sector": "Technology", "marketValueForeign": 100},
                {"symbol": "MSFT", "sector": "Technology", "marketValueForeign": 700},
            ],
        }
        after = {
            "account": {"cashForeign": 100, "totalValueForeign": 1000},
            "positions": [
                {"symbol": "NVDA", "sector": "Technology", "marketValueForeign": 200},
                {"symbol": "MSFT", "sector": "Technology", "marketValueForeign": 700},
            ],
        }
        report = build_coach_report({
            "request": {"selectedFillId": "today"},
            "fills": [{**self.current, "sector": "Technology", "currentPrice": 105}],
            "portfolioBefore": {"selected": before, "byFillId": {"today": before}},
            "portfolioAfter": {"selected": after, "byFillId": {"today": after}},
        }, "analysis-portfolio", generated_at="2026-07-10T15:00:00Z")

        assert report is not None and report["page1"] is not None
        impact = report["page1"]["portfolioImpact"]
        self.assertEqual(impact["symbolWeightBefore"], 10.0)
        self.assertEqual(impact["symbolWeightAfter"], 20.0)
        self.assertEqual(impact["cashWeightBefore"], 20.0)
        self.assertEqual(impact["cashWeightAfter"], 10.0)
        self.assertIn("단일 종목 위험 증가", impact["riskFlags"])
        self.assertIn("현금 완충력 감소", impact["riskFlags"])

    def test_portfolio_impact_supports_gops_krw_account_contract(self) -> None:
        before = {
            "account": {"currency": "KRW", "cashKrw": 2_000, "totalValueKrw": 10_000},
            "positions": [{"symbol": "005930", "sector": "Technology", "marketValueKrw": 8_000}],
        }
        after = {
            "account": {"currency": "KRW", "cashKrw": 1_000, "totalValueKrw": 10_000},
            "positions": [{"symbol": "005930", "sector": "Technology", "marketValueKrw": 9_000}],
        }
        report = build_coach_report({
            "request": {"selectedFillId": "kr-fill"},
            "fills": [{
                "fillId": "kr-fill", "symbol": "005930", "companyName": "삼성전자", "sector": "Technology",
                "side": "buy", "filledAt": "2026-07-10T01:00:00Z", "averageFillPrice": 70_000,
            }],
            "portfolioBefore": {"selected": before, "byFillId": {"kr-fill": before}},
            "portfolioAfter": {"selected": after, "byFillId": {"kr-fill": after}},
        }, "analysis-krw", generated_at="2026-07-10T15:00:00Z")

        assert report is not None and report["page1"] is not None
        impact = report["page1"]["portfolioImpact"]
        self.assertEqual(impact["symbolWeightBefore"], 80.0)
        self.assertEqual(impact["symbolWeightAfter"], 90.0)
        self.assertEqual(impact["cashWeightBefore"], 20.0)
        self.assertEqual(impact["cashWeightAfter"], 10.0)

    def test_missing_values_are_not_invented_and_process_is_separate_from_result(self) -> None:
        snapshot = {"request": {}, "fills": [{"fillId": "today", "symbol": "NVDA", "side": "buy"}]}
        report = build_coach_report(snapshot, "analysis-1", generated_at="2026-07-10T15:00:00Z")
        assert report is not None and report["page1"] is not None
        trade = report["page1"]["trades"][0]
        self.assertIsNone(trade["currentReturnPercent"])
        assessment = report["page1"]["decisionAssessment"]
        self.assertEqual(assessment["processAssessment"], "확인 기록 없음")
        self.assertIsNone(assessment["outcomeAssessment"])

    def test_report_round_trip_preserves_coach_report(self) -> None:
        coach = build_coach_report({"fills": []}, "analysis-1", generated_at="2026-07-10T15:00:00Z")
        report = AnalysisReport(analysisId="analysis-1", symbol="NVDA", intent="coach", status="completed",
            createdAt="2026-07-10T15:00:00Z", summary="done", rationale="done", coachReport=coach)
        restored = deserialize_report(serialize_report(report))
        self.assertIsNotNone(restored)
        self.assertEqual(restored.coachReport, coach)
        self.assertEqual(json.loads(serialize_report(restored))["coachReport"]["contractVersion"], "coach-report.v2")

    def test_action_center_groups_deterministic_proposals_without_inventing_alert_requests(self) -> None:
        snapshot = CoachInputSnapshot.from_dict({"request": {"alerts": [
            {"id": 10, "symbol": "NVDA", "type": "price_cross", "status": "active", "proposal_source": "daily_trade", "target_price": 110},
            {"id": 11, "symbol": "AAPL", "type": "price_cross", "status": "disabled", "target_price": 90},
        ]}})
        page1 = {"reviewsByFillId": {"fill-1": {"watchConditions": [
            {"id": "price-stop", "label": "가격 기준", "reason": "검증 가격", "currentValue": 100, "threshold": 95, "operator": "<", "recommendedAction": "보유 근거 재검토", "alertSupported": True, "alertRequest": {"symbol": "NVDA", "type": "price_cross", "targetPrice": "95", "repeatLimit": 1}},
            {"id": "rsi-check", "label": "RSI 확인", "reason": "과열 완화", "alertSupported": False},
        ]}}}
        page3 = {"priorities": [
            {"id": "entry-1", "stage": "entry", "title": "진입 거래량 확인", "condition": "상대 거래량 1.2 미만", "nextAction": "다음 진입 전 확인"},
            {"id": "exit-1", "stage": "exit", "title": "분할 청산 재현", "condition": "목표가 도달", "nextAction": "청산 비율 기록"},
            {"id": "portfolio-1", "stage": "portfolio", "title": "집중도 상한 확인", "condition": "상위 종목 60% 이상", "nextAction": "비중 계획 확인"},
        ]}

        first = _build_action_center(snapshot, page1, page3)
        second = _build_action_center(snapshot, page1, page3)

        self.assertEqual(first, second)
        self.assertEqual(
            [item["proposalSource"] for item in first["recommendedAlerts"]],
            ["daily_trade", "daily_trade", "entry_habit", "exit_habit", "portfolio_risk"],
        )
        self.assertIn("alertRequest", first["recommendedAlerts"][0])
        self.assertEqual(first["recommendedAlerts"][0]["symbol"], "NVDA")
        self.assertEqual(first["recommendedAlerts"][0]["currentValue"], 100)
        self.assertEqual(first["recommendedAlerts"][0]["threshold"], 95)
        self.assertEqual(first["recommendedAlerts"][0]["operator"], "<")
        self.assertEqual(first["recommendedAlerts"][0]["recommendedAction"], "보유 근거 재검토")
        self.assertTrue(first["recommendedAlerts"][0]["alertSupported"])
        self.assertNotIn("alertRequest", first["recommendedAlerts"][1])
        self.assertTrue(all("alertRequest" not in item for item in first["recommendedAlerts"][2:]))
        self.assertEqual(first["watchingAlerts"][0]["proposalSource"], "daily_trade")
        self.assertNotIn("proposalSource", first["watchingAlerts"][1])

    @staticmethod
    def _portfolio_snapshot(as_of: str, concentration: float) -> dict:
        top = concentration * 10
        return {
            "sourceAsOf": as_of,
            "account": {"cashForeign": 1000 - top, "totalValueForeign": 1000},
            "positions": [{"symbol": "NVDA", "marketValueForeign": top}],
        }


if __name__ == "__main__":
    unittest.main()
