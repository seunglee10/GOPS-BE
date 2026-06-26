from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException, Response, status

from kis_trader.config import AppSettings, load_settings
from kis_trader.contracts.order import OrderValidationError
from kis_trader.orders.postgres_repository import PostgresOrderRepository
from kis_trader.orders.service import IdempotencyConflict, OrderAcceptanceService

from .models import OrderCreateRequest, model_to_dict


def create_app(
    *,
    settings: AppSettings | None = None,
    order_service: OrderAcceptanceService | None = None,
) -> FastAPI:
    resolved_settings = settings or load_settings()
    if order_service is None:
        repository = PostgresOrderRepository(resolved_settings.database_url)
        order_service = OrderAcceptanceService(settings=resolved_settings, storage=repository)

    app = FastAPI(
        title="GOPS Order Acceptance API",
        version="0.1.0",
        description="Minimal M1 order acceptance API. It records DB/outbox only and never calls KIS.",
    )

    @app.post("/orders", status_code=status.HTTP_202_ACCEPTED)
    def create_order(
        body: OrderCreateRequest,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, object]:
        if not idempotency_key or not idempotency_key.strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Idempotency-Key header is required.")
        try:
            result = order_service.accept(idempotency_key=idempotency_key, body=model_to_dict(body))
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except (OrderValidationError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

        response.status_code = status.HTTP_202_ACCEPTED
        return _order_response(result.order.to_api_dict())

    @app.get("/orders/{order_id}")
    def get_order(order_id: str) -> dict[str, object]:
        order = order_service.get_order(order_id)
        if order is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")
        return _order_response(order.to_api_dict())

    return app


def _order_response(order: dict[str, object]) -> dict[str, object]:
    order_id = str(order["order_id"])
    payload = dict(order)
    payload["status_url"] = f"/orders/{order_id}"
    payload["websocket_channel"] = f"orders:{order_id}"
    return payload
