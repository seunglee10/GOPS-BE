import { defaultIndicatorRegistry } from "../calculations/indicatorRegistry";
import { getEnabledCommandTypes, getLlmEnabledCommandTypes } from "../capabilities/chartCapabilities";
import type { Command, CommandDefinition, CommandRegistry, CommandValidationError } from "../types/commands";
import {
  DEFAULT_WATCHLIST,
  DOCUMENT_LIMITS,
  type ChartDocument,
  type LayerDocument,
  type PaneDocument,
  type PinMode,
  type WorkspaceDocument
} from "../types/documents";

export const ENABLED_COMMAND_TYPES: Command["type"][] = getEnabledCommandTypes();

export function getAvailableCommandTypes(registry: CommandRegistry = defaultCommandRegistry): Command["type"][] {
  return getLlmEnabledCommandTypes().filter((type) => Boolean(registry[type]));
}

export function getAvailableCommandTypesForPinMode(
  pinMode: PinMode,
  registry: CommandRegistry = defaultCommandRegistry
): Command["type"][] {
  if (pinMode === "locked") return [];
  return getAvailableCommandTypes(registry);
}

function error(code: CommandValidationError["code"], message: string, path?: string): CommandValidationError {
  return { code, message, path };
}

function findChart(document: WorkspaceDocument, chartId: string): ChartDocument | undefined {
  return document.charts.find((chart) => chart.id === chartId);
}

function layerExists(chart: ChartDocument, layerId: string): boolean {
  return chart.layers.some((layer) => layer.id === layerId);
}

function updateChart(document: WorkspaceDocument, chartId: string, updater: (chart: ChartDocument) => ChartDocument): WorkspaceDocument {
  return {
    ...document,
    charts: document.charts.map((chart) => (chart.id === chartId ? updater(chart) : chart))
  };
}

function updatePanel(document: WorkspaceDocument, panelId: string, updater: (panel: WorkspaceDocument["panels"][number]) => WorkspaceDocument["panels"][number]): WorkspaceDocument {
  return {
    ...document,
    panels: document.panels.map((panel) => (panel.id === panelId ? updater(panel) : panel))
  };
}

function touchChart(chart: ChartDocument): ChartDocument {
  return { ...chart, updatedAt: new Date().toISOString() };
}

function countLayers(chart: ChartDocument, type: LayerDocument["type"]): number {
  return chart.layers.filter((layer) => layer.type === type).length;
}

function validatePane(chart: ChartDocument, paneId: string, path: string): CommandValidationError[] {
  if (!chart.panes.some((pane) => pane.id === paneId)) {
    return [error("target_not_found", "Target pane was not found.", path)];
  }
  return [];
}

function validateNewPane(chart: ChartDocument, pane: PaneDocument | undefined, expectedPaneId: string): CommandValidationError[] {
  if (!pane) return [error("target_not_found", "Target pane was not found.", "payload.layer.paneId")];
  const errors: CommandValidationError[] = [];
  if (pane.id !== expectedPaneId) {
    errors.push(error("invalid_payload", "New pane id must match the indicator layer pane id.", "payload.pane.id"));
  }
  if (chart.panes.some((item) => item.id === pane.id)) {
    errors.push(error("invalid_payload", "Pane id already exists.", "payload.pane.id"));
  }
  if (chart.panes.length >= DOCUMENT_LIMITS.maxPanesPerChart) {
    errors.push(error("document_limit_exceeded", "Maximum pane count reached.", "payload.pane"));
  }
  if (!["price", "volume", "indicator"].includes(pane.kind)) {
    errors.push(error("invalid_payload", "Invalid pane kind.", "payload.pane.kind"));
  }
  return errors;
}

