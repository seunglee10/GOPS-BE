from pydantic import BaseModel, Field


class CompanyCompareRequest(BaseModel):
    baseSymbol: str = Field(min_length=1, max_length=10)
    compareSymbols: list[str] = Field(default_factory=list, min_length=1, max_length=3)
    question: str | None = Field(default=None, max_length=1000)
