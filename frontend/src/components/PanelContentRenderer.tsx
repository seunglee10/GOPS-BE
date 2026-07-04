import { X } from "lucide-react";
import type { MutableRefObject, PointerEvent as ReactPointerEvent } from "react";
import type { SemanticSelectionSnapshot } from "../chart/semanticTimeline";
import type { ChartSymbolDto } from "../chart/types";
import type { PanelContentInstance, PanelSlot, PanelSlotId } from "../layout/panelLayout";
import { OntologyPanel } from "../ontology/OntologyPanel";
import { ChartPanel, type ChartHeaderSnapshot, type ChartPanelHandle } from "./ChartPanel";
import { SymbolSearch } from "./SymbolSearch";

type PanelContentRendererProps = {
  slot: PanelSlot;
  content: PanelContentInstance;
  symbol: string;
  symbols: ChartSymbolDto[];
  laneHeight: number;
  chartPanelRef: MutableRefObject<ChartPanelHandle | null> | null;
  chartHeaderSnapshot?: ChartHeaderSnapshot;
  setSemanticSelection: (selection: SemanticSelectionSnapshot | null) => void;
  onChartHoverChange: (hovered: boolean) => void;
  onHeaderChange?: (header: ChartHeaderSnapshot) => void;
  onClosePanel: (slotId: PanelSlotId) => void;
  onChangePanelChartSymbol: (contentId: string, symbol: string) => void;
  onChartSwapPointerDown?: (event: ReactPointerEvent<HTMLElement>) => void;
};

export function PanelContentRenderer({
  slot,
  content,
  symbol,
  symbols,
  laneHeight,
  chartPanelRef,
  chartHeaderSnapshot,
  setSemanticSelection,
  onChartHoverChange,
  onHeaderChange,
  onClosePanel,
  onChangePanelChartSymbol,
  onChartSwapPointerDown
}: PanelContentRendererProps) {
  if (content.kind === "ontology") {
    return <OntologyPanel symbol={symbol} />;
  }

  if (content.kind !== "chart") {
    return <div className="workspace-panel-empty" aria-label={`${content.title} content`} data-panel-slot-id={slot.id} />;
  }

  const selectedSymbol = symbol.toUpperCase();
  const interval = chartHeaderSnapshot?.interval ?? "1D";
  const editable = !content.isDefaultChart;

  return (
    <div className={content.isDefaultChart ? "chart-instance is-default-chart" : "chart-instance is-editable-chart"}>
      <div
        className={editable ? "chart-instance-symbol chart-instance-swap-handle" : "chart-instance-symbol"}
        onPointerEnter={() => onChartHoverChange(true)}
        onPointerMove={() => onChartHoverChange(true)}
        onPointerDown={editable ? onChartSwapPointerDown : undefined}
      >
        <span className="chart-instance-interval">{interval}</span>
        {!editable && <span className="chart-instance-symbol-text">{selectedSymbol}</span>}
        {editable && (
          <div className="chart-instance-symbol-search-wrap" onPointerDown={(event) => event.stopPropagation()}>
            <SymbolSearch
              symbols={symbols}
              className="chart-instance-symbol-search"
              compact
              selectedSymbol={selectedSymbol}
              selectedLabel={selectedSymbol}
              placeholder={selectedSymbol}
              formatSelectedLabel={(symbolOption) => symbolOption.symbol}
              onSelectSymbol={(nextSymbol) => onChangePanelChartSymbol(content.id, nextSymbol)}
              onPointerActivity={() => onChartHoverChange(true)}
            />
          </div>
        )}
      </div>
      {editable && (
        <button
          type="button"
          className="chart-instance-close"
          aria-label="차트 패널 닫기"
          title="차트 패널 닫기"
          onPointerDown={(event) => event.stopPropagation()}
          onClick={() => onClosePanel(slot.id)}
        >
          <X size={13} />
        </button>
      )}
      <ChartPanel
        ref={chartPanelRef ?? undefined}
        symbol={selectedSymbol}
        symbols={symbols}
        laneHeight={laneHeight}
        onSemanticSelectionChange={setSemanticSelection}
        onChartHoverChange={onChartHoverChange}
        onHeaderChange={onHeaderChange}
      />
    </div>
  );
}
