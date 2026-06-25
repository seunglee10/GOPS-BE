import { useEffect, useMemo, useRef, useState } from "react";
import type { CalculationOutput } from "../types/calculations";
import type { ChartDocument, ChartToolMode, ChartViewport, DrawingLayer, PanelDocument } from "../types/documents";
import type { Command } from "../types/commands";
import type { CandlesBySymbol } from "../market/candleStore";
import { renderSceneToCanvases } from "./canvasRenderer";
import { buildRenderScene, type RenderDrawingLayer, type RenderInteractionState, type RenderPane, type RenderSize } from "./sceneBuilder";
import { visibleBarLimit } from "./timeScale";
import { panLogicalRange, rightOffsetForLogicalRange, zoomLogicalRangeAtAnchor } from "./timeScaleModel";
import { defaultLayerRendererRegistry } from "./layerRendererRegistry";
import { createCommandId, nowIso } from "../state/commandEngine";

interface ChartCanvasProps {
  chart: ChartDocument;
  chartPanel: PanelDocument;
  toolMode: ChartToolMode;
  showCrosshair: boolean;
  candlesBySymbol: CandlesBySymbol;
  calculationOutputs: Record<string, CalculationOutput>;
  onDispatch(command: Command): boolean;
  onViewportChange(patch: Partial<ChartViewport>): void;
}

type DragState =
  | { kind: "pan"; startX: number; logicalRange: { from: number; to: number } }
  | { kind: "newHorizontalLine"; paneId: string; price: number }
  | { kind: "moveHorizontalLine"; layerId: string; paneId: string; originalPrice: number; price: number };

