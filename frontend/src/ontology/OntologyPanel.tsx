import { useEffect, useMemo, useState } from "react";
import { buildOntologyGraphFromEvidence } from "./buildOntologyGraphFromEvidence";
import { requestOntologyReport } from "./ontologyReportClient";
import type { AgentEvidenceItem } from "./ontologyTypes";
import { OntologyGraphView } from "./OntologyGraphView";

type OntologyPanelProps = {
  symbol: string;
};

type LoadState = "loading" | "ready";

export function OntologyPanel({ symbol }: OntologyPanelProps) {
  const normalizedSymbol = symbol.trim().toUpperCase();
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [evidence, setEvidence] = useState<AgentEvidenceItem[]>([]);

  useEffect(() => {
    const controller = new AbortController();
    setLoadState("loading");
    requestOntologyReport({ symbol: normalizedSymbol }, controller.signal)
      .then((report) => {
        if (!controller.signal.aborted) {
          setEvidence(report?.providerEvidence ?? []);
          setLoadState("ready");
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setEvidence([]);
          setLoadState("ready");
        }
      });
    return () => controller.abort();
  }, [normalizedSymbol]);

  const graph = useMemo(
    () => buildOntologyGraphFromEvidence(evidence, normalizedSymbol),
    [evidence, normalizedSymbol]
  );
  const ontologyEvidence = useMemo(
    () => evidence.filter((item) => item.provider === "ontology" && item.status === "available"),
    [evidence]
  );

  if (loadState === "loading") {
    return <div className="ontology-panel ontology-panel-empty">관계 분석을 불러오고 있습니다</div>;
  }

  if (!graph) {
    return <div className="ontology-panel ontology-panel-empty">관계 분석 결과가 아직 없습니다</div>;
  }

  return (
    <div className="ontology-panel">
      <OntologyGraphView graph={graph} />
      <ol className="ontology-evidence-list" aria-label="Ontology evidence">
        {ontologyEvidence.slice(0, 4).map((item, index) => (
          <li key={`${item.title ?? "evidence"}-${index}`}>
            <strong>{item.title ?? "Ontology evidence"}</strong>
            {item.summary && <span>{item.summary}</span>}
          </li>
        ))}
      </ol>
    </div>
  );
}
