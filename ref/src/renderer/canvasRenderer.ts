import { defaultLayerRendererRegistry, type LayerRendererRegistry } from "./layerRendererRegistry";
import type { RenderPane, RenderScene } from "./sceneBuilder";

export interface CanvasStack {
  baseCanvas: HTMLCanvasElement;
  dataCanvas: HTMLCanvasElement;
  interactionCanvas: HTMLCanvasElement;
}

export interface ChartRenderer {
  mount(target: HTMLElement): void;
  resize(size: import("./sceneBuilder").RenderSize): void;
  render(scene: RenderScene): void;
  destroy(): void;
}

export function renderSceneToCanvases(
  canvases: CanvasStack,
  scene: RenderScene,
  registry: LayerRendererRegistry = defaultLayerRendererRegistry
): void {
  resizeCanvas(canvases.baseCanvas, scene);
  resizeCanvas(canvases.dataCanvas, scene);
  resizeCanvas(canvases.interactionCanvas, scene);
  const base = context(canvases.baseCanvas, scene);
  const data = context(canvases.dataCanvas, scene);
  const interaction = context(canvases.interactionCanvas, scene);
  clear(base, scene);
  clear(data, scene);
  clear(interaction, scene);
  drawBase(base, scene);
  drawData(data, scene, registry);
  drawInteraction(interaction, scene);
}

function resizeCanvas(canvas: HTMLCanvasElement, scene: RenderScene): void {
  const pixelWidth = Math.max(1, Math.floor(scene.size.width * scene.size.devicePixelRatio));
  const pixelHeight = Math.max(1, Math.floor(scene.size.height * scene.size.devicePixelRatio));
  if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
  if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
  canvas.style.width = `${scene.size.width}px`;
  canvas.style.height = `${scene.size.height}px`;
}

function context(canvas: HTMLCanvasElement, scene: RenderScene): CanvasRenderingContext2D {
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("Canvas 2D context unavailable.");
  ctx.setTransform(scene.size.devicePixelRatio, 0, 0, scene.size.devicePixelRatio, 0, 0);
  return ctx;
}

function clear(ctx: CanvasRenderingContext2D, scene: RenderScene): void {
  ctx.clearRect(0, 0, scene.size.width, scene.size.height);
}

function drawBase(ctx: CanvasRenderingContext2D, scene: RenderScene): void {
  const style = {
    backgroundColor: "#0f1115",
    gridColor: "#252a33",
    textColor: "#d4d7dd"
  };
  ctx.fillStyle = style.backgroundColor;
  ctx.fillRect(0, 0, scene.size.width, scene.size.height);
  ctx.font = "12px Inter, system-ui, sans-serif";
  for (const pane of scene.panes) {
    drawGrid(ctx, pane, style.gridColor);
    drawAxis(ctx, pane, style.textColor);
  }
  ctx.fillStyle = style.textColor;
  ctx.font = "600 13px Inter, system-ui, sans-serif";
  ctx.fillText(`${scene.symbol} ${scene.timeframe}`, 12, 17);
}

function drawGrid(ctx: CanvasRenderingContext2D, pane: RenderPane, color: string): void {
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  ctx.globalAlpha = 0.8;
  for (const tick of pane.xScale.ticks) {
    ctx.beginPath();
    ctx.moveTo(tick.x, pane.bounds.y);
    ctx.lineTo(tick.x, pane.bounds.y + pane.bounds.height);
    ctx.stroke();
  }
  for (const tick of pane.yScale.ticks) {
    ctx.beginPath();
    ctx.moveTo(pane.bounds.x, tick.y);
    ctx.lineTo(pane.bounds.x + pane.bounds.width, tick.y);
    ctx.stroke();
  }
  ctx.globalAlpha = 1;
}

function drawAxis(ctx: CanvasRenderingContext2D, pane: RenderPane, color: string): void {
  ctx.fillStyle = color;
  ctx.font = "11px Inter, system-ui, sans-serif";
  ctx.fillText(pane.title, pane.bounds.x + 8, pane.bounds.y + 16);
  for (const tick of pane.yScale.ticks) {
    ctx.fillText(tick.label, pane.bounds.x + pane.bounds.width + 8, tick.y + 4);
  }
  if (pane.kind === "volume") {
    for (const tick of pane.xScale.ticks) {
      ctx.fillText(tick.label, tick.x - 16, pane.bounds.y + pane.bounds.height - 4);
    }
  }
}

function drawData(ctx: CanvasRenderingContext2D, scene: RenderScene, registry: LayerRendererRegistry): void {
  if (scene.visibleCandles.length === 0) {
    ctx.fillStyle = "#8b93a7";
    ctx.font = "13px Inter, system-ui, sans-serif";
    ctx.fillText("Waiting for candles", 18, Math.max(48, scene.size.height / 2));
    return;
  }
  for (const layer of scene.layers) {
    const pane = scene.panes.find((item) => item.id === layer.paneId);
    const definition = registry[layer.type];
    if (!pane || !definition || !layer.visible) continue;
    ctx.save();
    ctx.beginPath();
    ctx.rect(pane.bounds.x, pane.bounds.y, pane.bounds.width, pane.bounds.height);
    ctx.clip();
    definition.draw(ctx, layer, pane);
    ctx.restore();
  }
}

function drawInteraction(ctx: CanvasRenderingContext2D, scene: RenderScene): void {
  const crosshair = scene.interaction.crosshair;
  if (!crosshair) return;
  const pane = scene.panes.find((item) => item.id === crosshair.paneId);
  if (!pane) return;
  ctx.strokeStyle = "rgba(212, 215, 221, 0.55)";
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(crosshair.x, pane.bounds.y);
  ctx.lineTo(crosshair.x, pane.bounds.y + pane.bounds.height);
  ctx.moveTo(pane.bounds.x, crosshair.y);
  ctx.lineTo(pane.bounds.x + pane.bounds.width, crosshair.y);
  ctx.stroke();
  ctx.setLineDash([]);
  if (scene.crosshairReadout) {
    const label = [
      scene.crosshairReadout.timestamp ? formatReadoutTime(scene.crosshairReadout.timestamp) : null,
      scene.crosshairReadout.valueLabel
    ]
      .filter(Boolean)
      .join("  ");
    ctx.font = "11px Inter, system-ui, sans-serif";
    const width = ctx.measureText(label).width + 12;
    const labelX = Math.min(pane.bounds.x + pane.bounds.width - width - 4, Math.max(pane.bounds.x + 4, crosshair.x + 10));
    const labelY = Math.max(pane.bounds.y + 6, crosshair.y - 24);
    ctx.fillStyle = "rgba(15, 17, 21, 0.92)";
    ctx.strokeStyle = "rgba(212, 215, 221, 0.35)";
    ctx.lineWidth = 1;
    ctx.fillRect(labelX, labelY, width, 20);
    ctx.strokeRect(labelX, labelY, width, 20);
    ctx.fillStyle = "#f8fafc";
    ctx.fillText(label, labelX + 6, labelY + 14);
  }
}

function formatReadoutTime(timestamp: string): string {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return timestamp;
  return `${String(date.getUTCHours()).padStart(2, "0")}:${String(date.getUTCMinutes()).padStart(2, "0")}:${String(date.getUTCSeconds()).padStart(2, "0")}`;
}
