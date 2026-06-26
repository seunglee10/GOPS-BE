from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CanonicalTopics:
    order_commands: str = "orders.commands.v1"
    submit_results: str = "broker.submit-results.v1"
    order_events: str = "broker.order-events.v1"
    dlq: str = "orders.dlq.v1"

    def as_set(self) -> set[str]:
        return {self.order_commands, self.submit_results, self.order_events, self.dlq}


CANONICAL_TOPIC_NAMES = CanonicalTopics().as_set()
