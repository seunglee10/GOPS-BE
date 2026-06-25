import { Eye, EyeOff, ListRestart, Minus, Plus, SlidersHorizontal, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { DEFAULT_INDICATOR_PRESETS } from "../calculations/indicators";
import { defaultIndicatorRegistry } from "../calculations/indicatorRegistry";
import { defaultChartToolRegistry } from "../registries/chartToolRegistry";
import { createCommandId, nowIso } from "../state/commandEngine";
import type { CalculationInputs, IndicatorType } from "../types/calculations";
import type { Command, CommandTarget } from "../types/commands";
import {
  DEFAULT_VISIBLE_BARS,
  DEFAULT_WATCHLIST,
  DOCUMENT_LIMITS,
  type ChartDocument,
  type ChartToolMode,
  type ComparisonSeriesLayer,
  type DrawingLayer,
  type HorizontalLineDrawing,
  type IndicatorLayer,
  type PaneDocument,
  type PanelDocument,
  type PinMode
} from "../types/documents";
import type { Timeframe } from "../types/market";

interface ChartPanelToolsProps {
  chart: ChartDocument;
  chartPanel: PanelDocument;
  latestPrice: number;
  onDispatch(command: Command): boolean;
}

const INDICATORS: IndicatorType[] = ["SMA", "EMA", "RSI", "MACD", "BOLLINGER_BANDS", "VWAP", "ATR", "VOLUME_MA"];
const TIMEFRAMES: Timeframe[] = ["1s", "5s", "15s", "1m", "5m", "15m", "1h", "1d"];
const COLORS = ["#f59e0b", "#38bdf8", "#a78bfa", "#f472b6", "#22c55e", "#ef4444"];
const PRICE_SOURCES = ["close", "open", "high", "low"] as const;
const VWAP_RESETS = ["session", "visibleRange"] as const;

type HorizontalLineLayer = DrawingLayer & { drawing: HorizontalLineDrawing };

export function ChartPanelTools({ chart, chartPanel, latestPrice, onDispatch }: ChartPanelToolsProps): JSX.Element {
  const [indicatorType, setIndicatorType] = useState<IndicatorType>("SMA");
  const [comparisonSymbol, setComparisonSymbol] = useState(DEFAULT_WATCHLIST.find((symbol) => symbol !== chart.symbol) ?? "MSFT");
  const [linePrice, setLinePrice] = useState(() => String(latestPrice || 100));
  const [lineLabel, setLineLabel] = useState("Manual level");
  const [lineColor, setLineColor] = useState("#38bdf8");

  const indicatorLayers = chart.layers.filter((layer): layer is IndicatorLayer => layer.type === "indicator");
  const comparisonLayers = chart.layers.filter((layer): layer is ComparisonSeriesLayer => layer.type === "comparisonSeries");
  const drawingLayers = chart.layers.filter(
    (layer): layer is HorizontalLineLayer => layer.type === "drawing" && layer.drawing.kind === "horizontalLine"
  );
  const removableLayers = chart.layers.filter((layer) => layer.type === "indicator" || layer.type === "comparisonSeries" || layer.type === "drawing");
  const comparisonOptions = useMemo(
    () => DEFAULT_WATCHLIST.filter((symbol) => symbol !== chart.symbol && !comparisonLayers.some((layer) => layer.symbol === symbol)),
    [chart.symbol, comparisonLayers]
  );
  const comparisonLimit = DOCUMENT_LIMITS.maxComparisonSymbolsPerChart;
  const comparisonAtLimit = comparisonLayers.length >= comparisonLimit;
  const comparisonAddDisabled = comparisonAtLimit || comparisonOptions.length === 0 || !comparisonSymbol;
  const toolMode = chartPanel.config && "toolMode" in chartPanel.config ? chartPanel.config.toolMode : "select";
  const showCrosshair = chartPanel.config && "showCrosshair" in chartPanel.config ? chartPanel.config.showCrosshair : true;

  useEffect(() => {
    if (comparisonOptions.length === 0) {
      if (comparisonSymbol !== "") setComparisonSymbol("");
      return;
    }
    if (!comparisonOptions.includes(comparisonSymbol)) {
      setComparisonSymbol(comparisonOptions[0]);
    }
  }, [comparisonOptions, comparisonSymbol]);

  function target(paneId = "pane-price", layerId?: string): CommandTarget {
    return {
      workspaceId: "workspace-main",
      panelId: chartPanel.id,
      chartId: chart.id,
      paneId,
      layerId
    };
  }

  function dispatch(command: Omit<Command, "id" | "actor" | "status" | "createdAt">): boolean {
    return onDispatch({
      ...command,
      id: createCommandId(),
      actor: "user",
      status: "applied",
      createdAt: nowIso()
    } as Command);
  }

  function addIndicator(): void {
    const nodeId = `calc-${indicatorType.toLowerCase()}-${crypto.randomUUID().slice(0, 8)}`;
    const renderMode = indicatorType === "BOLLINGER_BANDS" ? "band" : "line";
    const paneTarget = paneForIndicator(indicatorType);
    dispatch({
      type: "chart.indicator.add",
      target: target(),
      payload: {
        pane: paneTarget.pane,
        node: {
          id: nodeId,
          type: indicatorType,
          inputs: { ...DEFAULT_INDICATOR_PRESETS[indicatorType] },
          outputKey: `${indicatorType.toLowerCase()}-${nodeId.slice(-4)}`
        },
        layer: {
          id: `layer-indicator-${crypto.randomUUID().slice(0, 8)}`,
          type: "indicator",
          owner: "user",
          paneId: paneTarget.paneId,
          zIndex: 220,
          visible: true,
          locked: false,
          style: { color: COLORS[indicatorLayers.length % COLORS.length], lineWidth: 2 },
          calculationNodeId: nodeId,
          renderMode,
          createdAt: nowIso(),
          updatedAt: nowIso()
        }
      }
    });
  }

  function paneForIndicator(type: IndicatorType): { paneId: string; pane?: PaneDocument } {
    const definition = defaultIndicatorRegistry[type];
    if (definition.preferredPane === "price") return { paneId: "pane-price" };
    if (definition.preferredPane === "volume") return { paneId: "pane-volume" };
    const existing = chart.panes.find((pane) => pane.kind === "indicator" && pane.title === definition.label);
    if (existing) return { paneId: existing.id };
    const paneId = `pane-indicator-${type.toLowerCase()}`;
    return {
      paneId,
      pane: {
        id: paneId,
        kind: "indicator",
        title: definition.label,
        order: Math.max(...chart.panes.map((pane) => pane.order), 0) + 1,
        heightRatio: 0.22,
        minHeightPx: 120,
        yScale: {
          scaleId: `scale-${paneId}-right`,
          mode: type === "RSI" ? "oscillator" : "custom",
          position: "right",
          autoScale: type !== "RSI",
          min: type === "RSI" ? 0 : undefined,
          max: type === "RSI" ? 100 : undefined
        },
        visible: true
      }
    };
  }

  function updateIndicator(layer: IndicatorLayer, patch: CalculationInputs): void {
    const node = chart.calculationGraph.nodes.find((item) => item.id === layer.calculationNodeId);
    if (!node) return;
    dispatch({
      type: "chart.indicator.update",
      target: target(layer.paneId, layer.id),
      payload: {
        calculationNodeId: node.id,
        inputs: { ...node.inputs, ...patch },
        layerPatch: { updatedAt: nowIso() }
      }
    });
  }

  function removeIndicator(layer: IndicatorLayer): void {
    dispatch({
      type: "chart.indicator.remove",
      target: target(layer.paneId, layer.id),
      payload: { calculationNodeId: layer.calculationNodeId, layerId: layer.id }
    });
  }

  function addComparison(): void {
    if (comparisonAddDisabled) return;
    dispatch({
      type: "chart.comparison.add",
      target: target(),
      payload: {
        layer: {
          id: `layer-comparison-${comparisonSymbol.toLowerCase()}-${crypto.randomUUID().slice(0, 6)}`,
          type: "comparisonSeries",
          owner: "user",
          paneId: "pane-price",
          zIndex: 180,
          visible: true,
          locked: false,
          style: { color: COLORS[(comparisonLayers.length + 2) % COLORS.length], lineWidth: 1.7 },
          symbol: comparisonSymbol,
          baselineMode: "firstVisibleCompleteBar",
          normalization: "percentFromFirstVisibleCompleteBar",
          renderMode: "line",
          createdAt: nowIso(),
          updatedAt: nowIso()
        }
      }
    });
  }

  function addHorizontalLine(): void {
    const price = Number(linePrice);
    if (!Number.isFinite(price)) return;
    dispatch({
      type: "chart.drawing.add",
      target: target(),
      payload: {
        layer: {
          id: `layer-drawing-${crypto.randomUUID().slice(0, 8)}`,
          type: "drawing",
          owner: "user",
          paneId: "pane-price",
          zIndex: 300,
          visible: true,
          locked: false,
          style: { color: lineColor, lineWidth: 1.5 },
          drawing: { kind: "horizontalLine", price, label: lineLabel },
          createdAt: nowIso(),
          updatedAt: nowIso()
        }
      }
    });
  }

  function updateHorizontalLine(layer: HorizontalLineLayer, patch: { price?: number; label?: string; color?: string; visible?: boolean }): void {
    dispatch({
      type: "chart.drawing.update",
      target: target(layer.paneId, layer.id),
      payload: {
        layerId: layer.id,
        drawing: {
          kind: "horizontalLine",
          price: patch.price ?? ("price" in layer.drawing ? layer.drawing.price : latestPrice),
          label: patch.label ?? layer.drawing.label
        },
        style: { ...layer.style, color: patch.color ?? layer.style.color },
        visible: patch.visible ?? layer.visible
      }
    });
  }

  function removeLayer(layerId: string): void {
    const layer = chart.layers.find((item) => item.id === layerId);
    if (!layer) return;
    if (layer.type === "indicator") removeIndicator(layer);
    if (layer.type === "comparisonSeries") {
      dispatch({ type: "chart.comparison.remove", target: target(layer.paneId, layer.id), payload: { layerId: layer.id } });
    }
    if (layer.type === "drawing") {
      dispatch({ type: "chart.drawing.remove", target: target(layer.paneId, layer.id), payload: { layerId: layer.id } });
    }
  }

  function setLayerVisible(layerId: string, visible: boolean): void {
    const layer = chart.layers.find((item) => item.id === layerId);
    dispatch({
      type: "chart.layer.visibility.set",
      target: target(layer?.paneId ?? "pane-price", layerId),
      payload: { layerId, visible }
    });
  }

  function resetViewport(): void {
    dispatch({
      type: "chart.viewport.set",
      target: target(),
      payload: { mode: "followRealtime", visibleBars: DEFAULT_VISIBLE_BARS, rightOffsetBars: 0, from: undefined, to: undefined }
    });
  }

  function setPinMode(pinMode: PinMode): void {
    dispatch({
      type: "panel.pinMode.set",
      target: target(),
      payload: { panelId: chartPanel.id, pinMode }
    });
  }

  function setToolMode(nextToolMode: ChartToolMode): void {
    dispatch({
      type: "panel.chartTool.set",
      target: target(),
      payload: { panelId: chartPanel.id, toolMode: nextToolMode }
    });
  }

  function setShowCrosshair(nextShowCrosshair: boolean): void {
    dispatch({
      type: "panel.crosshair.set",
      target: target(),
      payload: { panelId: chartPanel.id, showCrosshair: nextShowCrosshair }
    });
  }

  return (
    <div className="chart-tools-panel">
      <div className="panel-header">
        <h2>Chart Tools</h2>
        <SlidersHorizontal size={16} />
      </div>
      <div className="chart-tools-scroll">
        <section className="tool-section">
          <h3>Chart</h3>
          <div className="tool-mode-group" aria-label="Chart tool mode">
            {(Object.keys(defaultChartToolRegistry) as ChartToolMode[]).map((mode) => (
              <button
                key={mode}
                type="button"
                className={mode === toolMode ? "active" : ""}
                onClick={() => setToolMode(mode)}
                title={defaultChartToolRegistry[mode].label}
              >
                <span>{defaultChartToolRegistry[mode].label}</span>
              </button>
            ))}
          </div>
          <div className="tool-grid two">
            <label>
              <span>Symbol</span>
              <select
                value={chart.symbol}
                onChange={(event) =>
                  dispatch({ type: "chart.symbol.set", target: target(), payload: { symbol: event.target.value } })
                }
              >
                {DEFAULT_WATCHLIST.map((symbol) => (
                  <option key={symbol} value={symbol}>
                    {symbol}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Timeframe</span>
              <select
                value={chart.timeframe}
                onChange={(event) =>
                  dispatch({ type: "chart.timeframe.set", target: target(), payload: { timeframe: event.target.value as Timeframe } })
                }
              >
                {TIMEFRAMES.map((timeframe) => (
                  <option key={timeframe} value={timeframe}>
                    {timeframe}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="tool-grid two">
            <label>
              <span>Pin mode</span>
              <select value={chartPanel.pinMode} onChange={(event) => setPinMode(event.target.value as PinMode)}>
                <option value="locked">locked</option>
                <option value="approval">approval</option>
                <option value="auto">auto</option>
              </select>
            </label>
            <label>
              <span>Crosshair</span>
              <input type="checkbox" checked={showCrosshair} onChange={(event) => setShowCrosshair(event.target.checked)} />
            </label>
          </div>
          <div className="tool-grid two">
            <button type="button" onClick={resetViewport}>
              <ListRestart size={15} />
              <span>Reset View</span>
            </button>
          </div>
        </section>

        <section className="tool-section">
          <h3>Indicators</h3>
          <div className="tool-grid add-row">
            <select value={indicatorType} onChange={(event) => setIndicatorType(event.target.value as IndicatorType)}>
              {INDICATORS.map((type) => (
                <option key={type} value={type}>
                  {type}
                </option>
              ))}
            </select>
            <button type="button" onClick={addIndicator}>
              <Plus size={15} />
              <span>Add</span>
            </button>
          </div>
          {indicatorLayers.map((layer) => {
            const node = chart.calculationGraph.nodes.find((item) => item.id === layer.calculationNodeId);
            return (
              <div className="tool-item" key={layer.id}>
                <div>
                  <strong>{node?.type ?? "Indicator"}</strong>
                  <small>{layer.id}</small>
                </div>
                {node ? <IndicatorInputs inputs={node.inputs} type={node.type} onChange={(patch) => updateIndicator(layer, patch)} /> : null}
                <LayerButtons layerId={layer.id} visible={layer.visible} onVisible={setLayerVisible} onRemove={() => removeIndicator(layer)} />
              </div>
            );
          })}
        </section>

        <section className="tool-section">
          <h3>Comparisons ({comparisonLayers.length}/{comparisonLimit})</h3>
          <div className="tool-grid add-row">
            <select
              value={comparisonSymbol}
              onChange={(event) => setComparisonSymbol(event.target.value)}
              disabled={comparisonAtLimit || comparisonOptions.length === 0}
            >
              {comparisonOptions.map((symbol) => (
                <option key={symbol} value={symbol}>
                  {symbol}
                </option>
              ))}
            </select>
            <button type="button" onClick={addComparison} disabled={comparisonAddDisabled}>
              <Plus size={15} />
              <span>Add</span>
            </button>
          </div>
          {comparisonLayers.map((layer) => (
            <div className="tool-item compact" key={layer.id}>
              <strong>{layer.symbol}</strong>
              <LayerButtons
                layerId={layer.id}
                visible={layer.visible}
                onVisible={setLayerVisible}
                onRemove={() => dispatch({ type: "chart.comparison.remove", target: target(layer.paneId, layer.id), payload: { layerId: layer.id } })}
              />
            </div>
          ))}
        </section>

        <section className="tool-section">
          <h3>Horizontal Lines</h3>
          <div className="tool-grid line-add">
            <input value={linePrice} onChange={(event) => setLinePrice(event.target.value)} aria-label="Line price" />
            <input value={lineLabel} onChange={(event) => setLineLabel(event.target.value)} aria-label="Line label" />
            <select value={lineColor} onChange={(event) => setLineColor(event.target.value)} aria-label="Line color">
              {COLORS.map((color) => (
                <option key={color} value={color}>
                  {color}
                </option>
              ))}
            </select>
            <button type="button" onClick={addHorizontalLine}>
              <Plus size={15} />
              <span>Add</span>
            </button>
          </div>
          {drawingLayers.map((layer) => (
            <div className="tool-item" key={layer.id}>
              <strong>{layer.drawing.label || "Horizontal line"}</strong>
              <div className="tool-grid three">
                <input
                  defaultValue={layer.drawing.price}
                  aria-label={`Price ${layer.id}`}
                  onBlur={(event) => updateHorizontalLine(layer, { price: Number(event.target.value) })}
                />
                <input
                  defaultValue={layer.drawing.label ?? ""}
                  aria-label={`Label ${layer.id}`}
                  onBlur={(event) => updateHorizontalLine(layer, { label: event.target.value })}
                />
                <select
                  value={layer.style.color ?? "#38bdf8"}
                  aria-label={`Color ${layer.id}`}
                  onChange={(event) => updateHorizontalLine(layer, { color: event.target.value })}
                >
                  {COLORS.map((color) => (
                    <option key={color} value={color}>
                      {color}
                    </option>
                  ))}
                </select>
              </div>
              <LayerButtons layerId={layer.id} visible={layer.visible} onVisible={setLayerVisible} onRemove={() => removeLayer(layer.id)} />
            </div>
          ))}
        </section>

        <section className="tool-section">
          <h3>Layers</h3>
          {chart.layers.map((layer) => (
            <div className="layer-row" key={layer.id}>
              <span>{layerLabel(layer)}</span>
              <div>
                <button type="button" onClick={() => setLayerVisible(layer.id, !layer.visible)} title={layer.visible ? "Hide" : "Show"}>
                  {layer.visible ? <Eye size={14} /> : <EyeOff size={14} />}
                </button>
                {removableLayers.some((item) => item.id === layer.id) ? (
                  <button type="button" onClick={() => removeLayer(layer.id)} title="Remove">
                    <Trash2 size={14} />
                  </button>
                ) : null}
              </div>
            </div>
          ))}
        </section>
      </div>
    </div>
  );
}

function IndicatorInputs({
  type,
  inputs,
  onChange
}: {
  type: IndicatorType;
  inputs: CalculationInputs;
  onChange(patch: CalculationInputs): void;
}): JSX.Element {
  const fields = indicatorFields(type);
  return (
    <div className="tool-grid indicator-inputs">
      {fields.map((field) => (
        <label key={field}>
          <span>{field}</span>
          {field === "source" ? (
            <select value={String(inputs[field] ?? "close")} onChange={(event) => onChange({ [field]: event.target.value })}>
              {PRICE_SOURCES.map((source) => (
                <option key={source} value={source}>
                  {source}
                </option>
              ))}
            </select>
          ) : field === "reset" ? (
            <select value={String(inputs[field] ?? "session")} onChange={(event) => onChange({ [field]: event.target.value })}>
              {VWAP_RESETS.map((reset) => (
                <option key={reset} value={reset}>
                  {reset}
                </option>
              ))}
            </select>
          ) : (
            <input
              type="number"
              min={field.endsWith("Period") || field === "period" ? 1 : undefined}
              step={field === "standardDeviation" ? 0.1 : 1}
              defaultValue={String(inputs[field] ?? "")}
              onBlur={(event) => {
                const value = Number(event.target.value);
                if (!Number.isFinite(value)) return;
                onChange({ [field]: value });
              }}
            />
          )}
        </label>
      ))}
    </div>
  );
}

function indicatorFields(type: IndicatorType): string[] {
  if (type === "MACD") return ["fastPeriod", "slowPeriod", "signalPeriod"];
  if (type === "BOLLINGER_BANDS") return ["period", "standardDeviation"];
  if (type === "VWAP") return ["reset"];
  if (type === "RSI") return ["period"];
  if (type === "ATR" || type === "VOLUME_MA") return ["period"];
  return ["source", "period"];
}

function LayerButtons({
  layerId,
  visible,
  onVisible,
  onRemove
}: {
  layerId: string;
  visible: boolean;
  onVisible(layerId: string, visible: boolean): void;
  onRemove(): void;
}): JSX.Element {
  return (
    <div className="tool-actions">
      <button type="button" onClick={() => onVisible(layerId, !visible)} title={visible ? "Hide" : "Show"}>
        {visible ? <Eye size={14} /> : <EyeOff size={14} />}
      </button>
      <button type="button" onClick={onRemove} title="Remove">
        <Minus size={14} />
      </button>
    </div>
  );
}

function layerLabel(layer: ChartDocument["layers"][number]): string {
  if (layer.type === "priceSeries") return "Price candles";
  if (layer.type === "volume") return "Volume";
  if (layer.type === "indicator") return `Indicator ${layer.calculationNodeId}`;
  if (layer.type === "comparisonSeries") return `Compare ${layer.symbol}`;
  if (layer.type === "drawing") return layer.drawing.kind === "horizontalLine" ? layer.drawing.label || "Horizontal line" : layer.drawing.kind;
  return "Proposal preview";
}