export function ChartCanvas({
  chart,
  chartPanel,
  toolMode,
  showCrosshair,
  candlesBySymbol,
  calculationOutputs,
  onDispatch,
  onViewportChange
}: ChartCanvasProps): JSX.Element {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const baseRef = useRef<HTMLCanvasElement | null>(null);
  const dataRef = useRef<HTMLCanvasElement | null>(null);
  const interactionRef = useRef<HTMLCanvasElement | null>(null);
  const [size, setSize] = useState<RenderSize>({ width: 800, height: 480, devicePixelRatio: 1 });
  const [interaction, setInteraction] = useState<RenderInteractionState>({});
  const dragRef = useRef<DragState | null>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const observer = new ResizeObserver(([entry]) => {
      const box = entry.contentRect;
      setSize({
        width: Math.max(320, box.width),
        height: Math.max(260, box.height),
        devicePixelRatio: window.devicePixelRatio || 1
      });
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  const scene = useMemo(
    () => buildRenderScene({ chart, candlesBySymbol, calculationOutputs, size, interaction }),
    [calculationOutputs, candlesBySymbol, chart, interaction, size]
  );

  useEffect(() => {
    if (!baseRef.current || !dataRef.current || !interactionRef.current) return;
    let frame = requestAnimationFrame(() => {
      if (!baseRef.current || !dataRef.current || !interactionRef.current) return;
      renderSceneToCanvases(
        {
          baseCanvas: baseRef.current,
          dataCanvas: dataRef.current,
          interactionCanvas: interactionRef.current
        },
        scene
      );
    });
    return () => cancelAnimationFrame(frame);
  }, [scene]);

  function paneAt(y: number) {
    return scene.panes.find((pane) => y >= pane.bounds.y && y <= pane.bounds.y + pane.bounds.height);
  }

  function crosshairAt(pane: RenderPane | undefined, x: number, y: number) {
    return showCrosshair && pane ? { crosshair: { x, y, paneId: pane.id } } : {};
  }

  function handlePointerMove(event: React.PointerEvent<HTMLDivElement>): void {
    const rect = event.currentTarget.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    const pane = paneAt(y);
    if (dragRef.current) {
      handleDragMove(dragRef.current, x, y, pane);
      return;
    }
    const hit = toolMode === "select" && pane ? hitTestScene(x, y, pane) : null;
    setInteraction({
      ...crosshairAt(pane, x, y),
      selectedLayerId: interaction.selectedLayerId,
      hoveredLayerId: hit?.layerId
    });
  }

  function handleWheel(event: React.WheelEvent<HTMLDivElement>): void {
    event.preventDefault();
    const rect = event.currentTarget.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const anchorLogical = scene.timeScale.xToLogical(x);
    const direction = event.deltaY > 0 ? 1.12 : 0.9;
    const limits = visibleBarLimit(chart.viewport);
    const totalBars = candlesBySymbol[chart.symbol]?.length ?? 0;
    const range = zoomLogicalRangeAtAnchor(scene.timeScale.logicalRange, anchorLogical, direction, {
      minBars: limits.min,
      maxBars: limits.max
    }, totalBars);
    onViewportChange({
      mode: "fixedLogicalRange",
      visibleBars: Math.max(1, Math.round(range.to - range.from + 1)),
      rightOffsetBars: rightOffsetForLogicalRange(totalBars, range),
      logicalFrom: range.from,
      logicalTo: range.to
    });
  }

  function handlePointerDown(event: React.PointerEvent<HTMLDivElement>): void {
    event.currentTarget.setPointerCapture(event.pointerId);
    const rect = event.currentTarget.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    const pane = paneAt(y);
    if (!pane) return;
    if (toolMode === "drawHorizontalLine" && pane.kind === "price") {
      const price = pane.yScale.yToValue(y);
      dragRef.current = { kind: "newHorizontalLine", paneId: pane.id, price };
      setInteraction({
        ...crosshairAt(pane, x, y),
        drawingPreview: createHorizontalLinePreview(pane, price)
      });
      return;
    }
    if (toolMode === "select") {
      const hit = hitTestScene(x, y, pane);
      if (hit?.layerId) {
        const layer = chart.layers.find((item): item is DrawingLayer => item.id === hit.layerId && item.type === "drawing");
        if (layer?.drawing.kind === "horizontalLine") {
          const price = layer.drawing.price;
          dragRef.current = { kind: "moveHorizontalLine", layerId: layer.id, paneId: pane.id, originalPrice: price, price };
          setInteraction({
            ...crosshairAt(pane, x, y),
            selectedLayerId: layer.id,
            drawingPreview: createHorizontalLinePreview(pane, price, layer)
          });
          return;
        }
      }
      dragRef.current = { kind: "pan", startX: x, logicalRange: scene.timeScale.logicalRange };
      setInteraction({ ...crosshairAt(pane, x, y), selectedLayerId: interaction.selectedLayerId });
      return;
    }
    setInteraction({ ...crosshairAt(pane, x, y), selectedLayerId: interaction.selectedLayerId });
  }

  function handlePointerUp(event: React.PointerEvent<HTMLDivElement>): void {
    event.currentTarget.releasePointerCapture(event.pointerId);
    if (dragRef.current) commitDrag(dragRef.current);
    dragRef.current = null;
  }

  function handleDragMove(drag: DragState, x: number, y: number, pane?: RenderPane): void {
    if (drag.kind === "pan") {
      const totalBars = candlesBySymbol[chart.symbol]?.length ?? 0;
      const deltaBars = Math.round((drag.startX - x) / Math.max(2, scene.timeScale.barSpacing));
      const range = panLogicalRange(drag.logicalRange, deltaBars, totalBars);
      onViewportChange({
        mode: "fixedLogicalRange",
        visibleBars: Math.max(1, Math.round(range.to - range.from + 1)),
        rightOffsetBars: rightOffsetForLogicalRange(totalBars, range),
        logicalFrom: range.from,
        logicalTo: range.to
      });
      return;
    }
    const targetPane = scene.panes.find((item) => item.id === drag.paneId);
    if (!targetPane) return;
    const price = targetPane.yScale.yToValue(y);
    drag.price = price;
    const sourceLayer = drag.kind === "moveHorizontalLine"
      ? chart.layers.find((item): item is DrawingLayer => item.id === drag.layerId && item.type === "drawing")
      : undefined;
    setInteraction({
      ...(showCrosshair && pane ? { crosshair: { x, y, paneId: pane.id } } : {}),
      selectedLayerId: drag.kind === "moveHorizontalLine" ? drag.layerId : undefined,
      drawingPreview: createHorizontalLinePreview(targetPane, price, sourceLayer)
    });
  }

  function commitDrag(drag: DragState): void {
    if (drag.kind === "newHorizontalLine") {
      onDispatch({
        id: createCommandId(),
        type: "chart.drawing.add",
        actor: "user",
        status: "applied",
        target: target(drag.paneId),
        payload: {
          layer: {
            id: `layer-drawing-${crypto.randomUUID().slice(0, 8)}`,
            type: "drawing",
            owner: "user",
            paneId: drag.paneId,
            zIndex: 300,
            visible: true,
            locked: false,
            style: { color: "#38bdf8", lineWidth: 1.5 },
            drawing: { kind: "horizontalLine", price: Number(drag.price.toFixed(4)), label: `Line ${drag.price.toFixed(2)}` },
            createdAt: nowIso(),
            updatedAt: nowIso()
          }
        },
        createdAt: nowIso()
      });
    }
    if (drag.kind === "moveHorizontalLine" && Math.abs(drag.price - drag.originalPrice) > 0.000001) {
      const layer = chart.layers.find((item): item is DrawingLayer => item.id === drag.layerId && item.type === "drawing");
      if (layer?.drawing.kind === "horizontalLine") {
        onDispatch({
          id: createCommandId(),
          type: "chart.drawing.update",
          actor: "user",
          status: "applied",
          target: { ...target(drag.paneId), layerId: drag.layerId },
          payload: {
            layerId: drag.layerId,
            drawing: { ...layer.drawing, price: Number(drag.price.toFixed(4)) },
            style: layer.style
          },
          createdAt: nowIso()
        });
      }
    }
    setInteraction((current) => ({ selectedLayerId: current.selectedLayerId }));
  }

  function target(paneId = "pane-price") {
    return {
      workspaceId: "workspace-main",
      panelId: chartPanel.id,
      chartId: chart.id,
      paneId
    };
  }

  function hitTestScene(x: number, y: number, pane: RenderPane) {
    for (const layer of [...scene.layers].reverse()) {
      if (layer.id.startsWith("preview-")) continue;
      if (layer.paneId !== pane.id || !layer.visible) continue;
      const definition = defaultLayerRendererRegistry[layer.type];
      const result = definition?.hitTest?.({ x, y }, layer, pane);
      if (result) return result;
    }
    return null;
  }

  function createHorizontalLinePreview(pane: RenderPane, price: number, sourceLayer?: DrawingLayer): RenderDrawingLayer {
    return {
      id: sourceLayer?.id ?? "runtime-horizontal-line-preview",
      type: "drawing",
      paneId: pane.id,
      zIndex: 1000,
      visible: true,
      selected: true,
      style: {
        color: sourceLayer?.style.color ?? "#38bdf8",
        lineWidth: sourceLayer?.style.lineWidth ?? 1.5,
        lineDash: [6, 4],
        opacity: 0.9
      },
      drawing: {
        kind: "horizontalLine",
        price,
        label: sourceLayer?.drawing.kind === "horizontalLine" ? sourceLayer.drawing.label : `Line ${price.toFixed(2)}`
      }
    };
  }

  return (
    <div
      ref={containerRef}
      className="chart-canvas"
      onPointerMove={handlePointerMove}
      onPointerLeave={() => setInteraction({})}
      onPointerDown={handlePointerDown}
      onPointerUp={handlePointerUp}
      onPointerCancel={handlePointerUp}
      onWheel={handleWheel}
    >
      <canvas ref={baseRef} aria-hidden="true" />
      <canvas ref={dataRef} aria-label={`${chart.symbol} candlestick chart`} />
      <canvas ref={interactionRef} aria-hidden="true" />
    </div>
  );
}
