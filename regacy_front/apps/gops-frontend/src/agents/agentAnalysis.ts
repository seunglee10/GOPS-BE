import type { CommandActor, LayoutCommand, LayoutCommandType, LayoutProposal, PanelType, WorkspaceLayout } from "../layout/types";
import { getPanelDefinition } from "../layout/panelRegistry";

export type AgentEvidenceItem = {
  provider: string;
  status: string;
  title?: string;
  summary?: string;
  url?: string;
  observedAt?: string;
  raw?: Record<string, unknown>;
};

export type AgentFinding = {
  agentId: string;
  role: string;
  summary: string;
  rationale?: string;
  confidence?: number;
  evidence: AgentEvidenceItem[];
  tags: string[];
};

export type NotificationDecision = {
  level: string;
  title?: string;
  message?: string;
  reason?: string;
};

export type IntentRoute = {
  source: string;
  intentType: string;
  selectedRoles: string[];
  confidence?: number;
  reason?: string;
};

export type FinalAnswerSection = {
  title: string;
  bullets: string[];
};

export type FinalAnswerCitation = {
  provider: string;
  title: string;
  url?: string;
  publishedAt?: string;
};

export type FinalAnswer = {
  title: string;
  summary: string;
  sections: FinalAnswerSection[];
  citations: FinalAnswerCitation[];
  limitations: string[];
};

export type AgentNewsPanelItem = {
  title: string;
  summary?: string;
  localizedTitle?: string;
  localizedSummary?: string;
  originalTitle?: string;
  originalSummary?: string;
  url?: string;
  source?: string;
  publishedAt?: string;
  symbol?: string;
  symbols: string[];
  eventType?: string;
  impactDirection?: string;
  relevanceScore?: number;
  importanceScore?: number;
};

export type AgentNewsPanelData = {
  symbol?: string;
  updatedAt?: string;
  latestNews: AgentNewsPanelItem[];
  majorNews: AgentNewsPanelItem[];
};

export type AgentAnalysisTiming = {
  totalMs?: number;
  cacheHit?: boolean;
  cacheLayer?: string;
  newsFetchMs?: number;
  roleAnalysisMs?: number;
  finalAnswerMs?: number;
};

export type AgentAnalysisReport = {
  analysisId: string;
  summary: string;
  symbol?: string;
  status?: string;
  route?: IntentRoute | null;
  finalAnswer?: FinalAnswer | null;
  findings: AgentFinding[];
  providerEvidence: AgentEvidenceItem[];
  notificationDecision?: NotificationDecision | null;
  layoutProposal?: LayoutProposal | null;
  timing?: AgentAnalysisTiming | null;
};

export type AgentAnalysisRequestInput = {
  agentIds: string[];
  messages: AgentAnalysisMessage[];
  symbol: string;
  intent: string;
  chartContext: unknown;
  layoutContext?: unknown;
  routerMode?: "hybrid" | "rules" | "strict-llm";
};

export type AgentAnalysisMessage = {
  role: "user" | "assistant" | "system";
  content: string;
  [key: string]: unknown;
};

const panelAliases: Record<PanelType, string[]> = {
  chart: ["차트", "캔들", "가격 그래프", "chart"],
  newsFeed: ["뉴스", "시장 뉴스", "기사", "헤드라인", "news"],
  hotRanking: ["Hot Ranking", "거래대금", "거래대금 순위", "랭킹", "ranking"],
  indicatorCompare: ["지표", "지표 비교", "인디케이터", "거시", "indicator"],
  orderTicket: ["주문", "주문 입력", "주문창", "매수창", "매도창", "order", "ticket"],
  portfolioHoldings: ["내 투자", "보유종목", "잔고", "계좌", "포트폴리오", "portfolio", "holdings", "balance"],
  aiSummary: ["AI 요약", "요약", "AI 어시스턴트", "assistant"],
  ontologyGraph: ["온톨로지", "관계 그래프", "기업 관계", "ontology"],
  chartDevLog: ["차트 로그", "진단 로그", "개발 로그", "chart dev log", "diagnostics"]
};

export function buildAgentAnalysisRequest({
  agentIds,
  messages,
  symbol,
  intent,
  chartContext,
  layoutContext,
  routerMode = "hybrid"
}: AgentAnalysisRequestInput) {
  const request = {
    agentIds,
    messages: messages.map((message) => ({ role: message.role, content: message.content })),
    symbol,
    intent,
    chartContext,
    routerMode
  };
  return layoutContext === undefined ? request : { ...request, layoutContext };
}

