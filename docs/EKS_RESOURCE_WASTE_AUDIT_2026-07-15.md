# EKS 자원 낭비 및 Right-sizing 점검 보고서

- 점검 시각: 2026-07-15 23:00~23:45 KST
- 실행 완료 확인 시각: 2026-07-16 00:27 KST
- 대상 AWS 계정: `<aws-account-id>`
- 리전: `ap-northeast-2` (서울)
- 클러스터: `gops-eks-cluster`
- 점검 방식: AWS API, Kubernetes API, Metrics Server, EC2 CloudWatch 지표를 사용한 읽기 전용 점검
- 변경 여부: **사용자 승인 후 패키지 A~E 적용 완료**

이 문서는 특정 시점의 운영 점검 결과이며, 배포 계약의 source of truth를 대체하지 않는다.
실제 변경 시에는 현재 코드, `docs/AGENT_AWS_BUILD.md`,
`docs/EKS_DATA_PRESERVING_REBUILD_PLAN.md`를 다시 확인해야 한다.

## 0. 실행 결과 요약

승인된 작업을 백업과 서비스별 검증을 거쳐 적용했다.

| 항목 | 변경 전 | 변경 후 |
| --- | ---: | ---: |
| 상시 노드 | 10 | **9** |
| 상시 vCPU | 32 | **20** |
| 상시 물리 메모리 | 124 GiB | **92 GiB** |
| 계산 범위 월간 비용 | $1,529.94 | **약 $1,018.51** |
| 계산 범위 월간 절감 | - | **약 $511.43 (33.4%)** |

따라서 사용자가 질문한 총 메모리 절감량은 계획값과 같은 **32 GiB**, 비율로는
**25.8%**다. 동적 `batch`가 실행될 때는 일시적으로 16 GiB 노드가 추가되며 Job 종료 후
다시 scale-to-zero된다.

적용 후 월간 비용 산식은 다음과 같다.

| 항목 | 월간 추정 |
| --- | ---: |
| 5 × `m5a.large` | $433.33 |
| 2 × `r5a.large` | $222.39 |
| 1 × `m5a.xlarge` | $173.33 |
| 1 × `c5a.large` | $70.31 |
| EKS control plane | $73.00 |
| gp3 약 506 GiB | $46.15 |
| **합계** | **$1,018.51** |

gp3 506 GiB는 기존 보고서와 같은 모델로 계산한 Bottlerocket boot 약 36 GiB,
NodeClass ephemeral 360 GiB, PVC 110 GiB의 합이다. 실제 청구서에서는 EKS Auto Mode의
service-owned node volume 표기와 사용 시간 경계 때문에 소폭 차이가 날 수 있다.

적용 내용:

1. `alfaka-chart-derived-data-worker` 2→0, 실패 중인 order-flow CronJob suspend
2. reminder를 `app-agent`, 무거운 예약 작업과 one-shot migration을 동적 `batch`로 이동
3. 정적 `batch-warm` NodePool과 8 GiB 상시 메모리 제거
4. `cache-db`와 `graphdb`를 `r5a.large`, `streaming`을 `m5a.large`로 변경
5. ClickHouse CPU request 4→3.5 vCPU, 노드를 `m5a.2xlarge`→`m5a.xlarge`로 변경
6. 앱 노드 로컬 디스크 80→50 GiB, 상태 저장 노드 80→20 GiB
7. 중복 `aws-ebs-csi-driver` add-on 제거 및 Auto Mode 신규 PVC smoke test 통과

`general-purpose`는 EKS 기본 NodePool이라 이번에 custom NodeClass로 교체하지 않고 80 GiB를
유지했다. 이를 위해 별도 system NodePool과 add-on 재배치를 만드는 것은 월 약 $5.47 절감에
비해 운영 복잡도가 크기 때문이다. `czardas-asset-builder`와 외부 AWS Load Balancer
Controller도 사전 보고서의 제외 조건대로 변경하지 않았다.

### 백업과 복구 검증

