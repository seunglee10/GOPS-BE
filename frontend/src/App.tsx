import { MoveVertical } from "lucide-react";
import { PointerEvent as ReactPointerEvent, useEffect, useMemo, useRef, useState } from "react";
import { ChartPanel } from "./components/ChartPanel";
import { PlaceholderPanel } from "./components/PlaceholderPanel";
import { SymbolSearch } from "./components/SymbolSearch";
import type { SemanticSelectionSnapshot } from "./chart/semanticTimeline";
import type { ChartSymbolDto } from "./chart/types";
import { sp500UniverseSeed } from "./market/sp500Universe.seed";
import { TreeMapCanvas } from "./treemap/TreeMapCanvas";

type MainView =
  | { mode: "treemap" }
  | { mode: "chart"; symbol: string };

type ChartLaneLayout = {
  top: number;
  height: number;
};

type LayoutDrag =
  | { type: "move"; startY: number; startTop: number; startHeight: number }
  | { type: "resize-top"; startY: number; startTop: number; startHeight: number }
  | { type: "resize-bottom"; startY: number; startTop: number; startHeight: number };

function initialLaneLayout(): ChartLaneLayout {
  if (typeof window === "undefined") {
    return { top: 194, height: 332 };
  }
  const height = chartMaxHeight(window.innerHeight);
  return clampChartLayout({
    top: Math.round((window.innerHeight - height) / 2),
    height
  });
}

