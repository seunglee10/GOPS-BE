import { useEffect, useMemo, useState } from "react";
import { Activity, AlertTriangle, Bot } from "lucide-react";
import { calculateGraph } from "./calculations/indicators";
import { defaultIndicatorRegistry } from "./calculations/indicatorRegistry";
import { summarizeMarket } from "./calculations/marketSummary";
import { isCommandTypeUserEnabled } from "./capabilities/chartCapabilities";
import { ChatPanel, type ChatMessage } from "./components/ChatPanel";
import { ChartPanel } from "./components/ChartPanel";
import { ProposalPanel } from "./components/ProposalPanel";
import { applyEventBatch, applySnapshot, type CandlesBySymbol } from "./market/candleStore";
import { MarketClient } from "./market/marketClient";
import { getAvailableCommandTypesForPinMode } from "./registries/commandRegistry";
import { applyCommand, createCommandId, ingestIncomingProposalsWithPolicy, nowIso } from "./state/commandEngine";
import { createDefaultWorkspace } from "./state/createDefaultWorkspace";
import { pendingProposals } from "./state/proposalStore";
import type { CalculationOutput, MarketSummary } from "./types/calculations";
import type { Command, CommandTarget } from "./types/commands";
import type { ChartDocument, ChartViewport, LayerDocument } from "./types/documents";
import type { ChatErrorResponse, ChatRequest, ChatResponse, LlmInsight } from "./types/llm";
import type { Candle, StreamStatus } from "./types/market";
import { selectVisibleCandles } from "./renderer/sceneBuilder";

const MARKET_SNAPSHOT_LIMIT = 1000;

