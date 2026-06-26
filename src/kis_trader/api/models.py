from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class OrderCreateRequest(BaseModel):
    market: str = Field(..., examples=["overseas"])
    symbol: str = Field(..., examples=["AAPL"])
    side: str = Field(..., examples=["buy"])
    qty: str = Field(..., examples=["1"])
    price: str = Field(..., examples=["145.00"])
    exchange: str = Field(..., examples=["NASD"])
    order_division: str = Field("00", examples=["00"])
    sell_type: str | None = None
    condition_price: str | None = None


def model_to_dict(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True)
    return model.dict(exclude_none=True)
