from __future__ import annotations

from decimal import Decimal
import unittest

from kis_trader.order_contract import OrderStatus
from kis_trader.reconciler import flatten_kis_order_rows, match_order_row, status_from_order_row


class ReconcilerTests(unittest.TestCase):
    def test_flattens_output_rows(self) -> None:
        payload = {"rt_cd": "0", "output1": [{"PDNO": "AAPL", "ODNO": "1"}], "output2": {"total": "1"}}

        rows = flatten_kis_order_rows(payload)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["ODNO"], "1")

    def test_status_from_partial_fill(self) -> None:
        status = status_from_order_row({"TOT_CCLD_QTY": "1"}, expected_qty=Decimal("2"))

        self.assertEqual(status, OrderStatus.PARTIALLY_FILLED)

    def test_status_from_full_fill(self) -> None:
        status = status_from_order_row({"TOT_CCLD_QTY": "2"}, expected_qty=Decimal("2"))

        self.assertEqual(status, OrderStatus.FILLED)

    def test_match_by_broker_order_id(self) -> None:
        order = {"broker_order_id": "KIS123", "symbol": "AAPL"}
        rows = [{"ODNO": "KIS999", "PDNO": "MSFT"}, {"ODNO": "KIS123", "PDNO": "AAPL"}]

        self.assertEqual(match_order_row(order, rows), rows[1])


if __name__ == "__main__":
    unittest.main()
