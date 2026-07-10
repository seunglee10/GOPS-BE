import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
AGENT_SHARED = ROOT / "systems" / "agent-orchestration" / "shared"
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"
for path in (str(AGENT_SHARED), str(MARKET_SHARED), str(BACKEND), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.contracts.chart import (  # noqa: E402
    AgentChatMessage,
    AgentChatRequest,
    chart_command_payload_schema,
    filled_command_payload,
)


NEW_DRAWING_TYPES = {
    "horizontalParallelLines",
    "trendParallelLines",
    "verticalParallelLines",
    "flagMarker",
    "riskRewardBox",
    "fibonacciRetracement",
}
CHART_INTERVALS = ["1m", "5m", "10m", "1h", "4h", "1D", "1W", "1M"]


class ChartCommandContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        schema_path = ROOT / "shared" / "chart-contract" / "chart-command.schema.json"
        cls.shared_schema = json.loads(schema_path.read_text(encoding="utf-8"))

    def test_shared_schema_preserves_parallel_and_flag_drawing_fields(self):
        payload_schema = self.shared_schema["properties"]["payload"]
        payload = payload_schema["properties"]
        drawing = self.shared_schema["$defs"]["drawingEntity"]["properties"]
        anchor = self.shared_schema["$defs"]["drawingAnchor"]["properties"]
        style = self.shared_schema["$defs"]["drawingStyle"]["properties"]

        self.assertTrue(NEW_DRAWING_TYPES.issubset(payload["drawingType"]["enum"]))
        self.assertTrue(NEW_DRAWING_TYPES.issubset(drawing["type"]["enum"]))
        self.assertNotIn("pointMarker", payload["drawingType"]["enum"])
        self.assertNotIn("pointMarker", drawing["type"]["enum"])
        self.assertEqual(payload["sourceInterval"]["enum"], CHART_INTERVALS)
        self.assertEqual(drawing["sourceInterval"]["enum"], CHART_INTERVALS)
        self.assertEqual(anchor["interval"]["enum"], CHART_INTERVALS)
        self.assertTrue({"colorToken", "fillToken", "textToken"}.issubset(style))
        self.assertEqual(
            (payload["parallelLineCount"]["minimum"], payload["parallelLineCount"]["maximum"]),
            (2, 10),
        )
        self.assertEqual(
            (drawing["parallelLineCount"]["minimum"], drawing["parallelLineCount"]["maximum"]),
            (2, 10),
        )
        self.assertEqual((style["fillOpacity"]["minimum"], style["fillOpacity"]["maximum"]), (0, 1))
        self.assertEqual(payload_schema["allOf"][0]["then"]["properties"]["anchors"], {"minItems": 3, "maxItems": 3})
        self.assertEqual(payload_schema["allOf"][1]["then"]["properties"]["anchors"], {"minItems": 2, "maxItems": 2})

    def test_backend_structured_output_schema_exposes_the_same_fields(self):
        schema = chart_command_payload_schema(["AAPL", "NVDA"])
        properties = schema["properties"]
        anchor = properties["anchors"]["items"]
        style = properties["style"]

        self.assertTrue(NEW_DRAWING_TYPES.issubset(properties["drawingType"]["enum"]))
        self.assertNotIn("pointMarker", properties["drawingType"]["enum"])
        self.assertTrue({"sourceInterval", "parallelLineCount"}.issubset(schema["required"]))
        self.assertIn("interval", anchor["required"])
        self.assertIn("fillOpacity", style["required"])
        self.assertTrue(
            {
                "colorToken",
                "fillToken",
                "textToken",
                "fontSize",
                "opacity",
                "extension",
            }.issubset(style["required"])
        )
        self.assertEqual(properties["sourceInterval"]["enum"], [*CHART_INTERVALS, None])
        self.assertEqual(anchor["properties"]["interval"]["enum"], [*CHART_INTERVALS, None])
        self.assertEqual(
            (properties["parallelLineCount"]["minimum"], properties["parallelLineCount"]["maximum"]),
            (2, 10),
        )
        self.assertEqual(
            (style["properties"]["fillOpacity"]["minimum"], style["properties"]["fillOpacity"]["maximum"]),
            (0, 1),
        )
        self.assertEqual(
            (style["properties"]["opacity"]["minimum"], style["properties"]["opacity"]["maximum"]),
            (0, 1),
        )
        self.assertEqual(style["properties"]["extension"]["enum"], ["segment", "ray", "line", None])
        self.assertEqual((properties["anchors"]["minItems"], properties["anchors"]["maxItems"]), (1, 3))
        self.assertIn("riskRewardBox requires exactly", properties["anchors"]["description"])
        empty_payload = filled_command_payload()
        self.assertIsNone(empty_payload["sourceInterval"])
        self.assertIsNone(empty_payload["parallelLineCount"])

    def test_agent_chat_request_round_trips_complete_drawing_entity(self):
        drawing = {
            "id": "drawing-trend-parallel-contract",
            "type": "trendParallelLines",
            "anchors": [
                {
                    "timestamp": "2026-07-08T13:30:00.000Z",
                    "price": 150.0,
                    "paneId": "price",
                    "symbol": "NVDA",
                    "logicalIndex": 0,
                    "interval": "1m",
                },
                {
                    "timestamp": "2026-07-08T13:31:00.000Z",
                    "price": 151.0,
                    "paneId": "price",
                    "symbol": "NVDA",
                    "logicalIndex": 1,
                    "interval": "1m",
                },
                {
                    "timestamp": "2026-07-08T13:30:00.000Z",
                    "price": 149.0,
                    "paneId": "price",
                    "symbol": "NVDA",
                    "logicalIndex": 0,
                    "interval": "1m",
                },
            ],
            "sourceInterval": "1m",
            "style": {"fillOpacity": 0.04, "lineWidth": 1.0},
            "label": "Trend channel",
            "parallelLineCount": 7,
            "visible": True,
            "createdBy": "llm",
        }
        request = AgentChatRequest(
            agentIds=["agent-01"],
            messages=[AgentChatMessage(role="user", content="추세 평행선을 그려줘")],
            context={"panel": {"drawings": [drawing]}},
        )

        restored = AgentChatRequest.model_validate_json(request.model_dump_json())

        self.assertEqual(restored.model_dump()["context"]["panel"]["drawings"][0], drawing)


if __name__ == "__main__":
    unittest.main()
