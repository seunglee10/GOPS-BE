from dataclasses import replace

import pytest

from kis_trader.kis.payload import build_kis_order_payload

from systems.order.tests.kis_trader.fixtures.orders import sample_command


def test_overseas_payload_is_converted_inside_adapter_boundary():
    payload = build_kis_order_payload(sample_command())

    assert payload["market"] == "overseas"
    assert payload["OVRS_EXCG_CD"] == "NASD"
    assert payload["PDNO"] == "AAPL"
    assert payload["OVRS_ORD_UNPR"] == "145.00"
    assert payload["ORD_DVSN"] == "00"


def test_non_overseas_payload_is_rejected_inside_adapter_boundary():
    with pytest.raises(ValueError, match="overseas"):
        build_kis_order_payload(replace(sample_command(), market="domestic"))
