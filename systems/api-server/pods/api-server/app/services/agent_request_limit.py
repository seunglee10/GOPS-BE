from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi.responses import JSONResponse

from app.contracts.agents import MAX_AGENT_REQUEST_BYTES


AGENT_BODY_PATHS = frozenset({
    "/api/agents/analyze",
    "/api/agents/layout/resolve",
    "/api/alerts/commands",
    "/api/llm/company-compare",
    "/api/llm/company-compare/quantitative",
    "/api/llm/related-index-commentary",
})


class AgentRequestTooLarge(RuntimeError):
    pass


class AgentRequestBodyLimitMiddleware:
    """Reject oversized agent request bodies before JSON parsing allocates them."""

    def __init__(self, app: Any, *, max_bytes: int = MAX_AGENT_REQUEST_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict[str, Any], receive: Callable[[], Awaitable[dict[str, Any]]], send: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        if scope.get("type") != "http" or scope.get("path") not in AGENT_BODY_PATHS:
            await self.app(scope, receive, send)
            return

        content_length = content_length_from_scope(scope)
        if content_length is not None and content_length > self.max_bytes:
            await self._reject(scope, receive, send)
            return

        received_bytes = 0

        async def limited_receive() -> dict[str, Any]:
            nonlocal received_bytes
            message = await receive()
            if message.get("type") == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_bytes:
                    raise AgentRequestTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except AgentRequestTooLarge:
            await self._reject(scope, receive, send)

    async def _reject(self, scope: dict[str, Any], receive: Callable[[], Awaitable[dict[str, Any]]], send: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": f"Agent request body must be {self.max_bytes} bytes or fewer."},
        )
        await response(scope, receive, send)


def content_length_from_scope(scope: dict[str, Any]) -> int | None:
    for name, value in scope.get("headers", []):
        if name.lower() != b"content-length":
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None
