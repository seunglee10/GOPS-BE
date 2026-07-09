# EKS Data-Preserving Clean Rebuild Plan

이 문서는 EKS platform을 새 NodePool과 새 PVC로 재배치하되, DB 데이터를 잃지
않는 clean rebuild runbook이다. 현재 단계에서는 repo 문서와 manifest 준비,
dry-run까지만 허용한다. AWS vCPU quota 승인과 별도 운영 승인 전에는 EKS에
적용하지 않는다.

## Summary

데이터 손실은 허용하지 않는다. 기존 stateful PVC를 어중간하게 재사용하지 않는
원칙은 유지하지만, 새 PVC는 빈 볼륨이 아니라 검증된 백업에서 복원한 볼륨이어야
한다.

Hard-preserve 대상:

- Postgres
- ClickHouse
- GraphDB

Default-preserve 대상:

- Redis. 순수 캐시 key만 버릴 수 있으며, report, idempotency, session, alert,
  recommendation 등 애플리케이션 상태 key는 보존한다.
- Kafka. 메시지와 consumer offset을 버리려면 해당 pipeline owner가 명시적으로
  승인해야 한다.

`fresh PVC`는 이 문서에서 "새로 만든 뒤 검증된 백업을 복원한 PVC"를 뜻한다.
빈 데이터로 시작하는 PVC가 아니다.

## Guardrails

- 기존 PVC는 복구 가능한 백업, checksum, restore 검증이 끝나기 전에는 삭제하지
  않는다.
- StatefulSet 이름과 `volumeClaimTemplates` 이름 때문에 새 PVC 이름이 기존 PVC와
  충돌하면, 둘 중 하나를 선택한다.
  - 새 claim name 또는 임시 StatefulSet 이름으로 복원 검증용 PVC를 먼저 만든다.
  - 같은 PVC 이름을 반드시 써야 하면, 기존 PVC 삭제 전에 EBS snapshot과 object
    backup을 모두 확보하고, 임시 restore 검증을 통과시킨다.
- Postgres migration은 재초기화가 아니라 기존 데이터 위에서 idempotent하게
  실행한다.
- ClickHouse schema init은 빈 DB 생성용으로만 쓰지 않는다. 복원 후 DDL drift
  확인과 필요한 additive migration만 수행한다.
- GraphDB는 `/opt/graphdb/home` 전체를 복원해 `nasdaq-fibo` repository,
  license, runtime 구조를 함께 보존한다.
- Redis와 Kafka를 reset하려면 "어떤 key/topic/offset을 버려도 되는지"를 별도
  승인 기록으로 남긴다.

## Target Node Layout

| NodePool | EC2 class | Workload |
| --- | --- | --- |
| `app-agent` | 2 x `m5a.large` or `m6a.large`, 2 vCPU / 8 GiB | backend, frontend, AI agent, market/news workers |
| `cache-db` | 1 x `m5a.xlarge` or `m6a.xlarge`, 4 vCPU / 16 GiB | Redis, Postgres |
| `streaming` | 1 x `m5a.xlarge` or `m6a.xlarge`, 4 vCPU / 16 GiB | Kafka only |
| `graphdb` | 1 x `m5a.xlarge` or `m6a.xlarge`, 4 vCPU / 16 GiB | GraphDB only |
| `clickhouse` | 1 x `m5a.2xlarge` or `m6a.2xlarge`, 8 vCPU / 32 GiB | ClickHouse only |
| `batch` | 0->1 x `m5a.xlarge` or `m6a.xlarge`, 4 vCPU / 16 GiB | backfill, eval, smoke, rebuild Jobs |

Expected vCPU:

```text
steady state: 24 vCPU
with batch:   28 vCPU
```

The live cluster may also keep one small `general-purpose` node for EKS add-ons
such as CoreDNS, the AWS Load Balancer Controller, EBS CSI, metrics-server, and
external-secrets. Count that add-on capacity separately unless those controllers
are explicitly moved to a dedicated system NodePool.

All new nodes are on-demand.

## Manifest Preparation

Before quota approval, prepare local YAML only.

NodePools:

- Add `app-agent`, `cache-db`, `streaming`, `graphdb`, and `batch`.
- Keep the dedicated `clickhouse` NodePool constrained to the 8 vCPU class.

