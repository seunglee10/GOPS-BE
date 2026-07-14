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

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/api/control/status")

    def set_mode(self, mode: str) -> dict[str, Any]:
        return self._request("PUT", "/api/control/mode", {"mode": mode})

    def action(self, action: str) -> dict[str, Any]:
        return self._request("POST", "/api/control/action", {"action": action})

    def set_phase(self, phase: str) -> dict[str, Any]:
        return self._request("PUT", "/api/control/phase", {"phase": phase})

    def account(self, user_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/control/account?userId={urllib.parse.quote(user_id)}")

    def basket_order(self, *, user_id: str, basket: str, side: str) -> dict[str, Any]:
        return self._request("POST", "/api/control/orders/basket", {
            "userId": user_id,
            "basket": basket,
            "side": side,
        })

    def individual_order(self, *, user_id: str, symbol: str, side: str, quantity: int) -> dict[str, Any]:
        return self._request("POST", "/api/control/orders", {
            "userId": user_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
        })

    def news(self) -> dict[str, Any]:
        return self._request("GET", "/v1beta1/news")

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
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
