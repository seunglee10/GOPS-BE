from typing import Any


def chart_command_payload_schema(supported_symbols: list[str]) -> dict[str, Any]:
    style_schema = {
        "type": ["object", "null"],
        "additionalProperties": False,
        "required": ["color", "fillColor", "lineWidth", "textColor", "lineDash"],
        "properties": {
            "color": {"type": ["string", "null"]},
            "fillColor": {"type": ["string", "null"]},
            "lineWidth": {"type": ["number", "null"]},
            "textColor": {"type": ["string", "null"]},
            "lineDash": {"type": ["array", "null"], "items": {"type": "number"}},
        },
    }
    comparison_base_schema = {
        "type": ["object", "null"],
        "additionalProperties": False,
        "required": ["mode", "timestamp"],
        "properties": {
            "mode": {"type": ["string", "null"], "enum": ["visibleRangeStart", "timestamp", None]},
            "timestamp": {"type": ["string", "null"]},
        },
    }
    comparison_schema = {
        "type": ["object", "null"],
        "additionalProperties": False,
        "required": ["id", "symbol", "label", "scaleMode", "base", "style"],
        "properties": {
            "id": {"type": ["string", "null"]},
            "symbol": {"type": ["string", "null"], "enum": [*supported_symbols, None]},
            "label": {"type": ["string", "null"]},
            "scaleMode": {"type": ["string", "null"], "enum": ["percent", None]},
            "base": comparison_base_schema,
            "style": style_schema,
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "symbol",
            "timeframe",
            "visibleCount",
            "rightOffset",
            "layer",
            "visible",
            "drawingType",
            "anchors",
            "style",
            "label",
            "comparison",
            "comparisonId",
        ],
        "properties": {
            "symbol": {"type": ["string", "null"], "enum": [*supported_symbols, None]},
            "timeframe": {"type": ["string", "null"], "enum": ["1m", "5m", "10m", "1h", "4h", "1D", "1W", "1M", None]},
            "visibleCount": {"type": ["number", "null"], "minimum": 6, "maximum": 525600},
            "rightOffset": {"type": ["number", "null"]},
            "layer": {"type": ["string", "null"], "enum": [
                "candles", "volume", "ma5", "ma20", "ma60", "sma:5", "sma:20", "sma:60", "sma:120",
                "ema:20", "wma:20", "bollinger:20:2", "rsi:14", "stochastic:14:3:3", "macd:12:26:9",
                "volume-profile", None,
            ]},
            "visible": {"type": ["boolean", "null"]},
            "drawingType": {"type": ["string", "null"], "enum": ["horizontalLine", "trendLine", "verticalMarker", "textLabel", "pointMarker", "arrow", "rangeBox", None]},
            "anchors": {
                "type": ["array", "null"],
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["timestamp", "price", "paneId", "symbol", "logicalIndex", "value"],
                    "properties": {
                        "timestamp": {"type": ["string", "null"]},
                        "price": {"type": ["number", "null"]},
                        "paneId": {"type": ["string", "null"]},
                        "symbol": {"type": ["string", "null"]},
                        "logicalIndex": {"type": ["number", "null"]},
                        "value": {"type": ["number", "null"]},
                    },
                },
            },
            "style": style_schema,
            "label": {"type": ["string", "null"]},
            "comparison": comparison_schema,
            "comparisonId": {"type": ["string", "null"]},
        },
    }


def chart_command_schema(supported_symbols: list[str], min_items: int) -> dict[str, Any]:
    return {
        "type": "array",
        "minItems": min_items,
        "maxItems": 4,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type", "payload"],
            "properties": {
                "type": {
                    "type": "string",
                    "enum": [
                        "chart.symbol.set",
                        "chart.timeframe.set",
                        "chart.viewport.set",
                        "chart.layer.visibility.set",
                        "chart.drawing.add",
                        "chart.comparison.add",
                    ],
                },
                "payload": chart_command_payload_schema(supported_symbols),
            },
        },
    }


def filled_command_payload(
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    visible_count: int | None = None,
    right_offset: int | None = None,
    layer: str | None = None,
    visible: bool | None = None,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "visibleCount": visible_count,
        "rightOffset": right_offset,
        "layer": layer,
        "visible": visible,
        "drawingType": None,
        "anchors": None,
        "style": None,
        "label": None,
        "comparison": None,
        "comparisonId": None,
    }
