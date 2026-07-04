from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
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
    assert_frontend_recursive_digging_contract()
    assert_frontend_digging_pan_contract()
    assert_frontend_agent_preview_contract()
    assert_frontend_integer_price_axis_contract()
    assert_frontend_volume_pane_contract()
    assert_frontend_chart_layout_contract()
    assert_frontend_layout_grid_contract()
    assert_frontend_treemap_main_view_contract()
    assert_frontend_ontology_contract()
    assert_frontend_palette_contract()
    assert_frontend_parent_summary_contract()
    assert_frontend_trend_menu_contract()
    assert_agent_actions_are_not_prompt_hardcoded()
    assert_analysis_snapshot()


def assert_fake_symbols() -> None:
    symbols = {profile.symbol for profile in mock.FAKE_SYMBOLS}
    assert symbols == {"TSLA", "AAPL", "GOOGL"}
    assert not symbols.intersection({"MU", "KO", "MSFT", "NVDA"})
    assert mock.normalize_symbol("googl") == "GOOGL"
    unsupported = asyncio.run(mock.get_candles(symbol="MSFT", interval="1D", limit=10, ma=""))
    assert unsupported["symbol"] == "MSFT"
    assert unsupported["status"] == "empty"
    assert unsupported["candles"] == []


def assert_intraday_aggregation(current_time: datetime) -> None:
    start = datetime(2026, 7, 2, 13, 30, tzinfo=timezone.utc)
    for interval, minutes in (("5m", 5), ("10m", 10)):
        candle = mock.aggregate_target_bucket("TSLA", interval, start, current_time)
        source = [
            mock.source_minute_candle("TSLA", start + timedelta(minutes=offset), current_time)
            for offset in range(minutes)
        ]
        assert candle is not None
        assert_candle_matches_source(candle, source)


def assert_daily_weekly_monthly_aggregation(current_time: datetime) -> None:
    day_start = datetime(2026, 7, 2, tzinfo=timezone.utc)
    day = mock.aggregate_target_bucket("TSLA", "1D", day_start, current_time)
    minutes = [
        mock.source_minute_candle("TSLA", day_start + timedelta(hours=13, minutes=30 + offset), current_time)
        for offset in range(mock.REGULAR_SESSION_MINUTES)
    ]
    assert day is not None
    assert_candle_matches_source(day, minutes)

    week_start = mock.floor_bucket(day_start, "1W")
    week = mock.aggregate_target_bucket("TSLA", "1W", week_start, current_time)
    week_days = [
        mock.aggregate_target_bucket("TSLA", "1D", week_start + timedelta(days=offset), current_time)
        for offset in range(7)
    ]
    assert week is not None
    assert_candle_matches_source(week, week_days)

    month_start = mock.floor_bucket(day_start, "1M")
    month = mock.aggregate_target_bucket("TSLA", "1M", month_start, current_time)
    month_days = []
    day = month_start
    while day < mock.add_bucket(month_start, "1M"):
        month_days.append(mock.aggregate_target_bucket("TSLA", "1D", day, current_time))
        day += timedelta(days=1)
    assert month is not None
    assert_candle_matches_source(month, month_days)


def assert_live_candle_contract() -> None:
    live_time = datetime(2026, 7, 2, 14, 37, 24, tzinfo=timezone.utc)

    first = mock.collect_live_candles("TSLA", "1m", datetime(2026, 7, 2, 14, 37, 10, tzinfo=timezone.utc), 80)[-1]
    second = mock.collect_live_candles("TSLA", "1m", datetime(2026, 7, 2, 14, 37, 30, tzinfo=timezone.utc), 80)[-1]
    assert first["timestamp"] == second["timestamp"]
    assert first["close"] != second["close"]
    assert second["volume"] > first["volume"]

    for interval in ("1m", "5m", "10m", "1D", "1W", "1M"):
        bucket = mock.floor_bucket(live_time, interval)
        candle = mock.aggregate_live_target_bucket("TSLA", interval, bucket, live_time)
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
                values.append(mock.live_source_minute_values("TSLA", minute, live_time))
            minute += timedelta(minutes=1)
        candle = mock.aggregate_live_target_bucket("TSLA", interval, bucket, live_time)
        assert candle is not None
        assert_candle_matches_ohlcv_source(candle, values)

    day_bucket = mock.floor_bucket(live_time, "1D")
    day = mock.aggregate_live_target_bucket("TSLA", "1D", day_bucket, live_time)
    assert day is not None
    assert_candle_matches_ohlcv_source(day, mock.live_regular_session_source_values("TSLA", day_bucket.date(), live_time))

    week_bucket = mock.floor_bucket(live_time, "1W")
    week = mock.aggregate_live_target_bucket("TSLA", "1W", week_bucket, live_time)
    week_days = [mock.aggregate_live_day("TSLA", week_bucket + timedelta(days=offset), live_time) for offset in range(7)]
    assert week is not None
    assert_candle_matches_source(week, week_days)

    month_bucket = mock.floor_bucket(live_time, "1M")
    month = mock.aggregate_live_target_bucket("TSLA", "1M", month_bucket, live_time)
    month_days = []
    day_cursor = month_bucket
    while day_cursor < mock.add_bucket(month_bucket, "1M"):
        month_days.append(mock.aggregate_live_day("TSLA", day_cursor, live_time))
        day_cursor += timedelta(days=1)
    assert month is not None
    assert_candle_matches_source(month, month_days)


def assert_volume_diversity(current_time: datetime) -> None:
    start = datetime(2026, 7, 2, 13, 30, tzinfo=timezone.utc)
    minute_candles = [
        mock.source_minute_candle("TSLA", start + timedelta(minutes=offset), current_time)
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
        mock.aggregate_target_bucket("TSLA", "1D", day_start + timedelta(days=offset), current_time)
        for offset in range(22)
    ]
    daily_volumes = sorted(candle["volume"] for candle in daily if candle)
    assert len(daily_volumes) >= 14
    assert daily_volumes[-1] > daily_volumes[0] * 1.35


def assert_digging_range_coverage() -> None:
    completed_time = datetime(2026, 8, 17, 20, 0, tzinfo=timezone.utc)
    july_start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    july_end = mock.add_bucket(july_start, "1M")

    parent = mock.aggregate_target_bucket("TSLA", "1M", july_start, completed_time)
    assert parent is not None

    query_start = mock.floor_bucket(july_start, "1W")
    query_end = mock.ceil_bucket(july_end, "1W")
    raw = mock.candles_for_range("TSLA", "1W", query_start, query_end, 0, completed_time)
    timestamps = [item["timestamp"] for item in raw["withLookback"]]
    assert "2026-06-29T00:00:00Z" in timestamps
    assert "2026-07-27T00:00:00Z" in timestamps

    day_start = datetime(2026, 7, 2, tzinfo=timezone.utc)
    day_end = mock.add_bucket(day_start, "1D")
    intraday = mock.candles_for_range("TSLA", "10m", day_start, day_end, 0, completed_time)
    assert len(intraday["withLookback"]) == 39


def assert_frontend_digging_contract() -> None:
    source = (REPO_ROOT / "frontend/src/chart/semanticTimeline.ts").read_text()
    assert 'case "1D":\n      return "10m";' in source
    assert 'case "10m":\n      return "1m";' in source
    assert 'case "5m":\n      return "1m";' in source
    assert 'case "1m":\n      return "footprint";' in source


