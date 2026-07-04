import { X } from "lucide-react";
import {
  type CSSProperties,
  type Dispatch,
  type MouseEvent as ReactMouseEvent,
  type MutableRefObject,
  type PointerEvent as ReactPointerEvent,
  type SetStateAction,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState
} from "react";
import type { SemanticSelectionSnapshot } from "../chart/semanticTimeline";
import type { ChartSymbolDto } from "../chart/types";
import {
  canInsertPanelAtBoundary,
  detectPanelBoundaries,
  insertOptionsForBoundary,
  insertPanelAtBoundary,
  panelGutter,
  panelSlotStyle,
  removePanelSlot,
  resizePanelBoundary,
  setPanelContentSymbol,
  swapPanelContents,
  type BoundaryInsertOption,
  type PanelBoundary,
  type PanelContentInstance,
  type PanelSlot,
  type PanelSlotId,
  type TiledPanelState,
  type ViewportSize
} from "../layout/panelLayout";
import { bottomNavigationHeight } from "../layout/workspaceMetrics";
import { ChartPanel, type ChartHeaderSnapshot, type ChartPanelHandle } from "./ChartPanel";
import { SymbolSearch } from "./SymbolSearch";
import { WorkspacePanelFrame } from "./WorkspacePanelFrame";

type PanelWorkspaceProps = {
  panelState: TiledPanelState;
  setPanelState: Dispatch<SetStateAction<TiledPanelState>>;
  viewportSize: ViewportSize;
  activeSymbol: string;
  symbols: ChartSymbolDto[];
  chartHeader: ChartHeaderSnapshot | null;
  chartPanelRef: MutableRefObject<ChartPanelHandle | null>;
  setSemanticSelection: (selection: SemanticSelectionSnapshot | null) => void;
  setChartHeader: (header: ChartHeaderSnapshot) => void;
};

type LayoutDrag =
  | {
    mode: "boundary";
    boundaryId: string;
    orientation: PanelBoundary["orientation"];
    startX: number;
    startY: number;
    startState: TiledPanelState;
  }
  | {
    mode: "swap";
    sourceSlotId: PanelSlotId;
  };

type BoundaryAddMenu = {
  boundaryId: string;
  left: number;
  top: number;
  options: BoundaryInsertOption[];
};

const panelNavHeight = 30;

