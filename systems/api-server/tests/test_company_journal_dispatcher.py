from __future__ import annotations

from datetime import datetime, timezone

from app.company_journal.dispatcher import build_job_from_cronjob, dispatch_if_pending, job_is_terminal


class FakeRepository:
    def __init__(self, pending: bool) -> None:
        self.pending = pending
        self.limits: list[int] = []

    def pending_requests(self, limit: int):
        self.limits.append(limit)
        return [object()] if self.pending else []


class FakeGateway:
    def __init__(self, *, active: bool = False, created: bool = True) -> None:
        self.active = active
        self.created = created
        self.active_checks = 0
        self.create_calls: list[datetime] = []

    def has_active_processor_job(self) -> bool:
        self.active_checks += 1
        return self.active

    def create_processor_job(self, now: datetime) -> tuple[str, bool]:
        self.create_calls.append(now)
        return "gops-company-journal-process-202607180210", self.created


def test_dispatcher_does_not_call_kubernetes_when_no_request_is_pending():
    repository = FakeRepository(pending=False)
    gateway = FakeGateway()

    result = dispatch_if_pending(repository, gateway)

    assert result == {"status": "idle", "pending": False, "jobCreated": False}
    assert repository.limits == [1]
    assert gateway.active_checks == 0
    assert gateway.create_calls == []


def test_dispatcher_skips_creation_while_another_processor_job_is_active():
    repository = FakeRepository(pending=True)
    gateway = FakeGateway(active=True)

    result = dispatch_if_pending(repository, gateway)

    assert result == {"status": "active", "pending": True, "jobCreated": False}
    assert gateway.active_checks == 1
    assert gateway.create_calls == []


def test_dispatcher_creates_one_processor_job_for_pending_requests():
    repository = FakeRepository(pending=True)
    gateway = FakeGateway()
    now = datetime(2026, 7, 18, 2, 10, tzinfo=timezone.utc)

    result = dispatch_if_pending(repository, gateway, now=now)

    assert result == {
        "status": "created",
        "pending": True,
        "jobCreated": True,
        "jobName": "gops-company-journal-process-202607180210",
    }
    assert gateway.create_calls == [now]


def test_dispatcher_treats_same_minute_job_conflict_as_idempotent_success():
    repository = FakeRepository(pending=True)
    gateway = FakeGateway(created=False)

    result = dispatch_if_pending(repository, gateway)

    assert result["status"] == "already_exists"
    assert result["pending"] is True
    assert result["jobCreated"] is False
    assert result["jobName"] == "gops-company-journal-process-202607180210"


def test_job_is_built_from_the_suspended_cronjob_template_without_changing_runtime_contract():
    cronjob = {
        "spec": {
            "jobTemplate": {
                "metadata": {
                    "labels": {
                        "app": "gops-company-journal-worker",
                        "gops.io/company-journal-role": "processor",
                    }
                },
                "spec": {
                    "backoffLimit": 2,
                    "template": {
                        "spec": {
                            "containers": [{"name": "worker", "image": "registry/api:sha"}],
                            "restartPolicy": "Never",
                        }
                    },
                },
            }
        }
    }

    job = build_job_from_cronjob(
        cronjob,
        namespace="alfaka-market-data",
        name="gops-company-journal-process-202607180210",
    )

    assert job["apiVersion"] == "batch/v1"
    assert job["kind"] == "Job"
    assert job["metadata"]["namespace"] == "alfaka-market-data"
    assert job["metadata"]["name"] == "gops-company-journal-process-202607180210"
    assert job["metadata"]["labels"]["gops.io/company-journal-role"] == "processor"
    assert job["spec"]["backoffLimit"] == 2
    assert job["spec"]["template"]["spec"]["containers"][0]["image"] == "registry/api:sha"


def test_only_complete_or_failed_jobs_are_terminal_for_duplicate_prevention():
    assert job_is_terminal({"status": {}}) is False
    assert job_is_terminal({"status": {"active": 1}}) is False
    assert job_is_terminal({"status": {"conditions": [{"type": "Complete", "status": "True"}]}}) is True
    assert job_is_terminal({"status": {"conditions": [{"type": "Failed", "status": "True"}]}}) is True
