import type { WorkspaceDocument } from "../types/documents";

const EPOCH = "1970-01-01T00:00:00.000Z";

export function createDefaultWorkspace(): WorkspaceDocument {
  return {
    id: "workspace-main",
    version: 1,
    activePanelId: "panel-chart-main",
    activeChartId: "chart-main",
    panels: [
      {
        id: "panel-chart-main",
        type: "chart",
        title: "Chart",
        pinMode: "approval",
        owner: "user",
        layout: {
          area: "main",
          order: 0,
          minWidthPx: 640,
          minHeightPx: 420
        },
        targetChartId: "chart-main",
        visible: true,
        config: {
          chartId: "chart-main",
          toolMode: "select",
          showCrosshair: true,
          toolsCollapsed: false
        }
      },
      {
        id: "panel-chat",
        type: "chat",
        title: "Chat",
        pinMode: "locked",
        owner: "user",
        layout: {
          area: "right",
          order: 1,
          widthPx: 380,
          minWidthPx: 320,
          minHeightPx: 420
        },
        visible: true,
        config: {
          scopedPanelIds: ["panel-chart-main"]
        }
      },
      {
        id: "panel-proposals",
        type: "proposalList",
        title: "AI Proposals",
        pinMode: "locked",
        owner: "system",
        layout: {
          area: "bottom",
          order: 0,
          heightPx: 180,
          minWidthPx: 640,
          minHeightPx: 120
        },
        visible: true,
        config: {
          scopedChartIds: ["chart-main"]
        }
      }
    ],
    charts: [
      {
        id: "chart-main",
        symbol: "AAPL",
        timeframe: "1m",
        provider: "dummy",
        viewport: {
          mode: "followRealtime",
          visibleBars: 180,
          rightOffsetBars: 0,
          logicalFrom: 0,
          logicalTo: 179,
          minVisibleBars: 20,
          maxVisibleBars: 1000
        },
        panes: [
          {
            id: "pane-price",
            kind: "price",
            title: "Price",
            order: 0,
            heightRatio: 0.75,
            minHeightPx: 280,
            yScale: {
              scaleId: "scale-price-right",
              mode: "price",
              position: "right",
              autoScale: true
            },
            visible: true
          },
          {
            id: "pane-volume",
            kind: "volume",
            title: "Volume",
            order: 1,
            heightRatio: 0.25,
            minHeightPx: 120,
            yScale: {
              scaleId: "scale-volume-right",
              mode: "volume",
              position: "right",
              autoScale: true
            },
            visible: true
          }
        ],
        layers: [
          {
            id: "layer-price-candles",
            type: "priceSeries",
            owner: "system",
            paneId: "pane-price",
            zIndex: 100,
            visible: true,
            locked: true,
            dataBinding: {
              bindingId: "binding-aapl-1m-candles"
            },
            style: {},
            seriesType: "candlestick",
            createdAt: EPOCH,
            updatedAt: EPOCH
          },
          {
            id: "layer-volume-bars",
            type: "volume",
            owner: "system",
            paneId: "pane-volume",
            zIndex: 100,
            visible: true,
            locked: true,
            dataBinding: {
              bindingId: "binding-aapl-1m-candles"
            },
            style: {},
            volumeMode: "bar",
            createdAt: EPOCH,
            updatedAt: EPOCH
          }
        ],
        dataBindings: [
          {
            id: "binding-aapl-1m-candles",
            source: "marketCandles",
            symbol: "AAPL",
            timeframe: "1m"
          }
        ],
        calculationGraph: {
          nodes: []
        },
        style: {
          theme: "dark",
          backgroundColor: "#0f1115",
          gridColor: "#252a33",
          textColor: "#d4d7dd",
          upColor: "#22c55e",
          downColor: "#ef4444",
          accentColor: "#f59e0b"
        },
        interactionState: {},
        createdAt: EPOCH,
        updatedAt: EPOCH
      }
    ],
    proposals: [],
    commandJournal: [],
    createdAt: EPOCH,
    updatedAt: EPOCH
  };
}
