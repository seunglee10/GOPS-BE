import hashlib
import sys
import unittest
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))

from fundamentals.concepts import REQUIRED_CONCEPTS, parse_concept_ref, validate_concept_map
from fundamentals.metrics import calculate_derived_metrics, calculate_total_debt, q4_synthetic_fact, select_latest_fact


def fact(metric, value, *, concept=None, accession=None, filed_at="2026-02-01", quality="available"):
    selected = concept or metric
    return {
        "metric": metric,
        "value": Decimal(str(value)),
        "accession": accession or selected,
        "filed_at": filed_at,
        "raw": {
            "selected_concept": selected,
            "quality": quality,
            "accession": accession or selected,
            "filed_at": filed_at,
        },
    }


class FundamentalsMetricTests(unittest.TestCase):
    def test_concept_map_tags_exist_in_companyfacts_fixture(self):
        facts = {}
        for concept_ref in REQUIRED_CONCEPTS:
            taxonomy, concept = parse_concept_ref(concept_ref)
            facts.setdefault(taxonomy, {})[concept] = {"units": {"USD": []}}
        payload = {"facts": facts}

        self.assertEqual(validate_concept_map(payload), {})

    def test_eps_selection_keeps_selected_concept_in_raw(self):
        payload = {
            "facts": {
                "us-gaap": {
                    "EarningsPerShareDiluted": {
                        "units": {
                            "USD/shares": [
                                {
                                    "val": 4.2,
                                    "fy": 2025,
                                    "fp": "FY",
                                    "form": "10-K",
                                    "filed": "2026-02-01",
                                    "end": "2025-12-31",
                                    "accn": "diluted-accn",
                                }
                            ]
                        }
                    },
                    "EarningsPerShareBasic": {
                        "units": {
                            "USD/shares": [
                                {
                                    "val": 4.4,
                                    "fy": 2025,
                                    "fp": "FY",
                                    "form": "10-K",
                                    "filed": "2026-02-02",
                                    "end": "2025-12-31",
                                    "accn": "basic-accn",
                                }
                            ]
                        }
                    },
                }
            }
        }

        selected = select_latest_fact(payload, "eps")

        self.assertEqual(selected["value"], Decimal("4.2"))
        self.assertEqual(selected["raw"]["selected_concept"], "EarningsPerShareDiluted")

    def test_total_debt_uses_aggregate_current_debt_without_double_counting(self):
        facts = {
            "debt_current_aggregate": fact("debt_current_aggregate", 100, concept="DebtCurrent"),
            "short_term_borrowings": fact("short_term_borrowings", 40, concept="ShortTermBorrowings"),
            "long_term_debt_current": fact("long_term_debt_current", 60, concept="LongTermDebtCurrent"),
            "finance_lease_liability_current": fact("finance_lease_liability_current", 10, concept="FinanceLeaseLiabilityCurrent"),
            "long_term_debt_noncurrent": fact("long_term_debt_noncurrent", 300, concept="LongTermDebtNoncurrent"),
        }

        total_debt = calculate_total_debt(facts)

        self.assertEqual(total_debt["value"], Decimal("400"))
        self.assertEqual(total_debt["raw"]["debt_composition"]["current_debt_strategy"], "aggregate")
        self.assertEqual(total_debt["raw"]["debt_composition"]["current_sources"], ["DebtCurrent"])

    def test_total_debt_sums_current_components_only_when_aggregate_missing(self):
        facts = {
            "short_term_borrowings": fact("short_term_borrowings", 40, concept="ShortTermBorrowings"),
            "long_term_debt_current": fact("long_term_debt_current", 60, concept="LongTermDebtCurrent"),
            "finance_lease_liability_current": fact("finance_lease_liability_current", 10, concept="FinanceLeaseLiabilityCurrent"),
            "long_term_debt_noncurrent": fact("long_term_debt_noncurrent", 300, concept="LongTermDebtNoncurrent"),
            "finance_lease_liability_noncurrent": fact("finance_lease_liability_noncurrent", 20, concept="FinanceLeaseLiabilityNoncurrent"),
        }

        total_debt = calculate_total_debt(facts)

        self.assertEqual(total_debt["value"], Decimal("430"))
        self.assertEqual(total_debt["raw"]["debt_composition"]["current_debt_strategy"], "components")
        self.assertEqual(
            total_debt["raw"]["debt_composition"]["current_sources"],
            ["ShortTermBorrowings", "LongTermDebtCurrent", "FinanceLeaseLiabilityCurrent"],
        )

    def test_derived_metrics_preserve_quality_and_missing_source(self):
        facts = {
            "revenue": fact("revenue", 200),
            "net_income": fact("net_income", 50),
            "operating_income": fact("operating_income", 80),
            "assets": fact("assets", 400),
            "liabilities": fact("liabilities", 100),
            "equity": fact("equity", 250, concept="StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", quality="equity_includes_nci"),
            "debt_current_aggregate": fact("debt_current_aggregate", 30, concept="DebtCurrent"),
            "long_term_debt_noncurrent": fact("long_term_debt_noncurrent", 70, concept="LongTermDebtNoncurrent"),
        }
        prior = {
            "revenue": fact("revenue", 160),
            "net_income": fact("net_income", 40),
            "operating_income": fact("operating_income", 64),
        }

        derived = calculate_derived_metrics(facts, prior)

        self.assertEqual(derived["net_margin"]["value"], Decimal("0.25"))
        self.assertEqual(derived["operating_margin"]["value"], Decimal("0.4"))
        self.assertEqual(derived["revenue_growth_yoy"]["value"], Decimal("0.25"))
        self.assertEqual(derived["liabilities_to_assets"]["value"], Decimal("0.25"))
        self.assertEqual(derived["liabilities_to_equity"]["raw"]["quality"], "equity_includes_nci")
        self.assertEqual(derived["roe"]["raw"]["quality"], "equity_includes_nci")
        self.assertEqual(derived["total_debt_to_assets"]["value"], Decimal("0.25"))
        self.assertEqual(derived["current_ratio"]["raw"]["quality"], "missing_source")
        self.assertEqual(derived["free_cash_flow"]["raw"]["quality"], "missing_source")
        self.assertEqual(derived["interest_coverage"]["raw"]["quality"], "missing_source")

    def test_stability_metrics_use_canonical_sec_inputs_and_expense_sign_policy(self):
        facts = {
            "revenue": fact("revenue", 200),
            "operating_income": fact("operating_income", 80),
            "liabilities": fact("liabilities", 100),
            "equity": fact("equity", 250),
            "current_assets": fact("current_assets", 60),
            "current_liabilities": fact("current_liabilities", 30),
            "interest_expense": fact("interest_expense", -4),
            "cash_and_cash_equivalents": fact("cash_and_cash_equivalents", 20, concept="CashAndCashEquivalentsAtCarryingValue"),
            "debt_current_aggregate": fact("debt_current_aggregate", 30, concept="DebtCurrent"),
            "long_term_debt_noncurrent": fact("long_term_debt_noncurrent", 70, concept="LongTermDebtNoncurrent"),
        }

        derived = calculate_derived_metrics(facts)

        self.assertEqual(derived["liabilities_to_equity"]["value"], Decimal("0.4"))
        self.assertEqual(derived["current_liabilities_to_equity"]["value"], Decimal("0.12"))
        self.assertEqual(derived["noncurrent_liabilities_to_equity"]["value"], Decimal("0.28"))
        self.assertEqual(derived["current_ratio"]["value"], Decimal("2"))
        self.assertEqual(derived["total_debt"]["value"], Decimal("100"))
        self.assertEqual(derived["interest_coverage"]["value"], Decimal("20"))
        self.assertEqual(derived["financial_cost_burden_ratio"]["value"], Decimal("0.02"))
        self.assertEqual(derived["net_debt"]["value"], Decimal("80"))
        self.assertEqual(derived["interest_coverage"]["raw"]["sign_policy"], "absolute_expense")

    def test_noncurrent_liability_ratio_rejects_inconsistent_source_relationship(self):
        derived = calculate_derived_metrics({
            "liabilities": fact("liabilities", 20),
            "current_liabilities": fact("current_liabilities", 30),
            "equity": fact("equity", 10),
        })

        self.assertIsNone(derived["noncurrent_liabilities_to_equity"]["value"])
        self.assertEqual(derived["noncurrent_liabilities_to_equity"]["raw"]["quality"], "invalid_source_relationship")

    def test_q4_synthetic_fact_has_stable_accession_hash_and_version(self):
        fy = fact("revenue", 100, accession="fy", filed_at="2026-02-15")
        q1 = fact("revenue", 10, accession="q1", filed_at="2025-05-01")
        q2 = fact("revenue", 20, accession="q2", filed_at="2025-08-01")
        q3 = fact("revenue", 30, accession="q3", filed_at="2025-11-01")

        synthetic = q4_synthetic_fact(fy, q1, q2, q3)

        self.assertEqual(synthetic["value"], Decimal("40"))
        self.assertEqual(synthetic["fp"], "Q4")
        self.assertEqual(synthetic["version_filed_at"], "2026-02-15")
        self.assertEqual(synthetic["raw"]["source_accession_hash"], hashlib.sha1(b"fy|q1|q2|q3").hexdigest()[:16])
        self.assertEqual(synthetic["raw"]["quality"], "synthetic_q4")


if __name__ == "__main__":
    unittest.main()
