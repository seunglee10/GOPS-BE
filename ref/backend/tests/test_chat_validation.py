from fastapi.testclient import TestClient

from backend.llm_client import _build_instructions, _schema_with_no_additional_properties, normalize_chart_proposals
from backend.main import app
from backend.schemas import ChatLlmResponse, ChatRequest
import backend.settings as settings_module
from backend.settings import get_settings


def test_chat_returns_503_without_openai_key(monkeypatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setitem(settings_module.DOTENV_VALUES, "OPENAI_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    client = TestClient(app)
    response = client.post(
        "/api/chat",
        json={
            "message": "Analyze AAPL",
            "workspace": {
                "activePanelId": "panel-chart-main",
                "activeChartId": "chart-main",
                "panels": [],
                "pendingProposalCount": 0,
            },
            "chart": {
                "id": "chart-main",
                "symbol": "AAPL",
                "timeframe": "1m",
                "viewport": {"mode": "followRealtime", "visibleBars": 180, "rightOffsetBars": 0},
                "panes": [],
                "layers": [],
                "availableCommands": ["chart.indicator.add"],
            },
            "market": {
                "symbol": "AAPL",
                "timeframe": "1m",
                "latestPrice": 100,
                "latestTimestamp": "2026-01-01T00:00:00.000Z",
                "changePercentFromFirstVisible": 0,
                "visibleHigh": 101,
                "visibleLow": 99,
                "visibleVolume": 1000,
                "averageVolume": 1000,
                "realizedVolatility": 0,
                "trend": "sideways",
                "notableSignals": [],
            },
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "openai_api_key_missing"
    get_settings.cache_clear()


def test_normalize_valid_llm_indicator_proposal() -> None:
    request = _chat_request()
    llm_response = ChatLlmResponse.model_validate(
        {
            "message": "A moving average could help frame the visible trend.",
            "insights": [{"title": "Trend", "description": "AAPL is drifting higher.", "severity": "info"}],
            "chartProposals": [
                {
                    "title": "Add SMA",
                    "rationale": "A 20 period SMA frames trend.",
                    "previewSummary": "Adds an SMA overlay.",
                    "commands": [
                        {
                            "type": "chart.indicator.add",
                            "target": {
                                "workspaceId": "workspace-main",
                                "panelId": "panel-chart-main",
                                "chartId": "chart-main",
                                "paneId": "pane-price",
                            },
                            "payload": {
                                "node": {
                                    "type": "SMA",
                                    "inputs": {"source": "close", "period": 20},
                                }
                            },
                        }
                    ],
                }
            ],
        }
    )

    proposals = normalize_chart_proposals(llm_response, request)

    assert proposals[0].status == "pending"
    assert proposals[0].commands[0]["actor"] == "ai"
    assert proposals[0].commands[0]["payload"]["node"]["type"] == "SMA"
    assert proposals[0].validationErrors == []


def test_normalize_rsi_indicator_add_creates_indicator_pane_payload() -> None:
    request = _chat_request()
    llm_response = ChatLlmResponse.model_validate(
        {
            "message": "RSI can help show momentum extremes.",
            "insights": [],
            "chartProposals": [
                {
                    "title": "Add RSI",
                    "rationale": "RSI belongs in an oscillator pane.",
                    "previewSummary": "Adds an RSI pane.",
                    "commands": [
                        {
                            "type": "chart.indicator.add",
                            "target": {
                                "workspaceId": "workspace-main",
                                "panelId": "panel-chart-main",
                                "chartId": "chart-main",
                                "paneId": "pane-price",
                            },
                            "payload": {
                                "node": {
                                    "type": "RSI",
                                    "inputs": {"source": "close", "period": 14},
                                }
                            },
                        }
                    ],
                }
            ],
        }
    )

    proposals = normalize_chart_proposals(llm_response, request)

    payload = proposals[0].commands[0]["payload"]
    assert proposals[0].status == "pending"
    assert payload["layer"]["paneId"] == "pane-indicator-rsi"
    assert payload["pane"]["kind"] == "indicator"
    assert payload["pane"]["yScale"]["mode"] == "oscillator"


def test_normalize_llm_update_and_remove_commands_match_chart_edit_payloads() -> None:
    request = _chat_request_with_layers()
    llm_response = ChatLlmResponse.model_validate(
        {
            "message": "I can adjust existing chart edits.",
            "insights": [],
            "chartProposals": [
                {
                    "title": "Edit existing overlays",
                    "rationale": "Uses the same command tools as the UI.",
                    "previewSummary": "Updates RSI, a line, visibility, and removals.",
                    "commands": [
                        {
                            "type": "chart.indicator.update",
                            "target": {
                                "workspaceId": "workspace-main",
                                "panelId": "panel-chart-main",
                                "chartId": "chart-main",
                                "paneId": "pane-indicator-rsi",
                                "layerId": "layer-rsi",
                            },
                            "payload": {"layerId": "layer-rsi", "inputs": {"period": 21}},
                        },
                        {
                            "type": "chart.indicator.remove",
                            "target": {
                                "workspaceId": "workspace-main",
                                "panelId": "panel-chart-main",
                                "chartId": "chart-main",
                                "paneId": "pane-indicator-rsi",
                                "layerId": "layer-rsi",
                            },
                            "payload": {"layerId": "layer-rsi"},
                        },
                        {
                            "type": "chart.drawing.update",
                            "target": {
                                "workspaceId": "workspace-main",
                                "panelId": "panel-chart-main",
                                "chartId": "chart-main",
                                "paneId": "pane-price",
                                "layerId": "layer-level",
                            },
                            "payload": {
                                "layerId": "layer-level",
                                "drawing": {"kind": "horizontalLine", "price": 105, "label": "AI adjusted level"},
                                "style": {"color": "#38bdf8", "lineWidth": 2},
                            },
                        },
                        {
                            "type": "chart.drawing.remove",
                            "target": {
                                "workspaceId": "workspace-main",
                                "panelId": "panel-chart-main",
                                "chartId": "chart-main",
                                "paneId": "pane-price",
                                "layerId": "layer-level",
                            },
                            "payload": {"layerId": "layer-level"},
                        },
                        {
                            "type": "chart.comparison.remove",
                            "target": {
                                "workspaceId": "workspace-main",
                                "panelId": "panel-chart-main",
                                "chartId": "chart-main",
                                "paneId": "pane-price",
                                "layerId": "layer-msft",
                            },
                            "payload": {"layerId": "layer-msft"},
                        },
                        {
                            "type": "chart.layer.visibility.set",
                            "target": {
                                "workspaceId": "workspace-main",
                                "panelId": "panel-chart-main",
                                "chartId": "chart-main",
                                "paneId": "pane-price",
                                "layerId": "layer-price-candles",
                            },
                            "payload": {"layerId": "layer-price-candles", "visible": False},
                        },
                    ],
                }
            ],
        }
    )

    proposals = normalize_chart_proposals(llm_response, request)

    commands = proposals[0].commands
    assert proposals[0].status == "pending"
    assert commands[0]["payload"] == {"calculationNodeId": "calc-rsi", "inputs": {"period": 21}}
    assert commands[1]["payload"] == {"calculationNodeId": "calc-rsi", "layerId": "layer-rsi"}
    assert commands[2]["payload"]["drawing"]["price"] == 105
    assert commands[3]["payload"] == {"layerId": "layer-level"}
    assert commands[4]["payload"] == {"layerId": "layer-msft"}
    assert commands[5]["payload"] == {"layerId": "layer-price-candles", "visible": False}


def test_normalize_symbol_timeframe_and_viewport_commands() -> None:
    request = _chat_request_with_layers()
    llm_response = ChatLlmResponse.model_validate(
        {
            "message": "I can reconfigure the chart surface.",
            "insights": [],
            "chartProposals": [
                {
                    "title": "Change chart setup",
                    "rationale": "Uses chart setup commands.",
                    "previewSummary": "Changes symbol, timeframe, and visible range.",
                    "commands": [
                        {
                            "type": "chart.symbol.set",
                            "target": {
                                "workspaceId": "workspace-main",
                                "panelId": "panel-chart-main",
                                "chartId": "chart-main",
                                "paneId": "pane-price",
                            },
                            "payload": {"symbol": "TSLA"},
                        },
                        {
                            "type": "chart.timeframe.set",
                            "target": {
                                "workspaceId": "workspace-main",
                                "panelId": "panel-chart-main",
                                "chartId": "chart-main",
                                "paneId": "pane-price",
                            },
                            "payload": {"timeframe": "5m"},
                        },
                        {
                            "type": "chart.viewport.set",
                            "target": {
                                "workspaceId": "workspace-main",
                                "panelId": "panel-chart-main",
                                "chartId": "chart-main",
                                "paneId": "pane-price",
                            },
                            "payload": {
                                "mode": "fixedLogicalRange",
                                "visibleBars": 90,
                                "rightOffsetBars": 2,
                                "logicalFrom": 100,
                                "logicalTo": 190,
                            },
                        },
                    ],
                }
            ],
        }
    )

    proposals = normalize_chart_proposals(llm_response, request)

    commands = proposals[0].commands
    assert proposals[0].status == "pending"
    assert commands[0]["payload"] == {"symbol": "TSLA"}
    assert commands[1]["payload"] == {"timeframe": "5m"}
    assert commands[2]["payload"] == {
        "mode": "fixedLogicalRange",
        "visibleBars": 90,
        "rightOffsetBars": 2,
        "logicalFrom": 100.0,
        "logicalTo": 190.0,
    }


def test_normalize_rejects_llm_commands_for_locked_panels() -> None:
    request = _chat_request()
    request = request.model_copy(
        update={
            "workspace": request.workspace.model_copy(
                update={
                    "panels": [
                        request.workspace.panels[0].model_copy(update={"pinMode": "locked"}),
                    ]
                }
            )
        }
    )
    llm_response = ChatLlmResponse.model_validate(
        {
            "message": "This locked panel cannot be edited by AI.",
            "insights": [],
            "chartProposals": [
                {
                    "title": "Add level",
                    "rationale": "Should be blocked.",
                    "previewSummary": "Attempts a chart edit.",
                    "commands": [
                        {
                            "type": "chart.drawing.add",
                            "target": {
                                "workspaceId": "workspace-main",
                                "panelId": "panel-chart-main",
                                "chartId": "chart-main",
                                "paneId": "pane-price",
                            },
                            "payload": {"drawing": {"kind": "horizontalLine", "price": 100, "label": "Locked"}},
                        }
                    ],
                }
            ],
        }
    )

    proposals = normalize_chart_proposals(llm_response, request)

    assert proposals[0].status == "invalid"
    assert proposals[0].validationErrors[0].code == "panel_locked"


def test_normalize_rejects_unknown_llm_command() -> None:
    request = _chat_request()
    llm_response = ChatLlmResponse.model_validate(
        {
            "message": "No changes were applied.",
            "insights": [],
            "chartProposals": [
                {
                    "title": "Unsafe",
                    "rationale": "Unsupported.",
                    "previewSummary": "Attempts an unsupported command.",
                    "commands": [
                        {
                            "type": "order.place",
                            "target": {
                                "workspaceId": "workspace-main",
                                "panelId": "panel-chart-main",
                                "chartId": "chart-main",
                            },
                            "payload": {},
                        }
                    ],
                }
            ],
        }
    )

    proposals = normalize_chart_proposals(llm_response, request)

    assert proposals[0].status == "invalid"
    assert proposals[0].validationErrors[0].code == "unknown_command_type"


def test_normalize_rejects_manifest_disabled_llm_command() -> None:
    request = _chat_request()
    request = request.model_copy(
        update={
            "chart": request.chart.model_copy(
                update={"availableCommands": [*request.chart.availableCommands, "chart.drawing.trendLine.add"]}
            )
        }
    )
    llm_response = ChatLlmResponse.model_validate(
        {
            "message": "No changes were applied.",
            "insights": [],
            "chartProposals": [
                {
                    "title": "Trend line",
                    "rationale": "Future drawing commands are disabled in the MVP.",
                    "previewSummary": "Attempts to add a trend line.",
                    "commands": [
                        {
                            "type": "chart.drawing.trendLine.add",
                            "target": {
                                "workspaceId": "workspace-main",
                                "panelId": "panel-chart-main",
                                "chartId": "chart-main",
                                "paneId": "pane-price",
                            },
                            "payload": {
                                "drawing": {
                                    "kind": "trendLine",
                                    "start": {"timestamp": "2026-01-01T00:00:00.000Z", "price": 99},
                                    "end": {"timestamp": "2026-01-01T00:05:00.000Z", "price": 105},
                                }
                            },
                        }
                    ],
                }
            ],
        }
    )

    proposals = normalize_chart_proposals(llm_response, request)

    assert proposals[0].status == "invalid"
    assert proposals[0].validationErrors[0].code == "unknown_command_type"


def test_normalize_rejects_manifest_llm_hidden_command() -> None:
    request = _chat_request()
    request = request.model_copy(
        update={
            "chart": request.chart.model_copy(
                update={"availableCommands": [*request.chart.availableCommands, "panel.pinMode.set"]}
            )
        }
    )
    llm_response = ChatLlmResponse.model_validate(
        {
            "message": "No changes were applied.",
            "insights": [],
            "chartProposals": [
                {
                    "title": "Change pin mode",
                    "rationale": "LLM cannot change user policy controls.",
                    "previewSummary": "Attempts to change pin mode.",
                    "commands": [
                        {
                            "type": "panel.pinMode.set",
                            "target": {
                                "workspaceId": "workspace-main",
                                "panelId": "panel-chart-main",
                                "chartId": "chart-main",
                            },
                            "payload": {},
                        }
                    ],
                }
            ],
        }
    )

    proposals = normalize_chart_proposals(llm_response, request)

    assert proposals[0].status == "invalid"
    assert proposals[0].validationErrors[0].code == "unsafe_ai_command"


def test_normalize_rejects_active_symbol_comparison() -> None:
    request = _chat_request()
    request = request.model_copy(
        update={
            "chart": request.chart.model_copy(
                update={"availableCommands": [*request.chart.availableCommands, "chart.comparison.add"]}
            )
        }
    )
    llm_response = ChatLlmResponse.model_validate(
        {
            "message": "No changes were applied.",
            "insights": [],
            "chartProposals": [
                {
                    "title": "Compare active symbol",
                    "rationale": "This should be rejected.",
                    "previewSummary": "Attempts to compare AAPL with itself.",
                    "commands": [
                        {
                            "type": "chart.comparison.add",
                            "target": {
                                "workspaceId": "workspace-main",
                                "panelId": "panel-chart-main",
                                "chartId": "chart-main",
                                "paneId": "pane-price",
                            },
                            "payload": {"symbol": "AAPL"},
                        }
                    ],
                }
            ],
        }
    )

    proposals = normalize_chart_proposals(llm_response, request)

    assert proposals[0].status == "invalid"
    assert proposals[0].validationErrors[0].code == "invalid_payload"


def test_auto_pin_mode_instruction_mentions_auto_apply_policy() -> None:
    request = _chat_request()
    request = request.model_copy(
        update={
            "workspace": request.workspace.model_copy(
                update={
                    "panels": [
                        request.workspace.panels[0].model_copy(update={"pinMode": "auto"}),
                    ]
                }
            )
        }
    )

    instructions = _build_instructions(request)

    assert "auto-apply valid proposals" in instructions
    assert "Use only these command types" in instructions


def test_openai_strict_schema_marks_all_properties_required() -> None:
    schema = _schema_with_no_additional_properties(ChatLlmResponse.model_json_schema())

    assert schema["required"] == ["message", "insights", "chartProposals"]
    defs = schema["$defs"]
    for definition in defs.values():
        properties = definition.get("properties")
        if properties:
            assert definition["additionalProperties"] is False
            assert definition["required"] == list(properties.keys())
            assert "default" not in json_dumps(definition)


def _chat_request() -> ChatRequest:
    return ChatRequest.model_validate(
        {
            "message": "Analyze AAPL",
            "workspace": {
                "activePanelId": "panel-chart-main",
                "activeChartId": "chart-main",
                "panels": [
                    {
                        "id": "panel-chart-main",
                        "type": "chart",
                        "title": "Chart",
                        "pinMode": "approval",
                        "targetChartId": "chart-main",
                    }
                ],
                "pendingProposalCount": 0,
            },
            "chart": {
                "id": "chart-main",
                "symbol": "AAPL",
                "timeframe": "1m",
                "viewport": {"mode": "followRealtime", "visibleBars": 180, "rightOffsetBars": 0},
                "panes": [{"id": "pane-price", "kind": "price", "title": "Price"}],
                "layers": [],
                "availableCommands": ["chart.indicator.add", "chart.drawing.add", "chart.viewport.set"],
            },
            "market": {
                "symbol": "AAPL",
                "timeframe": "1m",
                "latestPrice": 100,
                "latestTimestamp": "2026-01-01T00:00:00.000Z",
                "changePercentFromFirstVisible": 0,
                "visibleHigh": 101,
                "visibleLow": 99,
                "visibleVolume": 1000,
                "averageVolume": 1000,
                "realizedVolatility": 0,
                "trend": "sideways",
                "notableSignals": [],
            },
        }
    )


def _chat_request_with_layers() -> ChatRequest:
    request = _chat_request()
    data = request.model_dump(by_alias=True)
    data["chart"]["panes"] = [
        {"id": "pane-price", "kind": "price", "title": "Price"},
        {"id": "pane-volume", "kind": "volume", "title": "Volume"},
        {"id": "pane-indicator-rsi", "kind": "indicator", "title": "Relative Strength Index"},
    ]
    data["chart"]["layers"] = [
        {
            "id": "layer-price-candles",
            "type": "priceSeries",
            "paneId": "pane-price",
            "owner": "system",
            "visible": True,
            "summary": "candlestick price series",
        },
        {
            "id": "layer-rsi",
            "type": "indicator",
            "paneId": "pane-indicator-rsi",
            "owner": "ai",
            "visible": True,
            "summary": "indicator node calc-rsi",
        },
        {
            "id": "layer-level",
            "type": "drawing",
            "paneId": "pane-price",
            "owner": "ai",
            "visible": True,
            "summary": "horizontalLine drawing",
        },
        {
            "id": "layer-msft",
            "type": "comparisonSeries",
            "paneId": "pane-price",
            "owner": "ai",
            "visible": True,
            "summary": "comparison MSFT",
        },
    ]
    data["chart"]["availableCommands"] = [
        "chart.symbol.set",
        "chart.timeframe.set",
        "chart.viewport.set",
        "chart.indicator.add",
        "chart.indicator.update",
        "chart.indicator.remove",
        "chart.drawing.add",
        "chart.drawing.update",
        "chart.drawing.remove",
        "chart.layer.visibility.set",
        "chart.comparison.add",
        "chart.comparison.remove",
    ]
    return ChatRequest.model_validate(data)


def json_dumps(value) -> str:
    import json

    return json.dumps(value)
