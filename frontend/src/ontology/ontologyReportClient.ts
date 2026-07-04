import type { AgentEvidenceItem, AnalysisReport } from "./ontologyTypes";

type ImportMetaWithEnv = ImportMeta & {
  env?: Record<string, string | undefined>;
};

type OntologyReportRequest = {
  symbol: string;
};

const defaultOntologyReportUrl = "/api/agent-analysis/run";

export async function requestOntologyReport(
  request: OntologyReportRequest,
  signal?: AbortSignal
): Promise<AnalysisReport | null> {
  const endpoint = ontologyReportEndpoint();
  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbol: request.symbol }),
    signal
  });
  if (!response.ok) {
    return null;
  }
  return normalizeAnalysisReport(await response.json().catch(() => null));
}

function ontologyReportEndpoint(): string {
  const env = (import.meta as ImportMetaWithEnv).env;
  return env?.VITE_ONTOLOGY_REPORT_URL?.trim() || defaultOntologyReportUrl;
}

export function normalizeAnalysisReport(payload: unknown): AnalysisReport | null {
  if (!payload || typeof payload !== "object") {
    return null;
  }
  const source = payload as { report?: unknown; providerEvidence?: unknown; symbol?: unknown; generatedAt?: unknown };
  const report = source.report && typeof source.report === "object" ? source.report as typeof source : source;
  const providerEvidence = Array.isArray(report.providerEvidence)
    ? report.providerEvidence.filter(isAgentEvidenceItem)
    : [];
  return {
    providerEvidence,
    symbol: typeof report.symbol === "string" ? report.symbol : undefined,
    generatedAt: typeof report.generatedAt === "string" ? report.generatedAt : undefined
  };
}

function isAgentEvidenceItem(value: unknown): value is AgentEvidenceItem {
  if (!value || typeof value !== "object") {
    return false;
  }
  const item = value as AgentEvidenceItem;
  return typeof item.provider === "string" && typeof item.status === "string";
}