export function buildAgentLayoutContext(layout: WorkspaceLayout) {
  return {
    version: layout.version,
    selectedPanelId: layout.selectedPanelId,
    panels: layout.panels.map((panel) => {
      const definition = getPanelDefinition(panel.type);
      return {
        id: panel.id,
        type: panel.type,
        title: panel.title ?? definition.title,
        variant: panel.variant,
        placement: panel.placement,
        layoutPinned: Boolean(panel.layoutPinned),
        layoutWeight: panel.layoutWeight,
        minSpan: definition.minSpan,
        maxSpan: definition.maxSpan,
        aliases: panelAliases[panel.type]
      };
    })
  };
}

export function normalizeAgentAnalysisReport(payload: unknown): AgentAnalysisReport {
  const source = readObject(payload);
  if (!source) {
    throw invalidReportError();
  }

  const analysisId = readString(source.analysisId);
  const summary = readString(source.summary);
  if (!analysisId || !summary) {
    throw invalidReportError();
  }

  return {
    analysisId,
    summary,
    symbol: readString(source.symbol) ?? undefined,
    status: readString(source.status) ?? undefined,
    route: normalizeRoute(source.route),
    finalAnswer: normalizeFinalAnswer(source.finalAnswer),
    findings: readArray(source.findings).map(normalizeFinding).filter((item): item is AgentFinding => Boolean(item)),
    providerEvidence: readArray(source.providerEvidence).map(normalizeEvidence).filter((item): item is AgentEvidenceItem => Boolean(item)),
    notificationDecision: normalizeNotification(source.notificationDecision),
    layoutProposal: normalizeLayoutProposal(source.layoutProposal),
    timing: normalizeTiming(source.timing)
  };
}

export function formatAgentAnalysisReport(report: AgentAnalysisReport): string {
  const lines = report.finalAnswer ? formatFinalAnswer(report.finalAnswer) : [report.summary];

  const unusualEventFinding = report.findings.find((finding) =>
    finding.role === "unusual-event-explanation" && finding.summary && !finding.summary.toLowerCase().startsWith("no unusual")
  );
  if (unusualEventFinding) {
    lines.push("", `이상 이벤트: ${unusualEventFinding.summary}`);
  }

  const noDataEvidence = report.providerEvidence
    .filter((item) => item.status === "no-data")
    .slice(0, 5);
  const ontologyNoData = noDataEvidence.filter(isOntologyRelationshipNoData);
  const providerNoData = noDataEvidence.filter((item) => !isOntologyRelationshipNoData(item));
  if (ontologyNoData.length) {
    lines.push("", "확인되지 않은 내용:");
    lines.push(...ontologyNoData.map((item) => `- ${item.summary ?? "온톨로지 관계 근거가 확인되지 않았습니다."}`));
  }
  if (providerNoData.length) {
    lines.push("", "Provider status:");
    lines.push(...providerNoData.map((item) => `- ${providerNoDataLabel(item)}: ${item.summary ?? "데이터가 아직 연결되지 않았습니다."}`));
  }

  const decision = report.notificationDecision;
  if (decision && ["watch", "alert", "critical"].includes(decision.level)) {
    lines.push("", `알림 판단: ${decision.level.toUpperCase()}${decision.title ? ` - ${decision.title}` : ""}`);
    if (decision.message) {
      lines.push(decision.message);
    }
    if (decision.reason) {
      lines.push(`근거: ${decision.reason}`);
    }
  }

  const verificationFinding = report.findings.find((finding) =>
    finding.role === "verification-guardrail" && finding.summary && isVerificationWarning(finding)
  );
  if (verificationFinding) {
    lines.push("", `검증 경고: ${verificationFinding.summary}`);
  }

  const timingSummary = formatTimingSummary(report.timing);
  if (timingSummary) {
    lines.push("", timingSummary);
  }

  return lines.join("\n");
}

function formatFinalAnswer(finalAnswer: FinalAnswer): string[] {
  const lines = [finalAnswer.title, finalAnswer.summary];
  for (const section of finalAnswer.sections.slice(0, 3)) {
    if (!section.title || section.bullets.length === 0) {
      continue;
    }
    lines.push("", section.title);
    lines.push(...section.bullets.slice(0, 5).map((bullet) => `- ${bullet}`));
  }
  const linkedCitations = finalAnswer.citations.filter((citation) => Boolean(citation.url));
  if (linkedCitations.length) {
    lines.push("", "근거 링크:");
    lines.push(...linkedCitations.slice(0, 5).map((citation) =>
      `- ${citation.title} (${citation.url})`
    ));
  }
  if (finalAnswer.limitations.length) {
    lines.push("", "제한 사항:");
    lines.push(...finalAnswer.limitations.slice(0, 5).map((limitation) => `- ${limitation}`));
  }
  return lines;
}

