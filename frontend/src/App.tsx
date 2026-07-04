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

type BottomMenuKey = "I" | "II" | "III" | "IV" | "V" | "VI";
type BottomMenuSide = "left" | "right";

const idleAgentMessage = "";
const leftMenuKeys: BottomMenuKey[] = ["I", "II", "III"];
const rightMenuKeys: BottomMenuKey[] = ["IV", "V", "VI"];

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

  useEffect(() => {
    if (!activeBottomMenu) {
      return undefined;
    }

    const handleBottomMenuOutsidePointerDown = (event: PointerEvent) => {
      if (!(event.target instanceof Element)) {
        return;
      }
      if (event.target.closest(".bottom-menu-panel, .bottom-nav-actions")) {
        return;
      }
      setActiveBottomMenu(null);
    };

    document.addEventListener("pointerdown", handleBottomMenuOutsidePointerDown, true);
    return () => document.removeEventListener("pointerdown", handleBottomMenuOutsidePointerDown, true);
  }, [activeBottomMenu]);

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
      {activeBottomMenu && (
        <button
          type="button"
          className="bottom-menu-dismiss-layer"
          aria-label="Close bottom menu"
          onClick={() => setActiveBottomMenu(null)}
        />
      )}
      <nav className="workspace-bottom-nav" aria-label="Workspace command bar">
        <div
          className={`bottom-nav-actions left ${activeBottomMenu && leftMenuKeys.includes(activeBottomMenu) ? "is-menu-open" : ""}`}
          aria-label="Menu actions left"
        >
          <BottomMenuPanel
            side="left"
            activeKey={activeBottomMenu}
            onShowTreeMap={showTreeMap}
            onClose={() => setActiveBottomMenu(null)}
          />
          {leftMenuKeys.map((label) => (
            <button
              key={label}
              type="button"
              className={`workspace-nav-button surface-raised ${activeBottomMenu === label ? "is-active" : ""}`}
              aria-label={`Menu ${label}`}
              aria-expanded={activeBottomMenu === label}
              onClick={() => toggleBottomMenu(label)}
            >
              {label}
            </button>
          ))}
        </div>
        <form className="agent-box surface-recessed" onSubmit={runAgentPrompt}>
          <input
            value={agentInput}
            onChange={(event) => setAgentInput(event.target.value)}
            placeholder={mainView.mode === "chart" ? "차트에게 물어보기" : "종목을 선택한 뒤 차트에게 물어보기"}
            aria-label="Chart agent command"
            disabled={agentBusy}
          />
          <button type="submit" disabled={agentBusy}>{agentBusy ? "..." : "Run"}</button>
        </form>
        <div
          className={`bottom-nav-actions right ${activeBottomMenu && rightMenuKeys.includes(activeBottomMenu) ? "is-menu-open" : ""}`}
          aria-label="Menu actions right"
        >
          <BottomMenuPanel
            side="right"
            activeKey={activeBottomMenu}
            onShowTreeMap={showTreeMap}
            onClose={() => setActiveBottomMenu(null)}
          />
          {rightMenuKeys.map((label) => (
            <button
              key={label}
              type="button"
              className={`workspace-nav-button surface-raised ${activeBottomMenu === label ? "is-active" : ""}`}
              aria-label={`Menu ${label}`}
              aria-expanded={activeBottomMenu === label}
              onClick={() => toggleBottomMenu(label)}
            >
              {label}
            </button>
          ))}
        </div>
      </nav>
    </main>
  );
}

function BottomMenuPanel({
  side,
  activeKey,
  onShowTreeMap,
  onClose
}: {
  side: BottomMenuSide;
  activeKey: BottomMenuKey | null;
  onShowTreeMap: () => void;
  onClose: () => void;
}) {
  const sideKeys = side === "left" ? leftMenuKeys : rightMenuKeys;
  const isOpen = activeKey !== null && sideKeys.includes(activeKey);
  const menuContent = isOpen && activeKey === "I" ? (
    <button
      type="button"
      className="bottom-menu-item surface-raised"
      onClick={() => {
        onShowTreeMap();
        onClose();
      }}
    >
      홈화면
    </button>
  ) : (
    <p className="bottom-menu-empty">{isOpen && activeKey ? `${activeKey} menu` : "Menu"}</p>
  );

  return (
    <section
      className={`bottom-menu-panel surface-floating ${side} ${isOpen ? "is-open" : ""}`}
      aria-label={`${side === "left" ? "Left" : "Right"} menu panel`}
      aria-hidden={!isOpen}
    >
      <div className="bottom-menu-list">{menuContent}</div>
    </section>
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