def assert_frontend_recursive_digging_contract() -> None:
    timeline = (REPO_ROOT / "frontend/src/chart/semanticTimeline.ts").read_text()
    panel = (REPO_ROOT / "frontend/src/components/ChartPanel.tsx").read_text()
    assert "cursor = appendCandle(childCandle, childInterval, expansion.depth, expansion.id, undefined, cursor, true);" in timeline
    assert "appendCandle(candle, input.interval, 0, undefined, index, slotStart, true)" in timeline
    assert "parentNodeId.includes(expansionId)" not in panel
    assert "parentExpansionId?: string;" in timeline
    assert "symbol: string;" in timeline
    assert "const [activeExpansions, setActiveExpansions] = useState<SemanticExpansion[]>([]);" in panel
    assert "const renderExpansions = activeExpansions;" in panel
    assert "function upsertExpansion" in panel
    assert "function removeExpansionTree" in panel
    assert "setActiveExpansions((current) => upsertExpansion(current, expansion));" in panel
    assert "setActiveExpansions(expansion)" not in panel
    assert "activeExpansionRef" not in panel
    assert "switchSymbolInterval(unit.symbol, unit.interval" not in panel
    assert "intervalQueryRangeAround" not in panel
    assert "expansionOverride" not in panel
    assert "semanticExpansionId(unit.id)" in panel
    assert "parentExpansionId: unit.parentExpansionId" in panel
    assert "const childNodeId = semanticNodeId(expansion.symbol, childInterval, candle.timestamp, expansion.id);" in timeline
    assert "visited.has(expansion.id)" in timeline


def assert_frontend_digging_pan_contract() -> None:
    timeline = (REPO_ROOT / "frontend/src/chart/semanticTimeline.ts").read_text()
    canvas = (REPO_ROOT / "frontend/src/chart/ChartCanvas.tsx").read_text()
    panel = (REPO_ROOT / "frontend/src/components/ChartPanel.tsx").read_text()
    scene = (REPO_ROOT / "frontend/src/chart/scene.ts").read_text()
    assert "const maxExpansionWidth = Math.max(0, ...input.expansions.map((expansion) => expansionSlotWidth(expansion, expansionByParent)));" in timeline
    assert "const renderStartIndex = Math.max(0, input.visibleStartIndex - maxExpansionWidth - 2);" in timeline
    assert "const renderEndIndex = Math.min(input.candles.length, input.visibleEndIndex + maxExpansionWidth + 2);" in timeline
    assert "for (let index = renderStartIndex; index < renderEndIndex; index += 1)" in timeline
    assert "const overlapsViewport = slotStart < input.visibleSlotCount && slotStart + width > 0;" in timeline
    assert "totalSlots: Math.max(1, input.visibleSlotCount)," in timeline
    assert "input.visibleSlotCount + extraSlots" not in timeline
    assert "function expansionSlotWidth(" in timeline
    assert ".sort((left, right) => left.depth - right.depth || left.slotStart - right.slotStart)" in scene
    assert "const depthAlpha = Math.min(0.034, 0.012 + range.depth * 0.004);" in canvas
    assert "function drawExpansionSideShadow" in canvas
    assert "context.globalAlpha = 0.095;" not in canvas
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
    assert "() => drawDrawings(context, scene, previewDrawings, true)" in canvas


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
    assert "context.strokeStyle = colors.border;" in canvas
    assert "line(context, scene.plot.left, scene.plot.priceBottom, right, scene.plot.priceBottom);" in canvas
    assert "line(context, scene.plot.left, scene.plot.volumeTop, right, scene.plot.volumeTop)" not in canvas
    assert "scene.scales.volumeTicks.forEach((volume)" in canvas
    assert "drawAxisPill(context, formatVolumeAxisValue(volumeAtY(scene, crosshair.y)), scene.width - 8, crosshair.y, \"right\");" in canvas
    assert "notation: \"compact\"" not in canvas
    assert "function formatCompactVolumeNumber" in canvas
    assert 'const unit = value >= 999_500 ? "M" : "K";' in canvas
    assert "return `${formatCompactVolumeNumber(value / divisor)}${unit}`;" in canvas
    assert "function semanticContextOpacity" in canvas
    assert "context.globalAlpha *= semanticContextOpacity(scene, unit);" in canvas
    assert "function semanticVisualStyle" not in canvas
    assert "function applySemanticVisualStyle" not in canvas
    assert "SemanticVisualRole" not in canvas
    assert "SemanticVisualStyle" not in canvas
    assert "type RenderLayer" not in canvas
    assert "drawPriceAxisBackdrop" not in canvas
    assert "function candleStrokeColor" in canvas
    assert "return hovered ? colors.up : colors.upSoft;" in canvas
    assert "return hovered ? colors.down : colors.downSoft;" in canvas
    assert "context.globalAlpha *= 0.18;" in canvas
    assert "hoveredCandleId" not in canvas
    assert "? scene.plot.priceBottom : null" in panel
    assert ".volume-resize-handle::before" in styles
    assert "top: 5px;" in styles


