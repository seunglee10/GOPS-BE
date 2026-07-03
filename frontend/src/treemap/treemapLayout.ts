import type { TreeMapInputItem, TreeMapRect, TreeMapTile } from "./treemapTypes";

type WeightedNode<T> = {
  item: T;
  value: number;
  area: number;
};

type LayoutNode<T> = WeightedNode<T> & {
  rect: TreeMapRect;
};

type Group<T> = {
  id: string;
  label: string;
  value: number;
  changePercent: number;
  items: T[];
};

const minimumLayoutSize = 0.01;

export function layoutSp500TreeMap(items: TreeMapInputItem[], bounds: TreeMapRect): TreeMapTile[] {
  const safeBounds = normalizeRect(bounds);
  if (!items.length || safeBounds.width <= 0 || safeBounds.height <= 0) {
    return [];
  }

  const tiles: TreeMapTile[] = [];
  const sectors = groupItems(items, (item) => item.sector);
  const sectorNodes = squarify(sectors, safeBounds, (sector) => sector.value);

  sectorNodes.forEach(({ item: sector, rect }) => {
    const sectorId = `sector:${sector.label}`;
    tiles.push({
      ...rect,
      id: sectorId,
      kind: "sector",
      label: sector.label,
      value: sector.value,
      changePercent: sector.changePercent,
      depth: 0,
      sector: sector.label
    });

    const sectorInner = contentRect(rect, headerHeight(rect, 28), 5);
    const industries = groupItems(sector.items, (item) => item.industry);
    const industryNodes = squarify(industries, sectorInner, (industry) => industry.value);

    industryNodes.forEach(({ item: industry, rect: industryRect }) => {
      const industryId = `${sectorId}:industry:${industry.label}`;
      tiles.push({
        ...industryRect,
        id: industryId,
        kind: "industry",
        label: industry.label,
        value: industry.value,
        changePercent: industry.changePercent,
        depth: 1,
        parentId: sectorId,
        sector: sector.label,
        industry: industry.label
      });

      const symbolInner = contentRect(industryRect, headerHeight(industryRect, 16), 2);
      const symbolNodes = squarify(industry.items, symbolInner, (item) => item.value);
      symbolNodes.forEach(({ item, rect: symbolRect }) => {
        tiles.push({
          ...symbolRect,
          id: `symbol:${item.symbol}`,
          kind: "symbol",
          label: item.symbol,
          value: item.value,
          depth: 2,
          parentId: industryId,
          sector: item.sector,
          industry: item.industry,
          symbol: item.symbol,
          companyName: item.companyName,
          marketCap: item.marketCap,
          indexWeight: item.indexWeight,
          changePercent: item.changePercent
        });
      });
    });
  });

  return tiles;
}

export function hitTestTreeMapTile(tiles: TreeMapTile[], x: number, y: number): TreeMapTile | undefined {
  for (let index = tiles.length - 1; index >= 0; index -= 1) {
    const tile = tiles[index];
    if (
      tile.kind === "symbol" &&
      x >= tile.x &&
      x <= tile.x + tile.width &&
      y >= tile.y &&
      y <= tile.y + tile.height
    ) {
      return tile;
    }
  }
  return undefined;
}

function groupItems<T extends { changePercent: number }>(
  items: T[],
  getKey: (item: T) => string
): Group<T>[] {
  const groups = new Map<string, T[]>();
  items.forEach((item) => {
    const key = getKey(item);
    const existing = groups.get(key);
    if (existing) {
      existing.push(item);
    } else {
      groups.set(key, [item]);
    }
  });

  return [...groups.entries()]
    .map(([label, groupItemsForKey]) => {
      const value = groupItemsForKey.reduce((sum, item) => sum + readValue(item), 0);
      return {
        id: label,
        label,
        value,
        changePercent: weightedAverageChange(groupItemsForKey),
        items: groupItemsForKey
      };
    })
    .sort((left, right) => right.value - left.value || left.label.localeCompare(right.label));
}

