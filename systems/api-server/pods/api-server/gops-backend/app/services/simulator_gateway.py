from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class SimulatorUnavailable(RuntimeError):
    pass


class SimulatorGateway:
    def __init__(self, base_url: str | None = None, timeout_seconds: float = 2.0) -> None:
        self.base_url = (base_url or os.getenv("GOPS_SIMULATOR_URL", "http://127.0.0.1:8765")).rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.last_status: dict[str, Any] | None = None

    def status(self) -> dict[str, Any]:
        status = self._request("GET", "/api/control/status")
        self.last_status = status
        return status

    def set_mode(self, mode: str) -> dict[str, Any]:
        return self._request("PUT", "/api/control/mode", {"mode": mode})

    def action(self, action: str) -> dict[str, Any]:
        return self._request("POST", "/api/control/action", {"action": action})

    def set_speed(self, speed: int) -> dict[str, Any]:
        return self._request("PUT", "/api/control/speed", {"speed": speed})

    def quote(self, symbol: str) -> dict[str, Any]:
        return self._request("GET", f"/api/control/quote?symbol={urllib.parse.quote(symbol)}")

    def execution_events(self, run_id: str, after_sequence: int, limit: int = 50_000) -> dict[str, Any]:
        query = urllib.parse.urlencode({
            "runId": run_id,
            "afterSequence": max(0, int(after_sequence)),
            "limit": max(1, min(int(limit), 50_000)),
        })
        return self._request("GET", f"/api/control/execution-events?{query}")

    def candles(self, symbol: str, interval: str, limit: int) -> dict[str, Any]:
        query = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": limit})
        return self._request("GET", f"/api/control/candles?{query}")

    def order_flow(
        self,
        symbol: str,
        *,
        after_sequence: int | None = None,
        latest_only: bool = False,
    ) -> dict[str, Any]:
        parameters: dict[str, Any] = {"symbol": symbol}
        if after_sequence is not None:
            parameters["afterSequence"] = max(0, int(after_sequence))
        if latest_only:
            parameters["latestOnly"] = "true"
        query = urllib.parse.urlencode(parameters)
        return self._request("GET", f"/api/control/order-flow?{query}", timeout_seconds=10.0)

    def symbols(self, query: str = "", limit: int = 100) -> dict[str, Any]:
        encoded = urllib.parse.urlencode({"q": query, "limit": limit})
        return self._request("GET", f"/api/control/symbols?{encoded}")

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds or self.timeout_seconds) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.reason
            try:
                error_payload = json.loads(exc.read().decode("utf-8"))
                detail = error_payload.get("detail") or error_payload.get("message") or detail
            except Exception:
                pass
            raise SimulatorUnavailable(str(detail)) from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise SimulatorUnavailable(f"GOPS simulator unavailable at {self.base_url}") from exc
        if not isinstance(parsed, dict):
            raise SimulatorUnavailable("GOPS simulator returned an invalid response")
        return parsed