def assert_frontend_chart_layout_contract() -> None:
    scene = (REPO_ROOT / "frontend/src/chart/scene.ts").read_text()
    canvas = (REPO_ROOT / "frontend/src/chart/ChartCanvas.tsx").read_text()
    panel = (REPO_ROOT / "frontend/src/components/ChartPanel.tsx").read_text()
    app = (REPO_ROOT / "frontend/src/App.tsx").read_text()
    workspace = (REPO_ROOT / "frontend/src/components/PanelWorkspace.tsx").read_text()
    panel_content = (REPO_ROOT / "frontend/src/components/PanelContentRenderer.tsx").read_text()
    workspace_geometry = (REPO_ROOT / "frontend/src/components/panelWorkspaceGeometry.ts").read_text()
    styles = (REPO_ROOT / "frontend/src/styles.css").read_text()
    assert "top: safeHeight < 240 ? 62 : 84," in scene
    assert "bottom: chart.layers.volume ? 36 : 30," in scene
    assert "export function priceToY(scene: Pick<ChartScene, \"plot\" | \"scales\">, value: number): number" in scene
    assert "export function topPriceGridY(scene: Pick<ChartScene, \"plot\" | \"scales\">): number" in scene
    assert "return priceToY(scene, Math.max(...ticks));" in scene
    assert "priceToY(scene, price)" in canvas
    assert "topPriceGridY" in panel
    assert 'style={{ "--hover-ohlc-top": `${hoverOhlcTop}px` } as CSSProperties}' in panel
    assert 'className="hover-ohlc hover-ohlc-overlay"' in panel
    assert ".hover-ohlc {\n  display: grid;" in styles
    assert "grid-template-columns: 108px repeat(4, 72px);" in styles
    assert "column-gap: 10px;" in styles
    assert "font-size: 9px;" in styles
    assert "font-weight: 480;" in styles
    assert "font-variant-numeric: tabular-nums;" in styles
    assert ".hover-ohlc-overlay {\n  position: absolute;" in styles
    assert "top: var(--hover-ohlc-top, 86px);" in styles
    assert "top: 86px;" not in styles
    assert ".hover-ohlc div {\n  display: grid;" in styles
    assert "grid-template-columns: max-content minmax(0, 1fr);" in styles
    assert "gap: 1px;" in styles
    assert "text-align: left;" in styles
    assert ".hover-ohlc-time {\n  display: block;" in styles
    assert ".hover-ohlc .hover-ohlc-time dd" in styles
    assert "width: 108px;" in styles
    assert 'className="workspace-top-nav chart"' in app
    assert 'className={`header-quote-stack ${activeHeaderQuote?.tone ?? "unavailable"}`}' in app
    assert '<h1 className="company-ticker">{activeHeaderSymbol}</h1>' in app
    assert 'className="workspace-top-nav-spacer"' in app
    assert 'className="company-summary"' not in app
    assert 'className="company-summary"' not in panel
    assert "company-summary-back" not in app
    assert "symbol-name" not in app
    assert ".company-summary" not in styles
    assert ".company-summary-main" not in styles
    assert "grid-template-columns: minmax(180px, 1fr) auto minmax(180px, 1fr);" in styles
    assert ".company-ticker {" in styles
    assert "justify-self: center;" in styles
    assert "font-size: 54px;" in styles
    assert '--font-ui-serif: "Times New Roman", Times, Georgia, "Nanum Myeongjo", "Noto Serif KR", serif;' in styles
    assert "font-family: var(--font-ui-serif);" in styles
    assert "font-style: normal;" in styles
    assert "font-weight: 500;" in styles
    assert "font-synthesis: weight;" in styles
    assert ".header-quote-stack {" in styles
    assert "display: grid;" in styles
    assert "justify-self: start;" in styles
    assert "font-size: 13px;" in styles
    assert ".quote-price-line {\n  display: inline-flex;" in styles
    assert ".quote-price {\n  color: var(--color-text);\n  font-size: 17px;" in styles
    assert ".symbol-search {\n  position: relative;" in styles
    assert ".workspace-top-nav .symbol-search" not in styles
    assert "top-nav-symbol-search" not in app
    assert "activeHeaderName" not in app
    assert ".symbol-search-menu {\n  position: absolute;\n  top: 42px;\n  right: 0;" in styles
    assert ".chart-instance.is-editable-chart .chart-instance-symbol-search button {" in styles
    assert ".workspace-panel-frame.is-chart-hovered .chart-instance.is-editable-chart .chart-instance-symbol-search button" in styles
    assert ".chart-instance.is-editable-chart .chart-instance-symbol:hover .chart-instance-symbol-search button" not in styles
    assert ".chart-instance.is-editable-chart .chart-instance-symbol-search:hover button" not in styles
    assert ".chart-instance.is-editable-chart .chart-instance-symbol-search:focus-within button" in styles
    assert ".chart-instance.is-editable-chart .chart-instance-symbol-search.is-active button" in styles
    assert ".panel-header" not in panel
    assert ".toolbar {\n  position: absolute;\n  top: 16px;" in styles
    assert "function canMoveChartLayout" not in app
    assert "disabled={!laneCanMove}" not in app
    assert 'className="panel-move-button"' not in app
    assert "<Move size={16} />" not in app
    assert "panel-resize-grip" not in app
    assert "beginBoundaryResize" not in app
    assert "beginPanelSwap" not in app
    assert "panel-boundary" not in app
    assert "beginBoundaryResize" in workspace
    assert "beginPanelSwap" in workspace
    assert "panel-boundary" in workspace
    assert ".workspace-panel-frame {" in styles
    assert ".panel-boundary {" in styles
    assert ".panel-move-button {" not in styles
    assert ".panel-resize-grip {" not in styles
    assert ".chart-lane-frame.is-resize-disabled .chart-resize-grip" in styles

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


