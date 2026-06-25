from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from backend.schemas import (
    ChatLlmResponse,
    ChatRequest,
    ChatResponse,
    ChartProposalDocument,
    CommandValidationError,
)
from backend.settings import Settings


CAPABILITY_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "shared" / "chartCapabilities.json"


def _load_capability_manifest() -> dict[str, Any]:
    return json.loads(CAPABILITY_MANIFEST_PATH.read_text(encoding="utf-8"))


def _manifest_command_types(*, enabled: bool | None = None, llm_enabled: bool | None = None) -> set[str]:
    command_types: set[str] = set()
    for capability in CAPABILITY_MANIFEST.get("capabilities", []):
        if enabled is not None and bool(capability.get("enabled")) is not enabled:
            continue
        if llm_enabled is not None and bool(capability.get("llmEnabled")) is not llm_enabled:
            continue
        command_types.update(str(command_type) for command_type in capability.get("commandTypes", []))
    return command_types


CAPABILITY_MANIFEST = _load_capability_manifest()
ENABLED_COMMAND_TYPES = _manifest_command_types(enabled=True)
LLM_ENABLED_COMMAND_TYPES = _manifest_command_types(enabled=True, llm_enabled=True)
ENABLED_INDICATORS = {"SMA", "EMA", "RSI", "MACD", "BOLLINGER_BANDS", "VWAP", "ATR", "VOLUME_MA"}
ENABLED_SYMBOLS = {"AAPL", "MSFT", "NVDA", "TSLA", "SPY"}
MAX_COMPARISON_SYMBOLS = 3
DEFAULT_INDICATOR_PRESETS: dict[str, dict[str, str | int | float | bool]] = {
    "SMA": {"source": "close", "period": 20},
    "EMA": {"source": "close", "period": 20},
    "RSI": {"source": "close", "period": 14},
    "MACD": {"source": "close", "fastPeriod": 12, "slowPeriod": 26, "signalPeriod": 9},
    "BOLLINGER_BANDS": {"source": "close", "period": 20, "standardDeviation": 2},
    "VWAP": {"reset": "session"},
    "ATR": {"period": 14},
    "VOLUME_MA": {"period": 20},
}
TIMEFRAMES = {"1s", "5s", "15s", "1m", "5m", "15m", "1h", "1d"}
INDICATOR_PREFERRED_PANES = {
    "SMA": "price",
    "EMA": "price",
    "BOLLINGER_BANDS": "price",
    "VWAP": "price",
    "RSI": "indicator",
    "MACD": "indicator",
    "ATR": "indicator",
    "VOLUME_MA": "volume",
}
INDICATOR_LABELS = {
    "SMA": "Simple Moving Average",
    "EMA": "Exponential Moving Average",
    "RSI": "Relative Strength Index",
    "MACD": "Moving Average Convergence Divergence",
    "BOLLINGER_BANDS": "Bollinger Bands",
    "VWAP": "Volume Weighted Average Price",
    "ATR": "Average True Range",
    "VOLUME_MA": "Volume Moving Average",
}


class LlmResponseInvalid(Exception):
    pass


def iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def request_chat_completion(chat_request: ChatRequest, settings: Settings) -> ChatResponse:
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise RuntimeError("The openai package is not installed.") from exc

    schema = _schema_with_no_additional_properties(ChatLlmResponse.model_json_schema())
    client = AsyncOpenAI(api_key=settings.openai_api_key, timeout=settings.openai_timeout_seconds)
    response = await asyncio.wait_for(
        client.responses.create(
            model=settings.openai_model,
            instructions=_build_instructions(chat_request),
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": json.dumps(chat_request.model_dump(by_alias=True), ensure_ascii=False),
                        }
                    ],
                }
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "gops_chart_chat_response",
                    "strict": True,
                    "schema": schema,
                }
            },
        ),
        timeout=settings.openai_timeout_seconds,
    )

    text = _extract_output_text(response)
    if len(text) > 80_000:
        raise LlmResponseInvalid("LLM response exceeded size limits.")
    try:
        data = json.loads(text)
        llm_response = ChatLlmResponse.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise LlmResponseInvalid("LLM response did not match the expected schema.") from exc

    proposals = normalize_chart_proposals(llm_response, chat_request)
    usage = _extract_usage(response)
    return ChatResponse(
        id=f"chat-{uuid.uuid4()}",
        message=llm_response.message,
        insights=llm_response.insights,
        chartProposals=proposals,
        usage=usage,
        model=settings.openai_model,
        createdAt=iso_now(),
    )