- 실행 ID: `20260715-rightsize-v1`
- Postgres custom dump: 빈 임시 Postgres에 실제 복원, 사용자 테이블 33개 확인
- Redis RDB: checksum 통과, `redis-check-rdb`로 26,787 keys 확인
- GraphDB archive: 전체 압축 해제, 282.3 MiB와 `nasdaq-fibo/config.ttl` 확인
- EBS snapshot 5개: ClickHouse, Kafka, GraphDB, Redis, Postgres 모두 `completed`
- 원본 PVC는 삭제·교체·축소하지 않고 그대로 새 노드에 재attach

rollback snapshot:

```text
ClickHouse  snap-008b163660ac31808
Kafka       snap-06c8a94669baeafa3
GraphDB     snap-0543b60fff92739b3
Redis       snap-04494e3d9fa6ee424
Postgres    snap-0bfac668ca011d4f9
```

로컬 백업은 커밋 대상이 아닌 `.local-artifacts/` 아래에 있다. snapshot과 로컬 백업은
초기 관찰 기간의 rollback 자산이므로 지금 삭제하지 않았다. snapshot 저장 비용은 아래
월간 기준 비용에 포함하지 않았으며, 안정화 확인 후 별도 정리해야 한다.

### 적용 후 검증

- Deployment 36개: desired replica와 available replica 불일치 0개
- StatefulSet 5개: 모두 1/1 Ready, 지정 NodePool에 배치
- 외부 smoke: `https://stargops.com/` 200, `/api/health` 200
- 적용 후 첫 reminder Job 성공, `app-agent`에서 실행
- Kafka 핵심 group: market/quote processor, ClickHouse loader, processed S3 sink lag 0
- EBS Auto Mode: 임시 1 GiB PVC provision, attach, write/read, PV/EBS 삭제까지 통과
- Pod Pending 0, 컨테이너 restart 0

ClickHouse `chart_candles`의 `system.tables.total_rows`는 inventory보다 180행 줄었다.
이 테이블은 `ReplacingMergeTree(inserted_at)`이며, inventory 시각 이후 7개 merge의
`read_rows - rows` 합계가 정확히 180이었다. 외부 write 중지 상태에서 중복 행이 정상
정리된 것으로, PVC 교체나 데이터 손실 징후가 아니다.

`alfaka-raw-s3-archive` group에 남은 큰 lag는 현재 설정이 구독하지 않는 과거
trades/quotes topic offset이다. 현재 설정의 4개 raw archive topic 12개 partition은 active이며,
right-sizing 대상 핵심 consumer group의 lag는 모두 0이다.

## 1. 결론

현재 클러스터는 10개 노드, 32 vCPU, 124 GiB RAM을 상시 사용한다.
온디맨드 단가로 환산한 월간 기준 비용은 ALB, NAT Gateway, 데이터 전송,
CloudWatch Logs 수집 비용을 제외하고 약 **$1,530/월**이다.

승인 후 단계적으로 줄일 수 있는 현실적인 절감액은 약 **$500~510/월**이다.
현재 계산 범위의 약 33%이며, 적용 후 예상 기준 비용은 약 **$1,020~1,030/월**이다.
가장 큰 절감원은 상태 저장 전용 노드의 CPU right-sizing, `batch-warm` 상시 노드 제거,
노드 로컬 gp3 크기 축소다.

단, 아래 두 작업은 바로 하면 안 된다.

1. `app-agent` 4대를 3대로 줄이는 작업
2. 상태 저장 노드를 백업 없이 교체하는 작업

`app-agent`는 평균 CPU만 보면 여유가 있지만 6일 p95가 5.31 vCPU다.
3개 `m5a.large`의 Kubernetes allocatable CPU 합계도 5.34 vCPU이므로,
3대로 줄이면 관측 시간의 상위 5% 구간부터 거의 포화된다.

## 2. 용어

