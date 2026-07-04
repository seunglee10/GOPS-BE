from __future__ import annotations

import json
import sys

from gops_agents.retrieval.graph_expansion import refresh_graph_expansions, symbols_from_env


def main() -> int:
    symbols = symbols_from_env()
    if not symbols:
        print(json.dumps({
            "status": "skipped",
            "reason": "AGENT_GRAPH_EXPANSION_SYMBOLS or AGENT_GRAPH_EXPANSION_SYMBOL_FILE is required.",
        }, ensure_ascii=False))
        return 0
    results = refresh_graph_expansions(symbols)
    print(json.dumps({"status": "ok", "count": len(results), "results": results}, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
