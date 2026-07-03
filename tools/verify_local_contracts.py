from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agent_backend.app.analysis import analysis_tools, build_analysis_snapshot
from agent_backend.app.main import normalize_agent_response
from mock_backend.app import main as mock


def main() -> None:
    current_time = datetime(2026, 7, 2, 20, 0, tzinfo=timezone.utc)
    assert_fake_symbols()
    assert_intraday_aggregation(current_time)
    assert_daily_weekly_monthly_aggregation(current_time)
    assert_live_candle_contract()
    assert_volume_diversity(current_time)
    assert_digging_range_coverage()
    assert_frontend_digging_contract()
    assert_frontend_single_digging_contract()
    assert_frontend_digging_pan_contract()
    assert_frontend_agent_preview_contract()
    assert_frontend_integer_price_axis_contract()
    assert_frontend_volume_pane_contract()
    assert_frontend_chart_layout_contract()
    assert_frontend_parent_summary_contract()
    assert_frontend_trend_menu_contract()
    assert_agent_actions_are_not_prompt_hardcoded()
    assert_analysis_snapshot()


def assert_fake_symbols() -> None:
    symbols = {profile.symbol for profile in mock.FAKE_SYMBOLS}
    assert symbols == {"GOPS-ALP", "GOPS-ION", "GOPS-NOVA"}
    assert not symbols.intersection({"AAPL", "MSFT", "NVDA"})


def assert_intraday_aggregation(current_time: datetime) -> None:
    start = datetime(2026, 7, 2, 13, 30, tzinfo=timezone.utc)
    for interval, minutes in (("5m", 5), ("10m", 10)):
        candle = mock.aggregate_target_bucket("GOPS-ALP", interval, start, current_time)
        source = [
            mock.source_minute_candle("GOPS-ALP", start + timedelta(minutes=offset), current_time)
            for offset in range(minutes)
        ]
        assert candle is not None
        assert_candle_matches_source(candle, source)


def assert_daily_weekly_monthly_aggregation(current_time: datetime) -> None:
    day_start = datetime(2026, 7, 2, tzinfo=timezone.utc)
    day = mock.aggregate_target_bucket("GOPS-ALP", "1D", day_start, current_time)
    minutes = [
        mock.source_minute_candle("GOPS-ALP", day_start + timedelta(hours=13, minutes=30 + offset), current_time)
        for offset in range(mock.REGULAR_SESSION_MINUTES)
    ]
    assert day is not None
    assert_candle_matches_source(day, minutes)

    week_start = mock.floor_bucket(day_start, "1W")
    week = mock.aggregate_target_bucket("GOPS-ALP", "1W", week_start, current_time)
    week_days = [
        mock.aggregate_target_bucket("GOPS-ALP", "1D", week_start + timedelta(days=offset), current_time)
        for offset in range(7)
    ]
    assert week is not None
    assert_candle_matches_source(week, week_days)

    month_start = mock.floor_bucket(day_start, "1M")
    month = mock.aggregate_target_bucket("GOPS-ALP", "1M", month_start, current_time)
    month_days = []
    day = month_start
    while day < mock.add_bucket(month_start, "1M"):
        month_days.append(mock.aggregate_target_bucket("GOPS-ALP", "1D", day, current_time))
        day += timedelta(days=1)
    assert month is not None
    assert_candle_matches_source(month, month_days)


