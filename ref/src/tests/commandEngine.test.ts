import { describe, expect, it } from "vitest";
import { getLlmEnabledCommandTypes } from "../capabilities/chartCapabilities";
import { applyCommand, createCommandId, ingestIncomingProposalsWithPolicy, nowIso, validateCommand } from "../state/commandEngine";
import { getAvailableCommandTypesForPinMode } from "../state/commandRegistry";
import { createDefaultWorkspace } from "../state/createDefaultWorkspace";
import type { Command } from "../types/commands";
import type { DrawingLayer, IndicatorLayer, ComparisonSeriesLayer } from "../types/documents";

function target(panelId = "panel-chart-main") {
  return {
    workspaceId: "workspace-main",
    panelId,
    chartId: "chart-main",
    paneId: "pane-price"
  };
}

function indicatorLayer(id = "layer-sma"): IndicatorLayer {
  return {
    id,
    type: "indicator",
    owner: "ai",
    paneId: "pane-price",
    zIndex: 200,
    visible: true,
    locked: false,
    style: { color: "#f59e0b" },
    calculationNodeId: "calc-sma",
    renderMode: "line",
    createdAt: nowIso(),
    updatedAt: nowIso()
  };
}

function drawingLayer(id = "layer-level"): DrawingLayer {
  return {
    id,
    type: "drawing",
    owner: "ai",
    paneId: "pane-price",
    zIndex: 300,
    visible: true,
    locked: false,
    style: { color: "#38bdf8" },
    drawing: { kind: "horizontalLine", price: 100, label: "Level" },
    createdAt: nowIso(),
    updatedAt: nowIso()
  };
}

function comparisonLayer(id = "layer-msft", symbol = "MSFT"): ComparisonSeriesLayer {
  return {
    id,
    type: "comparisonSeries",
    owner: "ai",
    paneId: "pane-price",
    zIndex: 180,
    visible: true,
    locked: false,
    style: { color: "#a78bfa" },
    symbol,
    baselineMode: "firstVisibleClose",
    renderMode: "line",
    createdAt: nowIso(),
    updatedAt: nowIso()
  };
}

