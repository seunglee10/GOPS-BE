import type { ChartProposalDocument } from "../types/commands";
import type { WorkspaceDocument } from "../types/documents";
import { nowIso } from "./commandEngine";

export function mergeIncomingProposals(document: WorkspaceDocument, proposals: ChartProposalDocument[]): WorkspaceDocument {
  const existingIds = new Set(document.proposals.map((proposal) => proposal.id));
  const incoming = proposals.filter((proposal) => !existingIds.has(proposal.id));
  if (incoming.length === 0) return document;
  return {
    ...document,
    version: document.version + 1,
    proposals: [...document.proposals, ...incoming],
    updatedAt: nowIso()
  };
}

export function pendingProposals(document: WorkspaceDocument): ChartProposalDocument[] {
  return document.proposals.filter((proposal) => proposal.status === "pending");
}