def assert_live_candle_contract() -> None:
    live_time = datetime(2026, 7, 2, 14, 37, 24, tzinfo=timezone.utc)

    first = mock.collect_live_candles("GOPS-ALP", "1m", datetime(2026, 7, 2, 14, 37, 10, tzinfo=timezone.utc), 80)[-1]
    second = mock.collect_live_candles("GOPS-ALP", "1m", datetime(2026, 7, 2, 14, 37, 30, tzinfo=timezone.utc), 80)[-1]
    assert first["timestamp"] == second["timestamp"]
    assert first["close"] != second["close"]
    assert second["volume"] > first["volume"]

    for interval in ("1m", "5m", "10m", "1D", "1W", "1M"):
        bucket = mock.floor_bucket(live_time, interval)
        candle = mock.aggregate_live_target_bucket("GOPS-ALP", interval, bucket, live_time)
        assert candle is not None
        assert candle["isClosed"] is False
        assert_finite_candle(candle)

    for interval, minutes in (("5m", 5), ("10m", 10)):
        bucket = mock.floor_bucket(live_time, interval)
        current_minute = mock.floor_bucket(live_time, "1m")
        values = []
        minute = bucket
        for _ in range(minutes):
            if minute <= current_minute and mock.is_trading_minute(minute):
                values.append(mock.live_source_minute_values("GOPS-ALP", minute, live_time))
            minute += timedelta(minutes=1)
        candle = mock.aggregate_live_target_bucket("GOPS-ALP", interval, bucket, live_time)
        assert candle is not None
        assert_candle_matches_ohlcv_source(candle, values)

    day_bucket = mock.floor_bucket(live_time, "1D")
    day = mock.aggregate_live_target_bucket("GOPS-ALP", "1D", day_bucket, live_time)
    assert day is not None
    assert_candle_matches_ohlcv_source(day, mock.live_regular_session_source_values("GOPS-ALP", day_bucket.date(), live_time))

    week_bucket = mock.floor_bucket(live_time, "1W")
    week = mock.aggregate_live_target_bucket("GOPS-ALP", "1W", week_bucket, live_time)
    week_days = [mock.aggregate_live_day("GOPS-ALP", week_bucket + timedelta(days=offset), live_time) for offset in range(7)]
    assert week is not None
    assert_candle_matches_source(week, week_days)

    month_bucket = mock.floor_bucket(live_time, "1M")
    month = mock.aggregate_live_target_bucket("GOPS-ALP", "1M", month_bucket, live_time)
    month_days = []
    day_cursor = month_bucket
    while day_cursor < mock.add_bucket(month_bucket, "1M"):
        month_days.append(mock.aggregate_live_day("GOPS-ALP", day_cursor, live_time))
        day_cursor += timedelta(days=1)
    assert month is not None
    assert_candle_matches_source(month, month_days)


def assert_volume_diversity(current_time: datetime) -> None:
    start = datetime(2026, 7, 2, 13, 30, tzinfo=timezone.utc)
    minute_candles = [
        mock.source_minute_candle("GOPS-ALP", start + timedelta(minutes=offset), current_time)
        for offset in range(mock.REGULAR_SESSION_MINUTES)
    ]
    volumes = sorted(candle["volume"] for candle in minute_candles if candle)
    assert len(volumes) == mock.REGULAR_SESSION_MINUTES
    bottom_decile = volumes[:39]
    top_decile = volumes[-39:]
    assert sum(top_decile) / len(top_decile) > (sum(bottom_decile) / len(bottom_decile)) * 4
    assert volumes[-1] > volumes[0] * 8

    day_start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    daily = [
        mock.aggregate_target_bucket("GOPS-ALP", "1D", day_start + timedelta(days=offset), current_time)
        for offset in range(22)
    ]
    daily_volumes = sorted(candle["volume"] for candle in daily if candle)
    assert len(daily_volumes) >= 14
    assert daily_volumes[-1] > daily_volumes[0] * 1.35


def assert_digging_range_coverage() -> None:
    completed_time = datetime(2026, 8, 17, 20, 0, tzinfo=timezone.utc)
    july_start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    july_end = mock.add_bucket(july_start, "1M")

    parent = mock.aggregate_target_bucket("GOPS-ALP", "1M", july_start, completed_time)
    assert parent is not None

    query_start = mock.floor_bucket(july_start, "1W")
    query_end = mock.ceil_bucket(july_end, "1W")
    raw = mock.candles_for_range("GOPS-ALP", "1W", query_start, query_end, 0, completed_time)
    timestamps = [item["timestamp"] for item in raw["withLookback"]]
    assert "2026-06-29T00:00:00Z" in timestamps
    assert "2026-07-27T00:00:00Z" in timestamps

    day_start = datetime(2026, 7, 2, tzinfo=timezone.utc)
    day_end = mock.add_bucket(day_start, "1D")
    intraday = mock.candles_for_range("GOPS-ALP", "10m", day_start, day_end, 0, completed_time)
    assert len(intraday["withLookback"]) == 39


def assert_frontend_digging_contract() -> None:
    source = (REPO_ROOT / "frontend/src/chart/semanticTimeline.ts").read_text()
    assert 'case "1D":\n      return "10m";' in source
    assert 'case "10m":\n      return "1m";' in source
    assert 'case "5m":\n      return "1m";' in source
    assert 'case "1m":\n      return "footprint";' in source