def normalize_chart_proposals(llm_response: ChatLlmResponse, chat_request: ChatRequest) -> list[ChartProposalDocument]:
    proposals: list[ChartProposalDocument] = []
    for raw_proposal in llm_response.chartProposals:
        proposal_id = f"proposal-{uuid.uuid4()}"
        commands: list[dict[str, Any]] = []
        errors: list[CommandValidationError] = []
        for index, raw_command_model in enumerate(raw_proposal.commands):
            raw_command = raw_command_model.model_dump(by_alias=True, exclude_none=True)
            normalized, command_errors = _normalize_command(raw_command, proposal_id, chat_request, index)
            if normalized is not None:
                commands.append(normalized)
            errors.extend(command_errors)
        if errors:
            commands = []
        status = "pending" if commands else "invalid"
        target_panel_id = commands[0]["target"]["panelId"] if commands else chat_request.workspace.activePanelId
        target_chart_id = commands[0]["target"]["chartId"] if commands else chat_request.chart.id
        proposals.append(
            ChartProposalDocument(
                id=proposal_id,
                source="llm",
                status=status,  # type: ignore[arg-type]
                targetPanelId=target_panel_id,
                targetChartId=target_chart_id,
                title=raw_proposal.title,
                rationale=raw_proposal.rationale,
                previewSummary=raw_proposal.previewSummary,
                commands=commands,
                createdAt=iso_now(),
                validationErrors=errors,
            )
        )
    return proposals


def _normalize_command(
    raw_command: dict[str, Any], proposal_id: str, chat_request: ChatRequest, index: int
) -> tuple[dict[str, Any] | None, list[CommandValidationError]]:
    errors: list[CommandValidationError] = []
    path = f"chartProposals.commands.{index}"
    command_type = raw_command.get("type")
    if command_type not in ENABLED_COMMAND_TYPES:
        errors.append(_error("unknown_command_type", f"Unsupported command type: {command_type!r}.", f"{path}.type"))
        return None, errors
    if command_type not in LLM_ENABLED_COMMAND_TYPES:
        errors.append(_error("unsafe_ai_command", f"Command type is not exposed to LLM: {command_type}.", f"{path}.type"))
        return None, errors
    if command_type not in set(chat_request.chart.availableCommands):
        errors.append(_error("unknown_command_type", f"Command type is not available: {command_type}.", f"{path}.type"))
        return None, errors
    if str(command_type).startswith("proposal.") or command_type == "panel.pinMode.set":
        errors.append(_error("unsafe_ai_command", "LLM proposals cannot accept/reject proposals or change panel pin mode.", f"{path}.type"))
        return None, errors

    target = raw_command.get("target")
    if not isinstance(target, dict):
        errors.append(_error("missing_target", "Command target is required.", f"{path}.target"))
        return None, errors
    if target.get("chartId") != chat_request.chart.id:
        errors.append(_error("target_not_found", "Command targets a chart outside the active context.", f"{path}.target.chartId"))
    panel = next((panel for panel in chat_request.workspace.panels if panel.id == target.get("panelId")), None)
    if panel is None:
        errors.append(_error("target_not_found", "Command targets a missing panel.", f"{path}.target.panelId"))
    elif panel.pinMode == "locked":
        errors.append(_error("panel_locked", "AI commands cannot mutate locked panels.", f"{path}.target.panelId"))
    if errors:
        return None, errors

    payload = raw_command.get("payload")
    if not isinstance(payload, dict):
        errors.append(_error("invalid_payload", "Command payload must be an object.", f"{path}.payload"))
        return None, errors

    normalized_payload = _normalize_payload(str(command_type), payload, chat_request, target)
    if isinstance(normalized_payload, CommandValidationError):
        return None, [normalized_payload]

    return (
        {
            "id": f"cmd-{uuid.uuid4()}",
            "type": command_type,
            "actor": "ai",
            "status": "proposal",
            "target": {
                "workspaceId": target.get("workspaceId") or "workspace-main",
                "panelId": target["panelId"],
                "chartId": target["chartId"],
                "paneId": target.get("paneId"),
                "layerId": target.get("layerId"),
            },
            "payload": normalized_payload,
            "reason": raw_command.get("reason"),
            "proposalId": proposal_id,
            "createdAt": iso_now(),
        },
        [],
    )


