import { useEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from "react";
import type { Sp500UniverseItem } from "../market/sp500Universe.seed";
import { sp500WeightValue } from "../market/sp500Universe.seed";
import { hitTestTreeMapTile, layoutSp500TreeMap } from "./treemapLayout";
import type { TreeMapInputItem, TreeMapTile } from "./treemapTypes";
import { tileFillForChange, tileOpacityForChange, tileTextForChange, toneForChange } from "./treemapColors";
import { readThemeColors, type ThemeColors } from "../theme/colors";

type TreeMapCanvasProps = {
  items: Sp500UniverseItem[];
  onSelectSymbol: (symbol: string) => void;
};

type CanvasSize = {
  width: number;
  height: number;
};

const canvasPadding = 16;
const labelPadding = 8;
const tileGap = 0.85;

export function TreeMapCanvas({ items, onSelectSymbol }: TreeMapCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const tilesRef = useRef<TreeMapTile[]>([]);
  const [size, setSize] = useState<CanvasSize>({ width: 1, height: 1 });
  const [hoveredTile, setHoveredTile] = useState<TreeMapTile | null>(null);

  const inputItems = useMemo((): TreeMapInputItem[] => items.map((item) => ({
    symbol: item.symbol,
    companyName: item.companyName,
    sector: item.sector,
    industry: item.industry,
    value: sp500WeightValue(item),
    marketCap: item.marketCap,
    indexWeight: item.indexWeight,
    changePercent: item.changePercent
  })), [items]);

  const tiles = useMemo(() => layoutSp500TreeMap(inputItems, {
    x: canvasPadding,
    y: canvasPadding,
    width: Math.max(1, size.width - canvasPadding * 2),
    height: Math.max(1, size.height - canvasPadding * 2)
  }), [inputItems, size.height, size.width]);

  const hoverMetaLeft = useMemo(() => {
    const symbolTiles = tiles.filter((tile) => tile.kind === "symbol");
    if (!symbolTiles.length) {
      return canvasPadding;
    }
    return Math.min(...symbolTiles.map((tile) => insetTile(tile, tileGap).x));
  }, [tiles]);

  const panelStyle = {
    "--treemap-hover-meta-left": `${Math.round(hoverMetaLeft)}px`
  } as CSSProperties;

  useEffect(() => {
    tilesRef.current = tiles;
  }, [tiles]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) {
      return;
    }
    const syncSize = () => {
      const rect = canvas.getBoundingClientRect();
      setSize({
        width: Math.max(1, rect.width),
        height: Math.max(1, rect.height)
      });
    };
    const observer = new ResizeObserver(syncSize);
    observer.observe(canvas);
    syncSize();
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) {
      return;
    }
    const ratio = window.devicePixelRatio || 1;
    const width = Math.max(1, Math.floor(size.width * ratio));
    const height = Math.max(1, Math.floor(size.height * ratio));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    const context = canvas.getContext("2d");
    if (!context) {
      return;
    }
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    drawTreeMap(context, size, tiles, hoveredTile);
  }, [hoveredTile, size, tiles]);

  const updateHover = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    const next = hitTestTreeMapTile(tilesRef.current, x, y) ?? null;
    setHoveredTile((current) => current?.id === next?.id ? current : next);
  };

  const selectHoveredTile = () => {
    if (hoveredTile?.symbol) {
      onSelectSymbol(hoveredTile.symbol);
    }
  };

  return (
    <section className="treemap-panel" style={panelStyle} aria-label="S&P 500 TreeMap">
      <canvas
        ref={canvasRef}
        className="treemap-canvas"
        aria-label="S&P 500 TreeMap canvas"
        onPointerMove={updateHover}
        onPointerLeave={() => setHoveredTile(null)}
        onClick={selectHoveredTile}
      />
      {hoveredTile?.symbol && (
        <div className="treemap-hover-meta" aria-live="polite">
          <strong>{hoveredTile.symbol}</strong>
          <span>{hoveredTile.companyName}</span>
          <em>{formatChange(hoveredTile.changePercent)}</em>
          <small>{hoveredTile.sector} / {hoveredTile.industry}</small>
        </div>
      )}
    </section>
  );
}

function drawTreeMap(
  context: CanvasRenderingContext2D,
  size: CanvasSize,
  tiles: TreeMapTile[],
  hoveredTile: TreeMapTile | null
) {
  const theme = readTheme();
  context.clearRect(0, 0, size.width, size.height);

  tiles.filter((tile) => tile.kind === "sector").forEach((tile) => drawSector(context, tile, theme));
  tiles.filter((tile) => tile.kind === "industry").forEach((tile) => drawIndustry(context, tile, theme));
  tiles.filter((tile) => tile.kind === "symbol").forEach((tile) => drawSymbol(context, tile, hoveredTile?.id, theme));
}

