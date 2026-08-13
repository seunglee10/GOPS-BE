from __future__ import annotations

import copy
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .repository import CompanyJournalRepository


DEFAULT_NAMESPACE = "alfaka-market-data"
DEFAULT_TEMPLATE_NAME = "gops-company-journal-process-template"
DEFAULT_PROCESSOR_LABEL = "gops.io/company-journal-role=processor"


class PendingRequestRepository(Protocol):
    def pending_requests(self, limit: int) -> list[Any]: ...


class ProcessorJobGateway(Protocol):
    def has_active_processor_job(self) -> bool: ...

    def create_processor_job(self, now: datetime) -> tuple[str, bool]: ...


class KubernetesConflictError(RuntimeError):
    pass


def dispatch_if_pending(
    repository: PendingRequestRepository,
    gateway: ProcessorJobGateway,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not repository.pending_requests(1):
        return {"status": "idle", "pending": False, "jobCreated": False}
    if gateway.has_active_processor_job():
        return {"status": "active", "pending": True, "jobCreated": False}

    job_name, created = gateway.create_processor_job(now or datetime.now(timezone.utc))
    return {
        "status": "created" if created else "already_exists",
        "pending": True,
        "jobCreated": created,
        "jobName": job_name,
    }


def build_job_from_cronjob(cronjob: dict[str, Any], *, namespace: str, name: str) -> dict[str, Any]:
    job_template = (cronjob.get("spec") or {}).get("jobTemplate") or {}
    job_spec = job_template.get("spec")
    if not isinstance(job_spec, dict) or not job_spec.get("template"):
        raise ValueError("company journal process CronJob has no usable job template")

    template_metadata = job_template.get("metadata") or {}
    metadata: dict[str, Any] = {"name": name, "namespace": namespace}
    for field in ("labels", "annotations"):
        value = template_metadata.get(field)
        if isinstance(value, dict) and value:
            metadata[field] = copy.deepcopy(value)

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": metadata,
        "spec": copy.deepcopy(job_spec),
    }


def job_is_terminal(job: dict[str, Any]) -> bool:
    status = job.get("status") or {}
    if status.get("completionTime"):
        return True
    return any(
        condition.get("type") in {"Complete", "Failed"} and str(condition.get("status")).lower() == "true"
        for condition in status.get("conditions") or []
    )


class KubernetesBatchJobGateway:
    def __init__(
        self,
        *,
        namespace: str,
        template_name: str,
        processor_label: str,
        api_base_url: str,
        token: str,
        ca_file: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.namespace = namespace
        self.template_name = template_name
        self.processor_label = processor_label
        self.api_base_url = api_base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.ssl_context = ssl.create_default_context(cafile=ca_file)

    @classmethod
    def from_environment(cls) -> "KubernetesBatchJobGateway":
        service_account_dir = Path(
            os.getenv("KUBERNETES_SERVICE_ACCOUNT_DIR", "/var/run/secrets/kubernetes.io/serviceaccount")
        )
        namespace_file = service_account_dir / "namespace"
        namespace = os.getenv("POD_NAMESPACE", "").strip()
        if not namespace and namespace_file.exists():
            namespace = namespace_file.read_text(encoding="utf-8").strip()
        namespace = namespace or DEFAULT_NAMESPACE

        host = os.getenv("KUBERNETES_SERVICE_HOST", "").strip()
        port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443").strip() or "443"
        if not host:
            raise RuntimeError("KUBERNETES_SERVICE_HOST is not configured")
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"

        token_file = service_account_dir / "token"
        ca_file = service_account_dir / "ca.crt"
        if not token_file.exists() or not ca_file.exists():
            raise RuntimeError("Kubernetes service account token or CA certificate is missing")

        return cls(
            namespace=namespace,
            template_name=os.getenv("COMPANY_JOURNAL_PROCESS_TEMPLATE", DEFAULT_TEMPLATE_NAME).strip()
            or DEFAULT_TEMPLATE_NAME,
            processor_label=os.getenv("COMPANY_JOURNAL_PROCESS_LABEL", DEFAULT_PROCESSOR_LABEL).strip()
            or DEFAULT_PROCESSOR_LABEL,
            api_base_url=f"https://{host}:{port}",
            token=token_file.read_text(encoding="utf-8").strip(),
            ca_file=str(ca_file),
            timeout_seconds=float(os.getenv("COMPANY_JOURNAL_KUBERNETES_TIMEOUT_SECONDS", "10")),
        )

    def has_active_processor_job(self) -> bool:
        query = urllib.parse.urlencode({"labelSelector": self.processor_label})
        payload = self._request_json("GET", f"{self._jobs_path()}?{query}")
        return any(not job_is_terminal(job) for job in payload.get("items") or [])

    def create_processor_job(self, now: datetime) -> tuple[str, bool]:
        timestamp = now.astimezone(timezone.utc).strftime("%Y%m%d%H%M")
        name = f"gops-company-journal-process-{timestamp}"
        cronjob_path = (
            f"/apis/batch/v1/namespaces/{urllib.parse.quote(self.namespace, safe='')}/cronjobs/"
            f"{urllib.parse.quote(self.template_name, safe='')}"
        )
        cronjob = self._request_json("GET", cronjob_path)
        job = build_job_from_cronjob(cronjob, namespace=self.namespace, name=name)
        try:
            self._request_json("POST", self._jobs_path(), job)
        except KubernetesConflictError:
            return name, False
        return name, True

    def _jobs_path(self) -> str:
        namespace = urllib.parse.quote(self.namespace, safe="")
        return f"/apis/batch/v1/namespaces/{namespace}/jobs"

    def _request_json(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self.api_base_url}{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "gops-company-journal-dispatcher/1.0",
            },
        )
        try:
            with urllib.request.urlopen(
                request,
                context=self.ssl_context,
                timeout=self.timeout_seconds,
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                raise KubernetesConflictError("processor Job already exists") from exc
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"Kubernetes API {method} {path} failed with {exc.code}: {detail}") from exc
        if not raw:
            return {}
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError(f"Kubernetes API {method} {path} returned a non-object response")
        return payload


def main() -> int:
    result = dispatch_if_pending(
        CompanyJournalRepository(),
        KubernetesBatchJobGateway.from_environment(),
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