def assert_frontend_layout_grid_contract() -> None:
    app = (REPO_ROOT / "frontend/src/App.tsx").read_text()
    grid = (REPO_ROOT / "frontend/src/layout/grid.ts").read_text()
    panel_layout = (REPO_ROOT / "frontend/src/layout/panelLayout.ts").read_text()
    panel_geometry = (REPO_ROOT / "frontend/src/layout/panelGeometry.ts").read_text()
    metrics = (REPO_ROOT / "frontend/src/layout/workspaceMetrics.ts").read_text()
    styles = (REPO_ROOT / "frontend/src/styles.css").read_text()
    frame = (REPO_ROOT / "frontend/src/components/WorkspacePanelFrame.tsx").read_text()
    workspace = (REPO_ROOT / "frontend/src/components/PanelWorkspace.tsx").read_text()
    panel_content = (REPO_ROOT / "frontend/src/components/PanelContentRenderer.tsx").read_text()
    workspace_geometry = (REPO_ROOT / "frontend/src/components/panelWorkspaceGeometry.ts").read_text()
    bottom_bar = (REPO_ROOT / "frontend/src/components/BottomCommandBar.tsx").read_text()
    panel = (REPO_ROOT / "frontend/src/components/ChartPanel.tsx").read_text()
    search = (REPO_ROOT / "frontend/src/components/SymbolSearch.tsx").read_text()
    package = (REPO_ROOT / "package.json").read_text()

    assert "export type GridLineIndex = 0 | 1 | 2 | 3 | 4;" in grid
    assert 'export type PanelEdgeBehavior = "normal" | "flush-at-page-edge";' in grid
    assert "export const gridLockMaxWidth = 760;" in grid
    assert "export const defaultChartGridSpan: PanelGridSpan = { start: 0, end: 4 };" in grid
    assert "export const workspaceTopInset = 76;" in metrics
    assert "export const bottomNavigationHeight = 76;" in metrics
    assert "export const navigationGap = 8;" in metrics
    assert "export const treeMapHoverMetaReserve = 28;" in metrics
    assert "export const workspaceBottomInset = bottomNavigationHeight + treeMapHoverMetaReserve + navigationGap;" in metrics
    assert "floatingPanelGridSpan" not in grid
    assert "treemapSearchGridSpan" not in grid
    assert "return Math.round(Math.min(18, Math.max(10, viewportWidth * 0.012)));" in grid
    assert 'edgeBehavior === "flush-at-page-edge" && line === 0' in grid
    assert 'edgeBehavior === "flush-at-page-edge" && line === 4' in grid
    assert "export function snapGridSpan" not in grid
    assert "export function lockedGridSpan" not in grid
    assert "export function isGridLocked(viewportWidth: number): boolean" in grid

    assert 'export type PanelContentKind = "chart" | "news" | "ontology" | "companyAnalysis" | "trade";' in panel_layout
    assert 'import { workspaceBottomInset, workspaceTopInset } from "./workspaceMetrics";' in panel_layout
    assert 'from "./panelGeometry";' in panel_layout
    assert "export function rectRight" in panel_geometry
    assert "export function rectBottom" in panel_geometry
    assert "export function rangesOverlap" in panel_geometry
    assert "export function rectsOverlap" in panel_geometry
    assert "export function almostEqual" in panel_geometry
    assert "export function uniqueStrings" in panel_geometry
    assert "export function sortedUnique" in panel_geometry
    assert "export function clamp" in panel_geometry
    assert "function rectRight(rect: PanelRect)" not in panel_layout
    assert "function rectBottom(rect: PanelRect)" not in panel_layout
    assert 'chart: "",' in panel_layout
    assert 'chart: "차트"' not in panel_layout
    assert "export type PanelSlot =" in panel_layout
    assert "chartGridX" not in panel_layout
    assert "export type PanelContentInstance =" in panel_layout
    assert "symbol?: string;" in panel_layout
    assert "isDefaultChart?: boolean;" in panel_layout
    assert "export type TiledPanelState =" in panel_layout
    assert "export type PanelBoundary =" in panel_layout
    assert 'export type PanelBoundaryInteraction = "resize" | "insert-only";' in panel_layout
    assert "interaction: PanelBoundaryInteraction;" in panel_layout
    assert 'pageEdge?: "left" | "right" | "top" | "bottom";' in panel_layout
    assert 'export type PanelBoundaryKind = "shared" | "outer";' in panel_layout
    assert 'export const insertablePanelKinds: PanelContentKind[] = ["news", "ontology", "companyAnalysis", "trade", "chart"];' in panel_layout
    assert "export function createInitialTiledPanelState" in panel_layout
    assert "export function detectPanelBoundaries" in panel_layout
    assert "export function resizePanelBoundary" in panel_layout
    assert "export function canInsertPanelAtBoundary" in panel_layout
    assert "export function insertPanelAtBoundary" in panel_layout
    assert "export function removePanelSlot" in panel_layout
    assert "export function setPanelContentSymbol" in panel_layout
    assert "export function swapPanelContents" in panel_layout
    assert "export function scaleTiledPanelState" in panel_layout
    assert "export function layoutHasGapsOrOverlaps" in panel_layout
    assert "export function panelSlotStyle" in panel_layout
    assert "export function panelGutter" in panel_layout
    assert "sharedVerticalGuide" in panel_layout
    assert "insertionGuidesForSlot" in panel_layout
    assert "outerGuidesForSlot" not in panel_layout
    assert "chartPageEdgeGuides" in panel_layout
    assert "slotsTouchingPageSide" not in panel_layout
    assert 'interaction: "insert-only"' in panel_layout
    assert 'interaction: "resize"' in panel_layout
    assert 'if (!boundary || boundary.interaction !== "resize")' in panel_layout
    assert "return layoutHasGapsOrOverlaps(next, viewport) ? state : next;" in panel_layout
    assert "function isChartSlot" in panel_layout
    assert 'return state.contents[slot.contentId]?.kind === "chart";' in panel_layout
    assert "insertedKind !== \"chart\" && isChartSlot(state, slot)" in panel_layout
    assert 'kind === "chart" && boundary.pageEdge === "left"' in panel_layout
    assert 'kind === "chart" && boundary.pageEdge === "right"' in panel_layout
    assert 'kind === "chart" && boundary.pageEdge === "bottom"' in panel_layout
    assert 'boundary.pageEdge === "top"' not in panel_layout
    assert 'pageEdge: "bottom"' in panel_layout
    assert "boundaryInsertCapacity" in panel_layout
    assert "sideInsertCapacity" in panel_layout
    assert "pageEdgeExtraShrinkForInsertSlot" in panel_layout
    assert "slotFlushesPageEdge" in panel_layout
    assert "fullHeightPageSideSlotIds" in panel_layout
    assert "rangeStart: workspace.top + gutter" in panel_layout
    assert "rangeEnd: workspaceBottom - gutter" in panel_layout
    assert 'positiveSlotIds: fullHeightPageSideSlotIds(state, workspace, gutter, "left")' in panel_layout
    assert 'negativeSlotIds: fullHeightPageSideSlotIds(state, workspace, gutter, "right")' in panel_layout
    assert "pageEdgeShrinkForSlot" in panel_layout
    assert "const isFlushSlot = slotFlushesPageEdge(slot, side, workspace);" in panel_layout
    assert 'insertedKind === "chart" && !isFlushSlot' in panel_layout
    assert "slot.rect.top < workspace.top + gutter - tolerance" in panel_layout
    assert "const bottomLimit = isChart && almostEqual(slotBottom, rectBottom(workspace)) ? rectBottom(workspace) : rectBottom(workspace) - gutter;" in panel_layout
    assert "slotBottom > bottomLimit + tolerance" in panel_layout
    assert "rectBottom(workspace) - gutter - chartTop" not in panel_layout
    assert "const chartHeight = Math.max(chartMinHeight, rectBottom(workspace) - chartTop);" in panel_layout
    assert "Math.abs(gap - gutter) > tolerance" in panel_layout
    assert "hasHorizontalSlotBetween" in panel_layout
    assert "hasVerticalSlotBetween" in panel_layout
    assert "rect: insertedPanelRect(state, boundary, negative, insertSize, gutter, kind, workspace)" in panel_layout
    assert "horizontalChartInsertBounds" in panel_layout
    assert "chartSlotFromIds" in panel_layout
    assert 'const inheritedChartBounds = kind === "chart" ? horizontalChartInsertBounds(state, boundary) : null;' in panel_layout
    assert "left: inheritedChartBounds.left" in panel_layout
    assert "width: inheritedChartBounds.right - inheritedChartBounds.left" in panel_layout
    assert "chartSlotFromIds(state, boundary.positiveSlotIds) ?? chartSlotFromIds(state, boundary.negativeSlotIds)" in panel_layout
    assert 'const insetLeft = kind !== "chart"' in panel_layout
    assert 'const insetRight = kind !== "chart"' in panel_layout
    assert "removeCoveredPageEdgeGuides" in panel_layout
    assert "expandAdjacentSlotsAfterRemoval" in panel_layout
    assert "minimumCrossSize" in panel_layout
    assert "kind: \"outer\"" in panel_layout
    assert "insertSize + gutter" in panel_layout
    assert "mergeBoundarySegments" in panel_layout
    assert "removableNeighbor" in panel_layout
    assert "movePanelLayout" not in panel_layout
    assert "resizePanelLayout" not in panel_layout
    assert "applyPanelLayoutChange" not in panel_layout
    assert "resolvePanelPush" not in panel_layout
    assert "const [viewportSize, setViewportSize] = useState<ViewportSize>(() => currentViewportSize());" in app
    assert "const [panelState, setPanelState] = useState<TiledPanelState>(() => initialPanelState());" in app
    assert "const [chartHover" not in app
    assert "setChartHover" not in app
    assert "const [treeMapLaneHover, setTreeMapLaneHover] = useState(false);" in app
    assert "const [hoveredChartSlotId, setHoveredChartSlotId] = useState<PanelSlotId | null>(null);" not in app
    assert "const setChartSlotHover = useCallback((slotId: PanelSlotId, hovered: boolean) => {" not in app
    assert "const panelBoundaries = useMemo(() => detectPanelBoundaries(panelState, viewportSize), [panelState, viewportSize]);" not in app
    assert "const activeBoundarySlotIds = useMemo(() => (" not in app
    assert "setPanelState(resizePanelBoundary(drag.startState, drag.boundaryId, delta, viewport));" not in app
    assert "setPanelState((current) => insertPanelAtBoundary(" not in app
    assert "setPanelState((current) => removePanelSlot(current, slotId, viewportSizeRef.current));" not in app
    assert "setPanelState((current) => setPanelContentSymbol(current, contentId, symbol));" not in app
    assert "swapPanelContents(current, drag.sourceSlotId, target.id)" not in app
    assert 'import { PanelWorkspace } from "./components/PanelWorkspace";' in app
    assert "<PanelWorkspace" in app
    assert "setPanelState={setPanelState}" in app
    assert "scaleTiledPanelState(current, previous, next)" in app
    assert "const workspaceStyle = {\n    \"--layout-gutter\": `${layoutGutter}px`" in app
    assert "style={panelSlotStyle(slot)}" not in app
    assert "function currentViewportSize" in app
    assert 'import { BottomCommandBar, type BottomMenuKey, type ChatLogEntry } from "./components/BottomCommandBar";' in app
    assert "<BottomCommandBar" in app
    assert "snapGridSpan(" not in app
    assert 'className="chart-horizontal-resize-grip left"' not in app
    assert 'className="chart-horizontal-resize-grip right"' not in app
    assert 'mainView.mode === "chart" && (' in app
    assert "panelCanResizeHorizontal" not in app
    assert "floatingPanelGridSpan" not in app
    assert "supportPanelLeftGridSpan" not in app
    assert "supportPanelRightGridSpan" not in app
    assert "const rowBottom = Math.max(rowTop + chartSupportPanelMinHeight, chartLane.top - chartSupportPanelGap);" not in app
    assert "const treeMapLaneStyle: CSSProperties = {" in app
    assert "height: treeMapHeight," in app
    assert 'type LayoutDrag =\n  { mode: "treemap"; type: "resize-bottom"; startY: number; startHeight: number };' in app
    assert "treemapSearchGridSpan" not in app
    assert "style={treemapSearchStyle}" not in app
    assert "isChart && slot.rect.left > 1 ? \"has-left-boundary\"" in workspace
    assert "isChart && slot.rect.left + slot.rect.width < viewportSize.width - 1 ? \"has-right-boundary\"" in workspace
    assert "const isDefaultChart = Boolean(content.isDefaultChart);" in workspace
    assert "canClose={!isDefaultChart && !isChart}" in workspace
    assert "canSwap={!isDefaultChart}" in workspace
    assert "isChartHovered={isChart && hoveredChartSlotId === slot.id}" in workspace
    assert "onChartHoverChange={(hovered) => setChartSlotHover(slot.id, hovered)}" in workspace
    assert "onChartSwapPointerDown={!isDefaultChart ? beginPanelSwap(slot.id) : undefined}" in workspace
    assert "boundary.interaction === \"resize\" ? \"can-resize\" : \"is-insert-only\"" in workspace
    assert "boundary.pageEdge ? \"is-page-edge\"" in workspace
    assert "canAdd ? \"has-add\"" in workspace
    assert 'className="panel-move-button"' not in app
    assert "panel-resize-grip" not in app
    assert "renderPanelControls" not in app
    assert "Panel 02" not in app
    assert "Panel 03" not in app
    assert "WorkspacePanelFrame" not in app
    assert "renderPanelContent" not in app
    assert "WorkspacePanelFrame" in workspace
    assert "PanelContentRenderer" in workspace
    assert "renderPanelContent" not in workspace
    assert "workspace-panel-empty" not in app
    assert "workspace-panel-empty" in panel_content
    assert "workspace-panel-surface" in app
    assert "chart-lane-side-shadow" not in app
    assert 'className="panel-add-menu surface-floating"' in workspace
    assert "function boundaryAddMenuPosition" in workspace_geometry
    assert "function clampNumber" in workspace_geometry
    assert "workspace-top-nav" in app
    assert 'className="workspace-top-nav chart"' in app
    assert "workspace-bottom-nav" not in app
    assert "workspace-bottom-nav" in bottom_bar
    assert 'export type BottomMenuKey = "I" | "II" | "III" | "IV" | "V" | "VI";' in bottom_bar
    assert 'const leftMenuKeys: BottomMenuKey[] = ["I", "II", "III"];' in bottom_bar
    assert 'const rightMenuKeys: BottomMenuKey[] = ["IV", "V", "VI"];' in bottom_bar
    assert 'className={`bottom-menu-panel surface-floating ${side} ${isOpen ? "is-open" : ""}`}' in bottom_bar
    assert 'className={`bottom-nav-actions ${side} ${isMenuOpen ? "is-menu-open" : ""}`}' in bottom_bar
    assert 'className="bottom-menu-dismiss-layer"' in bottom_bar
    assert "handleOutsidePointerDown" in bottom_bar
    assert 'event.target.closest(".bottom-menu-panel, .bottom-nav-actions, .bottom-chat-panel, .agent-dock")' in bottom_bar
    assert 'document.addEventListener("pointerdown", handleOutsidePointerDown, true);' in bottom_bar
    assert 'document.removeEventListener("pointerdown", handleOutsidePointerDown, true);' in bottom_bar
    assert 'export type ChatLogEntry = {' in bottom_bar
    assert 'role: "user" | "assistant" | "system";' in bottom_bar
    assert "chatPanelOpen" in bottom_bar
    assert "setChatPanelOpen" in bottom_bar
    assert "const hasFloatingPanel = activeMenu !== null || chatPanelOpen;" in bottom_bar
    assert "const closeFloatingPanels = () => {" in bottom_bar
    assert "const toggleChatPanel = () => {" in bottom_bar
    assert "const toggleBottomMenu = (key: BottomMenuKey) => {" in bottom_bar
    assert "setChatPanelOpen(false);\n    onToggleMenu(key);" in bottom_bar
    assert "if (next && activeMenu) {" in bottom_bar
    assert "{hasFloatingPanel && (" in bottom_bar
    assert 'aria-label="Close bottom floating panel"' in bottom_bar
    assert "ChevronUp" in bottom_bar
    assert "ChevronDown" in bottom_bar
    assert "bottom-chat-panel surface-floating" in bottom_bar
    assert "bottom-chat-log" in bottom_bar
    assert "bottom-chat-message" in bottom_bar
    assert "agent-dock-toggle" in bottom_bar
    assert "activeMenu === label" in bottom_bar
    assert 'aria-label={`Menu ${label}`}' in bottom_bar
    assert "홈화면" in bottom_bar
    assert "onShowTreeMap();" in bottom_bar
    assert "Back to TreeMap" not in app
    assert "Reserved action" not in app
    assert "agent-answer" not in app
    assert "agentMessage" not in app
    assert "const [chatLog, setChatLog] = useState<ChatLogEntry[]>([]);" in app
    assert 'const userEntry = createChatLogEntry("user", prompt);' in app
    assert 'createChatLogEntry("assistant", "차트 에이전트가 차트를 읽고 있습니다.", true)' in app
    assert "replaceChatLogEntry(setChatLog, pendingEntry.id" in app
    assert "chatLog={chatLog}" in app
    assert "agent-box surface-recessed" in bottom_bar
    assert "workspace-nav-button surface-raised" in bottom_bar
    assert "surface-gradient" not in app
    assert "runAgentPrompt" in app
    assert "chartPanelRef" in app
    assert "chartPanelRef" in workspace
    assert "chartPanelRef" in panel_content
    assert "chart-instance is-default-chart" in panel_content
    assert "chart-instance is-editable-chart" in panel_content
    assert "chart-instance-swap-handle" in panel_content
    assert "chart-instance-symbol-search-wrap" in panel_content
    assert "onPointerDown={(event) => event.stopPropagation()}" in panel_content
    assert "chart-instance-symbol-search" in panel_content
    assert "chart-instance-interval" in panel_content
    assert "chartHeaderSnapshot" in panel_content
    assert "setChartHeaders" in workspace
    assert "function chartHeaderEquals" in workspace
    assert "chartHeaderEquals(current[content.id], header)" in workspace
    assert "chartHeaderEquals(current, header) ? current : header" in workspace
    assert "compact" in panel_content
    assert "formatSelectedLabel={(symbolOption) => symbolOption.symbol}" in panel_content
    assert "chart-instance-close" in panel_content
    assert "data-panel-slot-id={slot.id}" in frame
    assert "workspace-panel-nav" in frame
    assert "showNav?: boolean;" in frame
    assert "has-no-panel-nav" in frame
    assert "onPointerDown={canSwap ? onSwapPointerDown?.(slot.id) : undefined}" in frame
    assert "workspace-panel-swap-handle" not in frame
    assert "GripHorizontal" not in frame
    assert "workspace-panel-close" in frame

    assert "--layout-gutter: 24px;" in styles
    assert "--top-nav-height: 68px;" in styles
    assert "--bottom-nav-height: 76px;" in styles
    assert "--bottom-control-size: 44px;" in styles
    assert "--surface-raised-surface: color-mix(in srgb, var(--color-surface) 94%, var(--gops-white));" in styles
    assert "--surface-raised-highlight-shadow: -3px -3px 8px var(--color-border);" in styles
    assert "--surface-raised-depth-shadow: 5px 6px 12px var(--color-shadow);" in styles
    assert "--surface-raised-depth-opacity: 0.16;" in styles
    assert "--surface-floating-depth-shadow: 0 16px 34px var(--color-shadow);" in styles
    assert "--surface-floating-depth-opacity: 0.1;" in styles
    assert "--surface-recessed-shadow: inset 0 0 12px var(--color-shadow);" in styles
    assert "--surface-recessed-opacity: 0.2;" in styles
    assert "--surface-recessed-strong-opacity: 0.3;" in styles
    assert ".surface-flat" in styles
    assert ".surface-raised" in styles
    assert ".surface-gradient" not in styles
    assert ".surface-pressed" not in styles
    assert ".surface-floating" in styles
    assert ".surface-recessed-x" not in styles
    assert ".surface-recessed-y" not in styles
    assert ".workspace-top-nav" in styles
    assert ".workspace-bottom-nav" in styles
    assert ".agent-dock" in styles
    assert ".agent-dock-toggle" in styles
    assert ".bottom-chat-panel" in styles
    assert ".bottom-chat-panel.surface-raised" not in styles
    assert ".bottom-chat-panel::after" in styles
    assert ".bottom-chat-panel.is-open" in styles
    assert ".agent-box .agent-dock-toggle" not in styles
    assert "grid-template-columns: minmax(96px, 1fr) minmax(300px, 520px) minmax(96px, 1fr);" in styles
    assert "width: clamp(360px, calc(100vw - var(--layout-gutter) - var(--layout-gutter) - 176px), 660px);" in styles
    assert "height: clamp(468px, 68vh, 676px);" in styles
    assert "transform: translate(-50%, 104%);" in styles
    assert "transform: translate(-50%, 0);" in styles
    assert ".bottom-chat-log" in styles
    assert ".bottom-chat-message" in styles
    assert ".agent-answer" not in styles
    assert ".bottom-menu-panel {" in styles
    assert "width: clamp(248px, 23vw, 328px);" in styles
    assert "height: clamp(468px, 68vh, 676px);" in styles
    assert "transform 220ms ease" in styles
    assert "background: var(--color-background);" in styles
    assert ".bottom-menu-panel.left {" in styles
    assert ".bottom-menu-panel.right {" in styles
    assert ".bottom-menu-panel.is-open" in styles
    assert ".bottom-nav-actions.is-menu-open .workspace-nav-button:not(.is-active)" in styles
    assert ".bottom-nav-actions.is-menu-open .workspace-nav-button:not(.is-active)::after" in styles
    assert ".bottom-menu-list" in styles
    assert "justify-content: flex-start;" in styles
    assert ".bottom-menu-item" in styles
    assert ".bottom-menu-item.surface-raised" in styles
    assert 'className="bottom-menu-item surface-raised"' in bottom_bar
    assert ".bottom-menu-item::after" not in styles
    assert ".bottom-menu-dismiss-layer" in styles
    assert "width: var(--bottom-control-size);" in styles
    assert "height: var(--bottom-control-size);" in styles
    assert "border-radius: 50%;" in styles
    assert "min-height: var(--bottom-control-size);" in styles
    assert ".bottom-menu-panel::before" not in styles
    assert ".bottom-menu-panel::after" not in styles
    assert ".app-shell::before" in styles
    assert "--paper-fiber-light:" in styles
    assert "--paper-fiber-mid:" in styles
    assert "--paper-fiber-deep:" in styles
    assert "--paper-grain-light:" in styles
    assert "--paper-grain-mid:" in styles
    assert "--paper-grain-deep:" in styles
    assert "--paper-texture-opacity: 0.2;" in styles
    assert "--paper-grain-opacity: 0.18;" in styles
    assert "mix-blend-mode: multiply;" in styles
    assert "background-blend-mode: soft-light, multiply, multiply;" in styles
    assert ".app-shell::after" in styles
    assert "repeating-conic-gradient" not in styles
    assert "repeating-linear-gradient(0deg" in styles
    assert "repeating-linear-gradient(90deg" in styles
    assert "repeating-linear-gradient(7deg" not in styles
    assert "repeating-linear-gradient(128deg" not in styles
    assert "repeating-linear-gradient(154deg" not in styles
    assert "radial-gradient(circle at 18% 22%" not in styles
    assert ".canvas-workspace::before" not in styles
    assert "STARGOPS" not in styles
    assert ".workspace-panel-surface" in styles
    assert "box-shadow: var(--surface-recessed-shadow);" in styles
    assert ".surface-recessed::after {\n  box-shadow: var(--surface-recessed-shadow);\n  opacity: var(--surface-recessed-opacity);" in styles
    assert ".symbol-search:hover::after,\n.symbol-search.is-active::after {\n  opacity: var(--surface-recessed-opacity);" in styles
    assert ".workspace-panel-surface:hover::after,\n.workspace-panel-surface:focus-within::after,\n.workspace-panel-surface.is-chart-hovered::after" in styles
    assert "opacity: var(--surface-recessed-opacity);" in styles
    assert ".workspace-panel-surface.is-boundary-active::after,\n.workspace-panel-surface.is-panel-content-dragging::after" in styles
    assert "opacity: var(--surface-recessed-strong-opacity);" in styles
    assert ".workspace-panel-frame {" in styles
    assert ".workspace-panel-frame.has-no-panel-nav" in styles
    assert ".workspace-panel-nav {" in styles
    assert ".workspace-panel-nav.is-swappable" in styles
    assert ".workspace-panel-swap-handle" not in styles
    assert ".workspace-panel-close" in styles
    assert ".workspace-panel-frame:hover .workspace-panel-close" in styles
    assert ".panel-boundary {" in styles
    assert ".panel-boundary::before" in styles
    assert ".panel-boundary::after" in styles
    assert ".panel-boundary.vertical.has-add::before" in styles
    assert ".panel-boundary.horizontal.has-add::after" in styles
    assert ".panel-boundary-add" in styles
    assert "--boundary-add-clearance: calc(var(--boundary-add-size) / 2 + var(--boundary-add-gap));" in styles
    assert ".panel-boundary-add {\n  position: absolute;" in styles
    assert "transform: translate(-50%, -50%);" in styles
    assert ".panel-boundary-add svg" not in styles
    assert ".panel-boundary-add-glyph" in styles
    assert ".panel-boundary.is-page-edge .panel-boundary-add" in styles
    assert "background: var(--gops-white);" in styles
    assert "box-shadow: inset 0 0 14px var(--color-shadow);" not in styles
    assert "box-shadow: inset 0 0 8px var(--color-shadow);" not in styles
    assert ".panel-add-menu button:hover,\n.panel-add-menu button:focus-visible {\n  background: var(--color-background);" in styles
    assert "background: none;" in styles
    assert ".panel-add-menu" in styles
    assert ".chart-instance-symbol" in styles
    assert "grid-template-columns: 42px 118px;" in styles
    assert ".chart-instance-interval" in styles
    assert ".chart-instance-swap-handle" in styles
    assert "cursor: grab;" in styles
    assert ".chart-instance.is-editable-chart .chart-instance-symbol-search-wrap" in styles
    assert ".chart-instance-symbol-search" in styles
    assert ".chart-instance-close" in styles
    assert ".chart-lane-frame:hover .semantic-expansion-close" in styles
    assert ".chart-lane-side-shadow" not in styles
    assert ".chart-lane-frame::before" not in styles
    assert ".chart-lane-frame {\n  position: absolute;\n  z-index: 1;\n  min-height: 150px;\n  border-radius: 0;" in styles
    assert ".chart-lane-frame.workspace-panel-surface {\n  border-radius: 0;" in styles
    assert ".chart-lane-frame.workspace-panel-surface.has-left-boundary {\n  border-top-left-radius: var(--surface-radius);" in styles
    assert ".chart-lane-frame.workspace-panel-surface.has-right-boundary {\n  border-top-right-radius: var(--surface-radius);" in styles
    assert ".panel-resize-grip {" not in styles
    assert "cursor: ew-resize;" in styles
    assert ".panel-resize-grip::before" not in styles
    assert ".placeholder-panel" not in styles
    assert ".support-panels" not in styles
    assert ".support-panel" not in styles
    assert ".floating-panels" not in styles
    assert ".floating-panel" not in styles
    assert "active ? \"surface-recessed is-active\"" in search
    assert "buttonLabel" not in search
    assert "symbol-search-menu surface-flat surface-recessed" in search
    assert "ma-menu surface-raised" not in panel
    assert "trend-menu surface-raised" not in panel
    assert "semantic-expansion-close surface-raised" not in panel
    assert "agent-preview-controls surface-raised" not in panel
    assert "agent-box surface-raised" not in panel
    assert "function segmentedClass" in panel
    assert "function iconButtonClass" in panel
    assert '"segmented surface-raised surface-pressed active"' not in panel
    assert '"icon-button surface-raised surface-pressed active"' not in panel
    assert 'return active ? "segmented active" : "segmented";' in panel
    assert 'return active ? "icon-button active" : "icon-button";' in panel
    assert "export type ChartPanelHandle" in panel
    assert "useImperativeHandle(ref, () => ({ runAgentPrompt }), [runAgentPrompt]);" in panel
    assert "node tools/verify_layout_grid.mjs" in package


