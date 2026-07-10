from typing import Any


CHART_INTERVALS = ("1m", "5m", "10m", "1h", "4h", "1D", "1W", "1M")
DRAWING_TYPES = (
    "horizontalLine",
    "horizontalParallelLines",
    "trendLine",
    "trendParallelLines",
    "verticalMarker",
    "verticalParallelLines",
    "textLabel",
    "flagMarker",
    "arrow",
    "rangeBox",
    "riskRewardBox",
    "fibonacciRetracement",
)


def chart_command_payload_schema(supported_symbols: list[str]) -> dict[str, Any]:
    style_schema = {
        "type": ["object", "null"],
        "additionalProperties": False,
        "required": [
            "color",
            "colorToken",
            "fillColor",
            "fillToken",
            "fillOpacity",
            "lineWidth",
            "lineDash",
            "textColor",
            "textToken",
            "fontSize",
            "opacity",
            "extension",
        ],
        "properties": {
            "color": {"type": ["string", "null"]},
            "colorToken": {"type": ["string", "null"]},
            "fillColor": {"type": ["string", "null"]},
            "fillToken": {"type": ["string", "null"]},
            "fillOpacity": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
            "lineWidth": {"type": ["number", "null"]},
            "lineDash": {"type": ["array", "null"], "items": {"type": "number"}},
            "textColor": {"type": ["string", "null"]},
            "textToken": {"type": ["string", "null"]},
            "fontSize": {"type": ["number", "null"], "minimum": 8},
            "opacity": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
            "extension": {"type": ["string", "null"], "enum": ["segment", "ray", "line", None]},
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
            "sourceInterval",
            "parallelLineCount",
            "style",
            "label",
            "comparison",
            "comparisonId",
        ],
        "properties": {
            "symbol": {"type": ["string", "null"], "enum": [*supported_symbols, None]},
            "timeframe": {"type": ["string", "null"], "enum": [*CHART_INTERVALS, None]},
            "visibleCount": {"type": ["number", "null"], "minimum": 6, "maximum": 525600},
            "rightOffset": {"type": ["number", "null"]},
            "layer": {"type": ["string", "null"], "enum": ["candles", "volume", "ma5", "ma20", "ma60", None]},
            "visible": {"type": ["boolean", "null"]},
            "drawingType": {"type": ["string", "null"], "enum": [*DRAWING_TYPES, None]},
            "anchors": {
                "type": ["array", "null"],
                "minItems": 1,
                "maxItems": 3,
                "description": "riskRewardBox requires exactly [entry, stop, target]; fibonacciRetracement requires exactly two swing anchors.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["timestamp", "price", "paneId", "symbol", "logicalIndex", "value", "interval"],
                    "properties": {
                        "timestamp": {"type": ["string", "null"]},
                        "price": {"type": ["number", "null"]},
                        "paneId": {"type": ["string", "null"]},
                        "symbol": {"type": ["string", "null"]},
                        "logicalIndex": {"type": ["number", "null"]},
                        "value": {"type": ["number", "null"]},
                        "interval": {"type": ["string", "null"], "enum": [*CHART_INTERVALS, None]},
                    },
                },
            },
            "sourceInterval": {"type": ["string", "null"], "enum": [*CHART_INTERVALS, None]},
            "parallelLineCount": {"type": ["integer", "null"], "minimum": 2, "maximum": 10},
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
    source_interval: str | None = None,
    parallel_line_count: int | None = None,
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
        "sourceInterval": source_interval,
        "parallelLineCount": parallel_line_count,
        "style": None,
        "label": None,
        "comparison": None,
        "comparisonId": None,
    }
