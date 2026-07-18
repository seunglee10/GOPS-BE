from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "systems" / "agent-orchestration" / "shared"))

from gops_agents.orchestration.coach_analytics import (  # noqa: E402
    CoachInputSnapshot,
    _build_action_center,
    _portfolio_habit_rows,
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

    def test_similar_case_carries_historical_missed_marker_and_deterministic_comparison(self) -> None:
        candidate = self.candidate("checked-history", "2026-06-20T14:00:00Z")
        candidate["decisionChecks"] = [{
            "checkKey": "chart.rsi",
            "label": "RSI",
            "status": "unchecked",
            "marker": {"id": "marker-1", "type": "rsi", "relativeDay": -1, "value": 72},
        }]

        result = select_similar_cases(self.current, [candidate])[0]

        self.assertEqual(result["missedChecks"][0]["id"], "marker-1")
        self.assertEqual(result["mistakeSummary"], "확인 누락: RSI")
        self.assertIn("매수·매도 방향", result["sameAsToday"])

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
            self._fill_scoped_portfolio_snapshot("2026-06-30T20:00:00Z", 60, "fill-10"),
            self._fill_scoped_portfolio_snapshot("2026-05-31T20:00:00Z", 65, "fill-40"),
            self._fill_scoped_portfolio_snapshot("2025-12-22T20:00:00Z", 70, "fill-200"),
            self._fill_scoped_portfolio_snapshot("2026-07-11T20:00:00Z", 90, "future-fill"),
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

    def test_page2_long_term_profile_separates_process_from_outcome(self) -> None:
        complete_checks = [
            {"checkKey": key, "label": key, "status": "checked"}
            for key in ("chart.rsi", "chart.macd", "chart.volume", "news.company", "fundamentals.earnings", "market.context")
        ]
        cases = [
            {**self.candidate("confirmed-profit", "2026-06-30T14:00:00Z"), "decisionChecks": complete_checks},
            {**self.candidate("confirmed-loss", "2026-06-29T14:00:00Z"), "decisionChecks": complete_checks, "exitPrice": 96},
            {**self.candidate("missed-loss", "2026-06-28T14:00:00Z"), "decisionChecks": [{"checkKey": "chart.volume", "label": "거래량", "status": "unchecked"}], "exitPrice": 94},
            self.candidate("unrecorded-profit", "2026-06-27T14:00:00Z"),
            self.candidate("unrecorded-loss", "2026-06-26T14:00:00Z"),
        ]
        cases[-1]["exitPrice"] = 97
        report = build_coach_report({
            "request": {"requestedAt": "2026-07-10T15:00:00Z"},
            "chartContext": {"historicalCases": cases},
            "fills": [],
        }, "analysis-long-term-profile", generated_at="2026-07-10T15:00:00Z")

        assert report is not None and report["page2"] is not None
        profile = report["page2"]["reportsByPeriod"]["30d"]["entry"]["longTermProfile"]
        self.assertEqual(profile["decisionRecords"], {
            "recordedTradeCount": 3,
            "confirmedTradeCount": 2,
            "unconfirmedTradeCount": 3,
            "missedCheckTradeCount": 1,
        })
        self.assertEqual(sum(item["count"] for item in profile["processOutcome"]), 5)
        self.assertEqual(profile["patterns"][0]["id"], "decision-record-gap")
        self.assertIn("missed-chart.volume", {item["id"] for item in profile["patterns"]})
        self.assertEqual(len(profile["representativeTrades"]), 3)
        self.assertEqual(profile["representativeTrades"][0]["process"], "unconfirmed")
        self.assertEqual(profile["representativeTrades"][0]["outcome"], "negative")

    def test_portfolio_market_diversification_uses_snapshot_data_without_inventing_candidates(self) -> None:
        report = build_coach_report({
            "request": {"requestedAt": "2026-07-10T15:00:00Z"},
            "portfolioBefore": {"history": [{
                "sourceAsOf": "2026-07-09T20:00:00Z", "fillId": "portfolio-fill", "phase": "after",
                "account": {"totalValueForeign": 1000},
                "positions": [
                    {"symbol": "NVDA", "sector": "반도체", "marketValueForeign": 400},
                    {"symbol": "AMD", "sector": "반도체", "marketValueForeign": 210},
                    {"symbol": "JNJ", "sector": "헬스케어", "marketValueForeign": 80},
                ],
            }]},
            "marketContext": {"portfolioDiversification": {
                "sourceAsOf": "2026-07-10T14:00:00Z",
                "holdingSensitivities": [
                    {"symbol": "NVDA", "marketCorrelation": 0.82, "sectorCorrelation": 0.91},
                    {"symbol": "JNJ", "marketCorrelation": 0.32, "sectorCorrelation": 0.38},
                ],
                "candidates": [
                    {"id": "healthcare", "market": "미국 헬스케어", "sector": "헬스케어", "etfSymbol": "XLV", "correlationToConcentratedSector": 0.31, "relativeStrengthPercent": 2.4, "role": "defensive", "reason": "저상관 저장 시장 데이터"},
                ],
            }},
            "fills": [],
        }, "analysis-portfolio-diversification", generated_at="2026-07-10T15:00:00Z")

        assert report is not None and report["page2"] is not None
        model = report["page2"]["reportsByPeriod"]["30d"]["portfolio"]["longTermProfile"]["marketDiversification"]
        self.assertEqual(model["concentratedSector"], "반도체")
        self.assertEqual(model["concentratedWeightPercent"], 61.0)
        self.assertEqual(model["holdingSensitivities"][0]["independence"], "low")
        self.assertEqual(model["holdingSensitivities"][1]["independence"], "high")
        self.assertEqual(model["candidates"][0]["market"], "미국 헬스케어")
        self.assertEqual(model["candidates"][0]["suggestedMinWeightPercent"], 5.0)

        no_market_data = build_coach_report({
            "request": {"requestedAt": "2026-07-10T15:00:00Z"},
            "portfolioBefore": {"history": [{
                "sourceAsOf": "2026-07-09T20:00:00Z", "account": {"totalValueForeign": 1000},
                "positions": [{"symbol": "NVDA", "sector": "반도체", "marketValueForeign": 610}],
            }]},
            "fills": [],
        }, "analysis-portfolio-no-market", generated_at="2026-07-10T15:00:00Z")
        assert no_market_data is not None and no_market_data["page2"] is not None
        empty = no_market_data["page2"]["reportsByPeriod"]["30d"]["portfolio"]["longTermProfile"]["marketDiversification"]
        self.assertEqual(empty["candidates"], [])
        self.assertIn("분산 후보 시장 계산 데이터가 없습니다.", empty["missingData"])

    def test_raw_sell_history_builds_conservative_exit_habit_observations(self) -> None:
        cases = [
            self._sell_history_case(f"sell-{index}", f"2026-05-{index + 1:02d}T14:00:00Z")
            for index in range(5)
        ]
        report = build_coach_report({
            "request": {"requestedAt": "2026-07-10T15:00:00Z"},
            "chartContext": {"historicalCases": cases},
            "fills": [],
        }, "analysis-exit-habits", generated_at="2026-07-10T15:00:00Z")

        assert report is not None and report["page2"] is not None
        exit_report = report["page2"]["reportsByPeriod"]["90d"]["exit"]
        insights = {item["id"]: item for item in exit_report["insights"]}
        self.assertEqual(
            set(insights),
            {"exit-pre-sale-peak-giveback", "exit-post-sale-mfe"},
        )
        self.assertEqual(insights["exit-pre-sale-peak-giveback"]["sampleSize"], 5)
        self.assertIn("9.09%", insights["exit-pre-sale-peak-giveback"]["observedBehavior"])
        self.assertEqual(insights["exit-post-sale-mfe"]["kind"], "observation")
        self.assertEqual(insights["exit-post-sale-mfe"]["metrics"]["avgMfe"], 7.5)
        self.assertIn("분할 청산 비교 후보", insights["exit-post-sale-mfe"]["observedBehavior"])
        self.assertEqual(
            [item["label"] for item in exit_report["behavior"]],
            [
                "매도 후 최종 가격 경로(매도 관점)",
                "매도 후 최대 유리 경로(매도 관점)",
                "매도 후 최대 불리 경로(매도 관점)",
                "판단 확인 기록 보유 거래",
            ],
        )
        self.assertIn("결과가 좋았는지보다", exit_report["summary"])
        self.assertIn("계획한 가격에서 흔들리지 않고 팔았는지", exit_report["summary"])
        self.assertNotIn("평균 수익률", {item["label"] for item in exit_report["behavior"]})
        self.assertNotIn("평균 MFE", {item["label"] for item in exit_report["behavior"]})
        self.assertNotIn("평균 MAE", {item["label"] for item in exit_report["behavior"]})

        assert report["page3"] is not None and report["page4"] is not None
        self.assertEqual(
            {item["id"] for item in report["page3"]["priorities"] if item["stage"] == "exit"},
            set(insights),
        )
        self.assertEqual(
            [item["proposalSource"] for item in report["page4"]["recommendedAlerts"]],
            ["exit_habit", "exit_habit"],
        )
        self.assertTrue(all("alertRequest" not in item for item in report["page4"]["recommendedAlerts"]))

    def test_exit_habits_ignore_t0_t21_and_data_after_analysis_cutoff(self) -> None:
        cases = []
        for index in range(5):
            case = self._sell_history_case(
                f"bounded-{index}",
                f"2026-05-{index + 1:02d}T14:00:00Z",
                pre_peak=101,
                post_peak=104,
            )
            case["series"].extend([
                {"relativeDay": -1, "time": "2026-07-11T00:00:00Z", "high": 250, "low": 99, "close": 100},
                {"relativeDay": 20, "time": "2026-07-11T00:00:00Z", "high": 250, "low": 99, "close": 100},
            ])
            # The normal T+20 point is also outside the immutable request
            # cutoff, so the comparable hindsight horizon is not complete.
            for point in case["series"]:
                if point["relativeDay"] == 20:
                    point["time"] = "2026-07-11T00:00:00Z"
            cases.append(case)

        report = build_coach_report({
            "request": {"requestedAt": "2026-07-10T15:00:00Z"},
            "chartContext": {"historicalCases": cases},
            "fills": [],
        }, "analysis-exit-boundary", generated_at="2026-07-10T15:00:00Z")

        assert report is not None and report["page2"] is not None
        exit_report = report["page2"]["reportsByPeriod"]["90d"]["exit"]
        self.assertEqual(exit_report["sampleSize"], 5)
        self.assertEqual(exit_report["insights"], [])
        self.assertEqual(report["page4"]["recommendedAlerts"], [])

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

    def test_paper_portfolio_impact_uses_labeled_cost_basis_without_market_value(self) -> None:
        before = {
            "valuationBasis": "cost_basis",
            "account": {"cashBalance": "1000", "currency": "USD"},
            "positions": [
                {"symbol": "NVDA", "sector": "Technology", "costBasisValue": "1000"},
                {"symbol": "AMD", "sector": "Technology", "costBasisValue": "1000"},
            ],
        }
        after = {
            "valuationBasis": "cost_basis",
            "account": {"cashBalance": "500", "currency": "USD"},
            "positions": [
                {"symbol": "NVDA", "sector": "Technology", "costBasisValue": "1500"},
                {"symbol": "AMD", "sector": "Technology", "costBasisValue": "1000"},
            ],
        }
        report = build_coach_report({
            "request": {"selectedFillId": "today"},
            "fills": [{**self.current, "sector": "Technology", "currentPrice": 105}],
            "portfolioBefore": {"selected": before, "byFillId": {"today": before}},
            "portfolioAfter": {"selected": after, "byFillId": {"today": after}},
        }, "analysis-paper-portfolio", generated_at="2026-07-10T15:00:00Z")

        assert report is not None and report["page1"] is not None
        impact = report["page1"]["portfolioImpact"]
        self.assertEqual(impact["symbolWeightBefore"], 33.3333)
        self.assertEqual(impact["symbolWeightAfter"], 50.0)
        self.assertEqual(impact["cashWeightBefore"], 33.3333)
        self.assertEqual(impact["cashWeightAfter"], 16.6667)
        self.assertEqual(impact["sectorWeightBefore"], 66.6667)
        self.assertEqual(impact["sectorWeightAfter"], 83.3333)
        self.assertEqual(impact["valuationBasisBefore"], "cost_basis")
        self.assertEqual(impact["valuationBasisAfter"], "cost_basis")
        self.assertIn("단일 종목 위험 증가", impact["riskFlags"])
        self.assertIn("현금 완충력 감소", impact["riskFlags"])

    def test_paper_sector_weight_is_missing_when_any_position_lacks_sector(self) -> None:
        portfolio = {
            "valuationBasis": "cost_basis",
            "account": {"cashBalance": "1000", "currency": "USD"},
            "positions": [
                {"symbol": "NVDA", "sector": "Technology", "costBasisValue": "1000"},
                {"symbol": "AMD", "costBasisValue": "1000"},
            ],
        }
        report = build_coach_report({
            "request": {"selectedFillId": "today"},
            "fills": [{**self.current, "sector": "Technology", "currentPrice": 105}],
            "portfolioBefore": {"selected": portfolio, "byFillId": {"today": portfolio}},
            "portfolioAfter": {"selected": portfolio, "byFillId": {"today": portfolio}},
        }, "analysis-paper-missing-sector", generated_at="2026-07-10T15:00:00Z")

        assert report is not None and report["page1"] is not None
        impact = report["page1"]["portfolioImpact"]
        self.assertIsNone(impact["sectorWeightBefore"])
        self.assertIsNone(impact["sectorWeightAfter"])
        self.assertNotIn("섹터 집중도 상승", impact["riskFlags"])

    def test_portfolio_habit_counts_only_exact_after_state(self) -> None:
        generic_at = "2026-07-01T14:00:00Z"
        paper_before = {
            "sourceAsOf": "2026-07-02T14:00:00Z",
            "executionMode": "paper",
            "fillId": "paper:order-1",
            "phase": "before",
            "valuationBasis": "cost_basis",
            "account": {"cashBalance": 400},
            "positions": [{"symbol": "NVDA", "costBasisValue": 600}],
        }
        paper_after = {
            **paper_before,
            "sourceAsOf": "2026-07-02T14:00:01Z",
            "phase": "after",
            "account": {"cashBalance": 300},
            "positions": [{"symbol": "NVDA", "costBasisValue": 700}],
        }
        report = build_coach_report({
            "request": {"requestedAt": "2026-07-10T15:00:00Z"},
            "fills": [],
            "portfolioBefore": {"history": [
                self._portfolio_snapshot(generic_at, 55),
                self._portfolio_snapshot(generic_at, 65),
                paper_before,
                paper_after,
            ]},
        }, "analysis-paper-portfolio-history", generated_at="2026-07-10T15:00:00Z")

        assert report is not None and report["page2"] is not None
        portfolio_report = report["page2"]["reportsByPeriod"]["30d"]["portfolio"]
        self.assertEqual(portfolio_report["sampleSize"], 1)
        self.assertEqual(portfolio_report["behavior"][0]["value"]["value"], 70.0)

    def test_portfolio_habit_excludes_polls_and_before_rows_and_keeps_latest_after_per_fill(self) -> None:
        cutoff = datetime(2026, 6, 10, tzinfo=timezone.utc).timestamp()
        requested = datetime(2026, 7, 10, tzinfo=timezone.utc).timestamp()
        generic_poll = self._portfolio_snapshot("2026-07-01T12:00:00Z", 55)
        before = {
            **self._fill_scoped_portfolio_snapshot("2026-07-02T12:00:00Z", 60, "paper:one"),
            "phase": "before",
        }
        older_after = self._fill_scoped_portfolio_snapshot("2026-07-02T12:00:01Z", 65, "paper:one")
        latest_after = self._fill_scoped_portfolio_snapshot("2026-07-02T12:00:02Z", 70, "paper:one")
        kis_after = {
            **self._fill_scoped_portfolio_snapshot("2026-07-03T12:00:00Z", 75, "kis:two"),
            "executionMode": "kis",
            "valuationBasis": "market_value",
        }
        before_cutoff = self._fill_scoped_portfolio_snapshot("2026-06-09T12:00:00Z", 80, "paper:old")
        after_request = self._fill_scoped_portfolio_snapshot("2026-07-11T12:00:00Z", 85, "paper:future")

        rows = _portfolio_habit_rows(
            [generic_poll, before, older_after, latest_after, kis_after, before_cutoff, after_request],
            cutoff,
            requested,
        )

        self.assertEqual([row["fillId"] for row in rows], ["paper:one", "kis:two"])
        self.assertEqual(rows[0]["sourceAsOf"], "2026-07-02T12:00:02Z")
        self.assertEqual(rows[1]["executionMode"], "kis")

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

    def test_partial_confirmation_record_never_receives_good_process_grade(self) -> None:
        report = build_coach_report({
            "request": {
                "selectedFillId": "today",
                "decisionChecksByFillId": {
                    "today": [{
                        "checkKey": "chart.rsi",
                        "category": "chart",
                        "label": "RSI",
                        "status": "checked",
                    }],
                },
            },
            "fills": [{"fillId": "today", "symbol": "NVDA", "side": "buy"}],
        }, "analysis-partial-checks", generated_at="2026-07-10T15:00:00Z")

        assert report is not None and report["page1"] is not None
        assessment = report["page1"]["decisionAssessment"]
        self.assertEqual(assessment["grade"], "insufficient_data")
        self.assertEqual(assessment["processAssessment"], "확인 기록 불완전 (1/6)")
        self.assertEqual(report["page1"]["confidence"]["level"], "low")

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

    @classmethod
    def _fill_scoped_portfolio_snapshot(
        cls,
        as_of: str,
        concentration: float,
        fill_id: str,
    ) -> dict:
        return {
            **cls._portfolio_snapshot(as_of, concentration),
            "fillId": fill_id,
            "phase": "after",
        }

    @staticmethod
    def _sell_history_case(
        case_id: str,
        traded_at: str,
        *,
        pre_peak: float = 110,
        post_peak: float = 107.5,
    ) -> dict:
        filled_at = datetime.fromisoformat(traded_at.replace("Z", "+00:00")).astimezone(timezone.utc)
        series = []
        for relative_day in range(-5, 0):
            high = pre_peak if relative_day == -1 else min(pre_peak, 104 + relative_day + 5)
            series.append({
                "relativeDay": relative_day,
                "time": (filled_at + timedelta(days=relative_day)).isoformat().replace("+00:00", "Z"),
                "high": high,
                "low": 98,
                "close": 100,
            })
        # T0 is intentionally extreme. Daily T0 includes post-fill prices and
        # must never influence the pre-sale observation.
        series.append({"relativeDay": 0, "time": traded_at, "high": 500, "low": 50, "close": 100})
        for relative_day in range(1, 22):
            high = post_peak if relative_day == 10 else min(post_peak, 104)
            if relative_day == 21:
                high = 600
            series.append({
                "relativeDay": relative_day,
                "time": (filled_at + timedelta(days=relative_day)).isoformat().replace("+00:00", "Z"),
                "high": high,
                "low": 99,
                "close": 101,
            })
        return {
            "caseId": case_id,
            "fillId": case_id,
            "tradeDate": traded_at,
            "filledAt": traded_at,
            "symbol": "NVDA",
            "side": "sell",
            "averageFillPrice": 100,
            "entryPrice": 100,
            "seriesInterval": "1D",
            "series": series,
        }


if __name__ == "__main__":
    unittest.main()
