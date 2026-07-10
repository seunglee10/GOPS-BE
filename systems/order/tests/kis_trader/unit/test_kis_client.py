from __future__ import annotations

import json
import sys
import types
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any

import pytest

from kis_trader.domain.envelope import validate_order_envelope
from kis_trader.kis.client import DemoKisHttpClient
from kis_trader.kis.config import KisConfigError, load_kis_config
from kis_trader.kis.fake import KisExplicitReject

from systems.order.tests.kis_trader.fixtures.orders import sample_envelope


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


def valid_token_payload():
    expires_at = datetime.now() + timedelta(hours=1)
    return {"access_token": "demo-token", "access_token_token_expired": expires_at.strftime("%Y-%m-%d %H:%M:%S")}


def set_demo_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("KIS_ENV", "demo")
    monkeypatch.setenv("KIS_CREDENTIAL_SOURCE", "local-env")
    monkeypatch.setenv("KIS_DEMO_APP_KEY", "demo-key")
    monkeypatch.setenv("KIS_DEMO_APP_SECRET", "demo-secret")
    monkeypatch.setenv("KIS_DEMO_ACCOUNT_NO", "12345678")
    monkeypatch.setenv("KIS_ACCOUNT_PRODUCT_CODE", "01")
    monkeypatch.setenv("KIS_DEMO_BASE_URL", "https://openapivts.example.test:29443")
    monkeypatch.setenv("KIS_TOKEN_CACHE_PATH", str(tmp_path / "kis-token.json"))


def test_demo_overseas_order_posts_to_kis_open_api(monkeypatch, tmp_path):
    set_demo_env(monkeypatch, tmp_path)
    calls = []

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        if url.endswith("/oauth2/tokenP"):
            return FakeResponse(200, valid_token_payload())
        return FakeResponse(200, {"rt_cd": "0", "output": {"ODNO": "KIS-ORDER-1"}})

    monkeypatch.setattr("requests.post", fake_post)
    command = validate_order_envelope(sample_envelope())

    response = DemoKisHttpClient.from_env().submit_order(command)

    assert response["rt_cd"] == "0"
    assert response["broker_order_id"] == "KIS-ORDER-1"
    assert calls[0]["url"] == "https://openapivts.example.test:29443/oauth2/tokenP"
    assert calls[1]["url"] == "https://openapivts.example.test:29443/uapi/overseas-stock/v1/trading/order"
    assert calls[1]["headers"]["authorization"] == "Bearer demo-token"
    assert calls[1]["headers"]["tr_id"] == "VTTT1002U"
    assert calls[1]["json"]["CANO"] == "12345678"
    assert calls[1]["json"]["PDNO"] == "AAPL"


def test_demo_us_sell_order_uses_kis_open_api_sell_tr_id(monkeypatch, tmp_path):
    set_demo_env(monkeypatch, tmp_path)
    calls = []

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        if url.endswith("/oauth2/tokenP"):
            return FakeResponse(200, valid_token_payload())
        return FakeResponse(200, {"rt_cd": "0", "output": {"ODNO": "KIS-SELL-1"}})

    monkeypatch.setattr("requests.post", fake_post)
    command = validate_order_envelope(sample_envelope(payload={**sample_envelope()["payload"], "side": "sell"}))

    response = DemoKisHttpClient.from_env().submit_order(command)

    assert response["broker_order_id"] == "KIS-SELL-1"
    assert calls[1]["url"] == "https://openapivts.example.test:29443/uapi/overseas-stock/v1/trading/order"
    assert calls[1]["headers"]["tr_id"] == "VTTT1006U"
    assert calls[1]["json"]["PDNO"] == "AAPL"
    assert calls[1]["json"]["SLL_TYPE"] == "00"


def test_demo_client_rejects_non_overseas_command(monkeypatch, tmp_path):
    set_demo_env(monkeypatch, tmp_path)
    command = replace(validate_order_envelope(sample_envelope()), market="domestic")

    with pytest.raises(KisExplicitReject, match="overseas"):
        DemoKisHttpClient.from_env().submit_order(command)


def test_kis_reject_payload_raises_explicit_reject(monkeypatch, tmp_path):
    set_demo_env(monkeypatch, tmp_path)

    def fake_post(url: str, **_kwargs: Any) -> FakeResponse:
        if url.endswith("/oauth2/tokenP"):
            return FakeResponse(200, valid_token_payload())
        return FakeResponse(200, {"rt_cd": "1", "msg_cd": "APBK001", "msg1": "rejected"})

    monkeypatch.setattr("requests.post", fake_post)

    with pytest.raises(KisExplicitReject, match="APBK001 rejected"):
        DemoKisHttpClient.from_env().submit_order(validate_order_envelope(sample_envelope()))


def test_demo_order_history_uses_ccnl_endpoint(monkeypatch, tmp_path):
    set_demo_env(monkeypatch, tmp_path)
    get_calls = []

    def fake_post(url: str, **_kwargs: Any) -> FakeResponse:
        return FakeResponse(200, valid_token_payload())

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        get_calls.append({"url": url, **kwargs})
        return FakeResponse(200, {"rt_cd": "0", "output": []})

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("requests.get", fake_get)

    rows = DemoKisHttpClient.from_env().fetch_order_history(start_date=date(2026, 6, 27), end_date=date(2026, 6, 27), symbol="AAPL", side="buy")

    assert rows == []
    assert get_calls[0]["url"] == "https://openapivts.example.test:29443/uapi/overseas-stock/v1/trading/inquire-ccnl"
    assert get_calls[0]["headers"]["tr_id"] == "VTTS3035R"
    assert get_calls[0]["params"]["ORD_STRT_DT"] == "20260627"


