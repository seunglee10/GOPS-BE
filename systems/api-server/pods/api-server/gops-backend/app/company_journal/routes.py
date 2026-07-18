from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request

from app.routes.simulator import simulator_gateway_from_app
from app.services.simulator_gateway import SimulatorUnavailable

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
    request: Request,
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
    cutoff = company_journal_simulation_cutoff(request)
    try:
        if cutoff is None:
            return service.panel_evidence(normalized, parsed_benchmarks)
        return service.panel_evidence(normalized, parsed_benchmarks, cutoff=cutoff)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Company journal evidence is unavailable") from exc


@router.get("/{symbol}")
def get_company_journal(
    request: Request,
    symbol: str,
    background_tasks: BackgroundTasks,
    service: CompanyJournalService = Depends(get_company_journal_service),
) -> dict:
    normalized = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9.-]{1,15}", normalized):
        raise HTTPException(status_code=400, detail="Invalid symbol")
    cutoff = company_journal_simulation_cutoff(request)
    try:
        report = service.latest(normalized) if cutoff is None else service.latest(normalized, cutoff=cutoff)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Company journal storage is unavailable") from exc
    if cutoff is None:
        background_tasks.add_task(service.enqueue_if_stale, normalized, "panel")
    if report is None:
        return {
            "status": "pending",
            "symbol": normalized,
            "report": None,
            "message": "기업 분석을 준비하고 있습니다.",
        }
    return {"status": "ready", "symbol": normalized, "report": report}


def company_journal_simulation_cutoff(request: Request) -> datetime | None:
    try:
        status = simulator_gateway_from_app(request.app).status()
    except SimulatorUnavailable:
        return None
    if status.get("mode") != "simulation":
        return None
    value = str(status.get("virtualTime") or "").strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="simulation_virtual_time_unavailable") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
