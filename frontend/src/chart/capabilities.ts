import type { ChartCapability } from "./types";

export const chartCapabilities: ChartCapability[] = [
  {
    id: "chart-symbol",
    label: "Set symbol",
    description: "Change the active chart symbol and request a fresh candle snapshot.",
    commandTypes: ["chart.symbol.set"],
    payloadSchema: { type: "object", required: ["symbol"], properties: { symbol: { type: "string" } } },
    requiredContext: ["chartDocumentId"],
    previewable: true,
    autoApplyEligible: false,
    undoScope: "chart",
    conflictsWith: [],
    recommendedWith: ["chart-timeframe", "chart-viewport"],
    validationRules: ["symbol must be supported by the active market data provider"]
  },
  {
    id: "chart-timeframe",
    label: "Set timeframe",
    description: "Change the active candle interval.",
    commandTypes: ["chart.timeframe.set"],
    payloadSchema: { type: "object", required: ["timeframe"], properties: { timeframe: { enum: ["1m", "5m", "10m"] } } },
    requiredContext: ["chartDocumentId"],
    previewable: true,
    autoApplyEligible: true,
    undoScope: "chart",
    conflictsWith: [],
    recommendedWith: ["chart-symbol", "chart-viewport"],
    validationRules: ["timeframe must be one of 1m, 5m, 10m"]
  },
  {
    id: "chart-viewport",
    label: "Set viewport",
    description: "Pan, zoom, or reset the visible candle range without changing market data.",
    commandTypes: ["chart.viewport.set"],
    payloadSchema: {
      type: "object",
      properties: {
        visibleCount: { type: "number", minimum: 12, maximum: 180 },
        rightOffset: { type: "number", minimum: 0 }
      }
    },
    requiredContext: ["visibleRange", "chartDocumentId"],
    previewable: true,
    autoApplyEligible: true,
    undoScope: "chart",
    conflictsWith: [],
    recommendedWith: ["chart-layer-visibility"],
    validationRules: ["visibleCount and rightOffset are clamped to safe numeric bounds"]
  },
  {
    id: "chart-layer-visibility",
    label: "Layer visibility",
    description: "Show or hide chart layers such as MA lines or volume.",
    commandTypes: ["chart.layer.visibility.set"],
    payloadSchema: {
      type: "object",
      required: ["layer", "visible"],
      properties: {
        layer: { enum: ["candles", "volume", "ma5", "ma20", "ma60"] },
        visible: { type: "boolean" }
      }
    },
    requiredContext: ["activeLayers", "chartDocumentId"],
    previewable: true,
    autoApplyEligible: true,
    undoScope: "chart",
    conflictsWith: [],
    recommendedWith: ["chart-viewport"],
    validationRules: ["layer must exist and visible must be boolean"]
  },
  {
    id: "chart-drawing",
    label: "Drawing annotations",
    description: "Add, update, select, or remove editable data-coordinate chart drawings.",
    commandTypes: [
      "chart.drawing.add",
      "chart.drawing.update",
      "chart.drawing.remove",
      "chart.drawing.select",
      "chart.drawing.clearSelection"
    ],
    payloadSchema: { type: "object", properties: { drawingType: { type: "string" }, anchors: { type: "array" } } },
    requiredContext: ["chartDocumentId", "visibleRange", "coordinateTransform"],
    previewable: true,
    autoApplyEligible: false,
    undoScope: "chart",
    conflictsWith: [],
    recommendedWith: ["chart-preview", "chart-measurement", "chart-comparison"],
    validationRules: ["drawing anchors must use timestamp/price/pane/symbol data coordinates", "pixel coordinates are rejected"]
  },
  {
    id: "chart-preview",
    label: "Proposal preview",
    description: "Show, hide, apply, or clear the single pending LLM drawing/comparison preview.",
    commandTypes: ["chart.preview.set", "chart.preview.toggle", "chart.preview.apply", "chart.preview.clear"],
    payloadSchema: { type: "object", properties: { preview: { type: "object" }, previewVisible: { type: "boolean" } } },
    requiredContext: ["chartDocumentId"],
    previewable: true,
    autoApplyEligible: false,
    undoScope: "none",
    conflictsWith: [],
    recommendedWith: ["chart-drawing", "chart-comparison"],
    validationRules: ["pendingPreview does not mutate ChartDocument.drawings", "apply preview creates one grouped chart history entry"]
  },
  {
    id: "chart-comparison",
    label: "Comparison overlay",
    description: "Compare another symbol on a percent scale without distorting the main price scale.",
    commandTypes: ["chart.comparison.add", "chart.comparison.remove", "chart.comparison.update"],
    payloadSchema: { type: "object", properties: { comparison: { type: "object" }, comparisonId: { type: "string" } } },
    requiredContext: ["chartDocumentId", "visibleRange", "marketDataAvailability"],
    previewable: true,
    autoApplyEligible: false,
    undoScope: "chart",
    conflictsWith: [],
    recommendedWith: ["chart-viewport", "chart-drawing"],
    validationRules: ["comparison uses percent scale", "comparison line must not mutate main price scale"]
  },
  {
    id: "chart-measurement",
    label: "Measurement",
    description: "Measure price change, percent change, and duration between two anchors.",
    commandTypes: ["chart.measurement.add"],
    payloadSchema: { type: "object", properties: { anchors: { type: "array" } } },
    requiredContext: ["chartDocumentId", "coordinateTransform"],
    previewable: true,
    autoApplyEligible: false,
    undoScope: "chart",
    conflictsWith: [],
    recommendedWith: ["chart-drawing"],
    validationRules: ["measurement requires two data-coordinate anchors"]
  }
];
