from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any


logger = logging.getLogger(__name__)
SLOW_SIMULATOR_REQUEST_SECONDS = 1.0


class SimulatorUnavailable(RuntimeError):
    pass


class SimulatorTimeout(SimulatorUnavailable):
    pass


class SimulatorDataUnavailable(SimulatorUnavailable):
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
        status = self._request("PUT", "/api/control/mode", {"mode": mode})
        self.last_status = status
        return status

    def action(self, action: str) -> dict[str, Any]:
        status = self._request("POST", "/api/control/action", {"action": action})
        self.last_status = status
        return status

    def set_speed(self, speed: int) -> dict[str, Any]:
        status = self._request("PUT", "/api/control/speed", {"speed": speed})
        self.last_status = status
        return status

    def quote(self, symbol: str) -> dict[str, Any]:
        return self._request("GET", f"/api/control/quote?symbol={urllib.parse.quote(symbol)}")

    def quotes(self, symbols: list[str] | tuple[str, ...]) -> dict[str, Any]:
        normalized = list(dict.fromkeys(
            str(symbol).strip().upper()
            for symbol in symbols
            if str(symbol).strip()
        ))
        if not normalized:
            return {"runId": None, "quotes": {}, "missingSymbols": []}
        query = urllib.parse.urlencode({"symbols": ",".join(normalized)})
        return self._request("GET", f"/api/control/quotes?{query}")

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

    def indices(self) -> dict[str, Any]:
        return self._request("GET", "/api/control/indices")

    def index_performance(self, range_value: str, start_at: datetime | None) -> dict[str, Any]:
        parameters = {"range": range_value}
        if start_at is not None:
            parameters["startAt"] = start_at.isoformat()
        query = urllib.parse.urlencode(parameters)
        return self._request("GET", f"/api/control/indices/performance?{query}")

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        started_at = time.monotonic()
        outcome = "success"
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
            if not isinstance(parsed, dict):
                outcome = "invalid_response"
                raise SimulatorUnavailable("GOPS simulator returned an invalid response")
        except urllib.error.HTTPError as exc:
            outcome = f"http_{exc.code}"
            detail = exc.reason
            try:
                error_payload = json.loads(exc.read().decode("utf-8"))
                detail = error_payload.get("detail") or error_payload.get("message") or detail
            except Exception:
                pass
            error_type = SimulatorDataUnavailable if exc.code == 404 else SimulatorUnavailable
            raise error_type(str(detail)) from exc
        except TimeoutError as exc:
            outcome = "timeout"
            raise SimulatorTimeout(f"GOPS simulator timed out at {self.base_url}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                outcome = "timeout"
                raise SimulatorTimeout(f"GOPS simulator timed out at {self.base_url}") from exc
            outcome = "unavailable"
            raise SimulatorUnavailable(f"GOPS simulator unavailable at {self.base_url}") from exc
        except (OSError, ValueError) as exc:
            outcome = "invalid_response"
            raise SimulatorUnavailable(f"GOPS simulator unavailable at {self.base_url}") from exc
        finally:
            duration_seconds = time.monotonic() - started_at
            if duration_seconds >= SLOW_SIMULATOR_REQUEST_SECONDS or outcome == "timeout":
                parsed_path = urllib.parse.urlsplit(path)
                query = urllib.parse.parse_qs(parsed_path.query)
                logger.warning(
                    "simulator_gateway_request path=%s durationMs=%d outcome=%s symbol=%s interval=%s",
                    parsed_path.path,
                    round(duration_seconds * 1000),
                    outcome,
                    (query.get("symbol") or [None])[0],
                    (query.get("interval") or [None])[0],
                )
        return parsed