export function App(): JSX.Element {
  const [workspace, setWorkspace] = useState(() => createDefaultWorkspace());
  const [candlesBySymbol, setCandlesBySymbol] = useState<CandlesBySymbol>({});
  const [streamStatus, setStreamStatus] = useState<StreamStatus>("idle");
  const [streamError, setStreamError] = useState<string>("");
  const [chatStatus, setChatStatus] = useState<"idle" | "pending" | "success" | "error">("idle");
  const [chatError, setChatError] = useState<string>("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [insights, setInsights] = useState<LlmInsight[]>([]);
  const [commandErrors, setCommandErrors] = useState<string[]>([]);

  const chart = workspace.charts.find((item) => item.id === workspace.activeChartId) ?? workspace.charts[0];
  const chartPanel = workspace.panels.find((panel) => panel.id === "panel-chart-main") ?? workspace.panels[0];
  const comparisonSymbols = useMemo(
    () =>
      Array.from(
        new Set(
          chart.layers
            .filter((layer) => layer.type === "comparisonSeries")
            .map((layer) => (layer.type === "comparisonSeries" ? layer.symbol : ""))
            .filter(Boolean)
        )
      ),
    [chart.layers]
  );
  const subscribedSymbols = useMemo(() => Array.from(new Set([chart.symbol, ...comparisonSymbols])), [chart.symbol, comparisonSymbols]);
  const activeCandles = candlesBySymbol[chart.symbol] ?? [];
  const visibleCandles = useMemo(() => selectVisibleCandles(activeCandles, chart.viewport), [activeCandles, chart.viewport]);
  const proposals = useMemo(() => pendingProposals(workspace), [workspace]);
  const proposalPreview = useMemo(() => buildProposalPreview(proposals), [proposals]);
  const renderChart = useMemo<ChartDocument>(
    () => ({
      ...chart,
      layers: [...chart.layers, ...proposalPreview.layers],
      calculationGraph: { nodes: [...chart.calculationGraph.nodes, ...proposalPreview.nodes] }
    }),
    [chart, proposalPreview]
  );
  const calculationOutputs = useMemo<Record<string, CalculationOutput>>(
    () => calculateGraph(activeCandles, chart.calculationGraph, defaultIndicatorRegistry),
    [activeCandles, chart.calculationGraph]
  );
  const renderCalculationOutputs = useMemo<Record<string, CalculationOutput>>(
    () => calculateGraph(activeCandles, renderChart.calculationGraph, defaultIndicatorRegistry),
    [activeCandles, renderChart.calculationGraph]
  );
  const marketSummary = useMemo(
    () => summarizeMarket(chart.symbol, chart.timeframe, visibleCandles, calculationOutputs),
    [calculationOutputs, chart.symbol, chart.timeframe, visibleCandles]
  );
  const headerSummary = useMemo(
    () => buildHeaderSummary(marketSummary, activeCandles),
    [activeCandles, marketSummary]
  );
  useEffect(() => {
    const client = new MarketClient({
      symbols: subscribedSymbols,
      timeframe: chart.timeframe,
      snapshotLimit: MARKET_SNAPSHOT_LIMIT,
      onSnapshot: (message) => setCandlesBySymbol((current) => applySnapshot(current, message)),
      onEvents: (message) => setCandlesBySymbol((current) => applyEventBatch(current, message)),
      onStatus: setStreamStatus,
      onError: setStreamError
    });
    client.connect();
    return () => client.disconnect();
  }, [subscribedSymbols.join(","), chart.timeframe]);

  function dispatch(command: Command): boolean {
    if (command.actor === "user" && !isCommandTypeUserEnabled(command.type)) {
      setCommandErrors([`Command type is not enabled for user actions: ${command.type}`]);
      return false;
    }
    const result = applyCommand(workspace, command);
    if (!result.ok) {
      setCommandErrors(result.errors.map((item) => item.message));
      return false;
    }
    setCommandErrors([]);
    setWorkspace(result.document);
    return true;
  }

  function baseTarget(paneId = "pane-price"): CommandTarget {
    return {
      workspaceId: workspace.id,
      panelId: "panel-chart-main",
      chartId: chart.id,
      paneId
    };
  }

  function handleViewportChange(patch: Partial<ChartViewport>): void {
    dispatch({
      id: createCommandId(),
      type: "chart.viewport.set",
      actor: "user",
      status: "applied",
      target: baseTarget(),
      payload: patch,
      createdAt: nowIso()
    });
  }

  function handleSymbolChange(symbol: string): void {
    dispatch({
      id: createCommandId(),
      type: "chart.symbol.set",
      actor: "user",
      status: "applied",
      target: baseTarget(),
      payload: { symbol },
      createdAt: nowIso()
    });
  }

  function handleTimeframeChange(timeframe: ChartDocument["timeframe"]): void {
    dispatch({
      id: createCommandId(),
      type: "chart.timeframe.set",
      actor: "user",
      status: "applied",
      target: baseTarget(),
      payload: { timeframe },
      createdAt: nowIso()
    });
  }

  async function sendChatMessage(message: string): Promise<void> {
    const userMessage: ChatMessage = { id: createCommandId("msg"), role: "user", text: message, createdAt: nowIso() };
    setMessages((current) => [...current, userMessage]);
    setChatStatus("pending");
    setChatError("");
    try {
      const request = buildChatRequest(message, workspace, chart, marketSummary);
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request)
      });
      const payload = (await response.json()) as ChatResponse | ChatErrorResponse;
      if (!response.ok) {
        const errorPayload = payload as ChatErrorResponse;
        throw new Error(errorPayload.error?.message ?? "Chat request failed.");
      }
      const chatResponse = payload as ChatResponse;
      setMessages((current) => [
        ...current,
        {
          id: chatResponse.id,
          role: "assistant",
          text: chatResponse.message,
          createdAt: chatResponse.createdAt
        }
      ]);
      setInsights(chatResponse.insights);
      setWorkspace((current) => {
        const ingestResult = ingestIncomingProposalsWithPolicy(current, chatResponse.chartProposals);
        setCommandErrors(ingestResult.errors.map((item) => item.message));
        return ingestResult.document;
      });
      setChatStatus("success");
    } catch (error) {
      setChatStatus("error");
      setChatError(error instanceof Error ? error.message : "Chat request failed.");
    }
  }

  function acceptProposal(proposalId: string): void {
    dispatch({
      id: createCommandId(),
      type: "proposal.accept",
      actor: "user",
      status: "accepted",
      target: baseTarget(),
      payload: { proposalId },
      createdAt: nowIso()
    });
  }

  function rejectProposal(proposalId: string): void {
    dispatch({
      id: createCommandId(),
      type: "proposal.reject",
      actor: "user",
      status: "rejected",
      target: baseTarget(),
      payload: { proposalId },
      createdAt: nowIso()
    });
  }

  return (
    <main className="app-shell">
      <section className="chart-region">
        <div className="app-titlebar">
          <div>
            <span className="product-mark">GOPS</span>
            <span className="workspace-version">v{workspace.version}</span>
          </div>
          <div className={`stream-pill stream-${streamStatus}`}>
            <Activity size={15} />
            <span>{streamStatus}</span>
          </div>
        </div>
        <ChartPanel
          chart={chart}
          renderChart={renderChart}
          chartPanel={chartPanel}
          candlesBySymbol={candlesBySymbol}
          calculationOutputs={renderCalculationOutputs}
          streamStatus={streamStatus}
          streamError={streamError}
          marketSummary={headerSummary}
          latestPrice={marketSummary.latestPrice}
          onDispatch={dispatch}
          onViewportChange={handleViewportChange}
          onSymbolChange={handleSymbolChange}
          onTimeframeChange={handleTimeframeChange}
        />
      </section>
      <aside className="side-region">
        <ChatPanel
          status={chatStatus}
          error={chatError}
          messages={messages}
          insights={insights}
          onSend={sendChatMessage}
        />
      </aside>
      <section className="proposal-region">
        <ProposalPanel proposals={proposals} onAccept={acceptProposal} onReject={rejectProposal} />
        {commandErrors.length > 0 ? (
          <div className="command-errors" role="alert">
            <AlertTriangle size={15} />
            <span>{commandErrors[0]}</span>
          </div>
        ) : (
          <div className="command-errors command-errors-empty">
            <Bot size={15} />
            <span>{workspace.proposals.filter((proposal) => proposal.status === "invalid").length} invalid</span>
          </div>
        )}
      </section>
    </main>
  );
}