def assert_frontend_treemap_main_view_contract() -> None:
    app = (REPO_ROOT / "frontend/src/App.tsx").read_text()
    workspace = (REPO_ROOT / "frontend/src/components/PanelWorkspace.tsx").read_text()
    bottom_bar = (REPO_ROOT / "frontend/src/components/BottomCommandBar.tsx").read_text()
    panel = (REPO_ROOT / "frontend/src/components/ChartPanel.tsx").read_text()
    search = (REPO_ROOT / "frontend/src/components/SymbolSearch.tsx").read_text()
    treemap = (REPO_ROOT / "frontend/src/treemap/TreeMapCanvas.tsx").read_text()
    layout = (REPO_ROOT / "frontend/src/treemap/treemapLayout.ts").read_text()
    seed = (REPO_ROOT / "frontend/src/market/sp500Universe.seed.ts").read_text()
    styles = (REPO_ROOT / "frontend/src/styles.css").read_text()

    assert "type MainView =" in app
    assert '| { mode: "treemap" }' in app
    assert '| { mode: "chart"; symbol: string };' in app
    assert 'useState<MainView>({ mode: "treemap" })' in app
    assert "<TreeMapCanvas items={sp500UniverseSeed} onSelectSymbol={showChart} />" in app
    assert "activeSymbol={mainView.symbol}" in app
    assert "symbol={content.symbol ?? activeSymbol}" in workspace
    assert "onBackToTreeMap" not in app
    assert "onClick={index === 0 ? showTreeMap : undefined}" not in app
    assert 'aria-label={`Menu ${label}`}' in bottom_bar
    assert "STARGOPS" not in app
    assert "어떤 종목이 궁금하신가요?" not in app
    assert "mainView.mode === \"treemap\"" in app
    assert "mainView.mode === \"chart\"" in app
    assert 'mainView.mode === "chart" && (' in app
    assert 'className="workspace-top-nav chart"' in app
    assert "const treeMapLaneStyle: CSSProperties = {" in app
    assert 'const [treeMapHeight, setTreeMapHeight] = useState(() => initialTreeMapHeight());' in app
    assert "const beginTreeMapResize = (event: ReactPointerEvent<HTMLElement>) => {" in app
    assert 'mode: "treemap",' in app
    assert 'from "./layout/workspaceMetrics";' in app
    assert "const treeMapHoverMetaReserve = 28;" not in app
    assert "function initialPanelState" in app
    assert "createInitialTiledPanelState(currentViewportSize())" in app
    assert "function treeMapMaxHeight" in app

    assert "fetchSymbols" not in panel
    assert "No chart data for" in panel
    assert "function createInitialChart(symbol: string): ChartState" in panel
    assert "symbol: string;" in panel
    assert "symbols: ChartSymbolDto[];" in panel

    assert "function rankSymbolMatches" in search
    assert "function symbolSearchRank" in search
    assert "ticker === query" in search
    assert "submitFirstMatch(event.currentTarget.value)" in search

    assert "layoutSp500TreeMap" in treemap
    assert "hitTestTreeMapTile" in treemap
    assert "tileFillForChange" in treemap
    assert "treemap-hover-meta" in treemap
    assert '"--treemap-hover-meta-left"' in treemap
    assert "Math.min(...symbolTiles.map((tile) => insetTile(tile, tileGap).x))" in treemap
    assert "treemap-hover-card" not in treemap
    assert "drawHover" not in treemap
    assert "otherHovered" not in treemap
    assert "theme.colors.text : tileFillForChange" in treemap
    assert "theme.colors.background : tileTextForChange" in treemap
    assert "theme.sans" not in treemap
    assert "const changeFont = `500 12px ${theme.serif}`;" in treemap
    assert "context.font = `500 ${symbolSize}px ${theme.serif}`;" in treemap
    assert 'return toneForChange(changePercent) === "down" ? theme.colors.down : theme.colors.up;' in treemap
    assert 'serif: root.getPropertyValue("--font-ui-serif").trim() || "\\"Times New Roman\\", Times, Georgia, serif",' in treemap
    assert "context.fillRect(0, 0, size.width, size.height)" not in treemap
    assert "strokeRect(tile.x" not in treemap
    assert "tileStrokeForDepth" not in treemap
    assert "function insetTile" in treemap
    assert "export function layoutSp500TreeMap" in layout
    assert "export function hitTestTreeMapTile" in layout
    assert "export const sp500UniverseSeed" in seed
    assert '"symbol": "TSLA"' in seed
    assert '"symbol": "AAPL"' in seed
    assert '"symbol": "GOOGL"' in seed
    assert 'item.symbol === "TSLA" || item.symbol === "AAPL" || item.symbol === "GOOGL"' in app
    assert "item.symbol === \"MU\"" not in app
    assert "item.symbol === \"KO\"" not in app
    assert "--gops-background: #ece4d0;" in styles
    assert "--color-background: var(--gops-background);" in styles
    assert ".treemap-panel {\n  position: relative;" in styles
    assert "overflow: visible;" in styles
    assert "background: var(--color-background);" in styles
    assert ".treemap-hover-meta {" in styles
    assert "left: var(--treemap-hover-meta-left, 16px);" in styles
    assert "top: calc(100% + 8px);" in styles
    assert "transform: none;" in styles
    assert "treemap-hover-card" not in styles
    assert "treemap-landing-overlay" not in styles
    assert 'className="top-nav-symbol-search"' not in app
    assert "workspace-top-nav treemap" not in styles and ".workspace-top-nav.treemap" not in styles
    assert "treemap-symbol-search" not in styles


