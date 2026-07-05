from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Callable

from ..contracts import RuntimePolicy


class AgentAnalysisCanceled(Exception):
    def __init__(self, analysis_id: str, stage: str | None = None):
        self.analysis_id = analysis_id
        self.stage = stage
        message = f"Agent analysis {analysis_id} was canceled"
        if stage:
            message = f"{message} during {stage}"
        super().__init__(message)


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
    cancellation_checker: Callable[[], bool] | None = None
    analysis_id: str | None = None
    llm_budget: LlmBudget = field(init=False)

    def __post_init__(self) -> None:
        self.llm_budget = LlmBudget(max_calls=max(0, int(self.policy.max_realtime_llm_calls)))

    def refresh_policy(self, policy: RuntimePolicy) -> None:
        self.policy = policy
        self.llm_budget.max_calls = max(0, int(policy.max_realtime_llm_calls))

    def acquire_llm(self, label: str) -> bool:
        self.raise_if_canceled(label)
        return self.llm_budget.acquire(label, self.timing)

    def set_cancellation_checker(self, analysis_id: str, checker: Callable[[], bool]) -> None:
        self.analysis_id = analysis_id
        self.cancellation_checker = checker

    def is_canceled(self) -> bool:
        return bool(self.cancellation_checker and self.cancellation_checker())

    def raise_if_canceled(self, stage: str | None = None) -> None:
        if self.is_canceled():
            raise AgentAnalysisCanceled(self.analysis_id or "unknown", stage)
