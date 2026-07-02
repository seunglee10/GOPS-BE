from __future__ import annotations

import json
import os
import sys

from gops_agents.graph_expansion import GraphExpansionCache


def main() -> int:
    symbol = os.getenv("AGENT_GRAPH_EXPANSION_SMOKE_SYMBOL", "NVDA")
    expansion = GraphExpansionCache().load(symbol)
    ok = expansion.source != "none" or "graph_expansion_unavailable" in expansion.warnings
    print(json.dumps({
        "status": "ok" if ok else "failed",
        "symbol": symbol,
        "source": expansion.source,
        "cacheHit": expansion.cache_hit,
        "relatedSymbols": len(expansion.related_symbols),
        "themes": len(expansion.themes),
        "warnings": expansion.warnings,
    }, ensure_ascii=False, separators=(",", ":")))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
