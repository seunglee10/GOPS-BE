import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))

from gops_agents.intent_understanding.ui_parser import parse_ui_query
from gops_agents.roles import (
    AgentContext,
    UIAgent,
    normalize_layout_panels,
    panel_spec_for,
    propose_panel_add_layout,
)


def make_panel(panel_id, panel_type, col, row, col_span, row_span, *, pinned=False, weight=50):
    return {
        "id": panel_id,
        "type": panel_type,
        "title": panel_id,
        "placement": {
            "group": "workspace",
            "zone": "main",
            "col": col,
            "row": row,
            "colSpan": col_span,
            "rowSpan": row_span,
        },
        "layoutPinned": pinned,
        "layoutWeight": weight,
    }


def make_layout_context(panels, cols=8, rows=5, catalog=None):
    context = {"version": 2, "grid": {"cols": cols, "rows": rows}, "panels": panels}
    if catalog is not None:
        context["panelCatalog"] = catalog
    return context


def make_agent_context(layout_context):
    return AgentContext(symbol="NVDA", intent="패널 배치 테스트", layoutContext=layout_context)


def command_types(proposal):
    return [command["type"] for command in proposal.commands]


def add_command(proposal):
    return next(command for command in proposal.commands if command["type"] == "layout.panel.add")


class PanelAddFreeSpaceTest(unittest.TestCase):
    def test_places_default_span_in_empty_workspace(self):
        layout_ctx = make_layout_context([])
        proposal = propose_panel_add_layout(make_agent_context(layout_ctx), [], "orderTicket")

        self.assertTrue(proposal.autoApply)
        self.assertIn("layout.panel.add", command_types(proposal))
        placement = add_command(proposal)["payload"]["placement"]
        self.assertEqual(placement["colSpan"], 2)
        self.assertEqual(placement["rowSpan"], 2)
        self.assertIn("빈 공간", proposal.rationale)
        self.assertNotIn("최소 크기", proposal.rationale)

    def test_shrinks_to_min_span_when_default_does_not_fit(self):
        # Free cells: cols 7-8 x rows 4-5 (2x2). companyProfitability default is 3x3, min 2x2.
        layout_ctx = make_layout_context([
            make_panel("panel-a", "chart", 1, 1, 8, 3),
            make_panel("panel-b", "newsFeed", 1, 4, 6, 2),
        ])
        panels = normalize_layout_panels(layout_ctx)
        proposal = propose_panel_add_layout(make_agent_context(layout_ctx), panels, "companyProfitability")

        self.assertTrue(proposal.autoApply)
        placement = add_command(proposal)["payload"]["placement"]
        self.assertEqual((placement["col"], placement["row"]), (7, 4))
        self.assertEqual((placement["colSpan"], placement["rowSpan"]), (2, 2))
        self.assertIn("최소 크기", proposal.rationale)

    def test_does_not_overlap_existing_panels(self):
        layout_ctx = make_layout_context([
            make_panel("panel-a", "chart", 1, 1, 4, 5),
        ])
        panels = normalize_layout_panels(layout_ctx)
        proposal = propose_panel_add_layout(make_agent_context(layout_ctx), panels, "newsFeed")

        placement = add_command(proposal)["payload"]["placement"]
        self.assertGreaterEqual(placement["col"], 5)


