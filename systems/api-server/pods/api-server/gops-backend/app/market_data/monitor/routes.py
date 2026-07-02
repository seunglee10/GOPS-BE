from __future__ import annotations

from typing import Any

from fastapi import APIRouter
try:
    from pydantic import BaseModel, Field
except Exception:
    class BaseModel:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    def Field(default=None, **kwargs):
        return default

from app.market_data.monitor.service import get_monitor_service


router = APIRouter()
if not hasattr(router, "delete"):
    router.delete = router.post


class SubscriptionRequestBody(BaseModel):
    symbol: str = Field(min_length=1, max_length=12)
    layers: list[str] = Field(default_factory=list)
    reason: str | None = None
    ttlSeconds: int | None = Field(default=None, ge=60, le=86400)


@router.get("/api/monitor/market-data/overview")
def market_data_monitor_overview() -> dict[str, Any]:
    return get_monitor_service().overview()


@router.get("/api/monitor/market-data/redis")
def market_data_monitor_redis() -> dict[str, Any]:
    return get_monitor_service().redis_state()


@router.get("/api/monitor/market-data/s3")
def market_data_monitor_s3() -> dict[str, Any]:
    return get_monitor_service().s3_state()


@router.get("/api/monitor/market-data/clickhouse")
def market_data_monitor_clickhouse() -> dict[str, Any]:
    return get_monitor_service().clickhouse_state()


@router.get("/api/monitor/market-data/backfill")
def market_data_monitor_backfill() -> dict[str, Any]:
    return get_monitor_service().backfill()


@router.get("/api/monitor/market-data/subscriptions")
def market_data_monitor_subscriptions() -> dict[str, Any]:
    return get_monitor_service().subscriptions()


@router.post("/api/monitor/market-data/subscriptions")
def add_market_data_subscription(body: SubscriptionRequestBody) -> dict[str, Any]:
    return get_monitor_service().add_subscription(body.symbol, body.layers, reason=body.reason, ttl_seconds=body.ttlSeconds)


@router.delete("/api/monitor/market-data/subscriptions/{symbol}")
def remove_market_data_subscription(symbol: str) -> dict[str, Any]:
    return get_monitor_service().remove_subscription(symbol)