export function PanelWorkspace({
  panelState,
  setPanelState,
  viewportSize,
  activeSymbol,
  symbols,
  chartHeader,
  chartPanelRef,
  setSemanticSelection,
  setChartHeader
}: PanelWorkspaceProps) {
  const [hoveredChartSlotId, setHoveredChartSlotId] = useState<PanelSlotId | null>(null);
  const [activeBoundaryId, setActiveBoundaryId] = useState<string | null>(null);
  const [draggingSlotId, setDraggingSlotId] = useState<PanelSlotId | null>(null);
  const [addMenu, setAddMenu] = useState<BoundaryAddMenu | null>(null);
  const [chartHeaders, setChartHeaders] = useState<Record<string, ChartHeaderSnapshot>>({});
  const dragRef = useRef<LayoutDrag | null>(null);
  const panelStateRef = useRef<TiledPanelState>(panelState);
  const viewportSizeRef = useRef<ViewportSize>(viewportSize);

  useEffect(() => {
    panelStateRef.current = panelState;
  }, [panelState]);

  useEffect(() => {
    viewportSizeRef.current = viewportSize;
  }, [viewportSize]);

  const panelBoundaries = useMemo(() => detectPanelBoundaries(panelState, viewportSize), [panelState, viewportSize]);
  const activeBoundary = useMemo(() => (
    activeBoundaryId ? panelBoundaries.find((boundary) => boundary.id === activeBoundaryId) ?? null : null
  ), [activeBoundaryId, panelBoundaries]);
  const activeBoundarySlotIds = useMemo(() => (
    new Set([...(activeBoundary?.negativeSlotIds ?? []), ...(activeBoundary?.positiveSlotIds ?? [])])
  ), [activeBoundary]);
  const layoutGutter = panelGutter(viewportSize);

  const setChartSlotHover = useCallback((slotId: PanelSlotId, hovered: boolean) => {
    setHoveredChartSlotId((current) => {
      if (hovered) {
        return slotId;
      }
      return current === slotId ? null : current;
    });
  }, []);

  const recordChartHeader = useCallback((content: PanelContentInstance, header: ChartHeaderSnapshot) => {
    setChartHeaders((current) => ({ ...current, [content.id]: header }));
    if (content.isDefaultChart) {
      setChartHeader(header);
    }
  }, [setChartHeader]);

  const finishLayoutDrag = useCallback((event?: PointerEvent) => {
    const drag = dragRef.current;
    if (drag?.mode === "swap" && event) {
      const target = hitTestSwappableSlot(panelStateRef.current, event.clientX, event.clientY, drag.sourceSlotId);
      if (target) {
        setPanelState((current) => swapPanelContents(current, drag.sourceSlotId, target.id));
      }
    }
    dragRef.current = null;
    setActiveBoundaryId(addMenu?.boundaryId ?? null);
    setDraggingSlotId(null);
  }, [addMenu?.boundaryId, setPanelState]);

  const applyLayoutDrag = useCallback((clientX: number, clientY: number, viewport: ViewportSize) => {
    const drag = dragRef.current;
    if (!drag || drag.mode === "swap") {
      return;
    }
    const delta = drag.orientation === "vertical" ? clientX - drag.startX : clientY - drag.startY;
    setPanelState(resizePanelBoundary(drag.startState, drag.boundaryId, delta, viewport));
  }, [setPanelState]);

  useEffect(() => {
    const handlePointerMove = (event: PointerEvent) => {
      if (!dragRef.current) {
        return;
      }
      event.preventDefault();
      applyLayoutDrag(event.clientX, event.clientY, viewportSizeRef.current);
    };
    const handlePointerUp = (event: PointerEvent) => finishLayoutDrag(event);

    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerUp);
    window.addEventListener("pointercancel", handlePointerUp);
    return () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerUp);
      window.removeEventListener("pointercancel", handlePointerUp);
    };
  }, [applyLayoutDrag, finishLayoutDrag]);

  const beginBoundaryResize = (boundary: PanelBoundary) => (event: ReactPointerEvent<HTMLElement>) => {
    event.preventDefault();
    setAddMenu(null);
    setActiveBoundaryId(boundary.id);
    if (boundary.interaction !== "resize") {
      return;
    }
    event.currentTarget.setPointerCapture?.(event.pointerId);
    dragRef.current = {
      mode: "boundary",
      boundaryId: boundary.id,
      orientation: boundary.orientation,
      startX: event.clientX,
      startY: event.clientY,
      startState: panelState
    };
  };

  const beginPanelSwap = (slotId: PanelSlotId) => (event: ReactPointerEvent<HTMLElement>) => {
    event.preventDefault();
    event.currentTarget.setPointerCapture?.(event.pointerId);
    setAddMenu(null);
    setDraggingSlotId(slotId);
    dragRef.current = {
      mode: "swap",
      sourceSlotId: slotId
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

  const openBoundaryAddMenu = (boundary: PanelBoundary, event: ReactMouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    const options = insertOptionsForBoundary(panelState, boundary.id, viewportSize);
    if (!options.length) {
      return;
    }
    const position = boundaryAddMenuPosition(boundary, options.length, viewportSize, layoutGutter);
    setAddMenu({
      boundaryId: boundary.id,
      left: position.left,
      top: position.top,
      options
    });
    setActiveBoundaryId(boundary.id);
  };

  const insertPanel = (option: BoundaryInsertOption) => {
    if (!addMenu) {
      return;
    }
    setPanelState((current) => insertPanelAtBoundary(
      current,
      addMenu.boundaryId,
      option.kind,
      viewportSizeRef.current,
      { symbol: chartHeader?.symbol ?? activeSymbol }
    ));
    setAddMenu(null);
    setActiveBoundaryId(null);
  };

  const closePanel = (slotId: PanelSlotId) => {
    const closing = panelStateRef.current.slots.find((slot) => slot.id === slotId);
    setPanelState((current) => removePanelSlot(current, slotId, viewportSizeRef.current));
    if (closing) {
      setChartHeaders((current) => {
        const next = { ...current };
        delete next[closing.contentId];
        return next;
      });
    }
    setHoveredChartSlotId((current) => current === slotId ? null : current);
    setAddMenu(null);
    setActiveBoundaryId(null);
  };

  const changePanelChartSymbol = (contentId: string, symbol: string) => {
    setPanelState((current) => setPanelContentSymbol(current, contentId, symbol));
  };

  return (
    <>
      {panelState.slots.map((slot) => {
        const content = panelState.contents[slot.contentId];
        const isChart = content.kind === "chart";
        const isDefaultChart = Boolean(content.isDefaultChart);
        return (
          <WorkspacePanelFrame
            key={slot.id}
            slot={slot}
            content={content}
            style={panelSlotStyle(slot)}
            className={[
              isChart && slot.rect.left > 1 ? "has-left-boundary" : "",
              isChart && slot.rect.left + slot.rect.width < viewportSize.width - 1 ? "has-right-boundary" : "",
              draggingSlotId === slot.id ? "is-panel-content-dragging" : ""
            ].filter(Boolean).join(" ")}
            isBoundaryActive={activeBoundarySlotIds.has(slot.id)}
            isChartHovered={isChart && hoveredChartSlotId === slot.id}
            showNav={!isChart}
            canSwap={!isDefaultChart}
            canClose={!isDefaultChart && !isChart}
            onClose={closePanel}
            onSwapPointerDown={beginPanelSwap}
            onPointerEnter={() => isChart && setChartSlotHover(slot.id, true)}
            onPointerLeave={() => {
              if (isChart && !dragRef.current) {
                setChartSlotHover(slot.id, false);
              }
            }}
          >
            {renderPanelContent({
              slot,
              content,
              symbol: content.symbol ?? activeSymbol,
              symbols,
              laneHeight: Math.max(120, isChart ? slot.rect.height : slot.rect.height - panelNavHeight),
              chartPanelRef: isDefaultChart ? chartPanelRef : null,
              chartHeaderSnapshot: chartHeaders[content.id],
              setSemanticSelection,
              onChartHoverChange: (hovered) => setChartSlotHover(slot.id, hovered),
              onHeaderChange: isChart ? (header) => recordChartHeader(content, header) : undefined,
              onClosePanel: closePanel,
              onChangePanelChartSymbol: changePanelChartSymbol,
              onChartSwapPointerDown: !isDefaultChart ? beginPanelSwap(slot.id) : undefined
            })}
          </WorkspacePanelFrame>
        );
      })}
      {panelBoundaries.map((boundary) => {
        const canAdd = canInsertPanelAtBoundary(panelState, boundary.id, viewportSize);
        return (
          <div
            key={boundary.id}
            className={[
              "panel-boundary",
              boundary.orientation,
              boundary.interaction === "resize" ? "can-resize" : "is-insert-only",
              canAdd ? "has-add" : "",
              boundary.pageEdge ? "is-page-edge" : "",
              activeBoundaryId === boundary.id ? "is-active" : ""
            ].join(" ")}
            style={boundaryStyle(boundary)}
            role="separator"
            aria-orientation={boundary.orientation === "vertical" ? "vertical" : "horizontal"}
            onPointerEnter={() => setActiveBoundaryId(boundary.id)}
            onPointerLeave={() => {
              if (!dragRef.current && addMenu?.boundaryId !== boundary.id) {
                setActiveBoundaryId(null);
              }
            }}
            onPointerDown={beginBoundaryResize(boundary)}
            onPointerMove={(event) => {
              setActiveBoundaryId(boundary.id);
              updateDrag(event);
            }}
            onPointerUp={endDrag}
            onPointerCancel={endDrag}
          >
            {canAdd && (
              <button
                type="button"
                className="panel-boundary-add"
                aria-label="패널 추가"
                title="패널 추가"
                onPointerDown={(event) => event.stopPropagation()}
                onPointerEnter={() => setActiveBoundaryId(boundary.id)}
                onPointerMove={() => setActiveBoundaryId(boundary.id)}
                onClick={(event) => openBoundaryAddMenu(boundary, event)}
              >
                <span className="panel-boundary-add-glyph" aria-hidden="true" />
              </button>
            )}
          </div>
        );
      })}
      {addMenu && (
        <div className="panel-add-menu surface-floating" style={{ left: addMenu.left, top: addMenu.top }} onPointerDown={(event) => event.stopPropagation()}>
          {addMenu.options.map((option) => (
            <button key={option.kind} type="button" onClick={() => insertPanel(option)}>
              {option.title}
            </button>
          ))}
        </div>
      )}
    </>
  );
}

function renderPanelContent({
  slot,
  content,
  symbol,
  symbols,
  laneHeight,
  chartPanelRef,
  chartHeaderSnapshot,
  setSemanticSelection,
  onChartHoverChange,
  onHeaderChange,
  onClosePanel,
  onChangePanelChartSymbol,
  onChartSwapPointerDown
}: {
  slot: PanelSlot;
  content: PanelContentInstance;
  symbol: string;
  symbols: ChartSymbolDto[];
  laneHeight: number;
  chartPanelRef: MutableRefObject<ChartPanelHandle | null> | null;
  chartHeaderSnapshot?: ChartHeaderSnapshot;
  setSemanticSelection: (selection: SemanticSelectionSnapshot | null) => void;
  onChartHoverChange: (hovered: boolean) => void;
  onHeaderChange?: (header: ChartHeaderSnapshot) => void;
  onClosePanel: (slotId: PanelSlotId) => void;
  onChangePanelChartSymbol: (contentId: string, symbol: string) => void;
  onChartSwapPointerDown?: (event: ReactPointerEvent<HTMLElement>) => void;
}) {
  if (content.kind === "chart") {
    const selectedSymbol = symbol.toUpperCase();
    const interval = chartHeaderSnapshot?.interval ?? "1D";
    return (
      <div className={content.isDefaultChart ? "chart-instance is-default-chart" : "chart-instance is-editable-chart"}>
        <div
          className={!content.isDefaultChart ? "chart-instance-symbol chart-instance-swap-handle" : "chart-instance-symbol"}
          onPointerEnter={() => onChartHoverChange(true)}
          onPointerMove={() => onChartHoverChange(true)}
          onPointerDown={!content.isDefaultChart ? onChartSwapPointerDown : undefined}
        >
          <span className="chart-instance-interval">{interval}</span>
          {content.isDefaultChart && <span className="chart-instance-symbol-text">{selectedSymbol}</span>}
          {!content.isDefaultChart && (
            <div className="chart-instance-symbol-search-wrap" onPointerDown={(event) => event.stopPropagation()}>
              <SymbolSearch
                symbols={symbols}
                className="chart-instance-symbol-search"
                compact
                selectedSymbol={selectedSymbol}
                selectedLabel={selectedSymbol}
                placeholder={selectedSymbol}
                formatSelectedLabel={(symbol) => symbol.symbol}
                onSelectSymbol={(nextSymbol) => onChangePanelChartSymbol(content.id, nextSymbol)}
                onPointerActivity={() => onChartHoverChange(true)}
              />
            </div>
          )}
        </div>
        {!content.isDefaultChart && (
          <button
            type="button"
            className="chart-instance-close"
            aria-label="차트 패널 닫기"
            title="차트 패널 닫기"
            onPointerDown={(event) => event.stopPropagation()}
            onClick={() => onClosePanel(slot.id)}
          >
            <X size={13} />
          </button>
        )}
        <ChartPanel
          ref={chartPanelRef ?? undefined}
          symbol={selectedSymbol}
          symbols={symbols}
          laneHeight={laneHeight}
          onSemanticSelectionChange={setSemanticSelection}
          onChartHoverChange={onChartHoverChange}
          onHeaderChange={onHeaderChange}
        />
      </div>
    );
  }
  return <div className="workspace-panel-empty" aria-label={`${content.title} content`} data-panel-slot-id={slot.id} />;
}

function boundaryStyle(boundary: PanelBoundary): CSSProperties {
  if (boundary.orientation === "vertical") {
    return {
      left: boundary.position - 7,
      top: boundary.rangeStart,
      width: 14,
      height: boundary.rangeEnd - boundary.rangeStart
    };
  }
  return {
    left: boundary.rangeStart,
    top: boundary.position - 7,
    width: boundary.rangeEnd - boundary.rangeStart,
    height: 14
  };
}

function boundaryAddMenuPosition(
  boundary: PanelBoundary,
  optionCount: number,
  viewport: ViewportSize,
  gutter: number
): { left: number; top: number } {
  const menuWidth = 126;
  const menuHeight = optionCount * 28 + 10;
  const desiredLeft = boundary.orientation === "vertical"
    ? boundary.position
    : (boundary.rangeStart + boundary.rangeEnd) / 2;
  const desiredTop = boundary.orientation === "vertical"
    ? (boundary.rangeStart + boundary.rangeEnd) / 2
    : boundary.position;
  const minLeft = gutter + menuWidth / 2;
  const maxLeft = viewport.width - gutter - menuWidth / 2;
  const minTop = gutter + menuHeight / 2;
  const maxTop = viewport.height - bottomNavigationHeight - gutter - menuHeight / 2;

  return {
    left: clampNumber(desiredLeft, minLeft, maxLeft),
    top: clampNumber(desiredTop, minTop, maxTop)
  };
}

function clampNumber(value: number, min: number, max: number): number {
  if (max < min) {
    return (min + max) / 2;
  }
  return Math.min(max, Math.max(min, value));
}

function hitTestSwappableSlot(state: TiledPanelState, x: number, y: number, sourceSlotId: PanelSlotId): PanelSlot | null {
  return state.slots.find((slot) => (
    slot.id !== sourceSlotId &&
    !slot.required &&
    x >= slot.rect.left &&
    x <= slot.rect.left + slot.rect.width &&
    y >= slot.rect.top &&
    y <= slot.rect.top + slot.rect.height
  )) ?? null;
}