function normalizeFinding(value: unknown): AgentFinding | null {
  const source = readObject(value);
  const agentId = readString(source?.agentId);
  const role = readString(source?.role);
  const summary = readString(source?.summary);
  if (!source || !agentId || !role || !summary) {
    return null;
  }
  return {
    agentId,
    role,
    summary,
    rationale: readString(source.rationale) ?? undefined,
    confidence: typeof source.confidence === "number" ? source.confidence : undefined,
    evidence: readArray(source.evidence).map(normalizeEvidence).filter((item): item is AgentEvidenceItem => Boolean(item)),
    tags: readArray(source.tags).map(readString).filter((item): item is string => Boolean(item))
  };
}

function normalizeEvidence(value: unknown): AgentEvidenceItem | null {
  const source = readObject(value);
  const provider = readString(source?.provider);
  const status = readString(source?.status);
  if (!source || !provider || !status) {
    return null;
  }
  return {
    provider,
    status,
    title: readString(source.title) ?? undefined,
    summary: readString(source.summary) ?? undefined,
    url: readString(source.url) ?? undefined,
    observedAt: readString(source.observedAt) ?? undefined,
    raw: readObject(source.raw) ?? undefined
  };
}

function normalizeRoute(value: unknown): IntentRoute | null {
  const source = readObject(value);
  const routeSource = readString(source?.source);
  const intentType = readString(source?.intentType);
  const selectedRoles = readArray(source?.selectedRoles).map(readString).filter((item): item is string => Boolean(item));
  if (!source || !routeSource || !intentType) {
    return null;
  }
  return {
    source: routeSource,
    intentType,
    selectedRoles,
    confidence: typeof source.confidence === "number" ? source.confidence : undefined,
    reason: readString(source.reason) ?? undefined
  };
}

function normalizeFinalAnswer(value: unknown): FinalAnswer | null {
  const source = readObject(value);
  const title = readString(source?.title);
  const summary = readString(source?.summary);
  if (!source || !title || !summary) {
    return null;
  }
  return {
    title,
    summary,
    sections: readArray(source.sections).map(normalizeFinalAnswerSection).filter((item): item is FinalAnswerSection => Boolean(item)),
    citations: readArray(source.citations).map(normalizeFinalAnswerCitation).filter((item): item is FinalAnswerCitation => Boolean(item)),
    limitations: readArray(source.limitations).map(readString).filter((item): item is string => Boolean(item))
  };
}

function normalizeFinalAnswerSection(value: unknown): FinalAnswerSection | null {
  const source = readObject(value);
  const title = readString(source?.title);
  if (!source || !title) {
    return null;
  }
  return {
    title,
    bullets: readArray(source.bullets).map(readString).filter((item): item is string => Boolean(item))
  };
}

function normalizeFinalAnswerCitation(value: unknown): FinalAnswerCitation | null {
  const source = readObject(value);
  const provider = readString(source?.provider);
  const title = readString(source?.title);
  if (!source || !provider || !title) {
    return null;
  }
  return {
    provider,
    title,
    url: readString(source.url) ?? undefined,
    publishedAt: readString(source.publishedAt) ?? undefined
  };
}

function normalizeNotification(value: unknown): NotificationDecision | null {
  const source = readObject(value);
  const level = readString(source?.level);
  if (!source || !level) {
    return null;
  }
  return {
    level,
    title: readString(source.title) ?? undefined,
    message: readString(source.message) ?? undefined,
    reason: readString(source.reason) ?? undefined
  };
}

function normalizeTiming(value: unknown): AgentAnalysisTiming | null {
  const source = readObject(value);
  if (!source) {
    return null;
  }
  return {
    totalMs: readNumber(source.totalMs) ?? undefined,
    cacheHit: readBoolean(source.cacheHit) ?? undefined,
    cacheLayer: readString(source.cacheLayer) ?? undefined,
    newsFetchMs: readNumber(source.newsFetchMs) ?? undefined,
    roleAnalysisMs: readNumber(source.roleAnalysisMs) ?? undefined,
    finalAnswerMs: readNumber(source.finalAnswerMs) ?? undefined
  };
}

const layoutCommandTypes: LayoutCommandType[] = [
  "layout.panel.add",
  "layout.panel.remove",
  "layout.panel.move",
  "layout.panel.replace",
  "layout.panel.props.update",
  "layout.panel.pin",
  "layout.panel.unpin",
  "layout.panel.select",
  "layout.panel.priority.set",
  "layout.panels.arrange",
  "layout.boundary.resize",
  "layout.reflow",
  "layout.undo",
  "layout.redo",
  "layout.save",
  "layout.update",
  "layout.delete",
  "layout.load",
  "layout.favorite.set",
  "layout.default.restore",
  "layout.reset",
  "layout.autoApply.set",
  "layout.proposal.accept",
  "layout.proposal.reject"
];

