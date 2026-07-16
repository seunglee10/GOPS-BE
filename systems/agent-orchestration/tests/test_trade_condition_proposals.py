from __future__ import annotations

import unittest

from gops_agents.orchestration.trade_condition_proposals import build_trade_condition_proposals


class TradeConditionProposalTests(unittest.TestCase):
    def test_buy_proposal_uses_recent_structured_candle_low_and_keeps_quantity_missing(self):
        proposals = build_trade_condition_proposals(
            analysis_id="analysis-1",
            symbol="NVDA",
            intent="어느 가격에 매수하면 좋을지 추천해줘",
            chart_context={"candles": [
                {"low": 120 + index, "high": 125 + index}
                for index in range(25)
            ]},
        )

        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        self.assertEqual(proposal.side, "buy")
        self.assertEqual(proposal.direction, "atOrBelow")
        self.assertEqual(proposal.triggerPrice, 125.0)
        self.assertEqual(proposal.missingFields, ["quantity"])

    def test_sell_proposal_extracts_explicit_whole_share_quantity(self):
        proposals = build_trade_condition_proposals(
            analysis_id="analysis-2",
            symbol="AAPL",
            intent="매도 가격 추천하고 5주로 봐줘",
            chart_context={"candles": [{"low": 90, "high": 110}, {"low": 91, "high": 112}]},
        )

        self.assertEqual(proposals[0].triggerPrice, 112.0)
        self.assertEqual(proposals[0].quantity, 5)
        self.assertEqual(proposals[0].missingFields, [])

    def test_proposal_is_not_built_without_a_direction_or_valid_candles(self):
        self.assertEqual(build_trade_condition_proposals(
            analysis_id="analysis-3",
            symbol="TSLA",
            intent="가격 분석해줘",
            chart_context={"candles": [{"low": 100, "high": 120}]},
        ), [])


if __name__ == "__main__":
    unittest.main()
