"""Trading guardrails checked immediately before KIS POST."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from kis_trader.domain.commands import OrderCommand


def can_reprocess_dlq(role: str | None) -> bool:
    return role == "admin"


@dataclass(frozen=True)
class GuardrailDecision:
    allowed: bool
    reason: str | None = None


@dataclass
class TradingGuardrails:
    global_kill_switch: bool = False
    killed_accounts: set[str] = field(default_factory=set)
    killed_users: set[str] = field(default_factory=set)
    killed_symbols: set[str] = field(default_factory=set)
    account_circuit_breakers: set[str] = field(default_factory=set)
    daily_amount_limit: Decimal | None = None
    account_daily_amount_limits: dict[str, Decimal] = field(default_factory=dict)
    account_daily_amounts: dict[str, Decimal] = field(default_factory=dict)
    symbol_qty_limit: Decimal | None = None
    symbol_qty_limits: dict[str, Decimal] = field(default_factory=dict)
    per_minute_order_limit: int | None = None
    account_per_minute_order_limits: dict[str, int] = field(default_factory=dict)
    circuit_breaker_timeout_threshold: int | None = None
    circuit_breaker_reject_threshold: int | None = None
    circuit_breaker_reconciliation_threshold: int | None = None
    _minute_counts: dict[tuple[str, str, str], int] = field(default_factory=dict)
    _breaker_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def check(self, command: OrderCommand) -> GuardrailDecision:
        if command.env != "demo":
            return GuardrailDecision(False, "real trading disabled")
        if self.global_kill_switch:
            return GuardrailDecision(False, "global kill switch")
        if command.account_alias in self.killed_accounts:
            return GuardrailDecision(False, "account kill switch")
        if command.actor_id and command.actor_id in self.killed_users:
            return GuardrailDecision(False, "user kill switch")
        if command.symbol in self.killed_symbols:
            return GuardrailDecision(False, "symbol kill switch")
        if command.account_alias in self.account_circuit_breakers:
            return GuardrailDecision(False, "account circuit breaker")

        symbol_limit = self.symbol_qty_limits.get(command.symbol, self.symbol_qty_limit)
        if symbol_limit is not None and command.qty > symbol_limit:
            return GuardrailDecision(False, "symbol quantity limit")

        amount = command.qty * command.price
        daily_limit = self.account_daily_amount_limits.get(command.account_alias, self.daily_amount_limit)
        if daily_limit is not None:
            current = self.account_daily_amounts.get(command.account_alias, Decimal("0"))
            if current + amount > daily_limit:
                return GuardrailDecision(False, "daily amount limit")

        per_minute_limit = self.account_per_minute_order_limits.get(command.account_alias, self.per_minute_order_limit)
        if per_minute_limit is not None:
            key = (command.account_alias, command.symbol, self._minute_bucket())
            self._minute_counts[key] = self._minute_counts.get(key, 0) + 1
            if self._minute_counts[key] > per_minute_limit:
                return GuardrailDecision(False, "per-minute order limit")

        return GuardrailDecision(True)

    def record_accepted_order(self, command: OrderCommand) -> None:
        amount = command.qty * command.price
        self.account_daily_amounts[command.account_alias] = self.account_daily_amounts.get(command.account_alias, Decimal("0")) + amount

    def record_broker_outcome(self, account_alias: str, outcome: str) -> None:
        normalized = outcome.strip().lower()
        if normalized not in {"timeout", "reject", "reconciliation_required"}:
            return
        key = (account_alias, normalized)
        self._breaker_counts[key] = self._breaker_counts.get(key, 0) + 1
        threshold = self._threshold_for_outcome(normalized)
        if threshold is not None and self._breaker_counts[key] >= threshold:
            self.account_circuit_breakers.add(account_alias)

    def _threshold_for_outcome(self, outcome: str) -> int | None:
        if outcome == "timeout":
            return self.circuit_breaker_timeout_threshold
        if outcome == "reject":
            return self.circuit_breaker_reject_threshold
        if outcome == "reconciliation_required":
            return self.circuit_breaker_reconciliation_threshold
        return None

    def _minute_bucket(self) -> str:
        return self.now().strftime("%Y%m%d%H%M")
