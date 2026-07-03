import { MoveVertical } from "lucide-react";
import { PointerEvent as ReactPointerEvent, useEffect, useMemo, useRef, useState } from "react";
import { ChartPanel } from "./components/ChartPanel";
import { PlaceholderPanel } from "./components/PlaceholderPanel";
import type { SemanticSelectionSnapshot } from "./chart/semanticTimeline";

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
  const height = clampChartHeight(Math.round(window.innerHeight * 0.56), window.innerHeight);
  return clampChartLayout({
    top: Math.round((window.innerHeight - height) / 2),
    height
  });
}

export function App() {
  const [chartLane, setChartLane] = useState<ChartLaneLayout>(() => initialLaneLayout());
  const [semanticSelection, setSemanticSelection] = useState<SemanticSelectionSnapshot | null>(null);
  const [chartHover, setChartHover] = useState(false);
  const [gripHover, setGripHover] = useState<"top" | "bottom" | null>(null);
  const dragRef = useRef<LayoutDrag | null>(null);

  useEffect(() => {
    const applyDrag = (clientY: number) => {
      const drag = dragRef.current;
      if (!drag) {
        return;
      }
      const deltaY = clientY - drag.startY;
      if (drag.type === "move") {
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

  const beginDrag = (type: LayoutDrag["type"]) => (event: ReactPointerEvent<HTMLElement>) => {
    event.preventDefault();
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
            gripHover ? "is-grip-hovered" : ""
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
            onPointerEnter={() => setGripHover("top")}
            onPointerLeave={() => !dragRef.current && setGripHover(null)}
            onPointerDown={beginDrag("resize-top")}
            onPointerMove={updateDrag}
            onPointerUp={endDrag}
            onPointerCancel={endDrag}
          />
          <ChartPanel laneHeight={chartLane.height} onSemanticSelectionChange={setSemanticSelection} onChartHoverChange={setChartHover} />
          <div
            className="chart-resize-grip bottom"
            aria-hidden="true"
            onPointerEnter={() => setGripHover("bottom")}
            onPointerLeave={() => !dragRef.current && setGripHover(null)}
            onPointerDown={beginDrag("resize-bottom")}
            onPointerMove={updateDrag}
            onPointerUp={endDrag}
            onPointerCancel={endDrag}
          />
        </div>
        <div className="floating-panels" aria-label="Workspace panels">
          <PlaceholderPanel label="Panel 02" title="선택 시점 메타데이터" className="floating-panel top" style={floatingPanelStyles.top} metadata={panelMetadata} />
          <PlaceholderPanel label="Panel 03" title="연동 패널" className="floating-panel bottom" style={floatingPanelStyles.bottom} metadata={panelMetadata} />
        </div>
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
  const reservedPanelSpace = Math.max(82, Math.min(150, Math.round(viewportHeight * 0.14)));
  const minHeight = Math.min(170, Math.max(150, viewportHeight - reservedPanelSpace * 2 - 28));
  const maxHeight = Math.max(minHeight, Math.min(Math.round(viewportHeight * 0.72), viewportHeight - reservedPanelSpace * 2 - 28));
  const height = clampChartHeight(layout.height, viewportHeight, minHeight, maxHeight);
  const minTop = reservedPanelSpace + 8;
  const maxTop = Math.max(minTop, viewportHeight - reservedPanelSpace - height - 8);
  return {
    top: Math.round(Math.min(maxTop, Math.max(minTop, layout.top))),
    height
  };
}

function clampChartHeight(height: number, viewportHeight: number, minHeight = 150, maxHeight = Math.round(viewportHeight * 0.72)): number {
  return Math.round(Math.min(maxHeight, Math.max(minHeight, height)));
}
