# GOPS Image Strategy

This file defines current custom Docker image boundaries.

## Custom Images

| Image | Dockerfile | Main folders | Runtime |
| --- | --- | --- | --- |
| `gops-frontend` | `infra/docker/Dockerfile.gops-frontend` | `apps/gops-frontend`, `apps/chart-engine` | frontend pod |
| `gops-api-server` | `infra/docker/Dockerfile.gops-backend` | `systems/api-server`, market/order shared packages | api-server pod |
| `gops-market-ingestor` | `infra/docker/Dockerfile.gops-market-ingestor` | `systems/market-data/pods/market-ingestor`, `systems/market-data/shared` | market-ingestor pod |
| `gops-market-processor` | `infra/docker/Dockerfile.gops-market-processor` | `systems/market-data/pods/market-processor`, `systems/market-data/jobs/symbol-registry-sync`, `systems/market-data/jobs/coverage-repair`, `systems/market-data/shared` | market-processor pod, symbol-registry-sync job, coverage-repair job |
| `gops-market-storage` | `infra/docker/Dockerfile.gops-market-storage` | `s3-sink`, `clickhouse-loader`, market shared code | s3-sink and clickhouse-loader pods |
| `gops-backfill-worker` | `infra/docker/Dockerfile.gops-backfill-worker` | `systems/market-data/pods/backfill-worker`, market shared code | backfill-worker pod |
| `gops-order-worker` | `infra/docker/Dockerfile.gops-order-worker` | `systems/order/pods/order-outbox`, `systems/order/jobs`, order shared code | order-outbox pod and order jobs |
| `gops-kis-adapter` | `infra/docker/Dockerfile.gops-kis-adapter` | `systems/order/pods/kis-adapter`, order shared code | kis-adapter pod |

## Why These Boundaries

- `gops-kis-adapter` is separate because it touches KIS, secrets, and broker submission risk.
- `gops-market-storage` groups S3 sink and ClickHouse loader because both are market storage workers.
- `gops-order-worker` groups DB-centered order operations.
- Avoid one generic worker image for unrelated market, order, ontology, agent, and UI-composition runtimes.

## COPY Rule

Dockerfiles copy whole system `shared/` folders first:

```dockerfile
COPY systems/market-data/shared ./systems/market-data/shared
COPY systems/order/shared ./systems/order/shared
```

Narrow COPY paths later only for proven image-size, dependency, or security reasons.

Runtime `PYTHONPATH` should include shared folders:

```text
systems/market-data/shared
systems/order/shared
```

That preserves `alfaka.*` and `kis_trader.*` imports.

Compose, k8s, and Docker `CMD` should execute pod/job wrapper files, not shared modules directly.

## Official Or Managed Images

Use official or managed services unless the team decides otherwise:

```text
Kafka / MSK
Redis / ElastiCache or Valkey
Postgres / RDS
ClickHouse
S3
```

Local Compose may still run official images for development.

## Naming Rule

Keep names aligned:

```text
folder:  systems/order/pods/kis-adapter
image:   gops-kis-adapter
compose: kis-broker-adapter
k8s:     kis-broker-adapter
```

When adding a pod/job, decide image placement before finishing the implementation.
Create a new image when secrets, external API risk, scaling, dependencies, release cadence, or team ownership differ.
