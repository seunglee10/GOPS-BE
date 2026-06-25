import { Wifi, WifiOff } from "lucide-react";
import type { CalculationOutput, MarketSummary } from "../types/calculations";
import type { Command } from "../types/commands";
import type { ChartDocument, ChartViewport, PanelDocument } from "../types/documents";
import type { StreamStatus } from "../types/market";
import type { CandlesBySymbol } from "../market/candleStore";
import { ChartPanelTools } from "./ChartPanelTools";
import { ChartCanvas } from "../renderer/ChartCanvas";
import { Toolbar } from "./Toolbar";

interface ChartPanelProps {
  chart: ChartDocument;
  renderChart?: ChartDocument;
  chartPanel: PanelDocument;
  candlesBySymbol: CandlesBySymbol;
  calculationOutputs: Record<string, CalculationOutput>;
  streamStatus: StreamStatus;
  streamError: string;
  marketSummary: MarketSummary;
  latestPrice: number;
  onDispatch(command: Command): boolean;
  onViewportChange(patch: Partial<ChartViewport>): void;
  onSymbolChange(symbol: string): void;
  onTimeframeChange(timeframe: ChartDocument["timeframe"]): void;
}

export function ChartPanel({
  chart,
  renderChart,
  chartPanel,
  candlesBySymbol,
  calculationOutputs,
  streamStatus,
  streamError,
  marketSummary,
  latestPrice,
  onDispatch,
  onViewportChange,
  onSymbolChange,
  onTimeframeChange
}: ChartPanelProps): JSX.Element {
  const canvasChart = renderChart ?? chart;
  const toolMode = chartPanel.config && "toolMode" in chartPanel.config ? chartPanel.config.toolMode : "select";
  const showCrosshair = chartPanel.config && "showCrosshair" in chartPanel.config ? chartPanel.config.showCrosshair : true;
  const live = streamStatus === "live";
  const visibleChangeTitle = marketSummary.visibleChangeBaseTimestamp
    ? `Visible change from ${marketSummary.visibleChangeBaseTimestamp} close ${marketSummary.visibleChangeBaseClose?.toFixed(2)}`
    : "Visible change from first visible candle close";
  return (
    <div className="panel chart-panel">
      <div className="panel-header">
        <Toolbar symbol={chart.symbol} timeframe={chart.timeframe} onSymbolChange={onSymbolChange} onTimeframeChange={onTimeframeChange} />
        <div className="chart-stats">
          <span>{marketSummary.latestPrice ? marketSummary.latestPrice.toFixed(2) : "--"}</span>
          <span className={marketSummary.changePercentFromFirstVisible >= 0 ? "positive" : "negative"} title={visibleChangeTitle}>
            Visible {marketSummary.changePercentFromFirstVisible.toFixed(2)}%
          </span>
          {typeof marketSummary.liveChangePercent === "number" ? (
            <span className={marketSummary.liveChangePercent >= 0 ? "positive" : "negative"} title="Live candle change vs previous candle close">
              Live {marketSummary.liveChangePercent.toFixed(2)}%
            </span>
          ) : null}
          <span className={`live-dot ${live ? "is-live" : ""}`} title={streamError || streamStatus}>
            {live ? <Wifi size={15} /> : <WifiOff size={15} />}
          </span>
        </div>
      </div>
      <div className="chart-panel-body">
        <ChartCanvas
          chart={canvasChart}
          chartPanel={chartPanel}
          toolMode={toolMode}
          showCrosshair={showCrosshair}
          candlesBySymbol={candlesBySymbol}
          calculationOutputs={calculationOutputs}
          onDispatch={onDispatch}
          onViewportChange={onViewportChange}
        />
        <ChartPanelTools chart={chart} chartPanel={chartPanel} latestPrice={latestPrice} onDispatch={onDispatch} />
      </div>
    </div>
  );
}
