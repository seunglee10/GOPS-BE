import { Check, X } from "lucide-react";
import type { ChartProposalDocument, Command } from "../types/commands";

interface ProposalPanelProps {
  proposals: ChartProposalDocument[];
  onAccept(proposalId: string): void;
  onReject(proposalId: string): void;
}

export function ProposalPanel({ proposals, onAccept, onReject }: ProposalPanelProps): JSX.Element {
  return (
    <div className="panel proposal-panel">
      <div className="panel-header">
        <h2>AI Proposals</h2>
        <span className="status-text">{proposals.length} pending</span>
      </div>
      <div className="proposal-list">
        {proposals.length === 0 ? <p className="empty-state">No pending proposals</p> : null}
        {proposals.map((proposal) => (
          <article key={proposal.id} className="proposal-item">
            <div className="proposal-copy">
              <strong>{proposal.title}</strong>
              <p>{proposal.rationale}</p>
              <p>{proposal.previewSummary}</p>
              <span>{proposal.commands.length} commands</span>
              <ul className="proposal-command-list">
                {proposal.commands.map((command) => (
                  <li key={command.id}>{summarizeCommand(command)}</li>
                ))}
              </ul>
              {proposal.validationErrors.length > 0 ? (
                <ul className="proposal-errors">
                  {proposal.validationErrors.map((error, index) => (
                    <li key={`${proposal.id}-${index}`}>{error.message}</li>
                  ))}
                </ul>
              ) : null}
            </div>
            <div className="proposal-actions">
              <button type="button" onClick={() => onAccept(proposal.id)} title="Accept">
                <Check size={16} />
                <span>Accept</span>
              </button>
              <button type="button" onClick={() => onReject(proposal.id)} title="Reject">
                <X size={16} />
                <span>Reject</span>
              </button>
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}

function summarizeCommand(command: Command): string {
  if (command.type === "chart.symbol.set") return `Change symbol to ${command.payload.symbol}`;
  if (command.type === "chart.timeframe.set") return `Change timeframe to ${command.payload.timeframe}`;
  if (command.type === "chart.indicator.add") return `Add ${command.payload.node.type} indicator`;
  if (command.type === "chart.indicator.update") return `Update indicator ${command.payload.calculationNodeId}`;
  if (command.type === "chart.indicator.remove") return `Remove indicator ${command.payload.layerId}`;
  if (command.type === "chart.drawing.add") {
    const drawing = command.payload.layer.drawing;
    if (drawing.kind === "horizontalLine") return `Add horizontal line at ${drawing.price.toFixed(2)}`;
    return `Add ${drawing.kind} drawing`;
  }
  if (command.type === "chart.drawing.update") {
    if (command.payload.drawing.kind === "horizontalLine") return `Update horizontal line to ${command.payload.drawing.price.toFixed(2)}`;
    return `Update ${command.payload.drawing.kind} drawing`;
  }
  if (command.type === "chart.drawing.remove") return `Remove drawing ${command.payload.layerId}`;
  if (command.type === "chart.comparison.add") return `Compare ${command.payload.layer.symbol}`;
  if (command.type === "chart.comparison.remove") return `Remove comparison ${command.payload.layerId}`;
  if (command.type === "chart.viewport.set") return "Adjust chart viewport";
  if (command.type === "chart.layer.visibility.set") return `${command.payload.visible ? "Show" : "Hide"} layer ${command.payload.layerId}`;
  return command.type;
}