def test_demo_orderable_cash_uses_psamount_endpoint(monkeypatch, tmp_path):
    set_demo_env(monkeypatch, tmp_path)
    get_calls = []

    def fake_post(url: str, **_kwargs: Any) -> FakeResponse:
        return FakeResponse(200, valid_token_payload())

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        get_calls.append({"url": url, **kwargs})
        return FakeResponse(200, {"rt_cd": "0", "output": {"ord_psbl_frcr_amt": "12345.67", "ord_psbl_qty": "12"}})

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("requests.get", fake_get)

    balance = DemoKisHttpClient.from_env().fetch_orderable_cash(symbol="nvda", exchange="nasd", price="1.00")

    assert balance["orderable_cash"] == "12345.67"
    assert balance["orderable_qty"] == "12"
    assert balance["currency"] == "USD"
    assert get_calls[0]["url"] == "https://openapivts.example.test:29443/uapi/overseas-stock/v1/trading/inquire-psamount"
    assert get_calls[0]["headers"]["tr_id"] == "VTTS3007R"
    assert get_calls[0]["params"]["ITEM_CD"] == "NVDA"
    assert get_calls[0]["params"]["OVRS_EXCG_CD"] == "NASD"
    assert get_calls[0]["params"]["OVRS_ORD_UNPR"] == "1.00"


def test_demo_overseas_holdings_uses_balance_endpoint_and_normalizes_positions(monkeypatch, tmp_path):
    set_demo_env(monkeypatch, tmp_path)
    get_calls = []

    def fake_post(url: str, **_kwargs: Any) -> FakeResponse:
        return FakeResponse(200, valid_token_payload())

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        get_calls.append({"url": url, **kwargs})
        return FakeResponse(
            200,
            {
                "rt_cd": "0",
                "output1": [
                    {
                        "ovrs_pdno": "MU",
                        "ovrs_item_name": "Micron Technology",
                        "ovrs_cblc_qty": "10",
                        "pchs_avg_pric": "72.10",
                        "now_pric2": "185.30",
                        "frcr_evlu_amt2": "1853.00",
                        "frcr_evlu_pfls_amt": "1132.00",
                        "evlu_pfls_rt": "157.00",
                        "tr_crcy_cd": "USD",
                    }
                ],
                "output2": {
                    "frcr_use_psbl_amt": "1199.00",
                    "frcr_evlu_tota": "3052.00",
                    "ovrs_tot_pfls": "1132.00",
                    "tot_pftrt": "104.6",
                },
            },
        )

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("requests.get", fake_get)

    payload = DemoKisHttpClient.from_env().fetch_holdings(market="overseas", currency="USD")

    assert payload["status"] == "ok"
    assert payload["source"] == "kis-demo"
    assert payload["account"]["cashForeign"] == 1199.0
    assert payload["account"]["unrealizedPnlRate"] == 104.6
    assert payload["positions"][0]["symbol"] == "MU"
    assert payload["positions"][0]["quantity"] == 10.0
    assert payload["positions"][0]["marketValueForeign"] == 1853.0
    assert get_calls[0]["url"] == "https://openapivts.example.test:29443/uapi/overseas-stock/v1/trading/inquire-balance"
    assert get_calls[0]["headers"]["tr_id"] == "VTTS3012R"
    assert get_calls[0]["params"]["CANO"] == "12345678"
    assert get_calls[0]["params"]["TR_CRCY_CD"] == "USD"


def test_real_env_is_rejected_at_config_load(monkeypatch, tmp_path):
    monkeypatch.setenv("KIS_ENV", "real")
    monkeypatch.setenv("KIS_TOKEN_CACHE_PATH", str(tmp_path / "kis-token.json"))

    with pytest.raises(KisConfigError, match="KIS_ENV=real"):
        load_kis_config()


def test_demo_config_can_load_required_values_from_secret_manager(monkeypatch, tmp_path):
    monkeypatch.setenv("KIS_ENV", "demo")
    monkeypatch.delenv("KIS_CREDENTIAL_SOURCE", raising=False)
    monkeypatch.delenv("KIS_DEMO_APP_KEY", raising=False)
    monkeypatch.delenv("KIS_DEMO_APP_SECRET", raising=False)
    monkeypatch.delenv("KIS_DEMO_ACCOUNT_NO", raising=False)
    monkeypatch.delenv("KIS_SECRET_NAME", raising=False)
    monkeypatch.setenv("AWS_REGION", "ap-northeast-2")
    monkeypatch.setenv("KIS_TOKEN_CACHE_PATH", str(tmp_path / "kis-token.json"))

    class FakeSecretsManagerClient:
        def get_secret_value(self, SecretId: str) -> dict[str, str]:
            assert SecretId == "tead/gops/kis"
            return {
                "SecretString": json.dumps(
                    {
                        "KIS_DEMO_APP_KEY": "secret-key",
                        "KIS_DEMO_APP_SECRET": "secret-value",
                        "KIS_DEMO_ACCOUNT_NO": "12345678",
                    }
                )
            }

    def fake_client(service_name: str, region_name: str):
        assert service_name == "secretsmanager"
        assert region_name == "ap-northeast-2"
        return FakeSecretsManagerClient()

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=fake_client))
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")

    config = load_kis_config(env_file=empty_env)

    assert config.app_key == "secret-key"
    assert config.app_secret == "secret-value"
    assert config.account_no == "12345678"