function squarify<T>(items: T[], bounds: TreeMapRect, getValue: (item: T) => number): LayoutNode<T>[] {
  const rect = normalizeRect(bounds);
  const area = rect.width * rect.height;
  const positiveItems = items
    .map((item) => ({ item, value: Math.max(0, getValue(item)) }))
    .filter((item) => item.value > 0)
    .sort((left, right) => right.value - left.value);
  const total = positiveItems.reduce((sum, item) => sum + item.value, 0);

  if (!total || area <= 0) {
    return [];
  }

  const weighted = positiveItems.map((item): WeightedNode<T> => ({
    ...item,
    area: item.value / total * area
  }));

  const laidOut: LayoutNode<T>[] = [];
  let remainingRect = rect;
  let row: WeightedNode<T>[] = [];
  const queue = [...weighted];

  while (queue.length) {
    const next = queue[0];
    const side = shortestSide(remainingRect);
    if (!row.length || worstAspectRatio([...row, next], side) <= worstAspectRatio(row, side)) {
      row.push(next);
      queue.shift();
      continue;
    }
    const placed = layoutRow(row, remainingRect);
    laidOut.push(...placed.nodes);
    remainingRect = placed.remaining;
    row = [];
  }

  if (row.length) {
    const placed = layoutRow(row, remainingRect);
    laidOut.push(...placed.nodes);
  }

  return laidOut.map((node) => ({ ...node, rect: normalizeRect(node.rect) }));
}

function layoutRow<T>(
  row: WeightedNode<T>[],
  rect: TreeMapRect
): { nodes: LayoutNode<T>[]; remaining: TreeMapRect } {
  const area = row.reduce((sum, item) => sum + item.area, 0);
  if (rect.width >= rect.height) {
    const rowWidth = clamp(area / Math.max(rect.height, minimumLayoutSize), 0, rect.width);
    let y = rect.y;
    const nodes = row.map((item, index): LayoutNode<T> => {
      const isLast = index === row.length - 1;
      const height = isLast ? rect.y + rect.height - y : item.area / Math.max(rowWidth, minimumLayoutSize);
      const node = { ...item, rect: normalizeRect({ x: rect.x, y, width: rowWidth, height }) };
      y += height;
      return node;
    });
    return {
      nodes,
      remaining: normalizeRect({
        x: rect.x + rowWidth,
        y: rect.y,
        width: rect.width - rowWidth,
        height: rect.height
      })
    };
  }

  const rowHeight = clamp(area / Math.max(rect.width, minimumLayoutSize), 0, rect.height);
  let x = rect.x;
  const nodes = row.map((item, index): LayoutNode<T> => {
    const isLast = index === row.length - 1;
    const width = isLast ? rect.x + rect.width - x : item.area / Math.max(rowHeight, minimumLayoutSize);
    const node = { ...item, rect: normalizeRect({ x, y: rect.y, width, height: rowHeight }) };
    x += width;
    return node;
  });
  return {
    nodes,
    remaining: normalizeRect({
      x: rect.x,
      y: rect.y + rowHeight,
      width: rect.width,
      height: rect.height - rowHeight
    })
  };
}

function worstAspectRatio(row: WeightedNode<unknown>[], side: number): number {
  if (!row.length || side <= 0) {
    return Number.POSITIVE_INFINITY;
  }
  const areas = row.map((item) => item.area).filter((area) => area > 0);
  if (!areas.length) {
    return Number.POSITIVE_INFINITY;
  }
  const sum = areas.reduce((total, area) => total + area, 0);
  const max = Math.max(...areas);
  const min = Math.min(...areas);
  const sideSquared = side * side;
  const sumSquared = sum * sum;
  return Math.max((sideSquared * max) / sumSquared, sumSquared / (sideSquared * min));
}

function shortestSide(rect: TreeMapRect): number {
  return Math.max(minimumLayoutSize, Math.min(rect.width, rect.height));
}

function contentRect(rect: TreeMapRect, header: number, padding: number): TreeMapRect {
  const horizontal = Math.min(padding, rect.width / 4);
  const vertical = Math.min(padding, rect.height / 4);
  return normalizeRect({
    x: rect.x + horizontal,
    y: rect.y + header + vertical,
    width: rect.width - horizontal * 2,
    height: rect.height - header - vertical * 2
  });
}

function headerHeight(rect: TreeMapRect, desired: number): number {
  if (rect.width < 42 || rect.height < desired * 2.2) {
    return 0;
  }
  return Math.min(desired, rect.height * 0.24);
}

function weightedAverageChange(items: Array<{ changePercent: number }>): number {
  const total = items.reduce((sum, item) => sum + readValue(item), 0);
  if (!total) {
    return 0;
  }
  return items.reduce((sum, item) => sum + item.changePercent * readValue(item), 0) / total;
}

function readValue(item: unknown): number {
  if (item && typeof item === "object" && "value" in item) {
    const value = Number((item as { value?: unknown }).value);
    return Number.isFinite(value) ? Math.max(0, value) : 0;
  }
  return 0;
}

function normalizeRect(rect: TreeMapRect): TreeMapRect {
  return {
    x: finite(rect.x),
    y: finite(rect.y),
    width: Math.max(0, finite(rect.width)),
    height: Math.max(0, finite(rect.height))
  };
}

function finite(value: number): number {
  return Number.isFinite(value) ? value : 0;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