def assert_frontend_single_digging_contract() -> None:
    timeline = (REPO_ROOT / "frontend/src/chart/semanticTimeline.ts").read_text()
    panel = (REPO_ROOT / "frontend/src/components/ChartPanel.tsx").read_text()
    assert "cursor = appendCandle(childCandle, childInterval, expansion.depth, expansion.id, undefined, cursor, false);" in timeline
    assert "appendCandle(candle, input.interval, 0, undefined, index, slotStart, true)" in timeline
    assert "parentNodeId.includes(expansionId)" not in panel
    assert "const [activeExpansions, setActiveExpansions] = useState<SemanticExpansion[]>([]);" in panel
    assert "const renderExpansions = activeExpansions;" in panel
    assert "function upsertExpansion" in panel
    assert "setActiveExpansions((current) => upsertExpansion(current, expansion));" in panel
    assert "setActiveExpansions(expansion)" not in panel
    assert "activeExpansionRef" not in panel
    assert "unit.parentExpansionId" in panel
    assert "semanticNodeId(unit.symbol, unit.interval, unit.timestamp)" in panel
    assert "range: intervalQueryRangeAround(unit.timestamp, unit.interval, visibleCount)" in panel
    assert "expansionOverride: expansion" in panel
    assert "options.expansionOverride ? [options.expansionOverride] : []" in panel


def assert_frontend_digging_pan_contract() -> None:
    timeline = (REPO_ROOT / "frontend/src/chart/semanticTimeline.ts").read_text()
    canvas = (REPO_ROOT / "frontend/src/chart/ChartCanvas.tsx").read_text()
    panel = (REPO_ROOT / "frontend/src/components/ChartPanel.tsx").read_text()
    assert "const maxExpansionWidth = Math.max(0, ...input.expansions.map(expansionSlotWidth));" in timeline
    assert "const renderStartIndex = Math.max(0, input.visibleStartIndex - maxExpansionWidth - 2);" in timeline
    assert "const renderEndIndex = Math.min(input.candles.length, input.visibleEndIndex + maxExpansionWidth + 2);" in timeline
    assert "for (let index = renderStartIndex; index < renderEndIndex; index += 1)" in timeline
    assert "const overlapsViewport = slotStart < input.visibleSlotCount && slotStart + width > 0;" in timeline
    assert "totalSlots: Math.max(1, input.visibleSlotCount)," in timeline
    assert "input.visibleSlotCount + extraSlots" not in timeline
    assert "function expansionSlotWidth" in timeline
    assert "const depthAlpha = Math.min(0.046, 0.018 + range.depth * 0.006);" in canvas
    assert "function drawPlotClipped" in canvas
    assert "drawPlotClipped(context, scene, () => drawCandles(context, scene))" in canvas
    assert "drawPlotClipped(context, scene, () => drawVolume(context, scene))" in canvas
    assert "if (range.right <= scene.plot.right)" in canvas
    assert "const visibleRight = Math.min(scene.plot.right, bounds.right);" in canvas
    assert "left: expansionCloseLeft(scene, range)," in panel
    assert "top: expansionMetadataCenterY(scene.plot.top) - expansionCloseButtonSize / 2," in panel
    assert "function expansionCloseLeft" in panel
    assert "const thumbnailStopLeft = expansionParentThumbnailRight(scene.plot, range) + thumbnailGap;" in panel
    assert "return maxVisibleLeft >= thumbnailStopLeft ? maxVisibleLeft : desiredLeft;" in panel
    assert "left: range.right - 34," not in panel
    assert "Math.min(scene.plot.right - 34, range.right - 34)" not in panel
    assert 'aria-label="Pan left"' not in panel
    assert 'aria-label="Pan right"' not in panel
    assert 'aria-label="Zoom in"' not in panel
    assert 'aria-label="Zoom out"' not in panel
    assert 'aria-label="Reload candles"' not in panel


def assert_frontend_agent_preview_contract() -> None:
    panel = (REPO_ROOT / "frontend/src/components/ChartPanel.tsx").read_text()
    canvas = (REPO_ROOT / "frontend/src/chart/ChartCanvas.tsx").read_text()
    assert "const drawingActions = actions.filter(isDrawingAction);" in panel
    assert "setAgentDrawingPreview({" in panel
    assert "applyChartActions(current, preview.actions)" in panel
    assert "previewDrawings?: DrawingEntity[];" in canvas
    assert '{ id: "agent-preview", draw: () => drawDrawings(context, scene, previewDrawings, true) }' in canvas


