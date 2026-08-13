from typing import Literal

from pydantic import BaseModel, Field


class RelatedIndexEvidence(BaseModel):
    label: str = Field(min_length=1, max_length=32)
    value: str = Field(min_length=1, max_length=64)


class RelatedIndexCommentaryRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=12)
    companyName: str = Field(min_length=1, max_length=120)
    indexSymbol: str = Field(min_length=1, max_length=16)
    indexName: str = Field(min_length=1, max_length=120)
    relType: Literal["constituent", "sector", "macro"]
    relLabel: str = Field(min_length=1, max_length=120)
    correlation60d: float | None = Field(default=None, ge=-1, le=1)
    weightPct: float | None = Field(default=None, ge=0, le=100)
    companyChangePercent: float | None = Field(default=None, ge=-100, le=1000)
    indexChangePercent: float | None = Field(default=None, ge=-100, le=1000)
    evidence: list[RelatedIndexEvidence] = Field(default_factory=list, max_length=4)
    templateBody: str = Field(min_length=1, max_length=240)
