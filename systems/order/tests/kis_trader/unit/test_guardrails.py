from datetime import datetime, timezone
from decimal import Decimal

from kis_trader.operations.guardrails import TradingGuardrails, can_reprocess_dlq

from tests.kis_trader.fixtures.orders import sample_command


def test_dlq_reprocess_is_admin_only():
    assert can_reprocess_dlq("admin") is True
    assert can_reprocess_dlq("trader") is False
    assert can_reprocess_dlq("user") is False
    assert can_reprocess_dlq(None) is False


def test_symbol_specific_quantity_limit_blocks_order():
    command = sample_command()
    guardrails = TradingGuardrails(symbol_qty_limits={"AAPL": Decimal("0.5")})

    decision = guardrails.check(command)

    assert decision.allowed is False
    assert decision.reason == "symbol quantity limit"


def test_account_daily_amount_limit_uses_accumulated_amount():
    command = sample_command()
    guardrails = TradingGuardrails(
        account_daily_amount_limits={"demo-account": Decimal("200")},
        account_daily_amounts={"demo-account": Decimal("100")},
    )

    decision = guardrails.check(command)

    assert decision.allowed is False
    assert decision.reason == "daily amount limit"


def test_per_minute_rate_limit_resets_by_minute_bucket():
    moments = [
        datetime(2026, 6, 27, 1, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 27, 1, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 27, 1, 1, tzinfo=timezone.utc),
    ]
    command = sample_command()
    guardrails = TradingGuardrails(per_minute_order_limit=1, now=lambda: moments.pop(0))

    assert guardrails.check(command).allowed is True
    assert guardrails.check(command).reason == "per-minute order limit"
    assert guardrails.check(command).allowed is True


def test_repeated_timeouts_open_account_circuit_breaker():
    guardrails = TradingGuardrails(circuit_breaker_timeout_threshold=2)

    guardrails.record_broker_outcome("demo-account", "timeout")
    assert "demo-account" not in guardrails.account_circuit_breakers
    guardrails.record_broker_outcome("demo-account", "timeout")

    assert "demo-account" in guardrails.account_circuit_breakers