def _normalize_payload(
    command_type: str, payload: dict[str, Any], chat_request: ChatRequest, target: dict[str, Any]
) -> dict[str, Any] | CommandValidationError:
    if command_type == "chart.timeframe.set":
        timeframe = payload.get("timeframe")
        if timeframe not in TIMEFRAMES:
            return _error("invalid_payload", "Invalid timeframe.", "payload.timeframe")
        return {"timeframe": timeframe}

    if command_type == "chart.symbol.set":
        symbol = str(payload.get("symbol") or "").upper()
        if not symbol:
            return _error("invalid_payload", "Symbol is required.", "payload.symbol")
        if symbol not in ENABLED_SYMBOLS:
            return _error("invalid_payload", "Symbol is not in the enabled watchlist.", "payload.symbol")
        return {"symbol": symbol}

    if command_type == "chart.viewport.set":
        clean: dict[str, Any] = {}
        if "mode" in payload and payload["mode"] in {"followRealtime", "fixedRange", "fixedLogicalRange"}:
            clean["mode"] = payload["mode"]
        if "visibleBars" in payload:
            visible_bars = int(payload["visibleBars"])
            if visible_bars < 20 or visible_bars > 1000:
                return _error("invalid_payload", "visibleBars must be between 20 and 1000.", "payload.visibleBars")
            clean["visibleBars"] = visible_bars
        if "rightOffsetBars" in payload:
            right_offset = int(payload["rightOffsetBars"])
            if right_offset < 0:
                return _error("invalid_payload", "rightOffsetBars cannot be negative.", "payload.rightOffsetBars")
            clean["rightOffsetBars"] = right_offset
        if "logicalFrom" in payload:
            clean["logicalFrom"] = float(payload["logicalFrom"])
        if "logicalTo" in payload:
            clean["logicalTo"] = float(payload["logicalTo"])
        if "from" in payload:
            clean["from"] = payload["from"]
        if "to" in payload:
            clean["to"] = payload["to"]
        if not clean:
            return _error("invalid_payload", "Viewport payload must include a supported field.", "payload")
        return clean

    if command_type == "chart.layer.visibility.set":
        layer_id = str(payload.get("layerId") or target.get("layerId") or "")
        if not layer_id:
            return _error("invalid_payload", "Layer id is required.", "payload.layerId")
        if _find_layer(chat_request, layer_id) is None:
            return _error("target_not_found", "Layer was not found.", "payload.layerId")
        return {"layerId": layer_id, "visible": bool(payload.get("visible", True))}

    if command_type == "chart.indicator.add":
        node = payload.get("node") if isinstance(payload.get("node"), dict) else {}
        indicator_type = str(node.get("type") or payload.get("indicatorType") or payload.get("type") or "").upper()
        if indicator_type not in ENABLED_INDICATORS:
            return _error("invalid_payload", "Unsupported indicator type.", "payload.node.type")
        node_id = str(node.get("id") or f"calc-{indicator_type.lower()}-{uuid.uuid4().hex[:8]}")
        output_key = str(node.get("outputKey") or f"{indicator_type.lower()}-{uuid.uuid4().hex[:6]}")
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else payload.get("inputs")
        if not isinstance(inputs, dict):
            inputs = DEFAULT_INDICATOR_PRESETS[indicator_type]
        layer = payload.get("layer") if isinstance(payload.get("layer"), dict) else {}
        pane_info = _pane_for_indicator(indicator_type, chat_request, layer.get("paneId") or target.get("paneId"))
        pane_id = pane_info["paneId"]
        normalized: dict[str, Any] = {
            "node": {"id": node_id, "type": indicator_type, "inputs": inputs, "outputKey": output_key},
            "layer": {
                "id": str(layer.get("id") or f"layer-indicator-{uuid.uuid4().hex[:8]}"),
                "type": "indicator",
                "owner": "ai",
                "paneId": pane_id,
                "zIndex": int(layer.get("zIndex") or 220),
                "visible": bool(layer.get("visible", True)),
                "locked": False,
                "style": layer.get("style") if isinstance(layer.get("style"), dict) else {"color": "#f59e0b", "lineWidth": 2},
                "calculationNodeId": node_id,
                "renderMode": layer.get("renderMode") or _default_indicator_render_mode(indicator_type),
                "createdAt": iso_now(),
                "updatedAt": iso_now(),
            },
        }
        if pane_info.get("pane") is not None:
            normalized["pane"] = pane_info["pane"]
        return normalized

    if command_type == "chart.indicator.update":
        layer_id = str(payload.get("layerId") or target.get("layerId") or "")
        calculation_node_id = str(payload.get("calculationNodeId") or _infer_indicator_node_id(chat_request, layer_id) or "")
        if not calculation_node_id:
            return _error("invalid_payload", "Calculation node id is required.", "payload.calculationNodeId")
        if layer_id:
            layer_context = _find_layer(chat_request, layer_id)
            if layer_context is None:
                return _error("target_not_found", "Indicator layer was not found.", "payload.layerId")
            if layer_context.type != "indicator":
                return _error("layer_type_not_allowed", "Layer is not an indicator.", "payload.layerId")
        inputs = payload.get("inputs")
        if not isinstance(inputs, dict):
            node = payload.get("node") if isinstance(payload.get("node"), dict) else {}
            inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else None
        if not isinstance(inputs, dict) or not inputs:
            return _error("invalid_payload", "Indicator update requires inputs.", "payload.inputs")
        normalized_update: dict[str, Any] = {"calculationNodeId": calculation_node_id, "inputs": inputs}
        layer_patch = payload.get("layerPatch") if isinstance(payload.get("layerPatch"), dict) else payload.get("layer")
        if isinstance(layer_patch, dict):
            clean_patch = _clean_layer_patch(layer_patch)
            if clean_patch:
                normalized_update["layerPatch"] = clean_patch
        return normalized_update

    if command_type == "chart.indicator.remove":
        layer_id = str(payload.get("layerId") or target.get("layerId") or "")
        if not layer_id:
            return _error("invalid_payload", "Layer id is required.", "payload.layerId")
        layer_context = _find_layer(chat_request, layer_id)
        if layer_context is None:
            return _error("target_not_found", "Indicator layer was not found.", "payload.layerId")
        if layer_context.type != "indicator":
            return _error("layer_type_not_allowed", "Layer is not an indicator.", "payload.layerId")
        calculation_node_id = str(payload.get("calculationNodeId") or _infer_indicator_node_id(chat_request, layer_id) or "")
        if not calculation_node_id:
            return _error("invalid_payload", "Calculation node id is required.", "payload.calculationNodeId")
        return {
            "calculationNodeId": calculation_node_id,
            "layerId": layer_id,
        }

    if command_type == "chart.drawing.add":
        layer = payload.get("layer") if isinstance(payload.get("layer"), dict) else {}
        drawing = layer.get("drawing") if isinstance(layer.get("drawing"), dict) else payload.get("drawing")
        if not isinstance(drawing, dict):
            drawing = {
                "kind": "horizontalLine",
                "price": chat_request.market.latestPrice,
                "label": "AI level",
            }
        if drawing.get("kind") == "horizontalLine" and not isinstance(drawing.get("price"), (int, float)):
            drawing["price"] = chat_request.market.latestPrice
        if drawing.get("kind") != "horizontalLine":
            return _error("invalid_payload", "Only horizontalLine drawings are enabled in the MVP.", "payload.layer.drawing.kind")
        pane_id = str(layer.get("paneId") or target.get("paneId") or "pane-price")
        return {
            "layer": {
                "id": str(layer.get("id") or f"layer-drawing-{uuid.uuid4().hex[:8]}"),
                "type": "drawing",
                "owner": "ai",
                "paneId": pane_id,
                "zIndex": int(layer.get("zIndex") or 300),
                "visible": bool(layer.get("visible", True)),
                "locked": False,
                "style": layer.get("style") if isinstance(layer.get("style"), dict) else {"color": "#38bdf8", "lineWidth": 1.5},
                "drawing": drawing,
                "createdAt": iso_now(),
                "updatedAt": iso_now(),
            }
        }

    if command_type == "chart.drawing.update":
        layer_id = str(payload.get("layerId") or target.get("layerId") or "")
        if not layer_id:
            return _error("invalid_payload", "Layer id is required.", "payload.layerId")
        layer_context = _find_layer(chat_request, layer_id)
        if layer_context is None:
            return _error("target_not_found", "Drawing layer was not found.", "payload.layerId")
        if layer_context.type != "drawing":
            return _error("layer_type_not_allowed", "Layer is not a drawing.", "payload.layerId")
        drawing = payload.get("drawing")
        if not isinstance(drawing, dict):
            layer = payload.get("layer") if isinstance(payload.get("layer"), dict) else {}
            drawing = layer.get("drawing") if isinstance(layer.get("drawing"), dict) else None
        if not isinstance(drawing, dict) or drawing.get("kind") != "horizontalLine":
            return _error("invalid_payload", "Drawing update requires a horizontalLine drawing.", "payload.drawing")
        if not isinstance(drawing.get("price"), (int, float)):
            drawing["price"] = chat_request.market.latestPrice
        normalized_drawing: dict[str, Any] = {"layerId": layer_id, "drawing": drawing}
        style = payload.get("style")
        if not isinstance(style, dict):
            layer = payload.get("layer") if isinstance(payload.get("layer"), dict) else {}
            style = layer.get("style") if isinstance(layer.get("style"), dict) else None
        if isinstance(style, dict):
            normalized_drawing["style"] = style
        if "visible" in payload:
            normalized_drawing["visible"] = bool(payload.get("visible"))
        return normalized_drawing

    if command_type == "chart.drawing.remove":
        layer_id = str(payload.get("layerId") or target.get("layerId") or "")
        if not layer_id:
            return _error("invalid_payload", "Layer id is required.", "payload.layerId")
        layer_context = _find_layer(chat_request, layer_id)
        if layer_context is None:
            return _error("target_not_found", "Drawing layer was not found.", "payload.layerId")
        if layer_context.type != "drawing":
            return _error("layer_type_not_allowed", "Layer is not a drawing.", "payload.layerId")
        return {"layerId": layer_id}

    if command_type == "chart.comparison.add":
        layer = payload.get("layer") if isinstance(payload.get("layer"), dict) else {}
        symbol = str(layer.get("symbol") or payload.get("symbol") or "").upper()
        if not symbol:
            return _error("invalid_payload", "Comparison symbol is required.", "payload.symbol")
        if symbol not in ENABLED_SYMBOLS:
            return _error("invalid_payload", "Comparison symbol is not in the enabled watchlist.", "payload.symbol")
        if symbol == chat_request.chart.symbol:
            return _error("invalid_payload", "Comparison symbol must differ from the active chart symbol.", "payload.symbol")
        existing_comparisons = [item for item in chat_request.chart.layers if item.type == "comparisonSeries"]
        if len(existing_comparisons) >= MAX_COMPARISON_SYMBOLS:
            return _error("document_limit_exceeded", "Maximum comparison symbol count reached.", "payload.layer")
        if any(item.summary == f"comparison {symbol}" for item in existing_comparisons):
            return _error("invalid_payload", "Comparison symbol already exists.", "payload.symbol")
        return {
            "layer": {
                "id": str(layer.get("id") or f"layer-comparison-{symbol.lower()}-{uuid.uuid4().hex[:6]}"),
                "type": "comparisonSeries",
                "owner": "ai",
                "paneId": str(layer.get("paneId") or target.get("paneId") or "pane-price"),
                "zIndex": int(layer.get("zIndex") or 180),
                "visible": bool(layer.get("visible", True)),
                "locked": False,
                "style": layer.get("style") if isinstance(layer.get("style"), dict) else {"color": "#a78bfa", "lineWidth": 1.5},
                "symbol": symbol,
                "baselineMode": layer.get("baselineMode") or "firstVisibleCompleteBar",
                "normalization": layer.get("normalization") or "percentFromFirstVisibleCompleteBar",
                "renderMode": layer.get("renderMode") or "line",
                "createdAt": iso_now(),
                "updatedAt": iso_now(),
            }
        }

    if command_type == "chart.comparison.remove":
        layer_id = str(payload.get("layerId") or target.get("layerId") or "")
        if not layer_id:
            return _error("invalid_payload", "Layer id is required.", "payload.layerId")
        layer_context = _find_layer(chat_request, layer_id)
        if layer_context is None:
            return _error("target_not_found", "Comparison layer was not found.", "payload.layerId")
        if layer_context.type != "comparisonSeries":
            return _error("layer_type_not_allowed", "Layer is not a comparison series.", "payload.layerId")
        return {"layerId": layer_id}

    return payload