const setSymbol: CommandDefinition<Extract<Command, { type: "chart.symbol.set" }>> = {
  type: "chart.symbol.set",
  validate(command) {
    const symbol = command.payload.symbol.trim().toUpperCase();
    if (!symbol) return [error("invalid_payload", "Symbol is required.", "payload.symbol")];
    return DEFAULT_WATCHLIST.includes(symbol)
      ? []
      : [error("invalid_payload", "Symbol is not in the enabled watchlist.", "payload.symbol")];
  },
  apply(command, document) {
    const symbol = command.payload.symbol.trim().toUpperCase();
    return updateChart(document, command.target.chartId, (chart) =>
      touchChart({
        ...chart,
        symbol,
        dataBindings: chart.dataBindings.map((binding) =>
          binding.source === "marketCandles" ? { ...binding, symbol } : binding
        ),
        layers: chart.layers.filter((layer) => layer.type !== "comparisonSeries" || layer.symbol !== symbol)
      })
    );
  }
};

const setTimeframe: CommandDefinition<Extract<Command, { type: "chart.timeframe.set" }>> = {
  type: "chart.timeframe.set",
  validate(command) {
    return command.payload.timeframe ? [] : [error("invalid_payload", "Timeframe is required.", "payload.timeframe")];
  },
  apply(command, document) {
    const timeframe = command.payload.timeframe;
    return updateChart(document, command.target.chartId, (chart) =>
      touchChart({
        ...chart,
        timeframe,
        dataBindings: chart.dataBindings.map((binding) =>
          binding.source === "marketCandles" ? { ...binding, timeframe } : binding
        )
      })
    );
  }
};

const setViewport: CommandDefinition<Extract<Command, { type: "chart.viewport.set" }>> = {
  type: "chart.viewport.set",
  validate(command) {
    const errors: CommandValidationError[] = [];
    if (command.payload.visibleBars !== undefined && (command.payload.visibleBars < 20 || command.payload.visibleBars > 1000)) {
      errors.push(error("invalid_payload", "visibleBars must be between 20 and 1000.", "payload.visibleBars"));
    }
    if (command.payload.rightOffsetBars !== undefined && command.payload.rightOffsetBars < 0) {
      errors.push(error("invalid_payload", "rightOffsetBars cannot be negative.", "payload.rightOffsetBars"));
    }
    if (
      (command.payload.logicalFrom !== undefined && !Number.isFinite(command.payload.logicalFrom)) ||
      (command.payload.logicalTo !== undefined && !Number.isFinite(command.payload.logicalTo))
    ) {
      errors.push(error("invalid_payload", "Logical viewport values must be finite numbers.", "payload.logicalRange"));
    }
    return errors;
  },
  apply(command, document) {
    return updateChart(document, command.target.chartId, (chart) =>
      touchChart({
        ...chart,
        viewport: {
          ...chart.viewport,
          ...command.payload,
          visibleBars: command.payload.visibleBars
            ? Math.min(1000, Math.max(20, Math.round(command.payload.visibleBars)))
            : chart.viewport.visibleBars,
          rightOffsetBars: command.payload.rightOffsetBars
            ? Math.max(0, Math.round(command.payload.rightOffsetBars))
            : command.payload.rightOffsetBars === 0
              ? 0
              : chart.viewport.rightOffsetBars,
          logicalFrom:
            command.payload.logicalFrom !== undefined ? command.payload.logicalFrom : chart.viewport.logicalFrom,
          logicalTo:
            command.payload.logicalTo !== undefined ? command.payload.logicalTo : chart.viewport.logicalTo,
          minVisibleBars:
            command.payload.minVisibleBars !== undefined
              ? Math.max(1, Math.round(command.payload.minVisibleBars))
              : chart.viewport.minVisibleBars,
          maxVisibleBars:
            command.payload.maxVisibleBars !== undefined
              ? Math.max(1, Math.round(command.payload.maxVisibleBars))
              : chart.viewport.maxVisibleBars
        }
      })
    );
  }
};