def assert_frontend_ontology_contract() -> None:
    odc = (REPO_ROOT / "docs/ODC/ODC-proposal.md").read_text()
    mapper = (REPO_ROOT / "frontend/src/ontology/buildOntologyGraphFromEvidence.ts").read_text()
    types = (REPO_ROOT / "frontend/src/ontology/ontologyTypes.ts").read_text()
    client = (REPO_ROOT / "frontend/src/ontology/ontologyReportClient.ts").read_text()
    panel = (REPO_ROOT / "frontend/src/ontology/OntologyPanel.tsx").read_text()
    graph = (REPO_ROOT / "frontend/src/ontology/OntologyGraphView.tsx").read_text()
    panel_content = (REPO_ROOT / "frontend/src/components/PanelContentRenderer.tsx").read_text()
    vite = (REPO_ROOT / "frontend/vite.config.ts").read_text()
    package = (REPO_ROOT / "package.json").read_text()
    styles = (REPO_ROOT / "frontend/src/styles.css").read_text()

    assert "GraphDB, SPARQL, zip" in odc
    assert "AnalysisReport.providerEvidence" in odc
    assert '`provider === "ontology"`' in odc
    assert '`status === "available"`' in odc
    assert "관계 분석 결과가 아직 없습니다" in odc
    for relation_type in ("theme", "theme-company", "control", "theme-control", "shared-theme", "cross-control"):
        assert relation_type in odc
        assert f'relationType === "{relation_type}"' in mapper

    assert "export type AgentEvidenceItem" in types
    assert "export type AnalysisReport" in types
    assert "export type OntologyGraphData" in types
    assert "item.provider !== \"ontology\" || item.status !== \"available\"" in mapper
    assert "ensureSymbolNode(symbol);" in mapper
    assert "ticker -> themeName" not in mapper
    assert "NVDA" not in mapper
    assert "VITE_ONTOLOGY_REPORT_URL" in client
    assert 'const defaultOntologyReportUrl = "/api/agent-analysis/run";' in client
    assert '"/api/agent-analysis": agentTarget' in vite
    assert "normalizeAnalysisReport" in client
    assert "<OntologyPanel symbol={symbol} />" in panel_content
    assert "buildOntologyGraphFromEvidence(evidence, normalizedSymbol)" in panel
    assert "관계 분석 결과가 아직 없습니다" in panel
    assert "OntologyGraphView graph={graph}" in panel
    assert "viewBox={`0 0 ${graphWidth} ${graphHeight}`}" in graph
    assert "d3" not in graph.lower()
    assert "node tools/verify_ontology_graph.mjs" in package
    assert ".ontology-panel" in styles
    assert ".ontology-graph-svg" in styles
    assert ".ontology-node.is-active circle" in styles


