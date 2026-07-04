# GOPS Structure Guide

Use this file when deciding where future code belongs.

## Core Shape

```text
apps/       user-facing applications and UI engines
systems/    feature systems owned by the team
platform/   runtime dependency contracts
infra/      Docker, compose, Kubernetes, Terraform, AWS assets
shared/     rare cross-system contracts only
docs/       project reference docs
```

Each feature system should follow this shape when needed:

```text
systems/<system>/
  pods/       long-running services or workers
  jobs/       one-shot, scheduled, or manual jobs
  shared/     code shared inside this system
  tests/      tests owned by this system
  README.md   ownership and runtime notes
```

## Terms

| Term | Meaning |
| --- | --- |
| app | User-facing application or UI engine. |
| system | Feature/domain area with clear team ownership. |
| pod | Long-running runtime unit, usually a Kubernetes Deployment. |
| job | Finite runtime unit, usually a Kubernetes Job/CronJob or compose one-shot service. |
| shared | Code used by pods/jobs inside one system. |
| platform | Kafka, Redis, Postgres, ClickHouse, S3, secrets, or another dependency. |
| image | Docker build artifact. One image may run multiple related pods/jobs. |

## Placement Rules

1. UI-only work goes under `apps/`.
2. Backend/domain work goes under an existing `systems/<system>` or a new system.
3. Code shared only inside one system goes under that system's `shared/`.
4. Global `shared/` is only for stable cross-system contracts.
5. Preserve existing import namespaces when moving Python packages.
6. New Kafka topics, DB tables, S3 prefixes, external APIs, or secrets require platform/env docs.
7. New runtime processes must be classified as pod or job before implementation is considered done.
8. New pods/jobs must be mapped to an existing or new Docker image.
9. Every pod/job folder must own a minimal entrypoint wrapper, even when the real logic remains in system `shared/`.

## Current Systems

```text
systems/api-server/   FastAPI chart/order/WebSocket gateway
systems/market-data/  Alpaca ingest, processing, storage, backfill, serving helpers
systems/order/        KIS demo order domain, outbox, adapter, jobs
```

Create a new system only when it has clear ownership, runtime units, data contracts, or failure modes that do not belong to the existing systems.

Candidate future systems:

```text
systems/ontology/
systems/agent-orchestration/
systems/ui-composition/
systems/news-intelligence/
systems/user-context/
```

Do not create candidate folders until implementation starts.

## Pod / Job Checklist

For every new pod or job, document:

- owning system
- command
- wrapper entrypoint path
- image
- required env vars
- required secrets
- platform dependencies
- Kafka topics
- DB tables or migrations
- S3 prefixes
- health/readiness behavior for pods
- smoke check

## Image Checklist

When image boundaries change, update:

- `IMAGE_STRATEGY.md`
- `infra/docker/*`
- `docker-compose.yml`
- `infra/k8s/base` and overlays
- AWS/ECR scripts or Terraform when affected
- owning system README

## Platform Checklist

When platform contracts change, update:

- `platform/<dependency>/README.md`
- `ENVIRONMENT.md`
- `.env.example`
- compose service or endpoint notes
- k8s ConfigMap/Secret references
- Terraform/AWS handoff notes when AWS owns the resource

Kafka and stream processing stay staged:

```text
local compose -> single pod candidate -> managed AWS candidate
```

Do not hard-code MSK or any external stream processor as the next step before the team decides.
