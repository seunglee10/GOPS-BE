from __future__ import annotations

import os
import time
import traceback
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from app.alerts.notifications import RedisNotificationBroker
from app.alerts.repository import PostgresAlertRepository

from .repository import PostgresRecommendationRepository, RecommendationRepository
from .service import RecommendationDataSource, RecommendationService
from .scoring import market_session


DEFAULT_POLL_SECONDS = 30 * 60


class RecommendationWorker:
    def __init__(self, app: Any) -> None:
        self.app = app

    @classmethod
    def from_env(cls) -> "RecommendationWorker":
        app = SimpleNamespace(state=SimpleNamespace())
        app.state.recommendation_repository = PostgresRecommendationRepository.from_env()
        app.state.alert_repository = PostgresAlertRepository.from_env()
        try:
            app.state.alert_notification_broker = RedisNotificationBroker.from_env()
        except Exception:
            pass
        return cls(app)

    @property
    def repository(self) -> RecommendationRepository:
        return self.app.state.recommendation_repository

    def run_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        session_mode = _worker_session_mode(now)
        if not session_mode:
            return {"status": "not_due", "processed": 0, "generated": 0}

        service = getattr(self.app.state, "recommendation_service", None)
        if service is None:
            service = RecommendationService(
                repository=self.repository,
                data_source=RecommendationDataSource(self.app),
                app=self.app,
            )
            self.app.state.recommendation_service = service

        processed = 0
        generated = 0
        failures: list[dict[str, str]] = []
        for user_sub in self.repository.list_profile_user_subs():
            processed += 1
            try:
                result = service.refresh(user_sub, now=now, session_mode=session_mode)
            except Exception as exc:
                failures.append({"userSub": user_sub, "error": str(exc)})
                continue
            if result.get("status") in {"completed", "empty"} and not result.get("idempotentReplay") and not result.get("retryable"):
                generated += 1
        return {
            "status": "ok" if not failures else "partial",
            "processed": processed,
            "generated": generated,
            "failures": failures[:10],
        }


def run() -> None:
    worker = RecommendationWorker.from_env()
    poll_seconds = max(10, int(os.getenv("RECOMMENDATION_WORKER_POLL_SECONDS", str(DEFAULT_POLL_SECONDS))))
    print(f"Recommendation worker started: pollSeconds={poll_seconds}", flush=True)
    while True:
        try:
            result = worker.run_once()
            if result.get("processed") or result.get("status") == "partial":
                print(f"recommendation worker tick: {result}", flush=True)
        except Exception:
            traceback.print_exc()
        time.sleep(poll_seconds)


def _worker_session_mode(now: datetime) -> str | None:
    session = market_session(now)
    return session if session in {"pre", "regular"} else None


if __name__ == "__main__":
    run()
