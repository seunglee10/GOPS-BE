from __future__ import annotations

import re

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from .service import CompanyJournalService


router = APIRouter(prefix="/api/company-journal", tags=["company-journal"])
_service: CompanyJournalService | None = None


def get_company_journal_service() -> CompanyJournalService:
    global _service
    if _service is None:
        _service = CompanyJournalService()
    return _service


@router.get("/{symbol}/evidence")
def get_company_journal_evidence(
    symbol: str,
    benchmarks: str = Query(default="SPY"),
    service: CompanyJournalService = Depends(get_company_journal_service),
) -> dict:
    normalized = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9.-]{1,15}", normalized):
        raise HTTPException(status_code=400, detail="Invalid symbol")
    parsed_benchmarks = [value.strip().upper() for value in benchmarks.split(",") if value.strip()]
    if len(parsed_benchmarks) > 2 or any(not re.fullmatch(r"[A-Z0-9.-]{1,15}", value) for value in parsed_benchmarks):
        raise HTTPException(status_code=400, detail="Invalid benchmarks")
    try:
        return service.panel_evidence(normalized, parsed_benchmarks)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Company journal evidence is unavailable") from exc


@router.get("/{symbol}")
def get_company_journal(
    symbol: str,
    background_tasks: BackgroundTasks,
    service: CompanyJournalService = Depends(get_company_journal_service),
) -> dict:
    normalized = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9.-]{1,15}", normalized):
        raise HTTPException(status_code=400, detail="Invalid symbol")
    try:
        report = service.latest(normalized)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Company journal storage is unavailable") from exc
    background_tasks.add_task(service.enqueue_if_stale, normalized, "panel")
    if report is None:
        return {
            "status": "pending",
            "symbol": normalized,
            "report": None,
            "message": "기업 분석을 준비하고 있습니다.",
        }
    return {"status": "ready", "symbol": normalized, "report": report}
