import type { ChartAction, ChartState, DrawingEntity } from "../chart/types";

type ChartAgentRequest = {
  prompt: string;
  chart: ChartState;
};

export type ChartAgentResponse = {
  message: string;
  actions: ChartAction[];
  insights: string[];
};

const allowedActionTypes = new Set<ChartAction["type"]>([
  "setSymbol",
  "setInterval",
  "setTool",
  "toggleLayer",
  "setViewport",
  "addDrawing",
  "updateDrawing",
  "deleteDrawing",
  "selectDrawing",
  "clearDrawings"
]);

export async function requestChartAgentActions(request: ChartAgentRequest): Promise<ChartAgentResponse> {
  const response = await fetch("/api/chart-agent/actions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(toAgentPayload(request))
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const message = typeof payload?.message === "string" ? payload.message : `차트 에이전트 요청 실패: ${response.status}`;
    throw new Error(message);
  }
  return normalizeAgentResponse(payload);
}

function toAgentPayload({ prompt, chart }: ChartAgentRequest) {
  const lookback = Math.max(80, chart.visibleCount + Math.max(0, chart.rightOffset) + 80);
  const visibleCandles = chart.candles.slice(-Math.min(chart.candles.length, lookback));
  return {
    prompt,
    panel: {
      symbol: chart.symbol,
      interval: chart.interval,
      visibleCount: chart.visibleCount,
      rightOffset: chart.rightOffset,
      layers: chart.layers,
      toolMode: chart.toolMode,
      trendLineExtension: chart.trendLineExtension,
      drawings: chart.drawings
    },
    candles: visibleCandles,
    availableActions: Array.from(allowedActionTypes)
  };
}

function normalizeAgentResponse(payload: unknown): ChartAgentResponse {
  if (!payload || typeof payload !== "object") {
    throw new Error("차트 에이전트 응답이 올바르지 않습니다.");
  }
  const source = payload as ChartAgentResponse;
  const actions = Array.isArray(source.actions) ? source.actions.filter(isChartAction) : [];
  return {
    message: typeof source.message === "string" ? source.message : "차트 에이전트가 작업을 제안했습니다.",
    actions,
    insights: Array.isArray(source.insights) ? source.insights.filter((item): item is string => typeof item === "string") : []
  };
}

function isChartAction(action: unknown): action is ChartAction {
  if (!action || typeof action !== "object") {
    return false;
  }
  const candidate = action as ChartAction;
  if (!allowedActionTypes.has(candidate.type)) {
    return false;
  }
  if (candidate.type === "addDrawing") {
    return isDrawingEntity(candidate.drawing);
  }
  return true;
}

function isDrawingEntity(value: unknown): value is DrawingEntity {
  if (!value || typeof value !== "object") {
    return false;
  }
  const drawing = value as DrawingEntity;
  return (
    typeof drawing.id === "string" &&
    typeof drawing.type === "string" &&
    Array.isArray(drawing.anchors) &&
    typeof drawing.style === "object" &&
    typeof drawing.createdAt === "string" &&
    typeof drawing.updatedAt === "string"
  );
}