const addIndicator: CommandDefinition<Extract<Command, { type: "chart.indicator.add" }>> = {
  type: "chart.indicator.add",
  validate(command, document) {
    const chart = findChart(document, command.target.chartId);
    if (!chart) return [];
    const errors: CommandValidationError[] = [];
    if (!defaultIndicatorRegistry[command.payload.node.type]) {
      errors.push(error("layer_type_not_allowed", "Indicator type is not enabled.", "payload.node.type"));
    } else {
      errors.push(...defaultIndicatorRegistry[command.payload.node.type].validateInputs(command.payload.node.inputs));
    }
    if (chart.calculationGraph.nodes.some((node) => node.id === command.payload.node.id)) {
      errors.push(error("invalid_payload", "Calculation node id already exists.", "payload.node.id"));
    }
    if (layerExists(chart, command.payload.layer.id)) {
      errors.push(error("invalid_payload", "Layer id already exists.", "payload.layer.id"));
    }
    if (countLayers(chart, "indicator") >= DOCUMENT_LIMITS.maxIndicatorsPerChart) {
      errors.push(error("document_limit_exceeded", "Maximum indicator count reached.", "payload.layer"));
    }
    if (chart.panes.some((pane) => pane.id === command.payload.layer.paneId)) {
      errors.push(...validatePane(chart, command.payload.layer.paneId, "payload.layer.paneId"));
    } else {
      errors.push(...validateNewPane(chart, command.payload.pane, command.payload.layer.paneId));
    }
    return errors;
  },
  apply(command, document) {
    return updateChart(document, command.target.chartId, (chart) =>
      touchChart({
        ...chart,
        panes:
          command.payload.pane && !chart.panes.some((pane) => pane.id === command.payload.pane?.id)
            ? [...chart.panes, command.payload.pane]
            : chart.panes,
        calculationGraph: {
          nodes: [...chart.calculationGraph.nodes, command.payload.node]
        },
        layers: [...chart.layers, command.payload.layer]
      })
    );
  }
};

const updateIndicator: CommandDefinition<Extract<Command, { type: "chart.indicator.update" }>> = {
  type: "chart.indicator.update",
  validate(command, document) {
    const chart = findChart(document, command.target.chartId);
    if (!chart) return [];
    const node = chart.calculationGraph.nodes.find((item) => item.id === command.payload.calculationNodeId);
    if (!node) return [error("calculation_node_not_found", "Calculation node was not found.", "payload.calculationNodeId")];
    return defaultIndicatorRegistry[node.type].validateInputs(command.payload.inputs);
  },
  apply(command, document) {
    return updateChart(document, command.target.chartId, (chart) =>
      touchChart({
        ...chart,
        calculationGraph: {
          nodes: chart.calculationGraph.nodes.map((node) =>
            node.id === command.payload.calculationNodeId ? { ...node, inputs: command.payload.inputs } : node
          )
        },
        layers: command.payload.layerPatch
          ? chart.layers.map((layer) =>
              layer.type === "indicator" && layer.calculationNodeId === command.payload.calculationNodeId
                ? ({ ...layer, ...command.payload.layerPatch, updatedAt: new Date().toISOString() } as LayerDocument)
                : layer
            )
          : chart.layers
      })
    );
  }
};

const removeIndicator: CommandDefinition<Extract<Command, { type: "chart.indicator.remove" }>> = {
  type: "chart.indicator.remove",
  validate(command, document) {
    const chart = findChart(document, command.target.chartId);
    if (!chart) return [];
    if (!chart.calculationGraph.nodes.some((node) => node.id === command.payload.calculationNodeId)) {
      return [error("calculation_node_not_found", "Calculation node was not found.", "payload.calculationNodeId")];
    }
    return layerExists(chart, command.payload.layerId)
      ? []
      : [error("target_not_found", "Indicator layer was not found.", "payload.layerId")];
  },
  apply(command, document) {
    return updateChart(document, command.target.chartId, (chart) =>
      touchChart({
        ...chart,
        calculationGraph: {
          nodes: chart.calculationGraph.nodes.filter((node) => node.id !== command.payload.calculationNodeId)
        },
        layers: chart.layers.filter((layer) => layer.id !== command.payload.layerId)
      })
    );
  }
};