def _default_indicator_render_mode(indicator_type: str) -> str:
    if indicator_type in {"MACD", "VOLUME_MA"}:
        return "line"
    if indicator_type == "BOLLINGER_BANDS":
        return "band"
    return "line"


def _find_layer(chat_request: ChatRequest, layer_id: str):
    if not layer_id:
        return None
    return next((layer for layer in chat_request.chart.layers if layer.id == layer_id), None)


def _infer_indicator_node_id(chat_request: ChatRequest, layer_id: str) -> str | None:
    layer = _find_layer(chat_request, layer_id)
    if layer is None or layer.type != "indicator":
        return None
    prefix = "indicator node "
    if layer.summary.startswith(prefix):
        return layer.summary[len(prefix) :].strip() or None
    return None


def _pane_for_indicator(indicator_type: str, chat_request: ChatRequest, requested_pane_id: str | None) -> dict[str, Any]:
    preferred = INDICATOR_PREFERRED_PANES.get(indicator_type, "price")
    panes = chat_request.chart.panes
    if preferred == "price":
        pane = next((item for item in panes if item.kind == "price"), None)
        return {"paneId": pane.id if pane else requested_pane_id or "pane-price"}
    if preferred == "volume":
        pane = next((item for item in panes if item.kind == "volume"), None)
        return {"paneId": pane.id if pane else requested_pane_id or "pane-volume"}

    label = INDICATOR_LABELS.get(indicator_type, indicator_type)
    requested = next((item for item in panes if item.id == requested_pane_id and item.kind == "indicator"), None)
    if requested is not None:
        return {"paneId": requested.id}
    existing = next((item for item in panes if item.kind == "indicator" and item.title == label), None)
    if existing is not None:
        return {"paneId": existing.id}

    pane_id = f"pane-indicator-{indicator_type.lower()}"
    if any(item.id == pane_id for item in panes):
        return {"paneId": pane_id}
    return {
        "paneId": pane_id,
        "pane": {
            "id": pane_id,
            "kind": "indicator",
            "title": label,
            "order": len(panes),
            "heightRatio": 0.22,
            "minHeightPx": 120,
            "yScale": {
                "scaleId": f"scale-{pane_id}-right",
                "mode": "oscillator" if indicator_type == "RSI" else "custom",
                "position": "right",
                "autoScale": indicator_type != "RSI",
                "min": 0 if indicator_type == "RSI" else None,
                "max": 100 if indicator_type == "RSI" else None,
            },
            "visible": True,
        },
    }


