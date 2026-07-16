from __future__ import annotations

from typing import Any


DEFAULT_TAXONOMY = "us-gaap"


CONCEPT_MAP: dict[str, tuple[str, ...]] = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ),
    "net_income": ("NetIncomeLoss",),
    "operating_income": ("OperatingIncomeLoss",),
    "assets": ("Assets",),
    "liabilities": ("Liabilities",),
    "equity": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ),
    "current_assets": ("AssetsCurrent",),
    "current_liabilities": ("LiabilitiesCurrent",),
    "cash_and_cash_equivalents": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ),
    "operating_cash_flow": (
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ),
    "capex": (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ),
    "interest_expense": (
        "InterestExpense",
        "InterestExpenseDebt",
        "InterestExpenseNonOperating",
    ),
    "eps": (
        "EarningsPerShareDiluted",
        "EarningsPerShareBasic",
    ),
    "shares_outstanding": (
        "dei:EntityCommonStockSharesOutstanding",
        "EntityCommonStockSharesOutstanding",
    ),
    "debt_current_aggregate": ("DebtCurrent",),
    "debt_current_components": (
        "ShortTermBorrowings",
        "LongTermDebtCurrent",
        "FinanceLeaseLiabilityCurrent",
    ),
    "short_term_borrowings": ("ShortTermBorrowings",),
    "long_term_debt_current": ("LongTermDebtCurrent",),
    "finance_lease_liability_current": ("FinanceLeaseLiabilityCurrent",),
    "debt_noncurrent_components": (
        "LongTermDebtNoncurrent",
        "FinanceLeaseLiabilityNoncurrent",
    ),
    "long_term_debt_noncurrent": ("LongTermDebtNoncurrent",),
    "finance_lease_liability_noncurrent": ("FinanceLeaseLiabilityNoncurrent",),
}


REQUIRED_CONCEPTS = frozenset(
    concept
    for concepts in CONCEPT_MAP.values()
    for concept in concepts
)


def parse_concept_ref(concept_ref: str) -> tuple[str, str]:
    text = str(concept_ref or "").strip()
    if ":" not in text:
        return DEFAULT_TAXONOMY, text
    taxonomy, concept = text.split(":", 1)
    return taxonomy or DEFAULT_TAXONOMY, concept


def validate_concept_map(companyfacts_payload: dict[str, Any], concept_map: dict[str, tuple[str, ...]] | None = None) -> dict[str, list[str]]:
    """Return concepts from the map that are absent from a companyfacts payload."""
    concepts = concept_map or CONCEPT_MAP
    facts = (companyfacts_payload.get("facts") or {}) if isinstance(companyfacts_payload, dict) else {}
    missing: dict[str, list[str]] = {}
    for metric, candidates in concepts.items():
        absent = []
        for concept_ref in candidates:
            taxonomy, concept = parse_concept_ref(concept_ref)
            taxonomy_payload = facts.get(taxonomy) or {}
            if concept not in taxonomy_payload:
                absent.append(concept_ref)
        if absent:
            missing[metric] = absent
    return missing
