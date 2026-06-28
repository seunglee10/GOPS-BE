"""Fake KIS client and exception taxonomy used by adapter tests."""

from __future__ import annotations

from typing import Any


class KisTimeout(TimeoutError):
    pass


class KisConnectionReset(ConnectionError):
    pass


class KisTokenExpired(RuntimeError):
    pass


class KisExplicitReject(RuntimeError):
    pass


class KisHttpError(RuntimeError):
    def __init__(self, status_code: int, *, safe_to_retry: bool = False, message: str | None = None) -> None:
        super().__init__(message or f"KIS HTTP error: {status_code}")
        self.status_code = status_code
        self.safe_to_retry = safe_to_retry


class FakeKisClient:
    def __init__(self, outcomes: list[str] | None = None) -> None:
        self.outcomes = list(outcomes or ["success"])
        self.submit_calls = 0
        self.refresh_calls = 0
        self.history_rows: list[dict[str, Any]] = []

    def refresh_token(self) -> None:
        self.refresh_calls += 1

    def submit_order(self, command: Any) -> dict[str, Any]:
        self.submit_calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "success"
        if outcome == "success":
            return {"rt_cd": "0", "accepted": True, "broker_order_id": f"kis-{command.client_order_id}"}
        if outcome == "reject":
            raise KisExplicitReject("KIS explicit reject")
        if outcome == "timeout":
            raise KisTimeout("KIS timeout")
        if outcome == "connection_reset":
            raise KisConnectionReset("KIS connection reset")
        if outcome == "token_expired":
            raise KisTokenExpired("KIS token expired")
        if outcome == "safe_429":
            raise KisHttpError(429, safe_to_retry=True)
        if outcome == "unsafe_5xx":
            raise KisHttpError(500, safe_to_retry=False)
        raise RuntimeError(f"unknown fake KIS outcome: {outcome}")

    def fetch_order_history(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return list(self.history_rows)