def _clean_layer_patch(layer_patch: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    if isinstance(layer_patch.get("style"), dict):
        clean["style"] = layer_patch["style"]
    if "renderMode" in layer_patch and layer_patch["renderMode"] in {"line", "histogram", "band", "cloud"}:
        clean["renderMode"] = layer_patch["renderMode"]
    if "visible" in layer_patch:
        clean["visible"] = bool(layer_patch["visible"])
    return clean


def _build_instructions(chat_request: ChatRequest) -> str:
    active_panel = next((panel for panel in chat_request.workspace.panels if panel.id == chat_request.workspace.activePanelId), None)
    pin_mode = active_panel.pinMode if active_panel else "approval"
    available_commands = [command for command in chat_request.chart.availableCommands if command in LLM_ENABLED_COMMAND_TYPES]
    if pin_mode == "locked":
        available_commands = []
    available = ", ".join(available_commands)
    pin_instruction = {
        "locked": "The active chart panel is locked to AI edits: return chartProposals as an empty list.",
        "approval": "The active chart panel allows AI proposals, but the user must explicitly accept them before they are applied.",
        "auto": "The active chart panel allows AI proposals and the frontend will auto-apply valid proposals after validation.",
    }[pin_mode]
    return (
        "You are a concise market chart assistant for a dummy-data MVP. "
        "Return only valid structured JSON matching the provided schema. "
        f"{pin_instruction} "
        f"Use only these command types: {available or '(none)'}. "
        "Never say that a chart change has already been applied. Put suggested chart changes only in chartProposals. "
        "Avoid trading instructions, order placement, account claims, portfolio claims, and raw calculations not present in MarketSummary. "
        "Every chart proposal command must include target.workspaceId='workspace-main', "
        f"target.panelId='{chat_request.workspace.activePanelId}', target.chartId='{chat_request.chart.id}', and a payload object. "
        "Chart editing commands available to you are tool-like commands: "
        "chart.symbol.set uses payload.symbol; chart.timeframe.set uses payload.timeframe; "
        "chart.viewport.set uses payload.mode, visibleBars, rightOffsetBars, logicalFrom, logicalTo; "
        "chart.indicator.add uses payload.node.type and payload.node.inputs; "
        "chart.indicator.update uses payload.layerId or target.layerId plus payload.inputs; "
        "chart.indicator.remove uses payload.layerId or target.layerId; "
        "chart.drawing.add uses payload.drawing or payload.layer.drawing with kind horizontalLine; "
        "chart.drawing.update uses payload.layerId and payload.drawing; "
        "chart.drawing.remove, chart.comparison.remove, and chart.layer.visibility.set use payload.layerId; "
        "chart.comparison.add uses payload.symbol. "
        "For support or resistance levels prefer chart.drawing.add with a horizontalLine drawing. "
        "For oscillator studies such as RSI or MACD, still use chart.indicator.add; the backend will place them in the right pane."
    )


def _schema_with_no_additional_properties(schema: dict[str, Any]) -> dict[str, Any]:
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            value.pop("default", None)
            if value.get("type") == "object" or "properties" in value:
                value["additionalProperties"] = False
                properties = value.get("properties")
                if isinstance(properties, dict):
                    value["required"] = list(properties.keys())
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    copied = json.loads(json.dumps(schema))
    visit(copied)
    return copied


def _extract_output_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    output = getattr(response, "output", None)
    if isinstance(output, list):
        chunks: list[str] = []
        for item in output:
            content = getattr(item, "content", None) or item.get("content", []) if isinstance(item, dict) else []
            for piece in content:
                if isinstance(piece, dict) and piece.get("type") in {"output_text", "text"}:
                    chunks.append(str(piece.get("text", "")))
                elif hasattr(piece, "text"):
                    chunks.append(str(piece.text))
        if chunks:
            return "".join(chunks)
    raise LlmResponseInvalid("OpenAI response did not include output text.")


def _extract_usage(response: Any) -> dict[str, int | None] | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    if isinstance(usage, dict):
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
    return {"inputTokens": input_tokens, "outputTokens": output_tokens}


def _error(code: str, message: str, path: str | None = None) -> CommandValidationError:
    return CommandValidationError(code=code, message=message, path=path)  # type: ignore[arg-type]