const addDrawing: CommandDefinition<Extract<Command, { type: "chart.drawing.add" }>> = {
  type: "chart.drawing.add",
  validate(command, document) {
    const chart = findChart(document, command.target.chartId);
    if (!chart) return [];
    const errors: CommandValidationError[] = [];
    if (command.payload.layer.type !== "drawing") {
      errors.push(error("invalid_payload", "Layer type must be drawing.", "payload.layer.type"));
    }
    if (command.payload.layer.drawing.kind !== "horizontalLine") {
      errors.push(error("layer_type_not_allowed", "Only horizontal line drawings are enabled.", "payload.layer.drawing.kind"));
    }
    if (countLayers(chart, "drawing") >= DOCUMENT_LIMITS.maxDrawingsPerChart) {
      errors.push(error("document_limit_exceeded", "Maximum drawing count reached.", "payload.layer"));
    }
    if (layerExists(chart, command.payload.layer.id)) {
      errors.push(error("invalid_payload", "Layer id already exists.", "payload.layer.id"));
    }
    errors.push(...validatePane(chart, command.payload.layer.paneId, "payload.layer.paneId"));
    return errors;
  },
  apply(command, document) {
    return updateChart(document, command.target.chartId, (chart) => touchChart({ ...chart, layers: [...chart.layers, command.payload.layer] }));
  }
};

const updateDrawing: CommandDefinition<Extract<Command, { type: "chart.drawing.update" }>> = {
  type: "chart.drawing.update",
  validate(command, document) {
    const chart = findChart(document, command.target.chartId);
    if (!chart) return [];
    if (command.payload.drawing.kind !== "horizontalLine") {
      return [error("layer_type_not_allowed", "Only horizontal line drawings are enabled.", "payload.drawing.kind")];
    }
    return chart.layers.some((layer) => layer.id === command.payload.layerId && layer.type === "drawing")
      ? []
      : [error("target_not_found", "Drawing layer was not found.", "payload.layerId")];
  },
  apply(command, document) {
    return updateChart(document, command.target.chartId, (chart) =>
      touchChart({
        ...chart,
        layers: chart.layers.map((layer) =>
          layer.id === command.payload.layerId && layer.type === "drawing"
            ? {
                ...layer,
                drawing: command.payload.drawing,
                style: command.payload.style ?? layer.style,
                visible: command.payload.visible ?? layer.visible,
                updatedAt: new Date().toISOString()
              }
            : layer
        )
      })
    );
  }
};

const setLayerVisibility: CommandDefinition<Extract<Command, { type: "chart.layer.visibility.set" }>> = {
  type: "chart.layer.visibility.set",
  validate(command, document) {
    const chart = findChart(document, command.target.chartId);
    if (!chart) return [];
    return chart.layers.some((layer) => layer.id === command.payload.layerId)
      ? []
      : [error("target_not_found", "Layer was not found.", "payload.layerId")];
  },
  apply(command, document) {
    return updateChart(document, command.target.chartId, (chart) =>
      touchChart({
        ...chart,
        layers: chart.layers.map((layer) =>
          layer.id === command.payload.layerId
            ? { ...layer, visible: command.payload.visible, updatedAt: new Date().toISOString() }
            : layer
        )
      })
    );
  }
};

const removeDrawing: CommandDefinition<Extract<Command, { type: "chart.drawing.remove" }>> = {
  type: "chart.drawing.remove",
  validate(command, document) {
    const chart = findChart(document, command.target.chartId);
    if (!chart) return [];
    return chart.layers.some((layer) => layer.id === command.payload.layerId && layer.type === "drawing")
      ? []
      : [error("target_not_found", "Drawing layer was not found.", "payload.layerId")];
  },
  apply(command, document) {
    return updateChart(document, command.target.chartId, (chart) =>
      touchChart({ ...chart, layers: chart.layers.filter((layer) => layer.id !== command.payload.layerId) })
    );
  }
};