Placement policy:

- ClickHouse -> `clickhouse`
- Kafka -> `streaming`
- GraphDB -> `graphdb`
- Redis/Postgres -> `cache-db`
- backend/frontend/agent/market/news workers -> `app-agent`
- smoke/eval/benchmark/backfill/rebuild Jobs -> `batch`

Resource and PVC targets:

| Component | Resources | PVC | Data policy |
| --- | --- | --- | --- |
| ClickHouse | request `cpu 4`, `memory 6Gi`; limit `cpu 4`, `memory 8Gi` | `50Gi` minimum | Restore from verified backup or snapshot |
| Kafka | request `cpu 1`, `memory 2Gi`; limit `cpu 2`, `memory 4Gi`; `KAFKA_HEAP_OPTS=-Xms1g -Xmx2g` | `30Gi` minimum | Preserve logs/offsets unless reset is approved |
| GraphDB | `GDB_HEAP_SIZE=4g`; request `memory 6Gi`; limit `memory 7Gi` | `10Gi` minimum | Restore full `/opt/graphdb/home` archive |
| Redis | request `memory 1Gi`; limit `memory 4Gi`; `maxmemory=3Gi`; `volatile-lru` | `10Gi` minimum | Preserve non-cache state; cache reset requires approval |
| Postgres | request `memory 512Mi`; limit `memory 2Gi` | `10Gi` minimum | Restore from logical dump and/or snapshot |

The final PVC size must be at least the current used size plus headroom. If the
current data already approaches the target size, increase the target before
applying manifests.

Job TTL:

- smoke/eval/benchmark: `ttlSecondsAfterFinished: 3600`
- backfill/rebuild/restore: `ttlSecondsAfterFinished: 86400`

## Backup Plan

Take inventory before stopping services:

- PVC names, storage class, capacity, bound PV, and EBS volume ID.
- Current used bytes from each mounted data directory.
- Row counts or repository sizes needed for restore validation.
- Kafka topic list and consumer group offsets if Kafka state is preserved.
- Redis key counts by namespace if Redis state is preserved.

Prepared helper scripts:

```sh
scripts/aws/prepare-rebuild-shutdown.sh
scripts/aws/prepare-rebuild-shutdown.sh --execute
scripts/aws/collect-platform-backup-inventory.sh
scripts/aws/backup-postgres-logical.sh
scripts/aws/restore-postgres-logical.sh
scripts/aws/backup-redis-rdb.sh
scripts/aws/backup-graphdb-pvc.sh --force
scripts/aws/create-pvc-ebs-snapshots.sh
scripts/aws/create-pvc-ebs-snapshots.sh --execute
```

Run `prepare-rebuild-shutdown.sh` without `--execute` first to list the
Deployments, CronJobs, and active Jobs that will be affected. Use `--execute`
only after service downtime is approved for that rebuild window.

Run `create-pvc-ebs-snapshots.sh` without `--execute` first to print the PVC to
EBS volume plan. Use `--execute` only after stateful writers are stopped and the
operator approves AWS snapshot creation.

Current live Redis may have AOF disabled even when the repo manifest enables
AOF. If Redis state is preserved, run `backup-redis-rdb.sh` before scaling Redis
down, then restore or intentionally repopulate the preserved namespaces.

Backup requirements:

| Component | Required backup | Restore validation |
| --- | --- | --- |
| Postgres | Logical dump plus EBS snapshot while writes are stopped | `pg_isready`, schema version, critical table row counts |
| ClickHouse | EBS snapshot while loaders are stopped, plus table count manifest | `/ping`, database/table list, critical table row counts |
| GraphDB | Tar archive of `/opt/graphdb/home` plus checksum | `/repositories/nasdaq-fibo/size` and repository metadata |
| Redis | AOF/RDB or full `/data` backup plus EBS snapshot if non-cache state is preserved | `PING`, key count by preserved namespace, sample key reads |
| Kafka | EBS snapshot of `/var/lib/kafka` while broker is stopped if messages/offsets are preserved | topic list, consumer group offsets, producer/consumer smoke |

Backups must be stored outside the PVC being replaced. Local restore artifacts
must not be committed.

## Approval-Time Order

