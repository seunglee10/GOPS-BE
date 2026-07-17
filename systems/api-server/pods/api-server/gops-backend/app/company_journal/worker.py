from __future__ import annotations

import argparse
import json
import os

from .service import CompanyJournalService


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate verified AI company journals outside API requests")
    parser.add_argument("--enqueue-daily", action="store_true")
    parser.add_argument("--process-pending", action="store_true")
    parser.add_argument("--candidate-limit", type=int, default=int(os.getenv("COMPANY_JOURNAL_DAILY_LIMIT", "100")))
    parser.add_argument("--request-limit", type=int, default=int(os.getenv("COMPANY_JOURNAL_WORKER_LIMIT", "25")))
    args = parser.parse_args()
    if not args.enqueue_daily and not args.process_pending:
        parser.error("select --enqueue-daily and/or --process-pending")
    service = CompanyJournalService()
    result: dict[str, object] = {}
    if args.enqueue_daily:
        result["enqueued"] = service.enqueue_daily(max(1, args.candidate_limit))
    if args.process_pending:
        result["processed"] = service.process_pending(max(1, args.request_limit))
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
