import type { CandleData, DrawingAnchor, RenderScene } from "./types";

export type PaneLayout = {
  price: {
    left: number;
    top: number;
    right: number;
    bottom: number;
  };
  volume: {
    left: number;
    top: number;
    right: number;
    bottom: number;
  };
};

export type TimeScale = {
  visibleStartIndex: number;
  visibleEndIndex: number;
  slotWidth: number;
  candleCenter: (visibleIndex: number) => number;
  logicalToX: (logicalIndex: number) => number;
  xToLogical: (x: number) => number;
  timestampToX: (timestamp: string) => number | null;
  xToNearestCandle: (x: number) => CandleData | undefined;
};

export type PriceScale = {
  min: number;
  max: number;
  priceToY: (price: number) => number;
  yToPrice: (y: number) => number;
};

export type PercentScale = {
  min: number;
  max: number;
  percentToY: (percent: number) => number;
  yToPercent: (y: number) => number;
};

export type VolumeScale = {
  max: number;
  volumeToY: (volume: number) => number;
};

export type CoordinateTransform = {
  paneLayout: PaneLayout;
  timeScale: TimeScale;
  priceScale: PriceScale;
  percentScale: PercentScale;
  volumeScale: VolumeScale;
  anchorToPoint: (anchor: DrawingAnchor) => { x: number; y: number } | null;
  pointToAnchor: (x: number, y: number, symbol: string) => DrawingAnchor | null;
};

export function createTimeScale({
  candles,
  visibleCandles,
  visibleStartIndex,
  visibleEndIndex,
  left,
  right
}: {
  candles: CandleData[];
  visibleCandles: CandleData[];
  visibleStartIndex: number;
  visibleEndIndex: number;
  left: number;
  right: number;
}): TimeScale {
  const timestampIndex = new Map(candles.map((candle, index) => [candle.timestamp, index]));
  const width = Math.max(1, right - left);
  const slotWidth = width / Math.max(1, visibleCandles.length);
  const candleCenter = (visibleIndex: number) => left + slotWidth * visibleIndex + slotWidth / 2;
  const logicalToX = (logicalIndex: number) => candleCenter(logicalIndex - visibleStartIndex);

  return {
    visibleStartIndex,
    visibleEndIndex,
    slotWidth,
    candleCenter,
    logicalToX,
    xToLogical: (x: number) => visibleStartIndex + (x - left) / Math.max(1, slotWidth),
    timestampToX: (timestamp: string) => {
      const logicalIndex = timestampIndex.get(timestamp);
      return typeof logicalIndex === "number" ? logicalToX(logicalIndex) : null;
    },
    xToNearestCandle: (x: number) => {
      const visibleIndex = Math.max(0, Math.min(visibleCandles.length - 1, Math.round((x - left - slotWidth / 2) / Math.max(1, slotWidth))));
      return visibleCandles[visibleIndex];
    }
  };
}

export function createPriceScale(min: number, max: number, top: number, bottom: number): PriceScale {
  const range = Math.max(0.0001, max - min);
  return {
    min,
    max,
    priceToY: (price: number) => top + ((max - price) / range) * Math.max(1, bottom - top),
    yToPrice: (y: number) => max - ((y - top) / Math.max(1, bottom - top)) * range
  };
}

export function createPercentScale(min: number, max: number, top: number, bottom: number): PercentScale {
  const range = Math.max(0.0001, max - min);
  return {
    min,
    max,
    percentToY: (percent: number) => top + ((max - percent) / range) * Math.max(1, bottom - top),
    yToPercent: (y: number) => max - ((y - top) / Math.max(1, bottom - top)) * range
  };
}

export function createVolumeScale(max: number, top: number, bottom: number): VolumeScale {
  const height = Math.max(1, bottom - top);
  const safeMax = Math.max(1, max);
  return {
    max: safeMax,
    volumeToY: (volume: number) => bottom - (volume / safeMax) * height
  };
}

export function createCoordinateTransform(scene: Pick<RenderScene, "allCandles" | "candles" | "document" | "plot" | "visibleStartIndex" | "visibleEndIndex" | "scales">): CoordinateTransform {
  const paneLayout: PaneLayout = {
    price: {
      left: scene.plot.left,
      top: scene.plot.top,
      right: scene.plot.right,
      bottom: scene.plot.priceBottom
    },
    volume: {
      left: scene.plot.left,
      top: scene.plot.volumeTop,
      right: scene.plot.right,
      bottom: scene.plot.bottom
    }
  };
  const timeScale = createTimeScale({
    candles: scene.allCandles,
    visibleCandles: scene.candles,
    visibleStartIndex: scene.visibleStartIndex,
    visibleEndIndex: scene.visibleEndIndex,
    left: scene.plot.left,
    right: scene.plot.right
  });
  const priceScale = createPriceScale(scene.scales.minPrice, scene.scales.maxPrice, scene.plot.top, scene.plot.priceBottom);
  const percentScale = createPercentScale(scene.scales.minPercent, scene.scales.maxPercent, scene.plot.top, scene.plot.priceBottom);
  const volumeScale = createVolumeScale(scene.scales.maxVolume, scene.plot.volumeTop, scene.plot.bottom);

  return {
    paneLayout,
    timeScale,
    priceScale,
    percentScale,
    volumeScale,
    anchorToPoint: (anchor: DrawingAnchor) => {
      const anchoredX = typeof anchor.logicalIndex === "number"
        ? timeScale.logicalToX(anchor.logicalIndex)
        : anchor.timestamp ? timeScale.timestampToX(anchor.timestamp) : null;
      const value = typeof anchor.price === "number" ? anchor.price : anchor.value;
      if (typeof value !== "number") {
        return null;
      }
      const x = anchoredX ?? scene.plot.left;
      const y = anchor.paneId === "volume" ? volumeScale.volumeToY(value) : priceScale.priceToY(value);
      return { x, y };
    },
    pointToAnchor: (x: number, y: number, symbol: string) => {
      const candle = timeScale.xToNearestCandle(x);
      if (!candle) {
        return null;
      }
      return {
        timestamp: candle.timestamp,
        logicalIndex: Math.max(0, Math.min(scene.allCandles.length - 1, Math.round(timeScale.xToLogical(x)))),
        price: priceScale.yToPrice(y),
        paneId: "price",
        symbol
      };
    }
  };
}