const addComparison: CommandDefinition<Extract<Command, { type: "chart.comparison.add" }>> = {
  type: "chart.comparison.add",
  validate(command, document) {
    const chart = findChart(document, command.target.chartId);
    if (!chart) return [];
    const errors: CommandValidationError[] = [];
    const symbol = command.payload.layer.symbol.trim().toUpperCase();
    if (command.payload.layer.type !== "comparisonSeries") {
      errors.push(error("invalid_payload", "Layer type must be comparisonSeries.", "payload.layer.type"));
    }
    if (!symbol) {
      errors.push(error("invalid_payload", "Comparison symbol is required.", "payload.layer.symbol"));
    }
    if (!DEFAULT_WATCHLIST.includes(symbol)) {
      errors.push(error("invalid_payload", "Comparison symbol is not in the enabled watchlist.", "payload.layer.symbol"));
    }
    if (symbol === chart.symbol) {
      errors.push(error("invalid_payload", "Comparison symbol must differ from the active chart symbol.", "payload.layer.symbol"));
    }
    if (countLayers(chart, "comparisonSeries") >= DOCUMENT_LIMITS.maxComparisonSymbolsPerChart) {
      errors.push(error("document_limit_exceeded", "Maximum comparison symbol count reached.", "payload.layer"));
    }
    if (chart.layers.some((layer) => layer.type === "comparisonSeries" && layer.symbol === symbol)) {
      errors.push(error("invalid_payload", "Comparison symbol already exists.", "payload.layer.symbol"));
    }
    if (layerExists(chart, command.payload.layer.id)) {
      errors.push(error("invalid_payload", "Layer id already exists.", "payload.layer.id"));
    }
    errors.push(...validatePane(chart, command.payload.layer.paneId, "payload.layer.paneId"));
    return errors;
  },
  apply(command, document) {
    const layer = { ...command.payload.layer, symbol: command.payload.layer.symbol.trim().toUpperCase() };
    return updateChart(document, command.target.chartId, (chart) => touchChart({ ...chart, layers: [...chart.layers, layer] }));
  }
};

const removeComparison: CommandDefinition<Extract<Command, { type: "chart.comparison.remove" }>> = {
  type: "chart.comparison.remove",
  validate(command, document) {
    const chart = findChart(document, command.target.chartId);
    if (!chart) return [];
    return chart.layers.some((layer) => layer.id === command.payload.layerId && layer.type === "comparisonSeries")
      ? []
      : [error("target_not_found", "Comparison layer was not found.", "payload.layerId")];
  },
  apply(command, document) {
    return updateChart(document, command.target.chartId, (chart) =>
      touchChart({ ...chart, layers: chart.layers.filter((layer) => layer.id !== command.payload.layerId) })
    );
  }
};

const proposalAccept: CommandDefinition<Extract<Command, { type: "proposal.accept" }>> = {
  type: "proposal.accept",
  validate(command, document) {
    const proposal = document.proposals.find((item) => item.id === command.payload.proposalId);
    if (!proposal) return [error("target_not_found", "Proposal was not found.", "payload.proposalId")];
    return proposal.status === "pending" ? [] : [error("invalid_payload", "Only pending proposals can be accepted.", "payload.proposalId")];
  },
  apply(_command, document) {
    return document;
  }
};

const proposalReject: CommandDefinition<Extract<Command, { type: "proposal.reject" }>> = {
  type: "proposal.reject",
  validate(command, document) {
    const proposal = document.proposals.find((item) => item.id === command.payload.proposalId);
    if (!proposal) return [error("target_not_found", "Proposal was not found.", "payload.proposalId")];
    return proposal.status === "pending" ? [] : [error("invalid_payload", "Only pending proposals can be rejected.", "payload.proposalId")];
  },
  apply(_command, document) {
    return document;
  }
};

