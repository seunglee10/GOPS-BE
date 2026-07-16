from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any

from .concepts import CONCEPT_MAP


FLOW_PERIODS = {"Q1", "Q2", "Q3", "Q4", "FY"}


def select_latest_fact(
    companyfacts_payload: dict[str, Any],
    metric: str,
    *,
    unit_preference: tuple[str, ...] = ("USD", "shares", "USD/shares", "pure"),
    concept_map: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, Any] | None:
    """Select the newest fact for a metric using concept priority, then filing recency."""
    concepts = concept_map or CONCEPT_MAP
    us_gaap = ((companyfacts_payload.get("facts") or {}).get("us-gaap") or {}) if isinstance(companyfacts_payload, dict) else {}
    for concept in concepts.get(metric, ()):
        concept_payload = us_gaap.get(concept) or {}
        units = concept_payload.get("units") or {}
        for unit in ordered_units(units, unit_preference):
            facts = [item for item in units.get(unit, []) if usable_fact(item)]
            if not facts:
                continue
            selected = sorted(facts, key=fact_sort_key, reverse=True)[0]
            raw = {
                "selected_concept": concept,
                "taxonomy": "us-gaap",
                "unit": unit,
                "accession": selected.get("accn"),
                "filed_at": selected.get("filed"),
                "quality": "available",
            }
            if metric == "equity" and concept == "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest":
                raw["quality"] = "equity_includes_nci"
            elif metric == "cash_and_cash_equivalents" and concept == "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents":
                raw["quality"] = "cash_includes_restricted"
            return {
                "metric": metric,
                "value": numeric(selected.get("val")),
                "fy": selected.get("fy"),
                "fp": selected.get("fp"),
                "form": selected.get("form"),
                "filed_at": selected.get("filed"),
                "period_end": selected.get("end"),
                "accession": selected.get("accn"),
                "raw": raw,
            }
    return None


def calculate_derived_metrics(facts: dict[str, dict[str, Any] | None], prior_facts: dict[str, dict[str, Any] | None] | None = None) -> dict[str, dict[str, Any]]:
    derived: dict[str, dict[str, Any]] = {}
    prior_facts = prior_facts or {}
    add_ratio(derived, "net_margin", facts.get("net_income"), facts.get("revenue"))
    add_ratio(derived, "operating_margin", facts.get("operating_income"), facts.get("revenue"))
    add_ratio(derived, "roe", facts.get("net_income"), facts.get("equity"), extra_quality=quality_for_equity(facts.get("equity")))
    add_ratio(derived, "liabilities_to_assets", facts.get("liabilities"), facts.get("assets"))
    add_ratio(derived, "liabilities_to_equity", facts.get("liabilities"), facts.get("equity"), extra_quality=quality_for_equity(facts.get("equity")))
    add_ratio(derived, "current_liabilities_to_equity", facts.get("current_liabilities"), facts.get("equity"), extra_quality=quality_for_equity(facts.get("equity")))
    add_noncurrent_liabilities_ratio(derived, facts)
    add_ratio(derived, "current_ratio", facts.get("current_assets"), facts.get("current_liabilities"))
    add_difference(derived, "free_cash_flow", facts.get("operating_cash_flow"), facts.get("capex"))
    add_ratio(derived, "interest_coverage", facts.get("operating_income"), facts.get("interest_expense"), absolute_denominator=True)
    add_ratio(derived, "financial_cost_burden_ratio", facts.get("interest_expense"), facts.get("revenue"), absolute_numerator=True)
    add_growth(derived, "revenue_growth_yoy", facts.get("revenue"), prior_facts.get("revenue"))
    add_growth(derived, "net_income_growth_yoy", facts.get("net_income"), prior_facts.get("net_income"))
    add_growth(derived, "operating_income_growth_yoy", facts.get("operating_income"), prior_facts.get("operating_income"))

    debt = calculate_total_debt(facts)
    if debt is not None:
        derived["total_debt"] = debt
        add_ratio(derived, "total_debt_to_assets", debt, facts.get("assets"))
        add_ratio(derived, "total_debt_to_equity", debt, facts.get("equity"), extra_quality=quality_for_equity(facts.get("equity")))
    add_net_debt(derived, debt, facts.get("cash_and_cash_equivalents"))
    return derived


