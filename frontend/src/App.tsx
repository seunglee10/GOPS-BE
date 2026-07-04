import {
  type CSSProperties,
  type FormEvent,
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState
} from "react";
import { BottomCommandBar, type BottomMenuKey } from "./components/BottomCommandBar";
import { type ChartHeaderSnapshot, type ChartPanelHandle } from "./components/ChartPanel";
import { PanelWorkspace } from "./components/PanelWorkspace";
import { SymbolSearch } from "./components/SymbolSearch";
import type { SemanticSelectionSnapshot } from "./chart/semanticTimeline";
import type { ChartSymbolDto } from "./chart/types";
import { gridGutter } from "./layout/grid";
import {
  createInitialTiledPanelState,
  scaleTiledPanelState,
  type TiledPanelState,
  type ViewportSize
} from "./layout/panelLayout";
import {
  bottomNavigationHeight,
  navigationGap,
  treeMapHoverMetaReserve
} from "./layout/workspaceMetrics";
import { sp500UniverseSeed } from "./market/sp500Universe.seed";
import { TreeMapCanvas } from "./treemap/TreeMapCanvas";

type MainView =
  | { mode: "treemap" }
  | { mode: "chart"; symbol: string };

type LayoutDrag =
  { mode: "treemap"; type: "resize-bottom"; startY: number; startHeight: number };

const idleAgentMessage = "";

function initialPanelState(): TiledPanelState {
  if (typeof window === "undefined") {
    return createInitialTiledPanelState({ width: 1280, height: 720 });
  }
  return createInitialTiledPanelState(currentViewportSize());
}

function initialTreeMapHeight(): number {
  if (typeof window === "undefined") {
    return 620;
  }
  return treeMapMaxHeight(window.innerHeight);
}