const setPanelPinMode: CommandDefinition<Extract<Command, { type: "panel.pinMode.set" }>> = {
  type: "panel.pinMode.set",
  validate(command, document) {
    const panel = document.panels.find((item) => item.id === command.payload.panelId);
    if (!panel) return [error("target_not_found", "Panel was not found.", "payload.panelId")];
    return ["locked", "approval", "auto"].includes(command.payload.pinMode)
      ? []
      : [error("invalid_payload", "Invalid panel pin mode.", "payload.pinMode")];
  },
  apply(command, document) {
    return updatePanel(document, command.payload.panelId, (panel) => ({
      ...panel,
      pinMode: command.payload.pinMode
    }));
  }
};

const setPanelChartTool: CommandDefinition<Extract<Command, { type: "panel.chartTool.set" }>> = {
  type: "panel.chartTool.set",
  validate(command, document) {
    const panel = document.panels.find((item) => item.id === command.payload.panelId);
    if (!panel) return [error("target_not_found", "Panel was not found.", "payload.panelId")];
    if (panel.type !== "chart") return [error("invalid_payload", "Chart tool mode can only be set on chart panels.", "payload.panelId")];
    return ["select", "drawHorizontalLine"].includes(command.payload.toolMode)
      ? []
      : [error("invalid_payload", "Invalid chart tool mode.", "payload.toolMode")];
  },
  apply(command, document) {
    return updatePanel(document, command.payload.panelId, (panel) => ({
      ...panel,
      config: {
        ...(panel.config ?? {}),
        chartId: panel.targetChartId ?? command.target.chartId,
        toolMode: command.payload.toolMode,
        showCrosshair:
          panel.config && "showCrosshair" in panel.config ? Boolean(panel.config.showCrosshair) : true,
        toolsCollapsed:
          panel.config && "toolsCollapsed" in panel.config ? Boolean(panel.config.toolsCollapsed) : false
      }
    }));
  }
};

const setPanelCrosshair: CommandDefinition<Extract<Command, { type: "panel.crosshair.set" }>> = {
  type: "panel.crosshair.set",
  validate(command, document) {
    const panel = document.panels.find((item) => item.id === command.payload.panelId);
    if (!panel) return [error("target_not_found", "Panel was not found.", "payload.panelId")];
    return panel.type === "chart"
      ? []
      : [error("invalid_payload", "Crosshair visibility can only be set on chart panels.", "payload.panelId")];
  },
  apply(command, document) {
    return updatePanel(document, command.payload.panelId, (panel) => ({
      ...panel,
      config: {
        ...(panel.config ?? {}),
        chartId: panel.targetChartId ?? command.target.chartId,
        toolMode: panel.config && "toolMode" in panel.config ? panel.config.toolMode : "select",
        showCrosshair: command.payload.showCrosshair,
        toolsCollapsed:
          panel.config && "toolsCollapsed" in panel.config ? Boolean(panel.config.toolsCollapsed) : false
      }
    }));
  }
};

export const defaultCommandRegistry: CommandRegistry = {
  "chart.symbol.set": setSymbol as CommandDefinition,
  "chart.timeframe.set": setTimeframe as CommandDefinition,
  "chart.viewport.set": setViewport as CommandDefinition,
  "chart.indicator.add": addIndicator as CommandDefinition,
  "chart.indicator.update": updateIndicator as CommandDefinition,
  "chart.indicator.remove": removeIndicator as CommandDefinition,
  "chart.drawing.add": addDrawing as CommandDefinition,
  "chart.drawing.update": updateDrawing as CommandDefinition,
  "chart.drawing.remove": removeDrawing as CommandDefinition,
  "chart.layer.visibility.set": setLayerVisibility as CommandDefinition,
  "chart.comparison.add": addComparison as CommandDefinition,
  "chart.comparison.remove": removeComparison as CommandDefinition,
  "panel.pinMode.set": setPanelPinMode as CommandDefinition,
  "panel.chartTool.set": setPanelChartTool as CommandDefinition,
  "panel.crosshair.set": setPanelCrosshair as CommandDefinition,
  "proposal.accept": proposalAccept as CommandDefinition,
  "proposal.reject": proposalReject as CommandDefinition
};