- `NodePool`: 같은 용도와 인스턴스 조건을 가진 Kubernetes 노드 묶음이다.
- `request`: Pod가 최소한 필요하다고 예약하는 자원이다. Kubernetes 스케줄러는 실제 사용량이
  아니라 이 값을 기준으로 Pod를 노드에 배치한다.
- `limit`: 컨테이너가 사용할 수 있는 자원의 상한이다.
- `allocatable`: 운영체제와 Kubernetes 자체 사용분을 제외하고 Pod에 배정할 수 있는 노드 자원이다.
- `p95`: 전체 관측값의 95%가 이 값 이하라는 뜻이다. 평균보다 피크 부하 판단에 유용하다.
- `PVC`: 데이터베이스 데이터처럼 Pod가 재시작되어도 보존해야 하는 영구 디스크 요청이다.

## 3. 측정 한계

- EC2 CPU는 2026-07-09부터 2026-07-15까지 6일을 확인했다.
- 앱 노드 동시 CPU는 5분 간격 1,704개 관측값으로 계산했다.
- 메모리는 Metrics Server의 현재 값만 있다. `ContainerInsights` 지표가 없고
  AWS Compute Optimizer도 `Inactive` 상태이므로 장기 메모리 p95는 계산할 수 없다.
- 비용은 730시간/월, 온디맨드, 세전 기준이다. Savings Plans, Reserved Instance,
  Spot, 크레딧, 약정 할인은 반영하지 않았다.
