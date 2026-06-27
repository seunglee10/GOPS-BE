from kis_trader.kis.payload import build_kis_order_payload

from test.fixtures.orders import sample_command, sample_envelope
from kis_trader.domain.envelope import validate_order_envelope


def test_overseas_payload_is_converted_inside_adapter_boundary():
    payload = build_kis_order_payload(sample_command())

    assert payload["market"] == "overseas"
    assert payload["OVRS_EXCG_CD"] == "NASD"
    assert payload["PDNO"] == "AAPL"
    assert payload["OVRS_ORD_UNPR"] == "145.00"


def test_domestic_payload_is_converted_inside_adapter_boundary():
    envelope = sample_envelope(
        payload={
            "market": "domestic",
            "symbol": "005930",
            "side": "buy",
            "qty": "1",
            "price": "70000",
            "exchange": "KRX",
            "order_division": "00",
            "sell_type": "",
            "condition_price": "",
        }
    )
    command = validate_order_envelope(envelope)

    payload = build_kis_order_payload(command)

    assert payload["market"] == "domestic"
    assert payload["PDNO"] == "005930"
    assert payload["ORD_UNPR"] == "70000"
    assert payload["EXCG_ID_DVSN_CD"] == "KRX"