def assert_frontend_integer_price_axis_contract() -> None:
    scene = (REPO_ROOT / "frontend/src/chart/scene.ts").read_text()
    canvas = (REPO_ROOT / "frontend/src/chart/ChartCanvas.tsx").read_text()
    assert "priceTicks: number[];" in scene
    assert "return integerPriceDomain(min - pad, max + pad);" in scene
    assert "function niceIntegerStep" in scene
    assert "scene.scales.priceTicks.forEach((price)" in canvas
    assert "formatPriceAxisValue(price)" in canvas
    assert "context.fillText(price.toFixed(2)" not in canvas


def assert_frontend_volume_pane_contract() -> None:
    scene = (REPO_ROOT / "frontend/src/chart/scene.ts").read_text()
    canvas = (REPO_ROOT / "frontend/src/chart/ChartCanvas.tsx").read_text()
    panel = (REPO_ROOT / "frontend/src/components/ChartPanel.tsx").read_text()
    styles = (REPO_ROOT / "frontend/src/styles.css").read_text()
    assert "const volumeScalePadding = 1.18;" in scene
    assert "volumeTicks: number[];" in scene
    assert "const volumeRange = volumeDomain(maxVolume);" in scene
    assert "maxVolume: volumeRange.max" in scene
    assert "volumeTicks: volumeRange.ticks" in scene
    assert "function volumeDomain" in scene
    assert "function niceIntegerCeil" in scene
    assert "priceBoundary" in canvas
    assert "line(context, scene.plot.left, scene.plot.priceBottom, right, scene.plot.priceBottom);" in canvas
    assert "line(context, scene.plot.left, scene.plot.volumeTop, right, scene.plot.volumeTop)" not in canvas
    assert "scene.scales.volumeTicks.forEach((volume)" in canvas
    assert "drawAxisPill(context, formatVolumeAxisValue(volumeAtY(scene, crosshair.y)), scene.width - 8, crosshair.y, \"right\");" in canvas
    assert "notation: \"compact\"" not in canvas
    assert "function formatCompactVolumeNumber" in canvas
    assert 'const unit = value >= 999_500 ? "M" : "K";' in canvas
    assert "return `${formatCompactVolumeNumber(value / divisor)}${unit}`;" in canvas
    assert "function semanticVisualStyle" in canvas
    assert "function applySemanticVisualStyle" in canvas
    assert "applySemanticVisualStyle(context, semanticVisualStyle(scene, unit));" in canvas
    assert "hoveredCandleId" not in canvas
    assert "? scene.plot.priceBottom : null" in panel
    assert ".volume-resize-handle::before" in styles
    assert "top: 5px;" in styles