1. Stop writers and readers.
   - Scale app, agent, market, news, and order Deployments to `0`.
   - Suspend CronJobs.
   - Stop or delete active Jobs after recording what they were doing.
   - Confirm Kafka producers/consumers and DB writers are stopped.
2. Create backups.
   - Export logical backups where available.
   - Create EBS snapshots after stateful pods are quiesced.
   - Create checksum files for object backups.
3. Verify backups before destructive steps.
   - Check file sizes and checksums.
   - Run at least one temporary restore validation for Postgres and GraphDB.
   - Record ClickHouse table counts and Kafka/Redis inventories when preserved.
4. Scale down stateful services.
   - Scale ClickHouse, Kafka, GraphDB, Redis, and Postgres StatefulSets to `0`.
   - Delete StatefulSets only after backups are verified.
   - Do not delete PVCs unless the selected restore path requires the same PVC
     names and the verified backups/snapshots already exist.
5. Apply new infrastructure.
   - Apply NodePools.
   - Apply platform manifests.
   - Confirm new PVC sizes and target NodePool placement.
6. Restore data to new PVCs.
   - Postgres: restore dump or snapshot, then run migrations idempotently.
   - ClickHouse: restore snapshot or backup, then apply only needed additive DDL.
   - GraphDB: restore the full tar archive to `/opt/graphdb/home`.
   - Redis: restore preserved state, or explicitly start with approved empty
     cache-only namespaces.
   - Kafka: restore logs/offsets if preservation is required, or re-run topic
     init only after reset approval.
7. Validate stateful services.
   - Run service health checks.
   - Compare row counts, repository size, key counts, topics, and offsets.
   - Stop and roll back before app recovery if critical validation fails.
8. Restore application workloads.
   - Restore backend and agent orchestrator first.
   - Restore market/news/order workers.
   - Restore frontend last.
   - Resume CronJobs.
9. Retain rollback artifacts.
   - Keep old PVCs, EBS snapshots, and object backups through the agreed
     retention window.
   - Delete old PVCs only after the new platform has passed validation and the
     operator approves cleanup.

## Validation

Node placement:

- Every pod is on the intended `karpenter.sh/nodepool`.
- Stateful pods are not co-located outside the target policy.

PVCs:

- ClickHouse: `50Gi` or larger
- Kafka: `30Gi` or larger
- GraphDB: `10Gi` or larger
- Redis: `10Gi` or larger
- Postgres: `10Gi` or larger

Service checks:

- Kafka topic list succeeds.
- Redis `PING` succeeds.
- Postgres `pg_isready` succeeds.
- Postgres migrations complete without dropping existing data.
- GraphDB `/repositories/nasdaq-fibo/size` succeeds.
- ClickHouse `/ping` succeeds.
- backend `/health` succeeds.
- frontend `/healthz` succeeds.

Data checks:

- Postgres critical table counts match the pre-backup manifest.
- ClickHouse critical table counts match the pre-backup manifest, or documented
  rebuildable projections are regenerated from their official source.
- GraphDB repository size is non-zero and close to the pre-backup value.
- Redis preserved namespaces have expected key counts and sample keys.
- Kafka preserved topics and consumer groups match the pre-backup inventory.

Operations checks:

- GitHub Actions rollout time is recorded.
- Job TTL cleanup works.
- `chart-derived-data-worker` CrashLoop, if still present, is tracked as a
  separate incident and not hidden inside the rebuild.
- `recommendation-worker` and `alert-evaluator` stay at 0 replicas in the AWS
  in-cluster overlay until the deployed API image contains
  `app.recommendations.worker` and `app.alerts.evaluator`.

## Rollback

Rollback remains available until the retention window expires.

- Keep old PVCs when the no-collision restore path is used.
- Keep EBS snapshots even when same-name PVC replacement is required.
- Keep logical/object backups until the operator signs off.
- If restore validation fails, keep application workloads scaled down and restore
  the previous PVC or snapshot before resuming traffic.

## Assumptions

- DB data loss is not allowed.
- Old PVC direct reuse is avoided to prevent partially broken state from being
  carried forward.
- New PVCs must be restored from verified backups before service recovery.
- Redis and Kafka resets are not default behavior. They require explicit,
  component-level approval.
- AWS vCPU quota approval is required before applying the dedicated NodePool
  layout.
- All new nodes are on-demand.
