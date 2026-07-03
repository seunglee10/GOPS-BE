export const frameColumnRatios = [1, 1, 1, 1.32, 1.22] as const;
export const workspaceColumnCount = 4;
export const workspaceRowCount = 5;

function activeColumnRatios(systemColumnVisible = true): readonly number[] {
  return systemColumnVisible ? frameColumnRatios : frameColumnRatios.slice(0, workspaceColumnCount);
}

function totalFrameColumnRatio(systemColumnVisible = true): number {
  return activeColumnRatios(systemColumnVisible).reduce((sum, ratio) => sum + ratio, 0);
}

export function columnLinePercent(line: number, systemColumnVisible = true): number {
  const ratioBeforeLine = activeColumnRatios(systemColumnVisible).slice(0, line - 1).reduce((sum, ratio) => sum + ratio, 0);
  return (ratioBeforeLine / totalFrameColumnRatio(systemColumnVisible)) * 100;
}

export function columnSpanPercent(col: number, colSpan: number, systemColumnVisible = true): number {
  return columnLinePercent(col + colSpan, systemColumnVisible) - columnLinePercent(col, systemColumnVisible);
}

export function rowLinePercent(line: number): number {
  return ((line - 1) / workspaceRowCount) * 100;
}

export function workspaceColumnStarts(frameWidth: number, systemColumnVisible = true): number[] {
  const unit = frameWidth / totalFrameColumnRatio(systemColumnVisible);
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