def add_noncurrent_liabilities_ratio(target: dict[str, dict[str, Any]], facts: dict[str, dict[str, Any] | None]) -> None:
    liabilities = facts.get("liabilities")
    current_liabilities = facts.get("current_liabilities")
    equity = facts.get("equity")
    metric = "noncurrent_liabilities_to_equity"
    if liabilities is None or current_liabilities is None or equity is None or fact_value(equity) == 0:
        target[metric] = missing_metric(metric, liabilities, current_liabilities, equity)
        return
    noncurrent_value = fact_value(liabilities) - fact_value(current_liabilities)
    if noncurrent_value < 0:
        target[metric] = {
            "metric": metric,
            "value": None,
            "raw": {"quality": "invalid_source_relationship", "reason": "current_liabilities_exceed_total_liabilities"},
        }
        return
    target[metric] = {
        "metric": metric,
        "value": noncurrent_value / fact_value(equity),
        "raw": {"quality": quality_for_equity(equity) or "available"},
    }


def add_net_debt(target: dict[str, dict[str, Any]], debt: dict[str, Any] | None, cash: dict[str, Any] | None) -> None:
    metric = "net_debt"
    if debt is None or cash is None:
        target[metric] = missing_metric(metric, debt, cash)
        return
    debt_quality = str((debt.get("raw") or {}).get("quality") or "available")
    cash_quality = str((cash.get("raw") or {}).get("quality") or "available")
    quality = debt_quality if debt_quality != "available" else cash_quality
    target[metric] = {
        "metric": metric,
        "value": fact_value(debt) - fact_value(cash),
        "raw": {
            "quality": quality,
            "cash_concept": selected_concept(cash),
            "debt_composition": (debt.get("raw") or {}).get("debt_composition"),
        },
    }


def calculate_total_debt(facts: dict[str, dict[str, Any] | None]) -> dict[str, Any] | None:
    aggregate_current = facts.get("debt_current_aggregate")
    current_components = [
        component
        for component in (
            facts.get("short_term_borrowings") or facts.get("ShortTermBorrowings"),
            facts.get("long_term_debt_current") or facts.get("LongTermDebtCurrent"),
            facts.get("finance_lease_liability_current") or facts.get("FinanceLeaseLiabilityCurrent"),
        )
        if component is not None
    ]
    noncurrent_components = [
        component
        for component in (
            facts.get("long_term_debt_noncurrent") or facts.get("LongTermDebtNoncurrent"),
            facts.get("finance_lease_liability_noncurrent") or facts.get("FinanceLeaseLiabilityNoncurrent"),
        )
        if component is not None
    ]

    current_value = None
    current_sources = []
    if aggregate_current is not None:
        current_value = fact_value(aggregate_current)
        current_sources = [selected_concept(aggregate_current) or "DebtCurrent"]
    elif current_components:
        current_value = sum((fact_value(item) or Decimal("0")) for item in current_components)
        current_sources = [selected_concept(item) or str(item.get("metric") or "debt_component") for item in current_components]

    noncurrent_value = None
    noncurrent_sources = []
    if noncurrent_components:
        noncurrent_value = sum((fact_value(item) or Decimal("0")) for item in noncurrent_components)
        noncurrent_sources = [selected_concept(item) or str(item.get("metric") or "debt_component") for item in noncurrent_components]

    if current_value is None and noncurrent_value is None:
        return None

    value = (current_value or Decimal("0")) + (noncurrent_value or Decimal("0"))
    warnings = []
    if current_value is None or noncurrent_value is None:
        warnings.append("partial_debt_sources")
    composition = {
        "current_debt_strategy": "aggregate" if aggregate_current is not None else "components",
        "current_sources": current_sources,
        "noncurrent_sources": noncurrent_sources,
        "warnings": warnings,
    }
    return {
        "metric": "total_debt",
        "value": value,
        "raw": {
            "quality": "partial_source" if warnings else "available",
            "debt_composition": composition,
        },
    }


