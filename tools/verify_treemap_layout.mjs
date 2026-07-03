import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import vm from "node:vm";
import ts from "typescript";

const repoRoot = process.cwd();
const nodeRequire = createRequire(import.meta.url);

const seedModule = loadTsModule("frontend/src/market/sp500Universe.seed.ts");
const layoutModule = loadTsModule("frontend/src/treemap/treemapLayout.ts", {
  "./treemapTypes": {}
});

const { sp500UniverseSeed, sp500WeightValue } = seedModule;
const { layoutSp500TreeMap, hitTestTreeMapTile } = layoutModule;

assert(Array.isArray(sp500UniverseSeed), "sp500UniverseSeed must be an array");
assert(sp500UniverseSeed.length >= 500, "S&P500 seed should include the whole current index table");

const symbols = new Set();
for (const item of sp500UniverseSeed) {
  assert(typeof item.symbol === "string" && item.symbol.length > 0, "seed symbol is required");
  assert(!symbols.has(item.symbol), `duplicate symbol: ${item.symbol}`);
  symbols.add(item.symbol);
  assert(typeof item.sector === "string" && item.sector.length > 0, `${item.symbol} sector is required`);
  assert(typeof item.industry === "string" && item.industry.length > 0, `${item.symbol} industry is required`);
  assert(Number.isFinite(item.marketCap) && item.marketCap > 0, `${item.symbol} marketCap must be positive`);
  assert(item.indexWeight === undefined || item.indexWeight > 0, `${item.symbol} indexWeight must be positive when present`);
}

for (const required of ["TSLA", "AAPL", "GOOGL"]) {
  assert(symbols.has(required), `${required} must exist in S&P500 seed`);
}

const input = sp500UniverseSeed.map((item) => ({
  symbol: item.symbol,
  companyName: item.companyName,
  sector: item.sector,
  industry: item.industry,
  value: sp500WeightValue(item),
  marketCap: item.marketCap,
  indexWeight: item.indexWeight,
  changePercent: item.changePercent
}));

const bounds = { x: 0, y: 0, width: 1440, height: 620 };
const tiles = layoutSp500TreeMap(input, bounds);
const symbolTiles = tiles.filter((tile) => tile.kind === "symbol");
const sectorTiles = tiles.filter((tile) => tile.kind === "sector");
const tileById = new Map(tiles.map((tile) => [tile.id, tile]));

assert(symbolTiles.length === sp500UniverseSeed.length, "every seed symbol must keep a layout tile");
assert(sectorTiles.length >= 10, "sector grouping should be visible");

for (const tile of tiles) {
  assert(tile.width >= 0 && tile.height >= 0, `${tile.id} has negative dimensions`);
  assert(isInside(tile, bounds, 0.01), `${tile.id} must stay inside root bounds`);
  if (tile.parentId) {
    const parent = tileById.get(tile.parentId);
    assert(parent, `${tile.id} parent must exist`);
    assert(isInside(tile, parent, 0.01), `${tile.id} must stay inside parent bounds`);
  }
}

const rootArea = bounds.width * bounds.height;
const sectorArea = sectorTiles.reduce((sum, tile) => sum + tile.width * tile.height, 0);
assert(Math.abs(sectorArea - rootArea) / rootArea < 0.01, "sector area should cover the root bounds");

const tsla = symbolTiles.find((tile) => tile.symbol === "TSLA");
assert(tsla && tsla.width > 0 && tsla.height > 0, "TSLA tile should be measurable");
const hit = hitTestTreeMapTile(tiles, tsla.x + tsla.width / 2, tsla.y + tsla.height / 2);
assert(hit?.symbol === "TSLA", "hit test should resolve the deepest symbol tile");

console.log(`TreeMap seed/layout verified: ${sp500UniverseSeed.length} symbols, ${sectorTiles.length} sectors`);

function loadTsModule(relativePath, mockRequires = {}) {
  const absolutePath = path.join(repoRoot, relativePath);
  const source = fs.readFileSync(absolutePath, "utf8");
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2020,
      esModuleInterop: true
    }
  }).outputText;
  const module = { exports: {} };
  const dirname = path.dirname(absolutePath);
  const localRequire = (specifier) => {
    if (specifier in mockRequires) {
      return mockRequires[specifier];
    }
    const candidate = path.join(dirname, `${specifier}.ts`);
    if (fs.existsSync(candidate)) {
      return loadTsModule(path.relative(repoRoot, candidate), mockRequires);
    }
    return nodeRequire(specifier);
  };
  vm.runInNewContext(output, {
    exports: module.exports,
    module,
    require: localRequire,
    console
  }, { filename: absolutePath });
  return module.exports;
}

function isInside(rect, bounds, epsilon) {
  return rect.x >= bounds.x - epsilon &&
    rect.y >= bounds.y - epsilon &&
    rect.x + rect.width <= bounds.x + bounds.width + epsilon &&
    rect.y + rect.height <= bounds.y + bounds.height + epsilon;
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}
