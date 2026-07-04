import type {
  AgentEvidenceItem,
  OntologyGraphData,
  OntologyGraphEdge,
  OntologyGraphEdgeKind,
  OntologyGraphNode,
  OntologyGraphNodeKind
} from "./ontologyTypes";

function readNonEmptyString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function readSymbolArray(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .filter((item): item is string => typeof item === "string" && item.trim().length > 0)
    .map((item) => item.trim().toUpperCase());
}

export function buildOntologyGraphFromEvidence(
  evidence: readonly AgentEvidenceItem[],
  primarySymbol: string
): OntologyGraphData | null {
  const symbol = primarySymbol.trim().toUpperCase();
  const nodes = new Map<string, OntologyGraphNode>();
  const edges = new Map<string, OntologyGraphEdge>();
  let matched = false;

  const ensureNode = (id: string, label: string, kind: OntologyGraphNodeKind) => {
    if (!nodes.has(id)) {
      nodes.set(id, { id, label, kind });
    }
    return id;
  };
  const ensureSymbolNode = (ticker: string) => ensureNode(`symbol:${ticker.toUpperCase()}`, ticker.toUpperCase(), "symbol");
  const ensureThemeNode = (theme: string) => ensureNode(`theme:${theme}`, theme, "theme");
  const ensureCompanyNode = (name: string) => ensureNode(`company:${name}`, name, "company");
  const addEdge = (source: string, target: string, kind: OntologyGraphEdgeKind, label?: string) => {
    const id = `${kind}:${source}->${target}`;
    if (!edges.has(id)) {
      edges.set(id, { id, source, target, kind, label });
    }
  };

  for (const item of evidence) {
    if (item.provider !== "ontology" || item.status !== "available") {
      continue;
    }
    const raw = item.raw ?? {};
    const relationType = readNonEmptyString(raw.relationType);

    if (relationType === "theme" || relationType === "theme-company") {
      const ticker = readNonEmptyString(raw.ticker)?.toUpperCase() ?? symbol;
      const theme = readNonEmptyString(raw.themeName);
      if (theme) {
        addEdge(ensureSymbolNode(ticker), ensureThemeNode(theme), "theme");
        matched = true;
      }
      continue;
    }

    if (relationType === "control" || relationType === "theme-control") {
      const ticker = readNonEmptyString(raw.ticker)?.toUpperCase() ?? symbol;
      const controlled = readNonEmptyString(raw.controlledName);
      if (controlled) {
        addEdge(ensureSymbolNode(ticker), ensureCompanyNode(controlled), "control");
        matched = true;
      }
      continue;
    }

    if (relationType === "shared-theme") {
      const theme = readNonEmptyString(raw.themeName);
      const symbols = readSymbolArray(raw.symbols);
      if (theme && symbols.length >= 2) {
        const themeId = ensureThemeNode(theme);
        symbols.forEach((ticker) => addEdge(ensureSymbolNode(ticker), themeId, "shared-theme"));
        matched = true;
      }
      continue;
    }

    if (relationType === "cross-control") {
      const controller = readNonEmptyString(raw.controllerTicker)?.toUpperCase();
      const controlled = readNonEmptyString(raw.controlledTicker)?.toUpperCase();
      if (controller && controlled) {
        addEdge(
          ensureSymbolNode(controller),
          ensureSymbolNode(controlled),
          "cross-control",
          readNonEmptyString(raw.controlledName)
        );
        matched = true;
      }
    }
  }

  if (!matched) {
    return null;
  }

  ensureSymbolNode(symbol);

  return {
    symbol,
    nodes: Array.from(nodes.values()),
    edges: Array.from(edges.values()),
    generatedAt: new Date().toISOString()
  };
}