def assert_frontend_palette_contract() -> None:
    allowed_hex = {
        "#000000",
        "#ffffff",
        "#343532",
        "#ece4d0",
        "#66461c",
        "#9a7038",
        "#463b61",
        "#776b91",
        "#6ed65b",
        "#04915b",
        "#b00001",
        "#620101",
        "#3d4a08",
        "#788447",
    }
    checked_paths = [
        *sorted((REPO_ROOT / "frontend/src").rglob("*.css")),
        *sorted((REPO_ROOT / "frontend/src").rglob("*.ts")),
        *sorted((REPO_ROOT / "frontend/src").rglob("*.tsx")),
        *sorted((REPO_ROOT / "agent_backend/app").rglob("*.py")),
    ]
    for path in checked_paths:
        source = path.read_text()
        for match in re.findall(r"#[0-9a-fA-F]{3,8}", source):
            assert match.lower() in allowed_hex, f"Unexpected color {match} in {path}"
        lowered = source.lower()
        assert "rgba(" not in lowered, f"rgba() color found in {path}"
        assert "rgb(" not in lowered, f"rgb() color found in {path}"
        assert "hsl(" not in lowered, f"hsl() color found in {path}"
        assert "hsla(" not in lowered, f"hsla() color found in {path}"
        assert "transparent" not in lowered, f"transparent color found in {path}"
        assert "data:image" not in lowered, f"data image color texture found in {path}"

    styles = (REPO_ROOT / "frontend/src/styles.css").read_text()
    theme = (REPO_ROOT / "frontend/src/theme/colors.ts").read_text()
    chart_canvas = (REPO_ROOT / "frontend/src/chart/ChartCanvas.tsx").read_text()
    treemap_colors = (REPO_ROOT / "frontend/src/treemap/treemapColors.ts").read_text()
    drawings = (REPO_ROOT / "frontend/src/chart/drawings.ts").read_text()
    agent = (REPO_ROOT / "agent_backend/app/main.py").read_text()
    assert "--gops-background: #ece4d0;" in styles
    assert "--color-surface: var(--gops-background);" in styles
    assert "--color-muted: var(--gops-ink);" in styles
    assert "--color-border: color-mix(in srgb, var(--gops-background) 68%, var(--gops-white));" in styles
    assert "--color-shadow: var(--gops-ink);" in styles
    assert "--gops-umber: #66461c;" in styles
    assert "--gops-umber-soft: #9a7038;" in styles
    assert "--gops-violet: #463b61;" in styles
    assert "--gops-violet-soft: #776b91;" in styles
    assert "--gops-crimson: #6ed65b;" in styles
    assert "--gops-crimson-soft: #04915b;" in styles
    assert "--gops-teal: #b00001;" in styles
    assert "--gops-teal-soft: #620101;" in styles
    assert "--gops-moss: #3d4a08;" in styles
    assert "--gops-moss-soft: #788447;" in styles
    assert "--color-up: var(--gops-crimson);" in styles
    assert "--color-up-soft: var(--gops-crimson-soft);" in styles
    assert "--color-down: var(--gops-teal);" in styles
    assert "--color-down-soft: var(--gops-teal-soft);" in styles
    assert "--color-change-up: var(--gops-crimson-soft);" in styles
    assert "--color-change-down: var(--gops-teal-soft);" in styles
    assert "--gops-brown" not in styles
    assert "--gops-gold" not in styles
    assert "--gops-purple" not in styles
    assert "--gops-lavender" not in styles
    assert "--gops-red" not in styles
    assert "--gops-rose" not in styles
    assert "--gops-green" not in styles
    assert "--gops-mint" not in styles
    assert "--gops-olive" not in styles
    assert "--gops-sage" not in styles
    assert "--color-ma5: var(--gops-ink);" in styles
    assert "--color-ma20: var(--gops-ink);" in styles
    assert "--color-ma60: var(--gops-ink);" in styles
    assert "--color-preview: var(--gops-ink);" in styles
    assert "--color-footprint: var(--gops-ink);" in styles
    assert "--color-grid: var(--gops-ink);" in styles
    assert "--color-axis: var(--gops-ink);" in styles
    assert "--color-tile-text: var(--gops-ink);" in styles
    assert "--color-tile-text-inverse: var(--gops-background);" in styles
    assert "readThemeColors" in theme
    assert "resolveRawPaletteColor" in theme
    assert "readThemeColors()" in chart_canvas
    assert "function movingAverageAlpha" in chart_canvas
    assert "context.lineWidth = 1.05;" in chart_canvas
    assert "function drawExpansionSideShadow" in chart_canvas
    assert "const sideWidth = Math.min(6, Math.max(2, rangeWidth / 7));" in chart_canvas
    assert "context.globalAlpha = Math.max(0.008, 0.036 - index * 0.005);" in chart_canvas
    assert "colorToken" in drawings and "fillToken" in drawings and "fillOpacity" in drawings
    assert "colorToken" in agent and "fillToken" in agent and "textToken" in agent
    assert "hue" not in treemap_colors
    assert "saturation" not in treemap_colors
    assert "export function tileOpacityForChange" in treemap_colors
    assert "return change > 0 ? theme.upSoft : theme.downSoft;" in treemap_colors


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
    assert "trend-menu surface-raised" not in panel
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
        "panel": {"symbol": "TSLA", "interval": "1m", "visibleCount": 120, "rightOffset": 0},
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
                    "anchors": [{"timestamp": "2026-02-06T00:00:00Z", "price": 120, "symbol": "TSLA"}],
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
