import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import vm from "node:vm";
import ts from "typescript";

const repoRoot = process.cwd();
const nodeRequire = createRequire(import.meta.url);
const ontologyModule = loadTsModule("frontend/src/ontology/buildOntologyGraphFromEvidence.ts");
const { buildOntologyGraphFromEvidence } = ontologyModule;

const graph = buildOntologyGraphFromEvidence([
  {
    provider: "ontology",
    status: "available",
    raw: { relationType: "theme-company", ticker: "AMD", themeName: "AI/반도체/데이터센터" }
  },
  {
    provider: "ontology",
    status: "available",
    raw: { relationType: "shared-theme", symbols: ["NVDA", "ADI", "AMAT", "ANET"], themeName: "AI/반도체/데이터센터" }
  },
  {
    provider: "ontology",
    status: "available",
    raw: { relationType: "cross-control", controllerTicker: "NVDA", controlledTicker: "ANET", controlledName: "Arista Networks" }
  },
  {
    provider: "ontology",
    status: "no-data",
    raw: { relationType: "theme", ticker: "SHOULD_NOT_RENDER", themeName: "Ignored" }
  },
  {
    provider: "filing",
    status: "available",
    raw: { relationType: "theme", ticker: "SHOULD_NOT_RENDER", themeName: "Ignored" }
  },
  {
    provider: "ontology",
    status: "available",
    raw: { relationType: "unsupported", ticker: "SHOULD_NOT_RENDER", themeName: "Ignored" }
  }
], "NVDA");

assert(graph, "available ontology evidence should build a graph");
assert(hasNode(graph, "symbol:NVDA"), "primary symbol node should exist");
assert(hasNode(graph, "symbol:AMD"), "theme-company ticker node should exist");
assert(hasNode(graph, "symbol:ADI"), "shared-theme symbol node should exist");
assert(hasNode(graph, "symbol:AMAT"), "shared-theme symbol node should exist");
assert(hasNode(graph, "symbol:ANET"), "cross-control target symbol node should exist");
assert(hasNode(graph, "theme:AI/반도체/데이터센터"), "theme node should exist");
assert(!hasNode(graph, "symbol:SHOULD_NOT_RENDER"), "non-ontology/no-data/unsupported evidence should be ignored");
assert(hasEdge(graph, "theme:symbol:AMD->theme:AI/반도체/데이터센터"), "theme-company should map ticker to theme");
assert(hasEdge(graph, "shared-theme:symbol:ADI->theme:AI/반도체/데이터센터"), "shared-theme should map every symbol to theme");
assert(hasEdge(graph, "cross-control:symbol:NVDA->symbol:ANET"), "cross-control should map controller to controlled ticker");

const empty = buildOntologyGraphFromEvidence([
  { provider: "ontology", status: "no-data", raw: { relationType: "theme", ticker: "NVDA", themeName: "AI" } }
], "NVDA");
assert(empty === null, "no-data evidence should not create a graph");

console.log("Ontology graph contract verified");

function hasNode(graphData, id) {
  return graphData.nodes.some((node) => node.id === id);
}

function hasEdge(graphData, id) {
  return graphData.edges.some((edge) => edge.id === id);
}

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

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}