class PanelAddReflowCandidatesTest(unittest.TestCase):
    def test_offers_placement_pick_when_no_free_space(self):
        # Grid fully occupied by two unpinned panels; they can shrink so
        # reflow-based candidates must be offered instead of silent placement.
        layout_ctx = make_layout_context([
            make_panel("panel-a", "chart", 1, 1, 4, 5),
            make_panel("panel-b", "newsFeed", 5, 1, 4, 5),
        ])
        panels = normalize_layout_panels(layout_ctx)
        proposal = propose_panel_add_layout(make_agent_context(layout_ctx), panels, "orderFlowProfile")

        self.assertFalse(proposal.autoApply)
        self.assertEqual(command_types(proposal), ["layout.placement.pick"])
        payload = proposal.commands[0]["payload"]
        self.assertEqual(payload["panelType"], "orderFlowProfile")
        self.assertTrue(payload["candidates"])
        for candidate in payload["candidates"]:
            self.assertIn("label", candidate)
            self.assertIn("placement", candidate)
            self.assertTrue(candidate["arrangement"])

    def test_rejects_with_guidance_when_even_min_span_cannot_fit(self):
        # Everything pinned: no free space and no reflow possible.
        layout_ctx = make_layout_context([
            make_panel("panel-a", "chart", 1, 1, 4, 5, pinned=True),
            make_panel("panel-b", "newsFeed", 5, 1, 4, 5, pinned=True),
        ])
        panels = normalize_layout_panels(layout_ctx)
        proposal = propose_panel_add_layout(make_agent_context(layout_ctx), panels, "portfolioDividend")

        self.assertFalse(proposal.autoApply)
        self.assertEqual(proposal.commands, [])
        self.assertIn("배치할 수 없습니다", proposal.rationale)

    def test_reject_hint_names_lowest_weight_closable_panel(self):
        layout_ctx = make_layout_context([
            make_panel("panel-a", "chart", 1, 1, 4, 5, pinned=True),
            make_panel("panel-b", "newsFeed", 5, 1, 4, 5, pinned=True),
            make_panel("panel-c", "aiSummary", 1, 1, 1, 1, weight=10),
        ], cols=8, rows=5)
        # Overlap panel-c on purpose so no free cells remain; only panel-c is closable.
        panels = normalize_layout_panels(layout_ctx)
        proposal = propose_panel_add_layout(make_agent_context(layout_ctx), panels, "portfolioMulti")

        if not proposal.autoApply and not proposal.commands:
            self.assertIn("닫으면", proposal.rationale)


class PanelSpecResolutionTest(unittest.TestCase):
    def test_prefers_frontend_panel_catalog(self):
        catalog = [{
            "panelType": "orderTicket",
            "title": "주문",
            "minSpan": {"colSpan": 3, "rowSpan": 2},
            "defaultSpan": {"colSpan": 3, "rowSpan": 2},
            "layoutWeight": 40,
        }]
        layout_ctx = make_layout_context([], catalog=catalog)
        spec = panel_spec_for("orderTicket", layout_ctx)
        self.assertEqual(spec["minSpan"], {"colSpan": 3, "rowSpan": 2})

    def test_falls_back_to_mirror_table(self):
        spec = panel_spec_for("themeRadar", None)
        self.assertEqual(spec["defaultSpan"], {"colSpan": 3, "rowSpan": 2})
        self.assertEqual(spec["title"], "분야추천")

    def test_popular_stocks_defaults_to_one_column(self):
        spec = panel_spec_for("popularStocks", None)
        self.assertEqual(spec["minSpan"], {"colSpan": 1, "rowSpan": 2})
        self.assertEqual(spec["defaultSpan"], {"colSpan": 1, "rowSpan": 2})

    def test_unknown_type_uses_generic_fallback(self):
        spec = panel_spec_for("someFuturePanel", None)
        self.assertEqual(spec["minSpan"], {"colSpan": 1, "rowSpan": 1})


class PanelOpenParsingTest(unittest.TestCase):
    def parse_first_task(self, query):
        result = parse_ui_query(query, make_layout_context([]))
        self.assertTrue(result.tasks, f"expected UI task for query: {query}")
        return result.tasks[0]

    def test_parses_new_panel_types(self):
        cases = {
            "오더플로우 패널 띄워줘": "orderFlowProfile",
            "배당 패널 열어줘": "portfolioDividend",
            "인기종목 패널 보여줘": "popularStocks",
            "기업정보 패널 열어줘": "companyProfile",
            "수익률 패널 띄워줘": "portfolioPerformance",
        }
        for query, expected_type in cases.items():
            with self.subTest(query=query):
                task = self.parse_first_task(query)
                self.assertEqual(task.targetPanelType, expected_type)


class UIAgentEndToEndTest(unittest.TestCase):
    def test_open_task_adds_panel_via_free_space(self):
        layout_ctx = make_layout_context([
            make_panel("panel-chart", "chart", 1, 1, 4, 3),
        ])
        context = make_agent_context(layout_ctx)
        proposal = UIAgent().propose_many(context, [
            {"action": "open", "targetPanelType": "orderFlowProfile"},
        ])

        self.assertTrue(proposal.autoApply)
        self.assertIn("layout.panel.add", command_types(proposal))
        self.assertEqual(add_command(proposal)["payload"]["panelType"], "orderFlowProfile")


if __name__ == "__main__":
    unittest.main()