export function App() {
  const [mainView, setMainView] = useState<MainView>({ mode: "treemap" });
  const [viewportSize, setViewportSize] = useState<ViewportSize>(() => currentViewportSize());
  const [panelState, setPanelState] = useState<TiledPanelState>(() => initialPanelState());
  const [treeMapHeight, setTreeMapHeight] = useState(() => initialTreeMapHeight());
  const [chartHeader, setChartHeader] = useState<ChartHeaderSnapshot | null>(null);
  const [, setSemanticSelection] = useState<SemanticSelectionSnapshot | null>(null);
  const [agentInput, setAgentInput] = useState("");
  const [agentMessage, setAgentMessage] = useState(idleAgentMessage);
  const [agentBusy, setAgentBusy] = useState(false);
  const [treeMapLaneHover, setTreeMapLaneHover] = useState(false);
  const [activeBottomMenu, setActiveBottomMenu] = useState<BottomMenuKey | null>(null);
  const chartPanelRef = useRef<ChartPanelHandle | null>(null);
  const dragRef = useRef<LayoutDrag | null>(null);
  const viewportSizeRef = useRef<ViewportSize>(viewportSize);
  const isTreeMapMode = mainView.mode === "treemap";
  const laneCanResize = isTreeMapMode && canResizeTreeMapLayout(viewportSize.height);

  useEffect(() => {
    viewportSizeRef.current = viewportSize;
  }, [viewportSize]);

  const finishLayoutDrag = useCallback((event?: PointerEvent) => {
    void event;
    dragRef.current = null;
  }, []);

  const applyLayoutDrag = useCallback((_clientX: number, clientY: number, viewport: ViewportSize) => {
    const drag = dragRef.current;
    if (!drag) {
      return;
    }
    setTreeMapHeight(clampTreeMapHeight(drag.startHeight + clientY - drag.startY, viewport.height));
  }, []);

  useEffect(() => {
    const handlePointerMove = (event: PointerEvent) => {
      if (!dragRef.current) {
        return;
      }
      event.preventDefault();
      applyLayoutDrag(event.clientX, event.clientY, viewportSizeRef.current);
    };

    const handleResize = () => {
      const previous = viewportSizeRef.current;
      const next = currentViewportSize();
      viewportSizeRef.current = next;
      setViewportSize(next);
      setPanelState((current) => scaleTiledPanelState(current, previous, next));
      setTreeMapHeight((height) => clampTreeMapHeight(height, next.height));
    };

    const handlePointerUp = (event: PointerEvent) => finishLayoutDrag(event);

    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerUp);
    window.addEventListener("pointercancel", handlePointerUp);
    window.addEventListener("resize", handleResize);
    return () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerUp);
      window.removeEventListener("pointercancel", handlePointerUp);
      window.removeEventListener("resize", handleResize);
    };
  }, [applyLayoutDrag, finishLayoutDrag]);

  const layoutGutter = gridGutter(viewportSize.width);
  const workspaceStyle = {
    "--layout-gutter": `${layoutGutter}px`
  } as CSSProperties;
  const treeMapLaneStyle: CSSProperties = {
    top: 0,
    height: treeMapHeight,
    left: 0,
    width: viewportSize.width
  };

  const universeSymbols = useMemo((): ChartSymbolDto[] => sp500UniverseSeed.map((item) => ({
    symbol: item.symbol,
    name: item.companyName,
    sector: item.sector,
    isMock: item.symbol === "TSLA" || item.symbol === "AAPL" || item.symbol === "GOOGL"
  })), []);
  const activeHeaderSymbol = mainView.mode === "chart" ? chartHeader?.symbol ?? mainView.symbol : "";
  const activeHeaderName = mainView.mode === "chart"
    ? chartHeader?.name ?? universeSymbols.find((item) => item.symbol === activeHeaderSymbol)?.name ?? ""
    : "";
  const activeHeaderQuote = chartHeader?.liveQuote;

  const showChart = (symbol: string) => {
    setSemanticSelection(null);
    setAgentMessage(idleAgentMessage);
    setTreeMapLaneHover(false);
    setMainView({ mode: "chart", symbol: symbol.toUpperCase() });
  };

  const showTreeMap = () => {
    setSemanticSelection(null);
    setChartHeader(null);
    setAgentMessage(idleAgentMessage);
    setActiveBottomMenu(null);
    setMainView({ mode: "treemap" });
  };

  const toggleBottomMenu = (key: BottomMenuKey) => {
    setActiveBottomMenu((current) => (current === key ? null : key));
  };

  const runAgentPrompt = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const prompt = agentInput.trim();
    if (!prompt || agentBusy) {
      return;
    }
    setAgentInput("");
    if (mainView.mode !== "chart") {
      setAgentMessage("차트를 선택하면 분석할 수 있습니다.");
      return;
    }
    const chartPanel = chartPanelRef.current;
    if (!chartPanel) {
      setAgentMessage("차트가 준비되면 다시 시도해주세요.");
      return;
    }
    setAgentBusy(true);
    setAgentMessage("차트 에이전트가 차트를 읽고 있습니다.");
    try {
      const result = await chartPanel.runAgentPrompt(prompt);
      if (result.message) {
        setAgentMessage(result.message);
      }
    } catch (error: unknown) {
      setAgentMessage(error instanceof Error ? error.message : "차트 에이전트 요청에 실패했습니다.");
    } finally {
      setAgentBusy(false);
    }
  }, [agentBusy, agentInput, mainView.mode]);

  const beginTreeMapResize = (event: ReactPointerEvent<HTMLElement>) => {
    event.preventDefault();
    if (!laneCanResize) {
      return;
    }
    event.currentTarget.setPointerCapture?.(event.pointerId);
    setTreeMapLaneHover(true);
    dragRef.current = {
      mode: "treemap",
      type: "resize-bottom",
      startY: event.clientY,
      startHeight: treeMapHeight
    };
  };

  const updateDrag = (event: ReactPointerEvent<HTMLElement>) => {
    applyLayoutDrag(event.clientX, event.clientY, viewportSize);
  };

  const endDrag = (event: ReactPointerEvent<HTMLElement>) => {
    finishLayoutDrag(event.nativeEvent);
    try {
      event.currentTarget.releasePointerCapture?.(event.pointerId);
    } catch {
      // Pointer capture can be released by the browser when a drag leaves the element.
    }
  };

  return (
    <main className="app-shell">
      {mainView.mode === "chart" && (
        <header className="workspace-top-nav chart" aria-label="Workspace header">
          <div className="company-summary" aria-label="Current company">
            <div className="company-summary-main">
              <h1 className="company-ticker">{activeHeaderSymbol}</h1>
              <div className={`header-quote-stack ${activeHeaderQuote?.tone ?? "unavailable"}`} aria-label="Live quote">
                <span className="quote-percent">{activeHeaderQuote?.percentText ?? "-"}</span>
                <span className="quote-price-line">
                  <span className="quote-price">{activeHeaderQuote?.priceText ?? "-"}</span>
                  <span className="quote-change">{activeHeaderQuote?.changeText ?? "-"}</span>
                </span>
              </div>
            </div>
          </div>
          <SymbolSearch
            symbols={universeSymbols}
            className="top-nav-symbol-search"
            selectedSymbol={activeHeaderSymbol}
            selectedLabel={activeHeaderName}
            placeholder="종목명 검색"
            formatSelectedLabel={(symbol) => symbol.name}
            onSelectSymbol={showChart}
          />
        </header>
      )}
      <section className="canvas-workspace" style={workspaceStyle}>
        {mainView.mode === "treemap" ? (
          <div
            className={[
              "chart-lane-frame",
              "workspace-panel-surface",
              treeMapLaneHover ? "is-chart-hovered" : "",
              laneCanResize ? "" : "is-resize-disabled"
            ].filter(Boolean).join(" ")}
            style={treeMapLaneStyle}
            onPointerEnter={() => setTreeMapLaneHover(true)}
            onPointerLeave={() => {
              if (!dragRef.current) {
                setTreeMapLaneHover(false);
              }
            }}
          >
            <TreeMapCanvas items={sp500UniverseSeed} onSelectSymbol={showChart} />
            <div
              className="chart-resize-grip bottom"
              aria-hidden="true"
              onPointerDown={beginTreeMapResize}
              onPointerMove={updateDrag}
              onPointerUp={endDrag}
              onPointerCancel={endDrag}
            />
          </div>
        ) : (
          <PanelWorkspace
            panelState={panelState}
            setPanelState={setPanelState}
            viewportSize={viewportSize}
            activeSymbol={mainView.symbol}
            symbols={universeSymbols}
            chartHeader={chartHeader}
            chartPanelRef={chartPanelRef}
            setSemanticSelection={setSemanticSelection}
            setChartHeader={setChartHeader}
          />
        )}
      </section>
      {agentMessage && <p className="agent-answer">{agentMessage}</p>}
      <BottomCommandBar
        activeMenu={activeBottomMenu}
        agentBusy={agentBusy}
        agentInput={agentInput}
        isChartMode={mainView.mode === "chart"}
        onAgentInputChange={setAgentInput}
        onAgentSubmit={runAgentPrompt}
        onCloseMenu={() => setActiveBottomMenu(null)}
        onShowTreeMap={showTreeMap}
        onToggleMenu={toggleBottomMenu}
      />
    </main>
  );
}

function currentViewportSize(): ViewportSize {
  if (typeof window === "undefined") {
    return { width: 1280, height: 720 };
  }
  return { width: window.innerWidth, height: window.innerHeight };
}

function clampTreeMapHeight(height: number, viewportHeight: number): number {
  return Math.round(Math.min(treeMapMaxHeight(viewportHeight), Math.max(treeMapMinHeight(viewportHeight), height)));
}

function chartBottomReservedSpace(): number {
  return bottomNavigationHeight;
}

function treeMapMinHeight(viewportHeight: number): number {
  return Math.min(360, Math.max(260, viewportHeight - chartBottomReservedSpace() - treeMapHoverMetaReserve - 220));
}

function treeMapMaxHeight(viewportHeight: number): number {
  return Math.max(treeMapMinHeight(viewportHeight), viewportHeight - chartBottomReservedSpace() - treeMapHoverMetaReserve - navigationGap);
}

function canResizeTreeMapLayout(viewportHeight = currentViewportSize().height): boolean {
  return treeMapMaxHeight(viewportHeight) > treeMapMinHeight(viewportHeight) + 0.5;
}