function buildChatRequest(
  message: string,
  workspace: ReturnType<typeof createDefaultWorkspace>,
  chart: ChartDocument,
  market: ChatRequest["market"]
): ChatRequest {
  const activePanel = workspace.panels.find((panel) => panel.id === workspace.activePanelId);
  return {
    message,
    workspace: {
      activePanelId: workspace.activePanelId,
      activeChartId: workspace.activeChartId,
      panels: workspace.panels.map((panel) => ({
        id: panel.id,
        type: panel.type,
        title: panel.title,
        pinMode: panel.pinMode,
        targetChartId: panel.targetChartId
      })),
      pendingProposalCount: workspace.proposals.filter((proposal) => proposal.status === "pending").length
    },
    chart: {
      id: chart.id,
      symbol: chart.symbol,
      timeframe: chart.timeframe,
      viewport: chart.viewport,
      panes: chart.panes.map((pane) => ({ id: pane.id, kind: pane.kind, title: pane.title })),
      layers: chart.layers.map((layer) => ({
        id: layer.id,
        type: layer.type,
        paneId: layer.paneId,
        owner: layer.owner,
        visible: layer.visible,
        summary: summarizeLayer(layer)
      })),
      availableCommands: getAvailableCommandTypesForPinMode(activePanel?.pinMode ?? "approval")
    },
    market
  };
}

function summarizeLayer(layer: ChartDocument["layers"][number]): string {
  if (layer.type === "priceSeries") return `${layer.seriesType} price series`;
  if (layer.type === "volume") return `${layer.volumeMode} volume`;
  if (layer.type === "indicator") return `indicator node ${layer.calculationNodeId}`;
  if (layer.type === "comparisonSeries") return `comparison ${layer.symbol}`;
  if (layer.type === "drawing") return `${layer.drawing.kind} drawing`;
  return "AI proposal preview";
}

function buildHeaderSummary(base: MarketSummary, candles: Candle[]): MarketSummary {
  const latest = candles[candles.length - 1];
  const previous = candles[candles.length - 2];
  if (!latest || !previous || previous.close === 0) return base;
  return {
    ...base,
    latestPrice: latest.close,
    latestTimestamp: latest.timestamp,
    liveChangePercent: Number((((latest.close - previous.close) / previous.close) * 100).toFixed(2))
  };
}

function buildProposalPreview(proposals: ReturnType<typeof pendingProposals>): {
  layers: LayerDocument[];
  nodes: ChartDocument["calculationGraph"]["nodes"];
} {
  const layers: LayerDocument[] = [];
  const nodes: ChartDocument["calculationGraph"]["nodes"] = [];
  for (const proposal of proposals) {
    for (const command of proposal.commands) {
      if (command.type === "chart.indicator.add") {
        const node = { ...command.payload.node, id: `preview-${command.payload.node.id}` };
        nodes.push(node);
        layers.push({
          ...command.payload.layer,
          id: `preview-${command.payload.layer.id}`,
          owner: "ai",
          visible: true,
          locked: true,
          calculationNodeId: node.id,
          style: {
            ...command.payload.layer.style,
            color: command.payload.layer.style.color ?? "#f59e0b",
            lineWidth: command.payload.layer.style.lineWidth ?? 1.4,
            lineDash: [5, 5],
            opacity: 0.85
          }
        });
      }
      if (command.type === "chart.drawing.add") {
        layers.push({
          ...command.payload.layer,
          id: `preview-${command.payload.layer.id}`,
          owner: "ai",
          visible: true,
          locked: true,
          style: {
            ...command.payload.layer.style,
            color: command.payload.layer.style.color ?? "#38bdf8",
            lineDash: [5, 5],
            opacity: 0.85
          }
        });
      }
      if (command.type === "chart.comparison.add") {
        layers.push({
          ...command.payload.layer,
          id: `preview-${command.payload.layer.id}`,
          owner: "ai",
          visible: true,
          locked: true,
          style: {
            ...command.payload.layer.style,
            color: command.payload.layer.style.color ?? "#a78bfa",
            lineDash: [5, 5],
            opacity: 0.85
          }
        });
      }
    }
  }
  return { layers, nodes };
}