def assert_frontend_chart_layout_contract() -> None:
    scene = (REPO_ROOT / "frontend/src/chart/scene.ts").read_text()
    panel = (REPO_ROOT / "frontend/src/components/ChartPanel.tsx").read_text()
    styles = (REPO_ROOT / "frontend/src/styles.css").read_text()
    assert "top: safeHeight < 240 ? 62 : 84," in scene
    assert "bottom: chart.layers.volume ? 36 : 30," in scene
    assert 'className="hover-ohlc hover-ohlc-overlay"' in panel
    assert ".hover-ohlc {\n  display: grid;" in styles
    assert "grid-template-columns: 108px repeat(4, 72px);" in styles
    assert "column-gap: 10px;" in styles
    assert "font-size: 9px;" in styles
    assert "font-weight: 480;" in styles
    assert "font-variant-numeric: tabular-nums;" in styles
    assert ".hover-ohlc-overlay {\n  position: absolute;" in styles
    assert "top: 86px;" in styles
    assert ".hover-ohlc div {\n  display: grid;" in styles
    assert "grid-template-columns: max-content minmax(0, 1fr);" in styles
    assert "gap: 1px;" in styles
    assert "text-align: left;" in styles
    assert ".hover-ohlc-time {\n  display: block;" in styles
    assert ".hover-ohlc .hover-ohlc-time dd" in styles
    assert "width: 108px;" in styles
    assert ".panel-header {\n  position: absolute;\n  top: 16px;" in styles
    assert "justify-content: flex-start;" in styles
    assert "min-width: 0;" in styles
    assert ".live-quote {\n  position: absolute;\n  top: 2px;\n  left: 152px;" in styles
    assert "flex-wrap: nowrap;" in styles
    assert "min-width: 172px;" in styles
    assert ".toolbar {\n  position: absolute;\n  top: 16px;" in styles

    viewport = (REPO_ROOT / "frontend/src/chart/viewport.ts").read_text()
    interval_navigation = (REPO_ROOT / "frontend/src/chart/intervalNavigation.ts").read_text()
    panel = (REPO_ROOT / "frontend/src/components/ChartPanel.tsx").read_text()
    assert "const INITIAL_RIGHT_EMPTY_SPACE_RATIO = 1 / 3;" in viewport
    assert "export function latestCandleRightOffset" in viewport
    assert "fallback.rightOffset === latestCandleRightOffset(fallback.visibleCount)" in interval_navigation
    assert 'rightOffset: latestCandleRightOffset(defaultVisibleBarsForInterval("1D"))' in panel

    client = (REPO_ROOT / "frontend/src/chart/cdcClient.ts").read_text()
    assert "function chartSocketUrl" in client
    assert "function reconnectDelayMs" in client
    assert "reconnectTimer = window.setTimeout(connect, reconnectDelayMs(reconnectAttempts));" in client


def assert_frontend_parent_summary_contract() -> None:
    canvas = (REPO_ROOT / "frontend/src/chart/ChartCanvas.tsx").read_text()
    layout = (REPO_ROOT / "frontend/src/chart/expansionLayout.ts").read_text()
    assert "function buildParentSummaryColumns" in canvas
    assert "expansionSummaryVisibleBounds(scene.plot, range)" in canvas
    assert "expansionMetadataTop(scene.plot.top)" in canvas
    assert "export function expansionMetadataTop" in layout
    assert "export function expansionMetadataCenterY" in layout
    assert "line(context, candleCenter, high, candleCenter, bodyTop);" in canvas
    assert "line(context, candleCenter, bodyBottom, candleCenter, low);" in canvas
    assert "context.fillRect(bodyLeft, bodyTop, candleWidth, bodyHeight);" in canvas
    assert "const columnGap = 54;" in canvas
    assert "fillText(column.top, columnX" in canvas
    assert "fillText(column.bottom, columnX" in canvas
    assert "fillText(column.top, columnX" in canvas and "maxWidth" not in canvas.split("fillText(column.top, columnX", 1)[1].split(");", 1)[0]
    assert "fillText(column.bottom, columnX" in canvas and "maxWidth" not in canvas.split("fillText(column.bottom, columnX", 1)[1].split(");", 1)[0]
    assert "formatParentSummaryDate(range.from)" in canvas
    assert "formatParentSummaryDate(range.to)" in canvas
    assert "if (width < 72)" in canvas
    assert "if (width >= 132)" in canvas
    assert "if (width >= 190)" in canvas
    assert "formatParentSummaryRange" not in canvas
    assert "formatParentSummaryTime" not in canvas
    assert "`Start ${" not in canvas
    assert "`End ${" not in canvas
    assert "${range.parentInterval}" not in canvas


def assert_frontend_trend_menu_contract() -> None:
    panel = (REPO_ROOT / "frontend/src/components/ChartPanel.tsx").read_text()
    styles = (REPO_ROOT / "frontend/src/styles.css").read_text()
    assert "const [trendMenuOpen, setTrendMenuOpen] = useState(false);" in panel
    assert "const trendExtensionButtons = useMemo(() => ([" in panel
    assert '<div className="trend-control" key={tool.mode}>' in panel
    assert 'className="trend-menu" role="menu" aria-label="Trend line type"' in panel
    assert "<TrendExtensionIcon extension={extension} />" in panel
    assert "function TrendExtensionIcon" in panel
    assert 'className="trend-extension-icon"' in panel
    assert '<circle cx="4" cy="14" r="1.8" />' in panel
    assert '<polyline points="10 4 14 4 14 8" />' in panel
    assert '<polyline points="8 14 4 14 4 10" />' in panel
    assert "cycleTrendLineExtension" not in panel
    assert 'title="Trend extension"' not in panel
    assert ".trend-menu {" in styles
    assert ".trend-extension-icon {" in styles
    assert ".trend-extension-icon line,\n.trend-extension-icon polyline" in styles
    assert ".tool-glyph.trend-glyph" not in styles
    assert "radial-gradient(circle at 2px 13px" not in styles


