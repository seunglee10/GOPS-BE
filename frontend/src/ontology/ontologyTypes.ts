export type AgentEvidenceItem = {
  provider: string;
  status: string;
  title?: string;
  summary?: string;
  url?: string;
  observedAt?: string;
  raw?: Record<string, unknown>;
};

export type AnalysisReport = {
  providerEvidence?: AgentEvidenceItem[];
  symbol?: string;
  generatedAt?: string;
};

export type OntologyGraphNodeKind = "symbol" | "theme" | "company";
export type OntologyGraphEdgeKind = "theme" | "control" | "shared-theme" | "cross-control";

export type OntologyGraphNode = {
  id: string;
  label: string;
  kind: OntologyGraphNodeKind;
};

export type OntologyGraphEdge = {
  id: string;
  source: string;
  target: string;
  kind: OntologyGraphEdgeKind;
  label?: string;
};

export type OntologyGraphData = {
  symbol: string;
  nodes: OntologyGraphNode[];
  edges: OntologyGraphEdge[];
  generatedAt: string;
};
