import type { LayerId, PaneId } from "../types/documents";

export interface HitTestResult {
  paneId: PaneId;
  layerId?: LayerId;
  timestamp?: string;
  price?: number;
  drawingHandle?: "start" | "end" | "body";
}