export function App() {
  const [mainView, setMainView] = useState<MainView>({ mode: "treemap" });
  const [chartLane, setChartLane] = useState<ChartLaneLayout>(() => initialLaneLayout());
  const [semanticSelection, setSemanticSelection] = useState<SemanticSelectionSnapshot | null>(null);
  const [chartHover, setChartHover] = useState(false);
  const [gripHover, setGripHover] = useState<"top" | "bottom" | null>(null);
  const dragRef = useRef<LayoutDrag | null>(null);
  const laneCanMove = canMoveChartLayout(chartLane);
  const laneCanResize = canResizeChartLayout();

  useEffect(() => {
    const applyDrag = (clientY: number) => {
      const drag = dragRef.current;
      if (!drag) {
        return;
      }
      const deltaY = clientY - drag.startY;
      if (drag.type === "move") {
        if (!canMoveChartLayout({ top: drag.startTop, height: drag.startHeight })) {
          return;
        }
        setChartLane(clampChartLayout({ top: drag.startTop + deltaY, height: drag.startHeight }));
        return;
      }
      if (drag.type === "resize-top") {
        setChartLane(clampChartLayout({ top: drag.startTop + deltaY, height: drag.startHeight - deltaY }));
        return;
      }
      setChartLane(clampChartLayout({ top: drag.startTop, height: drag.startHeight + deltaY }));
    };

    const handlePointerMove = (event: PointerEvent) => {
      if (!dragRef.current) {
        return;
      }
      event.preventDefault();
      applyDrag(event.clientY);
    };

    const handlePointerEnd = () => {
      dragRef.current = null;
      setGripHover(null);
    };

    const handleResize = () => {
      setChartLane((layout) => clampChartLayout(layout));
    };

    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerEnd);
    window.addEventListener("pointercancel", handlePointerEnd);
    window.addEventListener("resize", handleResize);
    return () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerEnd);
      window.removeEventListener("pointercancel", handlePointerEnd);
      window.removeEventListener("resize", handleResize);
    };
  }, []);

  const floatingPanelStyles = useMemo(() => {
    const viewportHeight = typeof window === "undefined" ? 720 : window.innerHeight;
    const topPanelHeight = Math.max(86, Math.min(168, chartLane.top - 28));
    const bottomTop = chartLane.top + chartLane.height + 18;
    const bottomPanelHeight = Math.max(86, Math.min(168, viewportHeight - bottomTop - 18));
    return {
      top: { top: 18, height: topPanelHeight },
      bottom: { top: bottomTop, height: bottomPanelHeight }
    };
  }, [chartLane]);

  const panelMetadata = useMemo(() => {
    if (!semanticSelection) {
      return [];
    }
    return [
      { label: "Symbol", value: semanticSelection.symbol },
      { label: "Interval", value: String(semanticSelection.interval) },
      { label: "From", value: compactDate(semanticSelection.from) },
      { label: "To", value: compactDate(semanticSelection.to) },
      { label: "Open", value: formatMetric(semanticSelection.open) },
      { label: "High", value: formatMetric(semanticSelection.high) },
      { label: "Low", value: formatMetric(semanticSelection.low) },
      { label: "Close", value: formatMetric(semanticSelection.close) },
      { label: "Volume", value: formatVolume(semanticSelection.volume) }
    ].filter((item) => item.value !== "-");
  }, [semanticSelection]);

  const universeSymbols = useMemo((): ChartSymbolDto[] => sp500UniverseSeed.map((item) => ({
    symbol: item.symbol,
    name: item.companyName,
    sector: item.sector,
    isMock: item.symbol === "TSLA" || item.symbol === "AAPL" || item.symbol === "GOOGL"
  })), []);

  const showChart = (symbol: string) => {
    setSemanticSelection(null);
    setMainView({ mode: "chart", symbol: symbol.toUpperCase() });
  };

  const showTreeMap = () => {
    setSemanticSelection(null);
    setMainView({ mode: "treemap" });
  };

  const beginDrag = (type: LayoutDrag["type"]) => (event: ReactPointerEvent<HTMLElement>) => {
    event.preventDefault();
    if (type === "move" && !laneCanMove) {
      return;
    }
    if (type !== "move" && !laneCanResize) {
      return;
    }
    event.currentTarget.setPointerCapture?.(event.pointerId);
    setChartHover(true);
    if (type === "resize-top") {
      setGripHover("top");
    }
    if (type === "resize-bottom") {
      setGripHover("bottom");
    }
    dragRef.current = {
      type,
      startY: event.clientY,
      startTop: chartLane.top,
      startHeight: chartLane.height
    };
  };

  const updateDrag = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current;
    if (!drag) {
      return;
    }
    const deltaY = event.clientY - drag.startY;
    if (drag.type === "move") {
      if (!canMoveChartLayout({ top: drag.startTop, height: drag.startHeight })) {
        return;
      }
      setChartLane(clampChartLayout({ top: drag.startTop + deltaY, height: drag.startHeight }));
      return;
    }
    if (drag.type === "resize-top") {
      setChartLane(clampChartLayout({ top: drag.startTop + deltaY, height: drag.startHeight - deltaY }));
      return;
    }
    setChartLane(clampChartLayout({ top: drag.startTop, height: drag.startHeight + deltaY }));
  };

  const endDrag = (event: ReactPointerEvent<HTMLElement>) => {
    dragRef.current = null;
    setGripHover(null);
    try {
      event.currentTarget.releasePointerCapture?.(event.pointerId);
    } catch {
      // Pointer capture may already be gone when the browser ends a drag.
    }
  };

  return (
    <main className="app-shell">
      <section className="canvas-workspace">
        <div
          className={[
            "chart-lane-frame",
            chartHover ? "is-chart-hovered" : "",
            gripHover ? "is-grip-hovered" : "",
            laneCanMove ? "" : "is-move-disabled",
            laneCanResize ? "" : "is-resize-disabled"
          ].filter(Boolean).join(" ")}
          style={{ top: chartLane.top, height: chartLane.height }}
          onPointerEnter={() => setChartHover(true)}
          onPointerLeave={() => {
            if (!dragRef.current) {
              setChartHover(false);
              setGripHover(null);
            }
          }}
        >
          <button
            type="button"
            className="chart-move-button"
            aria-label="Move chart lane"
            title="Move chart lane"
            disabled={!laneCanMove}
            onPointerDown={beginDrag("move")}
            onPointerMove={updateDrag}
            onPointerUp={endDrag}
            onPointerCancel={endDrag}
          >
            <MoveVertical size={16} />
          </button>
          <div
            className="chart-resize-grip top"
            aria-hidden="true"
            onPointerEnter={() => laneCanResize && setGripHover("top")}
            onPointerLeave={() => !dragRef.current && setGripHover(null)}
            onPointerDown={beginDrag("resize-top")}
            onPointerMove={updateDrag}
            onPointerUp={endDrag}
            onPointerCancel={endDrag}
          />
          {mainView.mode === "treemap" ? (
            <TreeMapCanvas items={sp500UniverseSeed} onSelectSymbol={showChart} />
          ) : (
            <ChartPanel
              symbol={mainView.symbol}
              symbols={universeSymbols}
              laneHeight={chartLane.height}
              onSemanticSelectionChange={setSemanticSelection}
              onChartHoverChange={setChartHover}
              onSymbolChange={showChart}
              onBackToTreeMap={showTreeMap}
            />
          )}
          <div
            className="chart-resize-grip bottom"
            aria-hidden="true"
            onPointerEnter={() => laneCanResize && setGripHover("bottom")}
            onPointerLeave={() => !dragRef.current && setGripHover(null)}
            onPointerDown={beginDrag("resize-bottom")}
            onPointerMove={updateDrag}
            onPointerUp={endDrag}
            onPointerCancel={endDrag}
          />
        </div>
        {mainView.mode === "treemap" && (
          <>
            <div className="treemap-landing-overlay">
              <h1>어떤 종목이 궁금하신가요?</h1>
            </div>
            <SymbolSearch
              symbols={universeSymbols}
              className="treemap-symbol-search"
              buttonLabel="S&P500"
              onSelectSymbol={showChart}
            />
          </>
        )}
        {mainView.mode === "chart" && (
          <div className="floating-panels" aria-label="Workspace panels">
            <PlaceholderPanel label="Panel 02" title="선택 시점 메타데이터" className="floating-panel top" style={floatingPanelStyles.top} metadata={panelMetadata} />
            <PlaceholderPanel label="Panel 03" title="연동 패널" className="floating-panel bottom" style={floatingPanelStyles.bottom} metadata={panelMetadata} />
          </div>
        )}
      </section>
    </main>
  );
}

function compactDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(date);
}

function formatMetric(value: number | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(2) : "-";
}

function formatVolume(value: number | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? Math.round(value).toLocaleString("en-US") : "-";
}

function clampChartLayout(layout: ChartLaneLayout): ChartLaneLayout {
  const viewportHeight = typeof window === "undefined" ? 720 : window.innerHeight;
  const minHeight = chartMinHeight(viewportHeight);
  const maxHeight = chartMaxHeight(viewportHeight);
  const height = clampChartHeight(layout.height, viewportHeight, minHeight, maxHeight);
  const minTop = chartReservedPanelSpace(viewportHeight) + 8;
  const maxTop = Math.max(minTop, viewportHeight - chartReservedPanelSpace(viewportHeight) - height - 8);
  if (height >= maxHeight - 0.5) {
    return {
      top: Math.round((minTop + maxTop) / 2),
      height
    };
  }
  return {
    top: Math.round(Math.min(maxTop, Math.max(minTop, layout.top))),
    height
  };
}

function clampChartHeight(height: number, viewportHeight: number, minHeight = 150, maxHeight = Math.round(viewportHeight * 0.72)): number {
  return Math.round(Math.min(maxHeight, Math.max(minHeight, height)));
}

function chartReservedPanelSpace(viewportHeight: number): number {
  return Math.max(82, Math.min(150, Math.round(viewportHeight * 0.12)));
}

function chartMinHeight(viewportHeight: number): number {
  const reservedPanelSpace = chartReservedPanelSpace(viewportHeight);
  return Math.min(170, Math.max(150, viewportHeight - reservedPanelSpace * 2 - 28));
}

function chartMaxHeight(viewportHeight: number): number {
  const reservedPanelSpace = chartReservedPanelSpace(viewportHeight);
  return Math.max(chartMinHeight(viewportHeight), Math.min(Math.round(viewportHeight * 0.72), viewportHeight - reservedPanelSpace * 2 - 28));
}

function canMoveChartLayout(layout: ChartLaneLayout): boolean {
  const viewportHeight = typeof window === "undefined" ? 720 : window.innerHeight;
  return layout.height < chartMaxHeight(viewportHeight) - 0.5;
}

function canResizeChartLayout(): boolean {
  const viewportHeight = typeof window === "undefined" ? 720 : window.innerHeight;
  return chartMaxHeight(viewportHeight) > chartMinHeight(viewportHeight) + 0.5;
}
