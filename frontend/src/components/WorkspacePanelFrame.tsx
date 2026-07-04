import { X } from "lucide-react";
import type { CSSProperties, PointerEvent as ReactPointerEvent, ReactNode } from "react";
import type { PanelContentInstance, PanelSlot } from "../layout/panelLayout";

type WorkspacePanelFrameProps = {
  slot: PanelSlot;
  content: PanelContentInstance;
  style: CSSProperties;
  className?: string;
  isBoundaryActive?: boolean;
  isChartHovered?: boolean;
  showNav?: boolean;
  canSwap?: boolean;
  canClose?: boolean;
  onClose?: (slotId: string) => void;
  onSwapPointerDown?: (slotId: string) => (event: ReactPointerEvent<HTMLElement>) => void;
  onPointerEnter?: () => void;
  onPointerLeave?: () => void;
  children: ReactNode;
};

export function WorkspacePanelFrame({
  slot,
  content,
  style,
  className = "",
  isBoundaryActive = false,
  isChartHovered = false,
  showNav = true,
  canSwap = false,
  canClose = false,
  onClose,
  onSwapPointerDown,
  onPointerEnter,
  onPointerLeave,
  children
}: WorkspacePanelFrameProps) {
  return (
    <section
      className={[
        "workspace-panel-frame",
        "workspace-panel-surface",
        showNav ? "has-panel-nav" : "has-no-panel-nav",
        content.kind === "chart" ? "chart-lane-frame" : "content-panel-frame",
        isBoundaryActive ? "is-boundary-active" : "",
        isChartHovered ? "is-chart-hovered" : "",
        className
      ].filter(Boolean).join(" ")}
      style={style}
      data-panel-slot-id={slot.id}
      data-panel-kind={content.kind}
      onPointerEnter={onPointerEnter}
      onPointerLeave={onPointerLeave}
    >
      {showNav && (
        <header
          className={canSwap ? "workspace-panel-nav is-swappable" : "workspace-panel-nav"}
          aria-label={`${content.title} panel navigation`}
          onPointerDown={canSwap ? onSwapPointerDown?.(slot.id) : undefined}
        >
          <span className="workspace-panel-title">{content.title}</span>
          {canClose ? (
            <button
              type="button"
              className="workspace-panel-close"
              aria-label={`${content.title} 패널 닫기`}
              title="패널 닫기"
              onPointerDown={(event) => event.stopPropagation()}
              onClick={() => onClose?.(slot.id)}
            >
              <X size={13} />
            </button>
          ) : (
            <span className="workspace-panel-close-placeholder" aria-hidden="true" />
          )}
        </header>
      )}
      <div className={content.kind === "chart" ? "workspace-panel-body chart-panel-body" : "workspace-panel-body"}>
        {children}
      </div>
    </section>
  );
}