- 단가는 2026-07-15 AWS Pricing API에서 조회했다. EC2와 별도로 EKS Auto Mode 관리비가
  인스턴스별로 부과된다. AWS의 과금 구조는
  [EKS pricing](https://aws.amazon.com/eks/pricing/)과
  [EBS pricing](https://aws.amazon.com/ebs/pricing/)에서 확인할 수 있다.

## 4. 현재 인벤토리와 비용

### 4.1 노드

| NodePool | 수량/유형 | 용량 | 현재 CPU/메모리 | 6일 CPU 평균 / p95 / 최대 | 판단 |
| --- | ---: | ---: | ---: | ---: | --- |
| `app-agent` | 4 × `m5a.large` | 8 vCPU / 32 GiB | 노드별 CPU 10~23%, 메모리 48~56% | 동시 2.04 / 5.31 / 6.77 vCPU | 4대 유지 |
| `batch-warm` | 1 × `m5a.large` | 2 vCPU / 8 GiB | 6% / 12%, 점검 시 Pod 0개 | 9.88% / 44.42% / 83.02% | 상시 유지 재설계 후보 |
| `cache-db` | 1 × `m5a.xlarge` | 4 vCPU / 16 GiB | 7% / 9% | 6.88% / 16.60% / 29.61% | CPU 축소 후보 |
| `streaming` | 1 × `m5a.xlarge` | 4 vCPU / 16 GiB | 10% / 16% | 11.90% / 14.93% / 40.47% | CPU·메모리 축소 후보 |
| `graphdb` | 1 × `m5a.xlarge` | 4 vCPU / 16 GiB | 0% / 25% | 1.39% / 1.41% / 25.42% | CPU 축소 후보 |
| `clickhouse` | 1 × `m5a.2xlarge` | 8 vCPU / 32 GiB | 1% / 23% | 8.23% / 16.44% / 51.13% | 검증 후 축소 후보 |
| `general-purpose` | 1 × `c5a.large` | 2 vCPU / 4 GiB | 3% / 57% | 4.08% / 4.32% / 34.23% | 유지 |

`app-agent`의 Pod CPU request 합계는 6.275 vCPU로 allocatable 7.12 vCPU의 약 88%다.
현재는 HPA가 하나도 없고, 주문·시세 Pod에서 readiness/liveness probe timeout도 관측됐다.
따라서 앱 노드를 먼저 줄이는 접근은 위험하다.

### 4.2 월간 비용 기준선

| 항목 | 산식 | 월간 추정 |
| --- | --- | ---: |
| 5 × `m5a.large` | EC2 $0.106/h + Auto Mode $0.01272/h | $433.33 |
| 3 × `m5a.xlarge` | EC2 $0.212/h + Auto Mode $0.02544/h | $519.99 |
| 1 × `m5a.2xlarge` | EC2 $0.424/h + Auto Mode $0.05088/h | $346.66 |
| 1 × `c5a.large` | EC2 $0.086/h + Auto Mode $0.01032/h | $70.31 |
| EKS control plane | $0.10/h | $73.00 |
| gp3 950 GiB | $0.0912/GiB-month | $86.64 |
| **합계** | ALB/NAT/전송/로그 제외 | **$1,529.94** |

gp3 950 GiB는 노드 root 40 GiB, 노드 ephemeral 800 GiB, PVC 110 GiB로 구성된다.

## 5. 권고안

### 5.1 상태 저장 NodePool right-sizing

메모리를 유지하면서 CPU만 줄일 수 있는 곳은 메모리 최적화 인스턴스를 사용한다.
`r5a.large`는 2 vCPU / 16 GiB이므로 `cache-db`와 `graphdb`의 현재 메모리 용량을
유지하면서 CPU 비용을 줄일 수 있다.

| 대상 | 현재 request / 실제 사용 | 제안 | 예상 절감/월 | 위험도 |
| --- | --- | --- | ---: | --- |
| `cache-db` | 0.55 vCPU / 3.5 GiB request, CPU p95 약 0.66 vCPU, Pod 메모리 약 0.61 GiB | `m5a.xlarge` → `r5a.large` | $62.14 | 중간 |
| `graphdb` | 0.5 vCPU / 6 GiB request, CPU p95 약 0.06 vCPU, 메모리 3.1 GiB | `m5a.xlarge` → `r5a.large` | $62.14 | 중간 |
| `streaming` | 1 vCPU / 3 GiB request, CPU p95 약 0.60 vCPU, 최대 약 1.62 vCPU, Kafka 메모리 2.0 GiB | `m5a.xlarge` → `m5a.large` | $86.67 | 중간 |
| `clickhouse` | 4 vCPU / 6 GiB request, CPU p95 약 1.32 vCPU, 5분 최대 약 4.09 vCPU, 메모리 6.3 GiB | CPU request를 3.5 이하로 조정 후 `m5a.2xlarge` → `m5a.xlarge` | $173.33 | 높음 |
| **합계** |  |  | **$384.28** |  |

ClickHouse는 최대 관측치가 목표 인스턴스의 4 vCPU와 거의 같아 별도 단계로 진행해야 한다.
쿼리 지연, 적재 lag, 메모리, merge 부하를 관찰하고 실패 시 즉시 기존 NodePool로 되돌린다.

Postgres, Redis, Kafka, GraphDB, ClickHouse는 상태 저장 서비스다. 노드 변경 전에
`docs/EKS_DATA_PRESERVING_REBUILD_PLAN.md`의 백업·복구 검증 절차를 따라야 한다.

### 5.2 `batch-warm` 제거와 CronJob 재배치

점검 시 `batch-warm` 노드에는 실행 중인 Pod와 request가 모두 0이었다. 그러나 다음 작업이
이 노드를 사용한다.

- 5분마다 실행되는 `gops-market-reminder-notifications`
- 매일 실행되는 SEC fundamentals sync
- 평일 order-flow rollup
- 평일 chart geometry enqueue

단순히 이들을 동적 `batch` NodePool로 옮기면 5분 주기의 reminder 때문에 노드가 내려가지
않을 수 있다. 다음과 같이 함께 바꿔야 한다.

1. reminder는 stale workload를 먼저 정리해 여유를 만든 뒤 `app-agent`로 이동한다.
2. SEC/order-flow/geometry 작업은 scale-from-zero가 가능한 동적 `batch`로 이동한다.
3. scale-up 시간을 고려해 `activeDeadlineSeconds`를 검증한다.
4. 정적 `batch-warm`의 `spec.replicas: 1`을 제거하는 대신 새 동적 풀로 마이그레이션한다.

현재 문서는 scale-from-zero 대기 때문에 `batch-warm`을 한 대 유지하도록 명시한다.
따라서 이 변경을 승인하면 코드뿐 아니라 `docs/AGENT_AWS_BUILD.md`와 관련 runbook도
같이 갱신해야 한다.

- 상시 노드 제거의 gross 절감: 약 **$94.33/월**
- 동적 batch 실행 비용을 뺀 예상 net 절감: 약 **$80~90/월**

### 5.3 노드 ephemeral gp3 축소

현재 공통 `NodeClass/default`는 모든 노드에 80 GiB ephemeral gp3를 할당한다.
이는 Pod 이미지, 컨테이너 임시 파일, 로그 등에 쓰이는 노드 로컬 디스크다.
EKS Auto Mode는 [NodeClass](https://docs.aws.amazon.com/eks/latest/userguide/create-node-class.html)로
풀별 저장공간 크기를 다르게 지정할 수 있다.

관측 사용량은 다음과 같다.

- 상태 저장/시스템 노드: 약 2.4~4.3 GiB
- 앱 노드: 약 11.5~26.9 GiB
- `batch-warm`: 약 6.2 GiB

권고값은 앱 50 GiB, 상태 저장·시스템 20 GiB다. 앱 노드의 최대 사용량이 26.9 GiB이므로
40 GiB보다 50 GiB가 image cache와 eviction 여유를 더 확보한다.

`batch-warm` 제거 후 남는 9개 노드 기준으로 420 GiB를 줄여 약 **$38.30/월**을 절감한다.
기본 `NodeClass/default`는 직접 고치지 않고 용도별 custom NodeClass를 새로 만들어야 한다.
이 변경도 노드 교체를 일으키므로 상태 저장 백업 절차와 같이 실행한다.

### 5.4 현재 `dev` 계약과 다른 실행 중 workload

#### `alfaka-chart-derived-data-worker`

- 2 replicas
- 합계 request: 100m CPU / 256 MiB
- 현재 실제 사용: 약 10m CPU / 92 MiB
- Kafka topic의 3개 파티션 누적 메시지: 총 4건
- 현재 lag: 0
- 최근 24시간 로그: 없음
- 현재 `dev` 코드와 문서에는 별도 derived worker/queue가 없다고 명시

권고: 승인 후 replicas를 0으로 낮추고 24시간 관찰한 뒤 삭제한다. 정적 앱 노드 4대가
유지되는 동안 직접 비용 절감은 없지만 request를 회수하고 배포 계약을 일치시킨다.

#### `czardas-asset-builder`

- 1 replica
- request: 250m CPU / 512 MiB
- 현재 실제 사용: 약 9m CPU / 94 MiB
- 최근 24시간 로그: 없음
- 현재 `dev`에는 없지만 `origin/ABC` 브랜치의 commit `2142fe80` 이미지로 2026-07-14 배포됨

이 workload는 단순 stale로 단정할 수 없다. ABC 기능 테스트가 끝났다는 확인을 받은 뒤에만
scale-to-zero 한다. 사용자 확인 전에는 건드리지 않는다.

`chart-asset-builder`는 이름이 비슷하지만 현재 아키텍처에 존재하는 정상 optional runtime이다.
Kafka가 아니라 PostgreSQL queue를 polling하므로 Kafka active member가 없는 것은 제거 근거가 아니다.

### 5.5 실패 작업 정리

`alfaka-order-flow-daily-rollup`은 최근 실행에서 다음 오류로 매번 3회 실패했다.

```text
python: can't open file '/app/systems/market-data/jobs/order-flow-daily-rollup/main.py'
```

성공할 수 없는 동일 image로 평일마다 재시도하는 것은 작은 규모지만 명확한 낭비이며,
데이터 신뢰성 문제이기도 하다. 승인 후 올바른 image/path를 먼저 배포하거나, 수정될 때까지
CronJob을 suspend해야 한다. 이 항목의 절감액은 매우 작아 총 절감액에는 포함하지 않았다.

### 5.6 EKS Auto Mode와 중복된 add-on

클러스터에는 `aws-ebs-csi-driver` add-on과 controller Pod 2개가 실행 중이다.
하지만 현재 PVC 5개는 모두 Auto Mode 전용 driver인 `ebs.csi.eks.amazonaws.com`을 사용하고,
EC2 managed node group도 0개다. AWS는 Auto Mode 전용 클러스터에서 VPC CNI, kube-proxy,
CoreDNS, EBS CSI Driver 같은 add-on 다수가 중복될 수 있다고 설명한다.
관련 내용은 [EKS add-ons의 Auto Mode 고려사항](https://docs.aws.amazon.com/eks/latest/userguide/eks-add-ons.html)에서
확인할 수 있다.

권고: EBS CSI controller의 표준 driver `ebs.csi.aws.com`을 쓰는 PV가 없음을 배포 직전에
다시 확인한 뒤 add-on을 제거한다. 회수되는 request는 약 120m CPU / 464 MiB이며,
현재 실제 사용은 약 8m CPU / 127 MiB다. `general-purpose` 노드 한 대를 없앨 정도는 아니므로
직접 비용 절감액은 0으로 본다.

`vpc-cni`와 `kube-proxy` DaemonSet은 현재 desired Pod가 0이라 직접 컴퓨트 비용이 없다.
CoreDNS와 metrics-server는 이번 점검에서 유지한다.

별도 `aws-load-balancer-controller` 2개도 Auto Mode 내장 load balancer 기능과 중복 가능성이
있다. 그러나 현재 인터넷 ALB의 소유권과 finalizer를 확인하지 않고 제거하면 서비스가
중단될 수 있으므로 이번 승인 후보에서는 제외한다.

### 5.7 EBS PVC

| PVC | 할당 | 사용 | 판단 |
| --- | ---: | ---: | --- |
| ClickHouse | 50 GiB | 28.7 GiB, 59% | 유지 |
| Kafka | 30 GiB | 1.4 GiB, 5% | 다음 데이터 마이그레이션 때만 재검토 |
| Redis | 10 GiB | 1.2 GiB, 12% | 유지 |
| GraphDB | 10 GiB | 0.28 GiB, 3% | 유지 |
| Postgres | 10 GiB | 0.11 GiB, 1% | 유지 |

EBS는 온라인 확장은 가능하지만 제자리 축소는 지원하지 않는다. 새 볼륨 생성과 데이터
마이그레이션이 필요한데, 네 개 저사용 PVC를 모두 줄여도 절감액이 월 수 달러 수준이다.
위험 대비 이익이 작아 이번 작업에서 제외한다.

### 5.8 비용이 발생하지 않는 stale 객체

`gops-dev` namespace에는 replicas가 0인 이전 Deployment 14개와 관련 ClusterIP Service가 있다.
replicas 0 Deployment와 ClusterIP Service는 EC2/ELB 비용을 만들지 않는다. 정리하면 가독성은
좋아지지만 비용 절감은 없으므로 후순위다.

## 6. 유지해야 할 항목

- `app-agent` 4대: 6일 p95와 최대 CPU 때문에 유지
- `general-purpose` 1대: 시스템 Pod 메모리 사용률이 57%라 유지
- market processor, quote processor, tick loader 각 3 replicas: Kafka 3개 파티션을 각각 담당
- ClickHouse 50 GiB PVC: 이미 59% 사용
- NAT Gateway 1개: 중복 없음
- 인터넷 ALB 1개: 현재 `stargops.com` ingress가 사용
- 연결이 끊긴 EKS 태그 EBS 볼륨: 0개

## 7. 부가 관찰

- EKS control plane 로그 그룹이 약 5.68 GB이며 retention이 무기한이다. 30일 retention을
  설정하면 무제한 증가를 막을 수 있으나 절감액은 작다.
- API endpoint public access CIDR가 `0.0.0.0/0`이다. 비용 문제는 아니지만 운영 보안 관점에서
  관리자 CIDR 제한 또는 private endpoint 중심 운영을 별도 검토해야 한다.
- 주문/시세 관련 Pod에서 probe timeout 경고가 반복된다. right-sizing 전에 이 지연이
  CPU 포화인지 1초 probe timeout 설정 문제인지 구분해야 한다.
- HPA와 애플리케이션 PDB가 없다. 앱 계층을 동적으로 줄이려면 먼저 autoscaling과 disruption
  계약을 설계해야 한다.

## 8. 승인 단위와 권장 실행 순서

### 패키지 A — 가역적 정리

1. `alfaka-chart-derived-data-worker`를 0으로 낮춤
2. 24시간 동안 API 오류, Kafka lag, 관련 기능 회귀 관찰
3. 이상 없으면 live 객체와 오래된 manifest 잔여물 삭제
4. 실패 중인 order-flow CronJob은 image/path 수정 또는 임시 suspend

예상 직접 절감은 거의 없지만 이후 request 재배치의 전제다.

### 패키지 B — batch와 ephemeral storage

1. reminder를 `app-agent`로 이동
2. 무거운 CronJob을 동적 `batch`로 이동하고 deadline 검증
3. `batch-warm` 제거
4. custom NodeClass를 만들고 이 단계에서는 stateless 앱 50 GiB와 시스템 20 GiB만 적용
5. 상태 저장 풀의 20 GiB 적용은 패키지 C/D의 백업된 노드 교체와 함께 수행

예상 net 절감: **$96~106/월**

### 패키지 C — 상태 저장 right-sizing 1차

1. 백업과 복구 검증
2. `graphdb`: `m5a.xlarge` → `r5a.large`
3. `cache-db`: `m5a.xlarge` → `r5a.large`
4. `streaming`: `m5a.xlarge` → `m5a.large`
5. 각 풀의 NodeClass ephemeral storage를 같은 교체에서 80 GiB → 20 GiB로 변경
6. 각 단계마다 상태, latency, lag, 메모리를 확인하고 다음 단계 진행

예상 절감: **$227.37/월**

### 패키지 D — ClickHouse 별도 canary

1. 백업과 restore 검증
2. CPU request를 4에서 3.5 이하로 조정
3. NodeClass ephemeral storage를 80 GiB → 20 GiB로 변경
4. 비장중에 `m5a.xlarge`로 교체
5. query latency, insert latency, Kafka lag, merge, memory 확인
6. 기준 초과 시 즉시 `m5a.2xlarge`로 rollback

예상 절감: **$178.80/월**

### 패키지 E — 중복 add-on 정리

1. 표준 `ebs.csi.aws.com` PV가 0인지 재확인
2. EBS CSI add-on 제거
3. PVC attach/detach와 신규 test PVC 검증
4. 외부 AWS Load Balancer Controller는 별도 소유권 마이그레이션 계획이 생기기 전까지 유지

직접 비용 절감은 없지만 시스템 노드 request와 운영 중복을 줄인다.

## 9. 최종 권고

가장 안전한 순서는 **A → B → C → D → E**다. 패키지 B와 C만으로도 약
**$323~333/월**을 절감할 수 있다. ClickHouse는 가장 큰 단일 절감 후보지만 최대 CPU가
목표 인스턴스 용량에 근접했으므로 반드시 별도 canary로 진행한다.

승인된 A~E는 적용 완료했다. 다음 운영 확인은 미국 장중 ClickHouse query/insert latency,
Kafka lag, 노드 메모리와 eviction을 최소 24시간 관찰하는 것이다. 이상이 있으면 위 snapshot과
Git의 기존 NodePool manifest로 서비스별 rollback한다. 24시간 이상 이상이 없을 때만
`alfaka-chart-derived-data-worker` live 객체 삭제와 rollback snapshot 보존 기간 종료를 검토한다.