def q4_synthetic_fact(fy_fact: dict[str, Any], q1_fact: dict[str, Any], q2_fact: dict[str, Any], q3_fact: dict[str, Any]) -> dict[str, Any]:
    source_facts = [fy_fact, q1_fact, q2_fact, q3_fact]
    value = fact_value(fy_fact) - fact_value(q1_fact) - fact_value(q2_fact) - fact_value(q3_fact)
    accessions = [str(item.get("accession") or item.get("raw", {}).get("accession") or "") for item in source_facts]
    source_hash = hashlib.sha1("|".join(accessions).encode("utf-8")).hexdigest()[:16]
    filed_values = [str(item.get("filed_at") or item.get("raw", {}).get("filed_at") or "") for item in source_facts]
    return {
        "metric": fy_fact.get("metric"),
        "value": value,
        "fy": fy_fact.get("fy"),
        "fp": "Q4",
        "form": fy_fact.get("form"),
        "filed_at": max(filed_values),
        "period_end": fy_fact.get("period_end"),
        "accession": None,
        "version_filed_at": max(filed_values),
        "raw": {
            "quality": "synthetic_q4",
            "source_accessions": accessions,
            "source_accession_hash": source_hash,
        },
    }


def add_ratio(
    target: dict[str, dict[str, Any]],
    metric: str,
    numerator: dict[str, Any] | None,
    denominator: dict[str, Any] | None,
    *,
    extra_quality: str | None = None,
    absolute_numerator: bool = False,
    absolute_denominator: bool = False,
) -> None:
    if numerator is None or denominator is None:
        target[metric] = missing_metric(metric, numerator, denominator)
        return
    numerator_value = fact_value(numerator)
    denominator_value = fact_value(denominator)
    if absolute_numerator:
        numerator_value = abs(numerator_value)
    if absolute_denominator:
        denominator_value = abs(denominator_value)
    if denominator_value == 0:
        target[metric] = missing_metric(metric, numerator, denominator)
        target[metric]["raw"]["quality"] = "zero_denominator"
        return
    raw = {"quality": extra_quality or "available"}
    if absolute_numerator or absolute_denominator:
        raw["sign_policy"] = "absolute_expense"
    target[metric] = {"metric": metric, "value": numerator_value / denominator_value, "raw": raw}


def add_difference(target: dict[str, dict[str, Any]], metric: str, minuend: dict[str, Any] | None, subtrahend: dict[str, Any] | None) -> None:
    if minuend is None or subtrahend is None:
        target[metric] = missing_metric(metric, minuend, subtrahend)
        return
    target[metric] = {"metric": metric, "value": fact_value(minuend) - fact_value(subtrahend), "raw": {"quality": "available"}}


def add_growth(target: dict[str, dict[str, Any]], metric: str, current: dict[str, Any] | None, prior: dict[str, Any] | None) -> None:
    if current is None or prior is None or fact_value(prior) == 0:
        target[metric] = missing_metric(metric, current, prior)
        return
    target[metric] = {"metric": metric, "value": (fact_value(current) / fact_value(prior)) - Decimal("1"), "raw": {"quality": "available"}}


def missing_metric(metric: str, *sources: dict[str, Any] | None) -> dict[str, Any]:
    missing = [index for index, source in enumerate(sources) if source is None]
    return {"metric": metric, "value": None, "raw": {"quality": "missing_source", "missing_source_indexes": missing}}


def quality_for_equity(fact: dict[str, Any] | None) -> str | None:
    if not fact:
        return None
    raw = fact.get("raw") if isinstance(fact, dict) else {}
    return "equity_includes_nci" if isinstance(raw, dict) and raw.get("quality") == "equity_includes_nci" else None


def ordered_units(units: dict[str, Any], preference: tuple[str, ...]) -> list[str]:
    keys = list(units.keys())
    preferred = [unit for unit in preference if unit in units]
    return preferred + [unit for unit in keys if unit not in preferred]


def usable_fact(item: dict[str, Any]) -> bool:
    return isinstance(item, dict) and item.get("val") is not None and str(item.get("form") or "") in {"10-K", "10-Q", "10-K/A", "10-Q/A", "20-F", "40-F"}


def fact_sort_key(item: dict[str, Any]) -> tuple[str, str, str]:
    return (str(item.get("filed") or ""), str(item.get("end") or ""), str(item.get("accn") or ""))


def fact_value(item: dict[str, Any] | None) -> Decimal:
    if item is None:
        return Decimal("0")
    return numeric(item.get("value"))


def numeric(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def selected_concept(item: dict[str, Any]) -> str | None:
    raw = item.get("raw") if isinstance(item, dict) else None
    if isinstance(raw, dict):
        return raw.get("selected_concept")
    return None


def json_default(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    return json.dumps(value, ensure_ascii=True, default=str)