def assert_agent_actions_are_not_prompt_hardcoded() -> None:
    weekly = [
        {"timestamp": "2026-06-15T00:00:00Z", "high": 123, "low": 111, "close": 120, "volume": 100},
        {"timestamp": "2026-06-22T00:00:00Z", "high": 127, "low": 118, "close": 126, "volume": 980},
    ]
    context = {
        "prompt": "주봉으로 바꾸고 거래량이 큰 봉에 표시해줘",
        "panel": {"symbol": "GOPS-ALP", "interval": "1m", "visibleCount": 120, "rightOffset": 0},
        "candles": {"current": [], "daily": [], "weekly": weekly},
    }

    raw = {
        "message": "LLM이 직접 고른 액션입니다.",
        "actions": [
            {"type": "setInterval", "interval": "1W"},
            {"type": "setViewport", "visibleCount": 120, "rightOffset": -80},
            {
                "type": "addDrawing",
                "drawing": {
                    "id": "llm-selected",
                    "type": "pointMarker",
                    "anchors": [{"timestamp": "2026-02-06T00:00:00Z", "price": 120, "symbol": "GOPS-ALP"}],
                    "style": {"color": "#ff0000"},
                    "label": "LLM 선택",
                    "visible": True,
                    "createdBy": "agent",
                },
            },
        ],
        "insights": ["LLM이 분석 데이터를 참고해 선택했습니다."],
    }
    response = normalize_agent_response(raw, context)
    assert response["message"] == "LLM이 직접 고른 액션입니다."
    assert response["insights"] == ["LLM이 분석 데이터를 참고해 선택했습니다."]
    assert response["actions"][1]["rightOffset"] == -80
    assert response["actions"][2]["drawing"]["type"] == "pointMarker"

    empty_response = normalize_agent_response({"message": "no action", "actions": [], "insights": []}, context)
    assert empty_response["actions"] == []


def assert_analysis_snapshot() -> None:
    candles = [
        {"timestamp": f"2026-06-{day:02d}T00:00:00Z", "open": 100 + day, "high": 103 + day, "low": 98 + day, "close": 101 + day, "volume": 1000 + day * 100, "ma5": 100 + day, "ma20": 99 + day, "ma60": 98 + day}
        for day in range(1, 16)
    ]
    tool_names = {tool["name"] for tool in analysis_tools()}
    assert {"supportResistance", "swingPoints", "trendCandidates", "volumeEvents", "maContext"}.issubset(tool_names)
    snapshot = build_analysis_snapshot({"current": candles})
    current = snapshot["current"]
    assert current["count"] == len(candles)
    assert current["swingPoints"]["high"]["timestamp"] == "2026-06-15T00:00:00Z"
    assert current["volumeEvents"]["events"][0]["timestamp"] == "2026-06-15T00:00:00Z"
    assert current["maContext"]["bias"]["ma5"] == "above"


def assert_candle_matches_source(candle: dict, source: list[dict | None]) -> None:
    values = [item for item in source if item]
    assert values
    assert candle["open"] == values[0]["open"]
    assert candle["high"] == max(item["high"] for item in values)
    assert candle["low"] == min(item["low"] for item in values)
    assert candle["close"] == values[-1]["close"]
    assert candle["volume"] == sum(item["volume"] for item in values)


def assert_candle_matches_ohlcv_source(candle: dict, source: list[tuple[float, float, float, float, int]]) -> None:
    assert source
    assert candle["open"] == source[0][0]
    assert candle["high"] == max(item[1] for item in source)
    assert candle["low"] == min(item[2] for item in source)
    assert candle["close"] == source[-1][3]
    assert candle["volume"] == sum(item[4] for item in source)


def assert_finite_candle(candle: dict) -> None:
    for field in ("open", "high", "low", "close", "volume"):
        value = candle[field]
        assert isinstance(value, int | float)
        assert value == value
    assert candle["high"] >= max(candle["open"], candle["close"])
    assert candle["low"] <= min(candle["open"], candle["close"])


if __name__ == "__main__":
    main()
