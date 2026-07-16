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

    def set_speed(self, speed: int) -> dict[str, Any]:
        return self._request("PUT", "/api/control/speed", {"speed": speed})

    def account(self, user_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/control/account?userId={urllib.parse.quote(user_id)}")

    def individual_order(
        self,
        *,
        user_id: str,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str,
        limit_price: float | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request("POST", "/api/control/orders", {
            "userId": user_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "orderType": order_type,
            "limitPrice": limit_price,
            "idempotencyKey": idempotency_key,
        })

    def order(self, user_id: str, order_id: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"userId": user_id})
        return self._request("GET", f"/api/control/orders/{urllib.parse.quote(order_id)}?{query}")

    def order_events(self, user_id: str, order_id: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"userId": user_id})
        return self._request("GET", f"/api/control/orders/{urllib.parse.quote(order_id)}/events?{query}")

    def quote(self, symbol: str) -> dict[str, Any]:
        return self._request("GET", f"/api/control/quote?symbol={urllib.parse.quote(symbol)}")

    def candles(self, symbol: str, interval: str, limit: int) -> dict[str, Any]:
        query = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": limit})
        return self._request("GET", f"/api/control/candles?{query}")

    def symbols(self, query: str = "", limit: int = 100) -> dict[str, Any]:
        encoded = urllib.parse.urlencode({"q": query, "limit": limit})
        return self._request("GET", f"/api/control/symbols?{encoded}")

    def conditions(self, user_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/control/conditions?userId={urllib.parse.quote(user_id)}")

    def create_condition(self, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/control/conditions", {"userId": user_id, **payload})

    def update_condition(
        self,
        user_id: str,
        condition_id: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/api/control/conditions/{condition_id}",
            {"userId": user_id, **payload},
        )

    def delete_condition(self, user_id: str, condition_id: int) -> dict[str, Any]:
        query = urllib.parse.urlencode({"userId": user_id})
        return self._request("DELETE", f"/api/control/conditions/{condition_id}?{query}")

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
