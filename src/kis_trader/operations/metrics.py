"""Operational metric and alert helpers."""

from __future__ import annotations

from typing import Any


def alert_conditions(metrics: dict[str, Any]) -> list[str]:
    alerts: list[str] = []
    if int(metrics.get("submit_failed_unknown_count", 0)) > 0:
        alerts.append("SUBMIT_FAILED_UNKNOWN present")
    if int(metrics.get("reconciliation_required_count", 0)) > 0:
        alerts.append("RECONCILIATION_REQUIRED present")
    if int(metrics.get("dlq_count", 0)) > 0:
        alerts.append("DLQ present")
    if int(metrics.get("outbox_unpublished", 0)) > 0:
        alerts.append("outbox unpublished events present")
    if int(metrics.get("circuit_breaker_open_count", 0)) > 0:
        alerts.append("circuit breaker open")
    return alerts
