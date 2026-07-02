from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from ..contracts import RuntimePolicy


@dataclass
class LlmBudget:
    max_calls: int = 1
    used_calls: int = 0
    blocked_calls: int = 0
    labels: list[str] = field(default_factory=list)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def acquire(self, label: str, timing: dict[str, Any] | None = None) -> bool:
        with self._lock:
            if self.used_calls >= self.max_calls:
                self.blocked_calls += 1
                if isinstance(timing, dict):
                    timing["llmBudgetBlocked"] = int(timing.get("llmBudgetBlocked") or 0) + 1
                return False
            self.used_calls += 1
            self.labels.append(label)
            if isinstance(timing, dict):
                timing["llmCalls"] = self.used_calls
                timing["llmCallLabels"] = list(self.labels)
            return True


@dataclass
class RuntimeRunContext:
    policy: RuntimePolicy
    timing: dict[str, Any] = field(default_factory=dict)
    llm_budget: LlmBudget = field(init=False)

    def __post_init__(self) -> None:
        self.llm_budget = LlmBudget(max_calls=max(0, int(self.policy.max_realtime_llm_calls)))

    def refresh_policy(self, policy: RuntimePolicy) -> None:
        self.policy = policy
        self.llm_budget.max_calls = max(0, int(policy.max_realtime_llm_calls))

    def acquire_llm(self, label: str) -> bool:
        return self.llm_budget.acquire(label, self.timing)
