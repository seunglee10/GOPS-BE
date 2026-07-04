import { useMemo, useState } from "react";
import type {
  OntologyGraphData,
  OntologyGraphEdge,
  OntologyGraphNode,
  OntologyGraphNodeKind
} from "./ontologyTypes";

type OntologyGraphViewProps = {
  graph: OntologyGraphData;
};

type PositionedNode = OntologyGraphNode & {
  x: number;
  y: number;
};

const graphWidth = 640;
const graphHeight = 360;

export function OntologyGraphView({ graph }: OntologyGraphViewProps) {
  const [activeNodeId, setActiveNodeId] = useState<string | null>(null);
  const layout = useMemo(() => layoutOntologyGraph(graph), [graph]);
  const activeNode = activeNodeId ? graph.nodes.find((node) => node.id === activeNodeId) : null;

  return (
    <div className="ontology-graph">
      <svg className="ontology-graph-svg" viewBox={`0 0 ${graphWidth} ${graphHeight}`} role="img" aria-label={`${graph.symbol} ontology graph`}>
        <g className="ontology-edges">
          {graph.edges.map((edge) => {
            const source = layout.nodes.get(edge.source);
            const target = layout.nodes.get(edge.target);
            if (!source || !target) {
              return null;
            }
            return (
              <line
                key={edge.id}
                className={`ontology-edge ${activeNodeId && edge.source !== activeNodeId && edge.target !== activeNodeId ? "is-muted" : ""}`}
                x1={source.x}
                y1={source.y}
                x2={target.x}
                y2={target.y}
              />
            );
          })}
        </g>
        <g className="ontology-nodes">
          {Array.from(layout.nodes.values()).map((node) => (
            <g
              key={node.id}
              className={`ontology-node ${node.kind} ${node.id === activeNodeId ? "is-active" : ""}`}
              transform={`translate(${node.x} ${node.y})`}
              onPointerEnter={() => setActiveNodeId(node.id)}
              onPointerLeave={() => setActiveNodeId(null)}
            >
              <circle r={nodeRadius(node.kind)} />
              <text y={node.kind === "theme" ? 29 : 25}>{node.label}</text>
            </g>
          ))}
        </g>
      </svg>
      <div className="ontology-graph-meta" aria-live="polite">
        <strong>{activeNode?.label ?? graph.symbol}</strong>
        <span>{activeNode ? nodeKindLabel(activeNode.kind) : `${graph.nodes.length} nodes / ${graph.edges.length} edges`}</span>
      </div>
    </div>
  );
}

function layoutOntologyGraph(graph: OntologyGraphData): { nodes: Map<string, PositionedNode> } {
  const primaryId = `symbol:${graph.symbol}`;
  const primary = graph.nodes.find((node) => node.id === primaryId) ?? graph.nodes.find((node) => node.kind === "symbol");
  const remaining = graph.nodes.filter((node) => node.id !== primary?.id);
  const themes = remaining.filter((node) => node.kind === "theme");
  const companies = remaining.filter((node) => node.kind === "company");
  const symbols = remaining.filter((node) => node.kind === "symbol");
  const positions = new Map<string, PositionedNode>();

  if (primary) {
    positions.set(primary.id, { ...primary, x: 136, y: graphHeight / 2 });
  }
  placeColumn(themes, 380, 72, graphHeight - 72, positions);
  placeColumn([...symbols, ...companies], 520, 82, graphHeight - 82, positions);

  graph.nodes.forEach((node, index) => {
    if (!positions.has(node.id)) {
      positions.set(node.id, { ...node, x: 280 + (index % 3) * 92, y: 96 + Math.floor(index / 3) * 72 });
    }
  });

  pullConnectedSymbolsTowardPrimary(graph.edges, positions, primary?.id);
  return { nodes: positions };
}

function placeColumn(nodes: OntologyGraphNode[], x: number, top: number, bottom: number, positions: Map<string, PositionedNode>) {
  if (!nodes.length) {
    return;
  }
  const gap = nodes.length === 1 ? 0 : (bottom - top) / (nodes.length - 1);
  nodes.forEach((node, index) => {
    positions.set(node.id, { ...node, x, y: nodes.length === 1 ? (top + bottom) / 2 : top + gap * index });
  });
}

function pullConnectedSymbolsTowardPrimary(
  edges: OntologyGraphEdge[],
  positions: Map<string, PositionedNode>,
  primaryId: string | undefined
) {
  if (!primaryId) {
    return;
  }
  const connected = new Set(edges.flatMap((edge) => [edge.source, edge.target]));
  let offset = -46;
  positions.forEach((node) => {
    if (node.kind === "symbol" && node.id !== primaryId && connected.has(node.id)) {
      node.x = 250;
      node.y = graphHeight / 2 + offset;
      offset += 46;
    }
  });
}

function nodeRadius(kind: OntologyGraphNodeKind): number {
  return kind === "theme" ? 18 : kind === "company" ? 15 : 16;
}

function nodeKindLabel(kind: OntologyGraphNodeKind): string {
  return {
    symbol: "Symbol",
    theme: "Theme",
    company: "Company"
  }[kind];
}
