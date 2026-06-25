export const frameColumnRatios = [1, 1, 1, 1.32, 1.22] as const;
export const workspaceColumnCount = 4;
export const workspaceRowCount = 5;

function totalFrameColumnRatio(): number {
  return frameColumnRatios.reduce((sum, ratio) => sum + ratio, 0);
}

export function columnLinePercent(line: number): number {
  const ratioBeforeLine = frameColumnRatios.slice(0, line - 1).reduce((sum, ratio) => sum + ratio, 0);
  return (ratioBeforeLine / totalFrameColumnRatio()) * 100;
}

export function columnSpanPercent(col: number, colSpan: number): number {
  return columnLinePercent(col + colSpan) - columnLinePercent(col);
}

export function rowLinePercent(line: number): number {
  return ((line - 1) / workspaceRowCount) * 100;
}

export function workspaceColumnStarts(frameWidth: number): number[] {
  const unit = frameWidth / totalFrameColumnRatio();
  let nextStart = 0;

  return frameColumnRatios.slice(0, workspaceColumnCount).map((ratio) => {
    const start = nextStart;
    nextStart += ratio * unit;
    return start;
  });
}

export function workspaceRowStarts(frameHeight: number): number[] {
  const rowUnit = frameHeight / workspaceRowCount;
  return Array.from({ length: workspaceRowCount }, (_, index) => index * rowUnit);
}
