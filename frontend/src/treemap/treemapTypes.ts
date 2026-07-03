export type TreeMapRect = {
  x: number;
  y: number;
  width: number;
  height: number;
};

export type TreeMapInputItem = {
  symbol: string;
  companyName: string;
  sector: string;
  industry: string;
  value: number;
  marketCap: number;
  indexWeight?: number;
  changePercent: number;
};

export type TreeMapTileKind = "sector" | "industry" | "symbol";

export type TreeMapTile = TreeMapRect & {
  id: string;
  kind: TreeMapTileKind;
  label: string;
  value: number;
  depth: number;
  parentId?: string;
  sector?: string;
  industry?: string;
  symbol?: string;
  companyName?: string;
  marketCap?: number;
  indexWeight?: number;
  changePercent?: number;
};