describe("command engine", () => {
  it("exposes LLM chart edit commands according to panel pin mode", () => {
    const locked = getAvailableCommandTypesForPinMode("locked");
    const approval = getAvailableCommandTypesForPinMode("approval");
    const auto = getAvailableCommandTypesForPinMode("auto");
    const manifestLlmCommands = getLlmEnabledCommandTypes();

    expect(locked).toEqual([]);
    expect(new Set(approval)).toEqual(new Set(manifestLlmCommands));
    expect(new Set(auto)).toEqual(new Set(manifestLlmCommands));
    expect(approval).toContain("chart.symbol.set");
    expect(approval).toContain("chart.indicator.update");
    expect(approval).toContain("chart.drawing.remove");
    expect(approval).not.toContain("panel.pinMode.set");
    expect(approval).not.toContain("panel.chartTool.set");
    expect(approval).not.toContain("proposal.accept");
  });

  it("creates a panel-local chart tool setup without a global manual tools panel", () => {
    const document = createDefaultWorkspace();

    expect(document.panels.map((panel) => panel.type)).toEqual(["chart", "chat", "proposalList"]);
    expect(document.panels.some((panel) => String(panel.type) === "manualChartTools")).toBe(false);
    expect(document.panels.find((panel) => panel.id === "panel-chart-main")?.config).toMatchObject({
      chartId: "chart-main",
      toolMode: "select",
      showCrosshair: true
    });
  });

  it("adds an indicator node and layer", () => {
    const document = createDefaultWorkspace();
    const command: Command = {
      id: createCommandId(),
      type: "chart.indicator.add",
      actor: "user",
      status: "applied",
      target: target(),
      payload: {
        node: { id: "calc-sma", type: "SMA", inputs: { source: "close", period: 20 }, outputKey: "sma-20" },
        layer: indicatorLayer()
      },
      createdAt: nowIso()
    };

    const result = applyCommand(document, command);

    expect(result.ok).toBe(true);
    const chart = result.document.charts[0];
    expect(chart.calculationGraph.nodes).toHaveLength(1);
    expect(chart.layers.some((layer) => layer.id === "layer-sma")).toBe(true);
  });

  it("adds an indicator pane when the indicator command supplies a new pane", () => {
    const document = createDefaultWorkspace();
    const command: Command = {
      id: createCommandId(),
      type: "chart.indicator.add",
      actor: "user",
      status: "applied",
      target: target(),
      payload: {
        pane: {
          id: "pane-indicator-rsi",
          kind: "indicator",
          title: "Relative Strength Index",
          order: 2,
          heightRatio: 0.22,
          minHeightPx: 120,
          yScale: {
            scaleId: "scale-pane-indicator-rsi-right",
            mode: "oscillator",
            position: "right",
            autoScale: false,
            min: 0,
            max: 100
          },
          visible: true
        },
        node: { id: "calc-rsi", type: "RSI", inputs: { source: "close", period: 14 }, outputKey: "rsi-14" },
        layer: {
          ...indicatorLayer("layer-rsi"),
          paneId: "pane-indicator-rsi",
          calculationNodeId: "calc-rsi"
        }
      },
      createdAt: nowIso()
    };

    const result = applyCommand(document, command);

    expect(result.ok).toBe(true);
    expect(result.document.charts[0].panes.some((pane) => pane.id === "pane-indicator-rsi")).toBe(true);
    expect(result.document.charts[0].layers.find((layer) => layer.id === "layer-rsi")?.paneId).toBe("pane-indicator-rsi");
  });

  it("adds a drawing layer", () => {
    const document = createDefaultWorkspace();
    const command: Command = {
      id: createCommandId(),
      type: "chart.drawing.add",
      actor: "user",
      status: "applied",
      target: target(),
      payload: { layer: drawingLayer() },
      createdAt: nowIso()
    };

    const result = applyCommand(document, command);

    expect(result.ok).toBe(true);
    expect(result.document.charts[0].layers.some((layer) => layer.type === "drawing")).toBe(true);
  });

  it("applies user chart commands without creating proposals", () => {
    const document = createDefaultWorkspace();
    const result = applyCommand(document, {
      id: createCommandId(),
      type: "chart.drawing.add",
      actor: "user",
      status: "applied",
      target: target(),
      payload: { layer: drawingLayer("layer-user-level") },
      createdAt: nowIso()
    });

    expect(result.ok).toBe(true);
    expect(result.document.proposals).toHaveLength(document.proposals.length);
    expect(result.document.proposals).toHaveLength(0);
    expect(result.document.charts[0].layers.some((layer) => layer.id === "layer-user-level")).toBe(true);
  });

  it("adds a comparison layer", () => {
    const document = createDefaultWorkspace();
    const command: Command = {
      id: createCommandId(),
      type: "chart.comparison.add",
      actor: "user",
      status: "applied",
      target: target(),
      payload: { layer: comparisonLayer() },
      createdAt: nowIso()
    };

    const result = applyCommand(document, command);

    expect(result.ok).toBe(true);
    expect(result.document.charts[0].layers.some((layer) => layer.type === "comparisonSeries")).toBe(true);
  });

  it("allows up to three comparison layers and rejects the fourth", () => {
    let document = createDefaultWorkspace();
    for (const symbol of ["MSFT", "NVDA", "TSLA"]) {
      const result = applyCommand(document, {
        id: createCommandId(),
        type: "chart.comparison.add",
        actor: "user",
        status: "applied",
        target: target(),
        payload: { layer: comparisonLayer(`layer-${symbol.toLowerCase()}`, symbol) },
        createdAt: nowIso()
      });
      expect(result.ok).toBe(true);
      document = result.document;
    }

    const fourth = applyCommand(document, {
      id: createCommandId(),
      type: "chart.comparison.add",
      actor: "user",
      status: "applied",
      target: target(),
      payload: { layer: comparisonLayer("layer-spy", "SPY") },
      createdAt: nowIso()
    });

    expect(fourth.ok).toBe(false);
    expect(fourth.errors.some((error) => error.code === "document_limit_exceeded")).toBe(true);
  });

  it("rejects a comparison layer matching the active chart symbol", () => {
    const document = createDefaultWorkspace();
    const result = applyCommand(document, {
      id: createCommandId(),
      type: "chart.comparison.add",
      actor: "user",
      status: "applied",
      target: target(),
      payload: { layer: comparisonLayer("layer-aapl", "AAPL") },
      createdAt: nowIso()
    });

    expect(result.ok).toBe(false);
    expect(result.errors.some((error) => error.path === "payload.layer.symbol")).toBe(true);
  });

  it("removes same-symbol comparison layers when the chart symbol changes", () => {
    const document = {
      ...createDefaultWorkspace(),
      charts: [
        {
          ...createDefaultWorkspace().charts[0],
          layers: [...createDefaultWorkspace().charts[0].layers, comparisonLayer("layer-msft", "MSFT")]
        }
      ]
    };

    const result = applyCommand(document, {
      id: createCommandId(),
      type: "chart.symbol.set",
      actor: "user",
      status: "applied",
      target: target(),
      payload: { symbol: "MSFT" },
      createdAt: nowIso()
    });

    expect(result.ok).toBe(true);
    expect(result.document.charts[0].layers.some((layer) => layer.type === "comparisonSeries" && layer.symbol === "MSFT")).toBe(false);
  });

  it("sets layer visibility through a command", () => {
    const document = createDefaultWorkspace();
    const result = applyCommand(document, {
      id: createCommandId(),
      type: "chart.layer.visibility.set",
      actor: "user",
      status: "applied",
      target: { ...target(), layerId: "layer-price-candles" },
      payload: { layerId: "layer-price-candles", visible: false },
      createdAt: nowIso()
    });

    expect(result.ok).toBe(true);
    expect(result.document.charts[0].layers.find((layer) => layer.id === "layer-price-candles")?.visible).toBe(false);
  });

  it("sets chart panel pin mode through a command", () => {
    const document = createDefaultWorkspace();
    const result = applyCommand(document, {
      id: createCommandId(),
      type: "panel.pinMode.set",
      actor: "user",
      status: "applied",
      target: target(),
      payload: { panelId: "panel-chart-main", pinMode: "locked" },
      createdAt: nowIso()
    });

    expect(result.ok).toBe(true);
    expect(result.document.panels.find((panel) => panel.id === "panel-chart-main")?.pinMode).toBe("locked");
  });

  it("sets chart panel tool mode through a command", () => {
    const document = createDefaultWorkspace();
    const result = applyCommand(document, {
      id: createCommandId(),
      type: "panel.chartTool.set",
      actor: "user",
      status: "applied",
      target: target(),
      payload: { panelId: "panel-chart-main", toolMode: "drawHorizontalLine" },
      createdAt: nowIso()
    });

    expect(result.ok).toBe(true);
    const panel = result.document.panels.find((item) => item.id === "panel-chart-main");
    expect(panel?.config && "toolMode" in panel.config ? panel.config.toolMode : undefined).toBe("drawHorizontalLine");
  });

  it("sets chart panel crosshair visibility through a command", () => {
    const document = createDefaultWorkspace();
    const result = applyCommand(document, {
      id: createCommandId(),
      type: "panel.crosshair.set",
      actor: "user",
      status: "applied",
      target: target(),
      payload: { panelId: "panel-chart-main", showCrosshair: false },
      createdAt: nowIso()
    });

    expect(result.ok).toBe(true);
    const panel = result.document.panels.find((item) => item.id === "panel-chart-main");
    expect(panel?.config && "showCrosshair" in panel.config ? panel.config.showCrosshair : undefined).toBe(false);
  });

  it("sets a fixed logical viewport through the command engine", () => {
    const document = createDefaultWorkspace();
    const result = applyCommand(document, {
      id: createCommandId(),
      type: "chart.viewport.set",
      actor: "user",
      status: "applied",
      target: target(),
      payload: {
        mode: "fixedLogicalRange",
        visibleBars: 90,
        rightOffsetBars: 4,
        logicalFrom: 12.5,
        logicalTo: 101.5
      },
      createdAt: nowIso()
    });

    expect(result.ok).toBe(true);
    expect(result.document.charts[0].viewport).toMatchObject({
      mode: "fixedLogicalRange",
      visibleBars: 90,
      rightOffsetBars: 4,
      logicalFrom: 12.5,
      logicalTo: 101.5
    });
  });

  it("rejects an invalid command type", () => {
    const errors = validateCommand(createDefaultWorkspace(), { type: "chart.bad.add", target: target() });
    expect(errors[0].code).toBe("unknown_command_type");
  });

  it("rejects a missing target", () => {
    const errors = validateCommand(createDefaultWorkspace(), { type: "chart.viewport.set", payload: {} });
    expect(errors[0].code).toBe("missing_target");
  });

  it("rejects non-finite logical viewport values", () => {
    const errors = validateCommand(createDefaultWorkspace(), {
      id: createCommandId(),
      type: "chart.viewport.set",
      actor: "user",
      status: "applied",
      target: target(),
      payload: { logicalFrom: Number.NaN, logicalTo: 120 },
      createdAt: nowIso()
    });

    expect(errors.some((item) => item.code === "invalid_payload" && item.path === "payload.logicalRange")).toBe(true);
  });

  it("rejects AI commands targeting locked panels", () => {
    const document = createDefaultWorkspace();
    const command: Command = {
      id: createCommandId(),
      type: "chart.viewport.set",
      actor: "ai",
      status: "proposal",
      target: target("panel-chat"),
      payload: { visibleBars: 90 },
      createdAt: nowIso()
    };

    const result = applyCommand(document, command);

    expect(result.ok).toBe(false);
    expect(result.errors.some((item) => item.code === "panel_locked")).toBe(true);
  });

  it("requires approval for direct AI proposal commands in approval pin mode", () => {
    const document = createDefaultWorkspace();
    const command: Command = {
      id: createCommandId(),
      type: "chart.drawing.add",
      actor: "ai",
      status: "proposal",
      target: target(),
      payload: { layer: drawingLayer("layer-ai-approval") },
      proposalId: "proposal-ai-approval",
      createdAt: nowIso()
    };

    const result = applyCommand(document, command);

    expect(result.ok).toBe(false);
    expect(result.errors.some((item) => item.code === "approval_required")).toBe(true);
    expect(result.document.charts[0].layers.some((layer) => layer.id === "layer-ai-approval")).toBe(false);
  });

  it("allows proposal-origin AI chart commands in auto pin mode", () => {
    const baseDocument = createDefaultWorkspace();
    const command: Command = {
      id: createCommandId(),
      type: "chart.drawing.add",
      actor: "ai",
      status: "proposal",
      target: target(),
      payload: { layer: drawingLayer("layer-ai-auto") },
      proposalId: "proposal-ai-auto",
      createdAt: nowIso()
    };
    const document = {
      ...baseDocument,
      panels: baseDocument.panels.map((panel) =>
        panel.id === "panel-chart-main" ? { ...panel, pinMode: "auto" as const } : panel
      ),
      proposals: [
        {
          id: "proposal-ai-auto",
          source: "llm" as const,
          status: "pending" as const,
          targetPanelId: "panel-chart-main",
          targetChartId: "chart-main",
          title: "Auto command",
          rationale: "Direct AI commands still need a document proposal.",
          previewSummary: "Adds a horizontal level.",
          commands: [command],
          createdAt: nowIso(),
          validationErrors: []
        }
      ]
    };

    const result = applyCommand(document, command);

    expect(result.ok).toBe(true);
    expect(result.document.charts[0].layers.some((layer) => layer.id === "layer-ai-auto")).toBe(true);
  });

  it("accepts grouped proposal commands as user commands", () => {
    const document = createDefaultWorkspace();
    const proposalCommand: Command = {
      id: createCommandId(),
      type: "chart.drawing.add",
      actor: "ai",
      status: "proposal",
      target: target(),
      payload: { layer: drawingLayer("layer-proposal-level") },
      proposalId: "proposal-1",
      createdAt: nowIso()
    };
    const withProposal = {
      ...document,
      proposals: [
        {
          id: "proposal-1",
          source: "llm" as const,
          status: "pending" as const,
          targetPanelId: "panel-chart-main",
          targetChartId: "chart-main",
          title: "Add level",
          rationale: "Visible level.",
          previewSummary: "Adds a horizontal level.",
          commands: [proposalCommand],
          createdAt: nowIso(),
          validationErrors: []
        }
      ]
    };

    const result = applyCommand(withProposal, {
      id: createCommandId(),
      type: "proposal.accept",
      actor: "user",
      status: "accepted",
      target: target(),
      payload: { proposalId: "proposal-1" },
      createdAt: nowIso()
    });

    expect(result.ok).toBe(true);
    expect(result.document.proposals[0].status).toBe("accepted");
    expect(result.document.charts[0].layers.some((layer) => layer.id === "layer-proposal-level")).toBe(true);
    expect(result.document.commandJournal).toHaveLength(1);
    expect(result.document.commandJournal[0].groupId).toBeTruthy();
    expect(result.document.commandJournal[0].proposalId).toBe("proposal-1");
  });

  it("auto-accepts valid incoming proposals for auto pinned chart panels", () => {
    const document = {
      ...createDefaultWorkspace(),
      panels: createDefaultWorkspace().panels.map((panel) =>
        panel.id === "panel-chart-main" ? { ...panel, pinMode: "auto" as const } : panel
      )
    };
    const proposalCommand: Command = {
      id: createCommandId(),
      type: "chart.drawing.add",
      actor: "ai",
      status: "proposal",
      target: target(),
      payload: { layer: drawingLayer("layer-auto-proposal-level") },
      proposalId: "proposal-auto",
      createdAt: nowIso()
    };

    const result = ingestIncomingProposalsWithPolicy(document, [
      {
        id: "proposal-auto",
        source: "llm",
        status: "pending",
        targetPanelId: "panel-chart-main",
        targetChartId: "chart-main",
        title: "Auto level",
        rationale: "Auto pin mode should apply valid AI chart edits.",
        previewSummary: "Adds a horizontal level.",
        commands: [proposalCommand],
        createdAt: nowIso(),
        validationErrors: []
      }
    ]);

    expect(result.errors).toEqual([]);
    expect(result.autoAcceptedProposalIds).toEqual(["proposal-auto"]);
    expect(result.document.proposals[0].status).toBe("accepted");
    expect(result.document.charts[0].layers.some((layer) => layer.id === "layer-auto-proposal-level")).toBe(true);
  });

  it("does not apply any proposal child command when accept validation fails", () => {
    const document = createDefaultWorkspace();
    const firstCommand: Command = {
      id: createCommandId(),
      type: "chart.drawing.add",
      actor: "ai",
      status: "proposal",
      target: target(),
      payload: { layer: drawingLayer("layer-duplicate-proposal") },
      proposalId: "proposal-atomic",
      createdAt: nowIso()
    };
    const duplicateCommand: Command = {
      ...firstCommand,
      id: createCommandId(),
      payload: { layer: drawingLayer("layer-duplicate-proposal") }
    };
    const withProposal = {
      ...document,
      proposals: [
        {
          id: "proposal-atomic",
          source: "llm" as const,
          status: "pending" as const,
          targetPanelId: "panel-chart-main",
          targetChartId: "chart-main",
          title: "Atomic",
          rationale: "Second command should fail.",
          previewSummary: "No partial application.",
          commands: [firstCommand, duplicateCommand],
          createdAt: nowIso(),
          validationErrors: []
        }
      ]
    };

    const result = applyCommand(withProposal, {
      id: createCommandId(),
      type: "proposal.accept",
      actor: "user",
      status: "accepted",
      target: target(),
      payload: { proposalId: "proposal-atomic" },
      createdAt: nowIso()
    });

    expect(result.ok).toBe(false);
    expect(result.document.charts[0].layers.some((layer) => layer.id === "layer-duplicate-proposal")).toBe(false);
    expect(result.document.proposals[0].status).toBe("pending");
  });

  it("rejecting a proposal leaves chart layers unchanged", () => {
    const document = createDefaultWorkspace();
    const beforeLayers = document.charts[0].layers;
    const withProposal = {
      ...document,
      proposals: [
        {
          id: "proposal-2",
          source: "llm" as const,
          status: "pending" as const,
          targetPanelId: "panel-chart-main",
          targetChartId: "chart-main",
          title: "Rejected",
          rationale: "Nope.",
          previewSummary: "No mutation.",
          commands: [],
          createdAt: nowIso(),
          validationErrors: []
        }
      ]
    };

    const result = applyCommand(withProposal, {
      id: createCommandId(),
      type: "proposal.reject",
      actor: "user",
      status: "rejected",
      target: target(),
      payload: { proposalId: "proposal-2" },
      createdAt: nowIso()
    });

    expect(result.ok).toBe(true);
    expect(result.document.proposals[0].status).toBe("rejected");
    expect(result.document.charts[0].layers).toEqual(beforeLayers);
  });
});