function drawSector(context: CanvasRenderingContext2D, tile: TreeMapTile, theme: TreeMapTheme) {
  if (tile.width < 92 || tile.height < 34) {
    return;
  }
  const nameFont = `500 14px ${theme.serif}`;
  const changeFont = `500 12px ${theme.serif}`;
  const changeText = formatChange(tile.changePercent);
  context.textBaseline = "top";
  context.font = changeFont;
  const changeWidth = context.measureText(changeText).width;
  const canShowChange = tile.width >= 146;
  const labelMaxWidth = tile.width - labelPadding * 2 - (canShowChange ? changeWidth + 12 : 0);
  context.font = nameFont;
  context.fillStyle = theme.colors.text;
  fillFittedText(context, tile.label, tile.x + labelPadding, tile.y + 7, labelMaxWidth);
  if (canShowChange) {
    context.font = changeFont;
    context.textAlign = "right";
    context.fillStyle = toneForChange(tile.changePercent) === "down" ? theme.colors.changeDown : theme.colors.changeUp;
    context.fillText(changeText, tile.x + tile.width - labelPadding, tile.y + 8);
    context.textAlign = "start";
  }
}

function drawIndustry(context: CanvasRenderingContext2D, tile: TreeMapTile, theme: TreeMapTheme) {
  if (tile.width < 70 || tile.height < 24) {
    return;
  }
  context.save();
  context.font = `500 9px ${theme.serif}`;
  context.fillStyle = theme.colors.muted;
  context.globalAlpha = 0.64;
  context.textBaseline = "top";
  fillFittedText(context, tile.label, tile.x + 5, tile.y + 3, tile.width - 10);
  context.restore();
}

function drawSymbol(
  context: CanvasRenderingContext2D,
  tile: TreeMapTile,
  hoveredTileId: string | undefined,
  theme: TreeMapTheme
) {
  const hovered = hoveredTileId === tile.id;
  const rect = insetTile(tile, tileGap);
  if (rect.width <= 0 || rect.height <= 0) {
    return;
  }
  context.fillStyle = hovered ? theme.colors.text : tileFillForChange(tile.changePercent, theme.colors);
  context.globalAlpha = hovered ? 1 : tileOpacityForChange(tile.changePercent);
  context.fillRect(rect.x, rect.y, rect.width, rect.height);
  context.globalAlpha = 1;

  const labelSpace = rect.width - 10;
  if (rect.width < 38 || rect.height < 27 || labelSpace < 24) {
    return;
  }
  const symbolSize = clamp(Math.min(rect.width / 5.8, rect.height / 3.4), 11, 25);
  const textColor = hovered ? theme.colors.background : tileTextForChange(tile.changePercent, theme.colors);
  context.font = `500 ${symbolSize}px ${theme.serif}`;
  context.fillStyle = textColor;
  context.textBaseline = "top";
  fillFittedText(context, tile.label, rect.x + 6, rect.y + 6, labelSpace);

  if (rect.height < 44) {
    return;
  }
  context.font = `500 ${Math.max(10, symbolSize * 0.72)}px ${theme.serif}`;
  context.fillStyle = hovered ? changeTextColor(tile.changePercent, theme) : textColor;
  fillFittedText(context, formatChange(tile.changePercent), rect.x + 6, rect.y + 8 + symbolSize, labelSpace);
}

function changeTextColor(changePercent: number | undefined, theme: TreeMapTheme): string {
  return toneForChange(changePercent) === "down" ? theme.colors.down : theme.colors.up;
}

function fillFittedText(
  context: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  maxWidth: number
) {
  if (maxWidth <= 8) {
    return;
  }
  let fitted = text;
  while (fitted.length > 1 && context.measureText(fitted).width > maxWidth) {
    fitted = `${fitted.slice(0, Math.max(1, fitted.length - 4))}...`;
  }
  context.fillText(fitted, x, y);
}

function formatChange(value: number | undefined): string {
  if (!Number.isFinite(value)) {
    return "0.00%";
  }
  const numeric = Number(value);
  const sign = numeric > 0 ? "+" : "";
  return `${sign}${numeric.toFixed(2)}%`;
}

type TreeMapTheme = {
  serif: string;
  colors: ThemeColors;
};

function readTheme(): TreeMapTheme {
  const root = getComputedStyle(document.documentElement);
  return {
    serif: root.getPropertyValue("--font-ui-serif").trim() || "\"Times New Roman\", Times, Georgia, serif",
    colors: readThemeColors()
  };
}

function insetTile(tile: TreeMapTile, gap: number) {
  const inset = Math.min(gap, tile.width / 3, tile.height / 3);
  return {
    x: tile.x + inset,
    y: tile.y + inset,
    width: Math.max(0, tile.width - inset * 2),
    height: Math.max(0, tile.height - inset * 2)
  };
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