function normalizeLayoutProposal(value: unknown): LayoutProposal | null {
  const source = readObject(value);
  const title = readString(source?.title);
  const rationale = readString(source?.rationale);
  if (!source || !title || !rationale) {
    return null;
  }

  return {
    id: readString(source.id) ?? `layout-proposal-${Date.now()}`,
    title,
    rationale,
    autoApply: typeof source.autoApply === "boolean" ? source.autoApply : true,
    panelPriorities: readArray(source.panelPriorities)
      .map(normalizePanelPriority)
      .filter((item): item is NonNullable<LayoutProposal["panelPriorities"]>[number] => Boolean(item)),
    commands: readArray(source.commands)
      .map(normalizeLayoutCommand)
      .filter((item): item is LayoutCommand => Boolean(item)),
    createdAt: readString(source.createdAt) ?? new Date().toISOString()
  };
}

function normalizePanelPriority(value: unknown): NonNullable<LayoutProposal["panelPriorities"]>[number] | null {
  const source = readObject(value);
  const panelId = readString(source?.panelId);
  const layoutWeight = readNumber(source?.layoutWeight);
  if (!source || !panelId || layoutWeight === null) {
    return null;
  }
  return {
    panelId,
    panelType: readString(source.panelType) ?? undefined,
    layoutWeight,
    reason: readString(source.reason) ?? undefined
  };
}

function normalizeLayoutCommand(value: unknown): LayoutCommand | null {
  const source = readObject(value);
  const type = readLayoutCommandType(source?.type);
  const payload = readObject(source?.payload);
  if (!source || !type || !payload) {
    return null;
  }

  const target = readObject(source.target);
  const proposalId = readString(source.proposalId) ?? undefined;
  return {
    id: readString(source.id) ?? `cmd-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    type,
    actor: readCommandActor(source.actor) ?? "llm",
    target: target ? {
      panelId: readString(target.panelId) ?? undefined,
      group: target.group === "workspace" || target.group === "agentRail" ? target.group : undefined,
      zone: target.zone === "main" || target.zone === "context" || target.zone === "mainContext" || target.zone === "agentRail"
        ? target.zone
        : undefined
    } : undefined,
    payload,
    createdAt: readString(source.createdAt) ?? new Date().toISOString(),
    ...(proposalId ? { proposalId } : {})
  };
}

function readLayoutCommandType(value: unknown): LayoutCommandType | null {
  return typeof value === "string" && layoutCommandTypes.includes(value as LayoutCommandType)
    ? value as LayoutCommandType
    : null;
}

function readCommandActor(value: unknown): CommandActor | null {
  return value === "user" || value === "llm" || value === "system" ? value : null;
}

function labelForProvider(provider: string): string {
  const labels: Record<string, string> = {
    news: "뉴스",
    macro: "거시",
    ontology: "온톨로지"
  };
  return labels[provider] ?? provider;
}

function providerNoDataLabel(item: AgentEvidenceItem): string {
  const relationType = typeof item.raw?.relationType === "string" ? item.raw.relationType : "";
  if (item.provider === "ontology" && relationType === "graphdb-unavailable") {
    return "GraphDB 연결 실패";
  }
  return `${labelForProvider(item.provider)} provider 미연결`;
}

function isOntologyRelationshipNoData(item: AgentEvidenceItem): boolean {
  if (item.provider !== "ontology") {
    return false;
  }
  const relationType = typeof item.raw?.relationType === "string" ? item.raw.relationType : "";
  return ["no-direct-control", "no-ontology-evidence"].includes(relationType);
}

function isVerificationWarning(finding: AgentFinding): boolean {
  const normalized = finding.summary.trim().toLowerCase();
  if (!normalized || normalized.startsWith("no trading-action guardrail violation detected")) {
    return false;
  }
  return true;
}

function readArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function readObject(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function readNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function readBoolean(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function formatTimingSummary(timing?: AgentAnalysisTiming | null): string | null {
  if (!timing) {
    return null;
  }
  const parts: string[] = [];
  if (typeof timing.newsFetchMs === "number") {
    parts.push(`검색 ${formatMilliseconds(timing.newsFetchMs)}`);
  }
  if (typeof timing.totalMs === "number") {
    parts.push(`전체 ${formatMilliseconds(timing.totalMs)}`);
  }
  if (!parts.length) {
    return null;
  }
  return parts.join(" / ");
}

function formatMilliseconds(ms: number): string {
  return `${(Math.max(0, ms) / 1000).toFixed(1)}초`;
}

function invalidReportError(): Error {
  return new Error("멀티에이전트 분석 응답 형식이 올바르지 않습니다.");
}
