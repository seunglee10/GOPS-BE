import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))

from gops_agents.intent_understanding.ui_parser import parse_ui_query
from gops_agents.roles import AgentContext, UIAgent, normalize_layout_panels


def panel(panel_id, panel_type, col, row, col_span, row_span, *, pinned=False, min_span=None, max_span=None):
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
        "layoutWeight": 50,
        "minSpan": min_span or {"colSpan": 1, "rowSpan": 1},
        "maxSpan": max_span or {"colSpan": 8, "rowSpan": 6},
    }


def layout(panels=None, **extra):
    return {"version": 2, "grid": {"cols": 8, "rows": 6}, "panels": panels or [], **extra}


def parse_task(query, context=None):
    result = parse_ui_query(query, context or layout())
    assert result.tasks, (query, result.warnings)
    return result.tasks[0]


def propose(query, context):
    task = parse_task(query, context)
    agent_context = AgentContext(symbol="NVDA", intent=query, layoutContext=context)
    return UIAgent().propose_many(agent_context, [task])


class PanelCommandParserTest(unittest.TestCase):
    def test_max_alias_family(self):
        for phrase in ("꽉 차게", "꽉차게", "화면 꽉", "꽉 채워", "꽉채워", "화면 가득", "풀스크린"):
            with self.subTest(phrase=phrase):
                task = parse_task(f"뉴스 패널 {phrase} 해줘")
                self.assertEqual(task.action, "resize")
                self.assertEqual(task.sizeIntent, "max")

    def test_targetless_commands(self):
        self.assertEqual(parse_task("빈틈 없게 정리해줘").action, "tidy")
        self.assertEqual(parse_task("되돌려줘").action, "undo")
        self.assertEqual(parse_task("기본 레이아웃으로 돌려줘").action, "reset")
        self.assertEqual(parse_task("지금 배치 저장해줘").action, "save")

    def test_original_tie_break_prefers_undo_when_available(self):
        self.assertEqual(parse_task("원래대로 해줘", layout(canUndo=True)).action, "undo")
        self.assertEqual(parse_task("원래대로 해줘", layout(canUndo=False)).action, "reset")

    def test_bulk_commands(self):
        close = parse_task("전체 패널 없애줘")
        self.assertEqual(close.action, "close")
        self.assertTrue(close.targetAll)
        resize = parse_task("전부 작게 해줘")
        self.assertEqual(resize.action, "resize")
        self.assertTrue(resize.targetAll)
        self.assertEqual(resize.sizeIntent, "small")

    def test_fraction_swap_relative_replace_pin_and_groups(self):
        context = layout([
            panel("chart-a", "chart", 1, 1, 4, 3),
            panel("news-a", "newsFeed", 5, 1, 2, 2),
        ], selectedPanelId="news-a")
        self.assertEqual(parse_task("뉴스 패널 반으로 줄여줘", context).sizeFraction, 0.5)
        swap = parse_task("차트랑 뉴스 위치 바꿔줘", context)
        self.assertEqual(swap.action, "swap")
        self.assertEqual(set(swap.targetPanelTypes), {"chart", "newsFeed"})
        relative = parse_task("뉴스를 차트 아래에 붙여줘", context)
        self.assertEqual(relative.anchorPanelType, "chart")
        self.assertEqual(relative.relationIntent, "below")
        replace = parse_task("뉴스 대신 오더플로우 띄워줘", context)
        self.assertEqual(replace.action, "replace")
        self.assertEqual(replace.replacePanelType, "newsFeed")
        self.assertEqual(replace.targetPanelType, "orderFlowProfile")
        self.assertEqual(parse_task("차트 고정해줘", context).action, "pin")
        self.assertEqual(parse_task("차트 고정 풀어줘", context).action, "unpin")
        group = parse_task("기업 분석 패널들 다 보여줘", context)
        self.assertEqual(group.action, "open")
        self.assertEqual(len(group.targetPanelTypes), 4)

    def test_deictic_reference(self):
        context = layout([panel("news-a", "newsFeed", 1, 1, 2, 2)], selectedPanelId="news-a")
        task = parse_task("이거 닫아줘", context)
        self.assertEqual(task.targetPanelId, "news-a")
        missing = parse_ui_query("이거 닫아줘", layout())
        self.assertFalse(missing.tasks)
        self.assertIn("ui_selected_panel_required", missing.warnings)


class PanelCommandProposalTest(unittest.TestCase):
    def test_relative_growth_and_noop(self):
        context = layout([panel("news-a", "newsFeed", 1, 1, 4, 3)])
        proposal = propose("뉴스 패널 더 크게 보여줘", context)
        arrangement = next(command for command in proposal.commands if command["type"] == "layout.panels.arrange")
        target = next(item for item in arrangement["payload"]["placements"] if item["panelId"] == "news-a")
        self.assertGreater(target["placement"]["colSpan"] * target["placement"]["rowSpan"], 12)

        maximized = propose("뉴스 패널 화면 꽉 차게 해줘", context)
        max_arrangement = next(command for command in maximized.commands if command["type"] == "layout.panels.arrange")
        max_target = next(item for item in max_arrangement["payload"]["placements"] if item["panelId"] == "news-a")
        self.assertEqual((max_target["placement"]["colSpan"], max_target["placement"]["rowSpan"]), (8, 6))

        maximum = layout([panel("news-a", "newsFeed", 1, 1, 8, 6)])
        noop = propose("뉴스 패널 더 크게 보여줘", maximum)
        self.assertFalse(noop.autoApply)
        self.assertIn("이미 최대", noop.rationale)

    def test_tidy_close_all_and_workspace_commands(self):
        context = layout([
            panel("chart-a", "chart", 4, 3, 2, 2),
            panel("news-a", "newsFeed", 7, 5, 2, 2, pinned=True),
        ])
        tidy = propose("깔끔하게 배치해줘", context)
        self.assertIn("layout.panels.arrange", [command["type"] for command in tidy.commands])
        closed = propose("전체 패널 없애줘", context)
        removed = [command for command in closed.commands if command["type"] == "layout.panel.remove"]
        self.assertEqual([command["payload"]["panelId"] for command in removed], ["chart-a"])
        self.assertIn("고정된", closed.rationale)
        self.assertEqual(propose("되돌려줘", context).commands[0]["type"], "layout.undo")
        self.assertEqual(propose("초기화해줘", context).commands[0]["type"], "layout.default.restore")
        self.assertEqual(propose("지금 배치 저장해줘", context).commands[0]["type"], "layout.save")

    def test_pin_replace_swap_and_fraction(self):
        context = layout([
            panel("chart-a", "chart", 1, 1, 4, 3),
            panel("news-a", "newsFeed", 5, 1, 2, 2),
        ], selectedPanelId="news-a")
        self.assertEqual(propose("차트 고정해줘", context).commands[0]["type"], "layout.panel.pin")
        self.assertEqual(propose("뉴스 대신 오더플로우 띄워줘", context).commands[0]["type"], "layout.panel.replace")
        self.assertIn("layout.panels.arrange", [command["type"] for command in propose("차트랑 뉴스 위치 바꿔줘", context).commands])
        self.assertIn("layout.panels.arrange", [command["type"] for command in propose("뉴스 패널 반으로 줄여줘", context).commands])


if __name__ == "__main__":
    unittest.main()
