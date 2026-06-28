# DevOps 작업 명세서

이 문서는 제공된 AWS 아키텍처 다이어그램을 기준으로, DevOps 담당자가 인프라 구축, Kubernetes 배포, 보안, 관측, 운영 자동화를 진행하기 위한 작업 명세입니다.

현재 아키텍처는 아직 논리 아키텍처 수준이므로, 실제 구현 전에 아래의 `미확정 항목`을 제품/백엔드/데이터 담당자와 확정해야 합니다.

## 1. 아키텍처 한 줄 요약

AWS 서울 리전(`ap-northeast-2`)의 VPC 안에 2개 Availability Zone을 두고, 퍼블릭 서브넷에는 ALB와 NAT Gateway를, 프라이빗 앱 서브넷에는 EKS Node Group을, 프라이빗 데이터 서브넷에는 RDS PostgreSQL, EC2 self-managed ClickHouse, Redis, Ontotext GraphDB, Amazon MSK를 배치하는 구조입니다.

운영 방식은 2AZ active/passive로 잡습니다. 평상시에는 1개 AZ를 active 운영 영역으로 쓰고, 나머지 1개 AZ는 AZ 장애 시 복구와 failover를 위한 standby 영역으로 둡니다. 현재 단계에서는 비용을 낮추기 위해 standby와 ClickHouse를 저비용 복구 대기 방식으로 두고, 최종 시연 전 확실한 장애 전환 방식으로 강화합니다.

사용자 트래픽은 `Route 53 -> Internet Gateway -> ALB -> EKS 서비스` 흐름으로 들어오고, 애플리케이션 Pod는 ECR, S3, 외부 API, 데이터 저장소, Kafka와 통신합니다.

## 2. 처음 보는 구성요소 설명

이 섹션은 각 기술을 처음 쓰는 사람 기준으로 짧게 정리합니다.

| 구성요소 | 무엇인가 | 이 코드/인프라에서 왜 필요한가 | 기본 사용 형태 |
| --- | --- | --- | --- |
| AWS | Amazon Web Services입니다. 서버, 네트워크, 데이터베이스, 보안 서비스를 빌려 쓰는 클라우드 플랫폼입니다. | 물리 서버를 직접 운영하지 않고 EKS, VPC, ECR, S3 같은 관리형 리소스를 사용하기 위해 필요합니다. | 콘솔, AWS CLI, Terraform 같은 IaC 도구로 리소스를 생성합니다. |
| VPC | Virtual Private Cloud입니다. AWS 안에서 만드는 전용 사설 네트워크입니다. | 앱 서버, DB, Kafka를 인터넷에서 직접 노출하지 않고 내부 네트워크로 묶기 위해 필요합니다. | CIDR 예: `10.0.0.0/16`, 그 안에 public/private subnet을 나눕니다. |
| Availability Zone | 하나의 AWS Region 안에 있는 독립 데이터센터 구역입니다. | 이 아키텍처에서는 2개 AZ를 두되, 평상시에는 1개 AZ를 active로 쓰고 다른 1개 AZ는 장애 복구용 standby로 둡니다. | 예: `ap-northeast-2a`를 active, `ap-northeast-2c`를 standby로 지정합니다. |
| Public Subnet | 인터넷으로 라우팅 가능한 서브넷입니다. | ALB와 NAT Gateway처럼 외부 통신이 필요한 리소스를 둡니다. | Route Table이 Internet Gateway로 향하는 경로를 가집니다. |
| Private Subnet | 인터넷에서 직접 접근할 수 없는 서브넷입니다. | EKS 노드와 데이터 저장소를 외부 노출 없이 운영하기 위해 필요합니다. | 외부로 나갈 때는 NAT Gateway 또는 VPC Endpoint를 사용합니다. |
| Internet Gateway | VPC와 인터넷을 연결하는 AWS 네트워크 게이트웨이입니다. | 사용자가 퍼블릭 ALB로 접속할 수 있게 합니다. | VPC에 붙이고 public route table에서 `0.0.0.0/0` 대상으로 지정합니다. |
| NAT Gateway | 프라이빗 서브넷의 리소스가 인터넷으로 나갈 수 있게 하는 AWS 관리형 게이트웨이입니다. | EKS Pod가 외부 API나 패키지 저장소에 접근하되, 외부에서 Pod로 직접 들어오지 못하게 합니다. | Public subnet에 만들고 private route table의 기본 경로로 지정합니다. |
| Route 53 | AWS의 DNS 서비스입니다. DNS는 사람이 읽는 도메인을 IP나 로드밸런서 주소로 바꾸는 시스템입니다. | `api.example.com` 같은 도메인을 ALB에 연결하기 위해 필요합니다. | Hosted Zone 생성 후 A/AAAA Alias 레코드를 ALB로 연결합니다. |
| ALB | Application Load Balancer입니다. HTTP/HTTPS 요청을 여러 대상에 나눠주는 AWS 로드밸런서입니다. | 사용자 요청을 EKS 내부 서비스로 안정적으로 전달하고 TLS 인증서를 붙이기 위해 필요합니다. | AWS Load Balancer Controller가 Kubernetes Ingress를 읽어 ALB를 생성합니다. |
| EKS | Elastic Kubernetes Service입니다. Kubernetes Control Plane을 AWS가 관리해주는 서비스입니다. Kubernetes는 컨테이너 앱을 배포, 복구, 확장하는 플랫폼입니다. | React, FastAPI, WebSocket Gateway 등 여러 서비스를 Pod 단위로 운영하기 위해 필요합니다. | EKS Cluster 생성 후 Node Group을 붙이고 `kubectl`로 워크로드를 배포합니다. |
| EKS Node Group | EKS에서 Pod가 실제로 실행되는 EC2 노드 묶음입니다. | 애플리케이션 컨테이너를 실행할 컴퓨팅 자원이 필요합니다. | Managed Node Group 또는 Karpenter로 노드를 자동 확장합니다. |
| EC2 | Elastic Compute Cloud입니다. AWS에서 빌려 쓰는 가상 서버입니다. vCPU, memory, EBS disk 같은 컴퓨터 사양을 골라 실행합니다. | EKS Worker Node, ClickHouse, Ontotext GraphDB처럼 직접 서버 자원이 필요한 컴포넌트를 실행하기 위해 사용합니다. | 예: `t3.large`, `m7i.large`, `r7i.large` 같은 instance type을 선택하고 private subnet에 배치합니다. |
| Auto Scaling Group | EC2 인스턴스 여러 대를 하나의 그룹으로 묶고, 원하는 대수만큼 유지하거나 자동으로 늘리고 줄이는 AWS 기능입니다. | EKS Node Group 뒤에서 Worker Node 개수를 유지하고, 장애 난 EC2를 교체하며, Pod가 늘어날 때 노드를 추가하기 위해 필요합니다. | `min`, `desired`, `max` 용량을 정하고, Launch Template과 scaling policy를 연결합니다. |
| Pod | Kubernetes에서 배포되는 가장 작은 실행 단위입니다. 보통 하나의 컨테이너가 들어갑니다. | Frontend Server, API Server, Trading Service 같은 각 서비스를 실행합니다. | Deployment가 Pod 복제본 수와 업데이트 방식을 관리합니다. |
| ECR | Elastic Container Registry입니다. Docker/OCI 컨테이너 이미지를 저장하는 AWS 저장소입니다. | CI에서 빌드한 이미지들을 EKS가 가져와 실행하기 위해 필요합니다. | `docker build`, `docker push`, Kubernetes image 필드에서 ECR 이미지 URI 사용. |
| ECR VPC Endpoint | VPC 안에서 ECR에 사설망으로 접근하게 해주는 엔드포인트입니다. | EKS 노드가 인터넷을 거치지 않고 이미지를 pull하도록 비용과 보안을 개선합니다. | Interface Endpoint를 `ecr.api`, `ecr.dkr`에 생성합니다. |
| S3 | Simple Storage Service입니다. 파일을 객체 형태로 저장하는 AWS 서비스입니다. | 로그, 데이터 레이크 원본, 백업, 정적 산출물 저장에 사용합니다. | 버킷을 만들고 IAM 권한으로 `GetObject`, `PutObject`를 제어합니다. |
| S3 VPC Endpoint | VPC 내부에서 S3로 사설 경로 접근을 제공하는 엔드포인트입니다. | NAT 비용 없이 EKS나 데이터 작업이 S3에 접근하도록 합니다. | Gateway Endpoint를 생성하고 private route table에 연결합니다. |
| Glue Data Catalog | AWS의 메타데이터 카탈로그입니다. 데이터 파일이 어떤 테이블/컬럼 구조인지 저장합니다. | S3에 쌓인 데이터를 Athena에서 SQL처럼 조회하기 위해 필요합니다. | Database/Table을 만들고 S3 경로와 스키마를 연결합니다. |
| Athena | S3 데이터를 서버 없이 SQL로 조회하는 AWS 분석 서비스입니다. | 저장된 로그/시장 데이터/분석 데이터를 운영자가 조회하고 검증하기 위해 사용합니다. | SQL 쿼리를 실행하고 결과를 S3에 저장합니다. |
| IAM | AWS 권한 관리 서비스입니다. 어떤 주체가 어떤 리소스에 어떤 행동을 할 수 있는지 정합니다. | Pod, CI, 운영자, 노드 권한을 최소 권한으로 나누기 위해 필요합니다. | Role과 Policy를 만들고 필요한 AWS API 권한만 부여합니다. |
| Secrets Manager | 비밀번호, API Key 같은 민감정보를 저장하는 AWS 서비스입니다. | OpenAI, Alpaca, 한국투자증권, DB 비밀번호 등을 코드나 이미지에 넣지 않기 위해 필요합니다. | Secret을 만들고 Pod는 External Secrets Operator 또는 CSI Driver로 주입받습니다. |
| EKS Pod Identity | EKS Pod에 AWS IAM Role을 연결하는 기능입니다. | Pod마다 필요한 AWS 권한만 주기 위해 필요합니다. 예를 들어 Market Data Ingestor만 S3 쓰기 권한을 받게 합니다. | ServiceAccount와 IAM Role을 연결하고 Pod가 해당 ServiceAccount를 사용합니다. |
| PostgreSQL | 관계형 데이터베이스입니다. 테이블, 행, SQL을 기반으로 데이터를 저장합니다. | 사용자, 주문, 거래 기록처럼 정합성이 중요한 데이터를 저장합니다. | 운영에서는 RDS PostgreSQL을 사용합니다. |
| ClickHouse | 컬럼 기반 분석 데이터베이스입니다. 대량 로그/시계열/분석 쿼리에 강합니다. | 시장 데이터, 차트, 집계 분석을 빠르게 조회하기 위해 사용합니다. | 운영에서는 EC2 self-managed를 1차안으로 선택합니다. |
| Redis | 메모리 기반 key-value 저장소입니다. 매우 빠른 캐시/세션/큐 용도로 씁니다. | API 캐시, WebSocket 상태, 짧은 TTL 데이터 저장에 사용합니다. | 운영에서는 ElastiCache Redis 또는 Valkey를 우선 검토합니다. |
| Kafka | 이벤트 스트리밍 플랫폼입니다. Producer가 메시지를 쓰고 Consumer가 순서대로 읽는 로그형 메시지 브로커입니다. | 시장 데이터 수집, 정규화, Flink 처리, API 반영을 느슨하게 연결하기 위해 필요합니다. | 운영에서는 Amazon MSK를 사용합니다. |
| Amazon MSK | Managed Streaming for Apache Kafka의 줄임말입니다. AWS가 Kafka broker 운영, 패치, 모니터링 연동을 관리해주는 서비스입니다. | Kafka를 직접 EKS나 EC2에 설치하지 않고, 운영 부담을 줄이면서 이벤트 스트리밍을 사용하기 위해 필요합니다. | MSK cluster를 private subnet에 만들고, 애플리케이션은 bootstrap broker 주소로 접속합니다. |
| Flink | 실시간 스트림 처리 엔진입니다. 들어오는 이벤트를 계속 읽어 정규화, 집계, 변환합니다. | Kafka의 시장 데이터를 가공해서 ClickHouse/PostgreSQL/Redis 등에 반영하기 위해 사용합니다. | 운영에서는 Flink Kubernetes Operator 또는 Amazon Managed Service for Apache Flink를 검토합니다. |
| FastAPI | Python으로 API 서버를 빠르게 만드는 웹 프레임워크입니다. | 아키텍처의 API Server 구현체로 보이며, 프론트엔드/서비스/외부 요청의 중심 진입점 역할을 합니다. | `uvicorn` 또는 `gunicorn`으로 실행하고 `/health` 같은 HTTP 엔드포인트를 제공합니다. |
| React | 브라우저 UI를 컴포넌트 단위로 만드는 JavaScript 라이브러리입니다. | 아키텍처의 Frontend Server가 React 앱을 제공하는 역할로 보입니다. | 이 아키텍처에서는 EKS의 Frontend Server Pod가 빌드된 React 정적 파일을 서빙합니다. |
| WebSocket | 클라이언트와 서버가 연결을 오래 유지하며 양방향 통신하는 프로토콜입니다. | 실시간 가격, 차트, 트레이딩 상태를 사용자에게 밀어주기 위해 필요합니다. | ALB idle timeout, sticky session 필요 여부, Pod drain 정책을 별도로 설계해야 합니다. |
| JWT | JSON Web Token입니다. 로그인에 성공한 사용자 정보를 서명된 토큰으로 담아 API 요청마다 검증하는 인증 방식입니다. | Cognito 같은 외부 인증 서비스 없이 API Server와 WebSocket Gateway에서 사용자 인증을 처리하기 위해 사용합니다. | `Authorization: Bearer <access_token>` 헤더로 전달하고, 서버는 서명/만료시간/권한 claim을 검증합니다. |
| Alpaca Algo Trader Plus API | Alpaca는 주식 시장 데이터 API를 제공하는 외부 데이터 공급자입니다. Algo Trader Plus는 실시간 시장 데이터 사용량을 제공하는 플랜으로 봅니다. | MVP에서는 거래 provider가 아니라 시장 데이터 수집용 외부 API로만 사용합니다. | API key/secret을 Secrets Manager에 저장하고 Market Data Ingestor가 HTTPS/WebSocket으로 호출합니다. |
| Alpaca News API | Alpaca가 제공하는 뉴스 데이터 API입니다. 종목이나 시장과 관련된 뉴스 데이터를 조회할 때 사용합니다. | 뉴스 수집은 MVP 제외이며 후속 단계에서 검토합니다. | 후속 단계에서 사용할 경우 Alpaca API key/secret을 그대로 사용하고, 뉴스 조회 endpoint를 호출합니다. |
| 한국투자증권 모의투자 API | 한국투자증권이 제공하는 국내 주식/해외 주식 거래 API의 모의투자 환경입니다. 실제 돈이 움직이지 않는 테스트 계좌로 주문/체결 흐름을 검증할 수 있습니다. | 운영 전 주문 로직, 체결 처리, 장애 대응을 안전하게 검증하기 위해 필요합니다. | app key/app secret/account number를 Secret으로 관리하고 paper trading 모드에서만 사용합니다. |
| Ontotext GraphDB | RDF 그래프 데이터베이스입니다. RDF는 데이터를 `주어-술어-목적어` 관계로 표현하는 표준 방식입니다. | Ontology Service가 금융 개념, 종목, 지표, 이벤트 사이의 관계를 조회하고 추론하는 데 사용합니다. | EC2에 배포하고 SPARQL 엔드포인트를 내부망에서만 열어 사용합니다. |
| FIBO | Financial Industry Business Ontology입니다. 금융 도메인의 표준 개념과 관계를 RDF/OWL 온톨로지로 정리한 모델입니다. | Ontotext GraphDB에 적재해 금융 개념의 공통 vocabulary로 사용합니다. | FIBO ontology 파일을 GraphDB repository에 import하고 Ontology Service가 SPARQL로 조회합니다. |

## 3. 아키텍처 기준 서비스 목록

| 서비스 | 예상 역할 | 실행 위치 | 외부 의존성 | 운영 포인트 |
| --- | --- | --- | --- | --- |
| Frontend Server | React UI 제공 | EKS private app subnet | API Server, WebSocket Gateway | EKS에서 직접 서빙. 정적 파일 캐싱 정책 필요 |
| API Server | HTTP API 중심 서버 | EKS private app subnet | PostgreSQL, Redis, ClickHouse, AI Agents, Trading Service, 외부 API | HPA 기준은 CPU보다 request latency/RPS 우선 검토 |
| WebSocket Gateway | 실시간 연결 관리 | EKS private app subnet | Redis, Kafka 또는 API Server | connection drain, idle timeout, scale-out 세션 전략 필요 |
| AI Agents Service | LLM 기반 작업 수행 | EKS private app subnet | OpenAI LLM API, API Server | LLM 고도화와 OpenAI 비용 최적화는 MVP 이후 단계 |
| Ontology Service | 도메인 지식/관계 모델 제공 | EKS private app subnet | Ontotext GraphDB, FIBO, AI Agents | 온톨로지와 GraphRAG는 MVP 제외, 후속 단계에서 배포 |
| Trading Service | 거래/전략 실행 | EKS private app subnet | PostgreSQL, Redis, ClickHouse, KIS Broker Adapter | 장애 시 중복 주문 방지와 idempotency 필요 |
| KIS Broker Adapter | KIS 모의투자 주문 API 호출 | EKS private app subnet | PostgreSQL, Kafka, 한국투자증권 모의투자 API | KIS secret 접근 단일화, timeout 즉시 재POST 금지 |
| Chart Builder | 차트 데이터 생성 | EKS private app subnet | ClickHouse, Redis, API Server | 무거운 쿼리 제한, 캐시 정책 필요 |
| Market Data Ingestor | 외부 시장 데이터 수집 | EKS private app subnet | Alpaca Algo Trader Plus API, Kafka | CronJob/Deployment 선택, 재시도/중복 제거 필요 |
| Flink Norm/Job/Task Manager | 스트림 정규화/집계 | EKS private app subnet 또는 전용 노드 | Kafka, ClickHouse, Redis, PostgreSQL | checkpoint 저장소, Job upgrade 전략 필요 |
| PostgreSQL | 트랜잭션 데이터 저장 | private data subnet | API/Trading/Ontology 등 | RDS PostgreSQL 사용, 백업/복구 리허설 필요 |
| ClickHouse | 분석/시계열 조회 | private data subnet | API/Chart/Flink | 샤드/레플리카/스토리지 전략 필요 |
| Redis | 캐시/세션/실시간 상태 | private data subnet | API/WebSocket/Trading | ElastiCache 권장, eviction 정책 필요 |
| Ontotext GraphDB | FIBO 기반 금융 온톨로지/지식 그래프 저장 | private data subnet | Ontology/AI Agents | repository 백업, FIBO import 절차, SPARQL 권한 관리 필요 |
| Amazon MSK | 스트리밍 이벤트 저장 | private data subnet | Ingestor/Flink/WebSocket 등 | MSK 사용 확정, topic/partition/retention 설계 필요 |

## 4. 네트워크 명세

### 4.1 VPC와 서브넷

확정 기본값입니다.

| 항목 | 권장값 | 설명 |
| --- | --- | --- |
| Region | `ap-northeast-2` | 서울 리전으로 확정 |
| VPC CIDR | `10.20.0.0/16` | 확정 |
| AZ 수 | 2개 | 1개 AZ active, 1개 AZ standby 복구용 |
| Public subnet | AZ-a `10.20.0.0/24`, AZ-b `10.20.1.0/24` | ALB, NAT Gateway 배치. standby AZ는 장애 복구용으로 준비 |
| Private app subnet | AZ-a `10.20.10.0/24`, AZ-b `10.20.11.0/24` | EKS Node Group 배치. 평상시 active AZ 중심 운영 |
| Private data subnet | AZ-a `10.20.20.0/24`, AZ-b `10.20.21.0/24` | RDS PostgreSQL, Redis, EC2 ClickHouse, Ontotext GraphDB, Amazon MSK를 함께 배치 |

### 4.2 라우팅

| 서브넷 | 기본 경로 | 목적 |
| --- | --- | --- |
| Public subnet | `0.0.0.0/0 -> Internet Gateway` | ALB와 NAT Gateway의 인터넷 연결 |
| Private app subnet | `0.0.0.0/0 -> 같은 AZ의 NAT Gateway` | 외부 API 호출과 패키지 접근 |
| Private data subnet | 기본 인터넷 경로 없음 권장 | 데이터 저장소와 Kafka 외부 노출 방지 |

NAT Gateway는 개발 단계에서 active AZ인 AZ-a에만 배치하고, 최종 시연 전 AZ-b에도 추가합니다. Private app subnet의 S3/ECR 접근은 NAT보다 VPC Endpoint를 우선 사용합니다.

### 4.3 VPC Endpoint

| Endpoint | 타입 | 필요한 이유 |
| --- | --- | --- |
| S3 | Gateway | S3 접근을 NAT 없이 처리 |
| ECR API | Interface | 이미지 메타데이터 조회 |
| ECR DKR | Interface | 컨테이너 이미지 pull |
| CloudWatch Logs | Interface | Pod/Node 로그 전송 |
| Secrets Manager | Interface | Secret 조회 |
| STS | Interface | Pod Identity/IAM Role 인증 토큰 발급 |

## 5. 보안 그룹 명세

| 보안 그룹 | Inbound | Outbound | 비고 |
| --- | --- | --- | --- |
| `sg-alb-public` | `443` from `0.0.0.0/0`, 필요 시 `80` redirect only | EKS Ingress target으로 | TLS는 ALB에서 종료 권장 |
| `sg-eks-node` | ALB에서 서비스 NodePort/Pod target port, 노드 간 통신 | DB/Redis/Kafka/외부 API/VPC Endpoint | self-managed node라면 kubelet 포트 제한 |
| `sg-postgres` | `5432` from `sg-eks-node` | 제한 | RDS PostgreSQL subnet group은 private data |
| `sg-clickhouse` | `8123/9000` from `sg-eks-node` | 제한 | 실제 포트는 배포 방식에 맞춤 |
| `sg-redis` | `6379` from `sg-eks-node` | 제한 | TLS 사용 여부 결정 |
| `sg-kafka` | `9092/9094` from `sg-eks-node` | 제한 | MSK TLS/SASL 사용 시 포트 확정 |
| `sg-graphdb` | Ontotext GraphDB 포트 from `sg-eks-node` | 제한 | SPARQL/관리 콘솔 포트는 내부망에서만 허용 |

## 6. EKS 클러스터 명세

| 항목 | 권장 명세 | 설명 |
| --- | --- | --- |
| Kubernetes 버전 | 현재 AWS EKS 지원 최신 안정 버전 | 버전은 구축 시점에 확인 |
| Endpoint access | Public 제한 + Private 활성화 권장 | 운영자는 VPN 또는 Bastion/SSO 경유 |
| Node Group | Managed Node Group 우선 | 운영 복잡도 감소 |
| Node subnet | private app subnet | Pod 외부 직접 노출 방지 |
| Node AMI | Bottlerocket 또는 Amazon Linux 2023 | 보안/운영 정책에 따라 선택 |
| Autoscaling | Cluster Autoscaler 또는 Karpenter | Pod 증가에 따라 노드 자동 확장 |
| Add-ons | VPC CNI, CoreDNS, kube-proxy, EBS CSI, AWS Load Balancer Controller | EKS 운영 기본 구성 |
| Pod 권한 | EKS Pod Identity | 서비스별 AWS 권한 분리 |

### 6.1 Node Group 분리

초기에는 하나의 범용 Node Group으로 시작하되, 운영 전에는 아래처럼 분리하는 것을 권장합니다.

| Node Group | 대상 워크로드 | 인스턴스 예시 | 이유 |
| --- | --- | --- | --- |
| `general` | Frontend, API, Ontology, Trading | `m7i.large` 계열 | 일반 API/웹 워크로드 |
| `realtime` | WebSocket Gateway | 네트워크 성능 좋은 인스턴스 | 연결 수와 네트워크 안정성 |
| `data-processing` | Ingestor, Flink | CPU/메모리 큰 인스턴스 | 스트림 처리와 배치 작업 |
| `system` | CoreDNS, controller | 작은 고정 노드 | 시스템 Pod 안정성 |

### 6.2 2AZ Active/Passive 운영 방식

평상시에는 active AZ의 Node Group에 주요 Pod를 배치합니다. 현재 단계에서는 비용을 낮추기 위해 standby AZ를 cold standby에 가깝게 운영합니다. 최종 시연 전에는 실제 장애 전환을 더 확실하게 보여줄 수 있도록 warm standby 또는 사전 확장 방식으로 전환합니다.

| 항목 | 권장 |
| --- | --- |
| active AZ | 애플리케이션 Pod와 주요 데이터 처리 워크로드 기본 배치 |
| standby AZ | 현재는 scale-to-zero에 가까운 저비용 대기 상태. 최종 시연 전 warm standby로 변경 검토 |
| Pod 배치 | `nodeAffinity`, `topologySpreadConstraints`, node label로 active AZ 우선 배치 |
| 장애 전환 | 현재는 standby Node Group 확장 후 ALB target health 회복 확인. 최종 시연 전 자동화 수준 강화 |
| 사전 검증 | 최종 시연 전 standby AZ 복구 리허설 필수 |
| 데이터 정책 | RTO보다 RPO를 우선합니다. 즉, 복구 시간이 길어져도 데이터 손실 0을 목표로 설계합니다. |

### 6.3 Auto Scaling Group 명세

EKS Managed Node Group은 내부적으로 Amazon EC2 Auto Scaling Group을 사용합니다. 즉, DevOps가 직접 EC2를 하나씩 띄우는 대신 Node Group의 scaling config를 관리하면, EKS가 그 뒤의 ASG와 EC2 생명주기를 조정합니다.

Auto Scaling Group의 핵심 값은 다음과 같습니다.

| 값 | 의미 | 이 프로젝트에서의 사용 |
| --- | --- | --- |
| `min` | 최소로 유지할 EC2 개수 | 비용을 낮추기 위해 standby AZ는 현재 0까지 허용 |
| `desired` | 지금 유지하고 싶은 EC2 개수 | 평상시 active AZ 중심으로 설정 |
| `max` | 최대로 늘릴 수 있는 EC2 개수 | 트래픽 급증이나 Flink 작업 증가에 대비 |
| Launch Template | 새 EC2를 만들 때 사용할 AMI, instance type, disk, tag 설정 | Node AMI, EBS 암호화, 보안 그룹을 표준화 |
| Scaling Policy | 언제 늘리고 줄일지 정하는 정책 | Cluster Autoscaler 또는 Karpenter가 pending Pod 기준으로 조정 |

ASG를 직접 콘솔에서 수정하기보다 Terraform/EKS Node Group 설정을 통해 관리합니다. EKS가 Managed Node Group의 ASG 값을 주기적으로 동기화하므로, 수동 변경은 임시 장애 대응 때만 사용합니다.

#### 현재 저비용 ASG 구성

| ASG/Node Group | AZ | 용도 | Instance type | min | desired | max | Capacity type |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `ng-active-general` | active AZ | Frontend, API, WebSocket, Trading, Ontology | `t3.large` | 1 | 1 | 3 | On-Demand |
| `ng-active-data-processing` | active AZ | Market Data Ingestor, Flink Task Manager | `t3.large` | 0 | 0 | 2 | On-Demand |
| `ng-standby-general` | standby AZ | AZ 장애 복구 대기 | `t3.large` | 0 | 0 | 2 | On-Demand |

현재 단계에서는 비용을 낮추기 위해 standby AZ의 `min`과 `desired`를 0으로 둡니다. active AZ 장애가 발생하면 `ng-standby-general`의 desired capacity를 올리고, ALB target health와 Pod readiness가 회복되는지 확인합니다.

#### 최종 시연 전 ASG 구성

| ASG/Node Group | AZ | 용도 | Instance type | min | desired | max | Capacity type |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `ng-active-general` | active AZ | 주요 서비스 상시 운영 | `m7i.large` | 2 | 2 | 4 | On-Demand |
| `ng-active-data-processing` | active AZ | Ingestor/Flink 처리 | `m7i.xlarge` | 1 | 1 | 3 | On-Demand |
| `ng-standby-general` | standby AZ | 장애 전환 시 즉시 서비스 실행 | `m7i.large` | 1 | 1 | 3 | On-Demand |

최종 시연 전에는 standby AZ에 최소 1개 노드를 미리 띄워 warm standby로 전환합니다. 이렇게 하면 비용은 늘지만, 장애 전환 시 이미지 pull, 노드 부팅, kubelet 등록 시간을 줄일 수 있습니다.

Spot Instance는 지금 단계에서는 기본값으로 쓰지 않습니다. Spot은 저렴하지만 중간에 회수될 수 있으므로, RPO 0과 시연 안정성이 중요한 주요 서비스에는 On-Demand를 우선합니다. 나중에 비용 최적화가 필요하면 Flink Task Manager나 배치성 작업부터 Spot을 검토합니다.

### 6.4 EC2 컴퓨터 스펙

아래 스펙은 AWS 공식 EC2 instance type 표를 기준으로 작성했습니다. 실제 비용은 서울 리전 가격, Savings Plans, Reserved Instance, 사용 시간에 따라 다시 계산해야 합니다.

| 대상 | 현재 저비용 스펙 | vCPU / Memory | Disk | 최종 시연 전 스펙 | vCPU / Memory | 비고 |
| --- | --- | --- | --- | --- | --- | --- |
| EKS general node | `t3.large` | 2 vCPU / 8 GiB | EBS gp3 40~80 GiB | `m7i.large` | 2 vCPU / 8 GiB | Frontend/API/WebSocket/Trading/Ontology 공용 |
| EKS data-processing node | `t3.large` 또는 미기동 | 2 vCPU / 8 GiB | EBS gp3 80 GiB | `m7i.xlarge` | 4 vCPU / 16 GiB | Ingestor/Flink가 무거워지면 분리 |
| ClickHouse EC2 | `t3.large` | 2 vCPU / 8 GiB | EBS gp3 100~200 GiB | `r7i.large` | 2 vCPU / 16 GiB | 현재는 비용 우선. 시연 전 replica/warm standby 검토 |
| Ontotext GraphDB EC2 | `t3.large` | 2 vCPU / 8 GiB | EBS gp3 100 GiB | `r7i.large` | 2 vCPU / 16 GiB | JVM heap과 FIBO import를 위해 memory 여유 필요 |
| Bastion/운영 접속 EC2 | 원칙적으로 미사용 | - | - | 필요 시 `t3.micro` | 2 vCPU / 1 GiB | 가능하면 SSM Session Manager로 대체 |

`t3` 계열은 burstable instance입니다. 평소에는 낮은 CPU 기준 성능으로 비용을 줄이고, CPU credit을 사용해 순간적으로 더 높은 성능을 냅니다. 지속적으로 CPU를 많이 쓰는 워크로드에는 적합하지 않으므로, 최종 시연 전에는 주요 노드를 `m7i` 또는 `r7i` 계열로 올리는 것을 권장합니다.

`m7i` 계열은 범용 인스턴스입니다. vCPU와 메모리 비율이 1:4라서 API 서버, 프론트엔드 서버, 일반 백엔드 워커에 무난합니다. `m7i.large`는 2 vCPU와 8 GiB 메모리, `m7i.xlarge`는 4 vCPU와 16 GiB 메모리입니다.

`r7i` 계열은 메모리 최적화 인스턴스입니다. ClickHouse와 GraphDB처럼 메모리를 많이 쓰는 데이터 서비스에 더 잘 맞습니다. `r7i.large`는 2 vCPU와 16 GiB 메모리입니다.

Stateful EC2인 ClickHouse와 Ontotext GraphDB는 일반적인 ASG로 무작정 교체하면 데이터 볼륨 연결과 복구 순서가 꼬일 수 있습니다. 따라서 기본은 단일 EC2 + EBS snapshot + S3 백업으로 두고, ASG를 붙이더라도 lifecycle hook과 복구 스크립트를 준비한 뒤 적용합니다.

### 6.5 스토리지 타입 선택 기준

S3는 SSD/HDD를 직접 고르는 서비스가 아닙니다. S3는 객체 저장소이므로 `Storage Class`를 고르고, EC2와 RDS는 디스크 성능 특성에 따라 SSD/HDD 계열 스토리지를 선택합니다.

| 대상 | 선택 방식 | 권장값 | 이유 |
| --- | --- | --- | --- |
| EKS node root volume | EBS volume type | `gp3` SSD | 비용과 성능 균형이 좋고, 일반적인 컨테이너 노드에 충분 |
| ClickHouse EC2 data volume | EBS volume type | 현재 `gp3`, 성능 부족 시 `io2` 검토 | 분석 쿼리와 merge 작업 때문에 HDD보다 SSD가 적합 |
| Ontotext GraphDB EC2 data volume | EBS volume type | `gp3` SSD | FIBO import, SPARQL query, JVM workload에 HDD는 지연시간이 커질 수 있음 |
| RDS PostgreSQL | RDS storage type | `gp3` | 운영 DB 기본 선택지. IOPS/throughput을 필요에 따라 조정 가능 |
| 대용량 순차 로그 보관 | EBS volume type | `st1` HDD 검토 가능 | 순차 처리 중심일 때만 검토. DB/GraphDB/ClickHouse 기본 디스크로는 비권장 |
| 장기 콜드 데이터 | EBS volume type | `sc1` HDD는 원칙적으로 비권장 | 매우 저렴하지만 지연시간이 커서 운영 데이터 서비스에는 부적합 |

현재 단계에서는 EC2와 RDS 모두 `gp3`를 기본값으로 둡니다. `io2`는 비용이 높으므로, 실제 부하 테스트에서 IOPS 또는 latency 문제가 확인될 때만 올립니다.

## 7. Kubernetes 배포 명세

### 7.1 Namespace

환경별로 Namespace를 분리합니다.

| Namespace | 용도 |
| --- | --- |
| `gops-dev` | 개발/기능 검증 |
| `gops-staging` | 운영 전 통합 검증 |
| `gops-prod` | 운영 |
| `observability` | Prometheus, Grafana, Loki 등 |
| `ingress-system` | AWS Load Balancer Controller |

### 7.2 공통 Kubernetes 객체

Kubernetes 객체를 처음 볼 때의 의미는 다음과 같습니다.

| 객체 | 역할 | 이 아키텍처에서 필요한 이유 |
| --- | --- | --- |
| Deployment | Pod 복제본 수, 업데이트 방식, 롤백을 관리합니다. | API, Frontend, WebSocket 등 장기 실행 서비스를 안정적으로 운영합니다. |
| Service | Pod 앞에 고정 네트워크 이름을 만듭니다. | Pod IP가 바뀌어도 API가 PostgreSQL 접근처럼 내부 통신을 안정적으로 하게 합니다. |
| Ingress | 외부 HTTP/HTTPS 요청을 내부 Service로 보내는 규칙입니다. | ALB가 `/api`, `/ws`, `/` 경로를 각각 적절한 서비스로 라우팅하게 합니다. |
| ConfigMap | 비밀이 아닌 설정값을 저장합니다. | `LOG_LEVEL`, 외부 API base URL, feature flag 같은 값을 이미지 밖에서 관리합니다. |
| Secret | 민감한 설정값을 저장합니다. | DB 비밀번호, 외부 API key를 코드에 넣지 않기 위해 사용합니다. |
| HorizontalPodAutoscaler | Pod 수를 자동으로 늘리고 줄입니다. | 트래픽 증가 시 API/WebSocket/Frontend Pod를 자동 확장합니다. |
| PodDisruptionBudget | 자발적 중단 중 최소 가용 Pod 수를 보장합니다. | 노드 업데이트 중에도 핵심 서비스가 모두 내려가지 않게 합니다. |

### 7.3 서비스별 최소 배포값

| 서비스 | Kind | 초기 replicas | HPA 기준 | Probe | 비고 |
| --- | --- | --- | --- | --- | --- |
| Frontend Server | Deployment | 2 | CPU 60%, RPS | `/health` | React 정적 파일을 EKS에서 서빙 |
| API Server | Deployment | active AZ 2개, standby AZ 0~1개 | latency, CPU, memory | `/health`, `/ready` | DB migration과 배포 순서 관리 |
| WebSocket Gateway | Deployment | active AZ 2개, standby AZ 0~1개 | active connections, CPU | `/health`, `/ready` | drain hook 필수 |
| AI Agents Service | Deployment | 2 | queue length, CPU | `/health`, `/ready` | 외부 LLM rate limit 대응 |
| Ontology Service | Deployment | 2 | CPU, latency | `/health`, `/ready` | Ontotext GraphDB 연결 확인 |
| Trading Service | Deployment | 2 | queue length, CPU | `/health`, `/ready` | 중복 실행 방지 락 필요 여부 확인 |
| Chart Builder | Deployment 또는 Job | 2 | queue length, CPU | `/health`, `/ready` | 무거운 작업은 Job/worker로 분리 |
| Market Data Ingestor | Deployment 또는 CronJob | 1~2 | lag, error rate | `/health`, `/ready` | 중복 수집 방지 필요 |
| Flink Job Manager | FlinkDeployment | 1 active | Flink metric | Flink REST health | HA 설정 필요 |
| Flink Task Manager | FlinkDeployment | workload 기반 | task/backpressure | Flink metric | checkpoint 필수 |

### 7.4 Ingress 경로 초안

| Host/Path | 대상 Service | 설명 |
| --- | --- | --- |
| `app.example.com/` | Frontend Server | React UI |
| `api.example.com/` 또는 `/api` | API Server | REST API |
| `ws.example.com/` 또는 `/ws` | WebSocket Gateway | 실시간 연결 |

프론트엔드는 EKS에서 서빙합니다. 운영에서는 프론트엔드와 API 도메인을 분리하는 것을 권장합니다. CORS 설정과 쿠키 보안 정책이 더 명확해집니다.

## 8. 컨테이너 이미지 명세

| 항목 | 명세 |
| --- | --- |
| Registry | ECR |
| 태그 규칙 | `{service}:{git-sha}`, `{service}:staging-{git-sha}`, `{service}:prod-{git-sha}` |
| `latest` 사용 | 금지 |
| 이미지 빌드 | MVP에서는 자동 CI/CD 없이 로컬 또는 수동 빌드 서버에서 수행하고 ECR에 push |
| 취약점 스캔 | ECR image scanning 또는 Trivy |
| 실행 사용자 | root 금지, non-root user |
| Healthcheck | 애플리케이션 `/health`와 Kubernetes probe 모두 구성 |

## 9. 설정과 Secret 명세

### 9.1 ConfigMap 후보

| Key | 예시 | 설명 |
| --- | --- | --- |
| `APP_ENV` | `staging`, `prod` | 실행 환경 |
| `LOG_LEVEL` | `info` | 로그 레벨 |
| `API_BASE_URL` | `https://api.example.com` | 프론트엔드가 바라볼 API |
| `WEBSOCKET_URL` | `wss://ws.example.com` | 프론트엔드 실시간 연결 주소 |
| `KAFKA_BOOTSTRAP_SERVERS` | `b-1...:9094` | Kafka 접속 주소 |
| `CLICKHOUSE_HOST` | `clickhouse.internal` | ClickHouse 주소 |
| `REDIS_HOST` | `redis.internal` | Redis 주소 |
| `TRADING_PROVIDER` | `kis-paper` | MVP 주문 provider. Alpaca는 거래 provider로 사용하지 않음 |

### 9.2 Secret 후보

| Secret | 저장 위치 | 사용 서비스 |
| --- | --- | --- |
| `POSTGRES_DSN` | Secrets Manager | API, Trading, Ontology |
| `REDIS_PASSWORD` | Secrets Manager | API, WebSocket, Trading |
| `CLICKHOUSE_PASSWORD` | Secrets Manager | API, Chart, Flink |
| `ALPACA_API_KEY` | Secrets Manager | Market Data Ingestor |
| `ALPACA_API_SECRET` | Secrets Manager | Market Data Ingestor |
| `KIS_APP_KEY` | Secrets Manager | KIS Broker Adapter |
| `KIS_APP_SECRET` | Secrets Manager | KIS Broker Adapter |
| `KIS_ACCOUNT_NO` | Secrets Manager | KIS Broker Adapter |
| `OPENAI_API_KEY` | Secrets Manager | AI Agents |
| `JWT_ACCESS_SECRET` | Secrets Manager | API Server, WebSocket Gateway |
| `JWT_REFRESH_SECRET` | Secrets Manager | API Server |

Kubernetes Secret을 직접 관리하기보다, External Secrets Operator 또는 Secrets Store CSI Driver로 AWS Secrets Manager와 동기화하는 방식을 권장합니다.

### 9.3 JWT 인증 설정

API 인증 방식은 자체 JWT로 확정합니다.

| 항목 | 결정 |
| --- | --- |
| 인증 방식 | 자체 JWT |
| Access Token | 30분 만료 |
| Refresh Token | 14일 만료 |
| 초기 저장 방식 | 프론트엔드 localStorage 또는 memory. 운영 전 HttpOnly Secure Cookie 전환 검토 |
| WebSocket 인증 | 연결 시 JWT 전달 후 WebSocket Gateway에서 검증 |
| 권한 모델 | `user`, `trader`, `admin` role |
| Secret 저장 | JWT signing secret은 Secrets Manager에 저장 |

JWT claim에는 최소한 `sub`, `role`, `iat`, `exp`를 포함합니다. `sub`는 사용자 식별자, `role`은 권한, `iat`는 발급 시간, `exp`는 만료 시간입니다.

## 10. 데이터 계층 명세

### 10.1 PostgreSQL

운영 기본안은 RDS PostgreSQL입니다. 2AZ active/passive 전략에 맞춰 active AZ에서 서비스를 운영하되, RDS는 장애 조치가 가능하도록 Multi-AZ 구성을 권장합니다.

| 항목 | 권장 |
| --- | --- |
| 배치 | private data subnet |
| HA | Multi-AZ |
| 백업 | 자동 백업 7~35일, PITR 활성화 |
| 암호화 | KMS at-rest encryption |
| 접근 | EKS Node SG에서만 허용 |
| migration | 별도 승인 단계로 실행 |

PITR은 Point-In-Time Recovery의 줄임말로, 특정 시점으로 DB를 복구하는 기능입니다.

### 10.2 Redis

운영 기본안은 AWS ElastiCache for Redis로 확정합니다. Valkey는 MVP 범위에서 사용하지 않습니다.

| 항목 | 권장 |
| --- | --- |
| 배치 | private data subnet |
| HA | 개발/초기 MVP는 단일 노드 또는 작은 replication group, 운영/시연 전 Multi-AZ replication group |
| TLS | 가능하면 활성화 |
| Auth | Redis AUTH 우선 |
| eviction | 서비스별 TTL/캐시 정책 확정 |

### 10.3 Kafka

Kafka는 Amazon MSK 사용으로 확정합니다. 사용자가 "MKS"라고 표현한 항목은 AWS의 정식 서비스명 기준으로 `MSK`라고 표기합니다.

| 항목 | 권장 |
| --- | --- |
| 배치 | private data subnet |
| Broker | 2AZ 구성. 평상시는 active AZ 중심으로 운영하고, active AZ 장애 시 standby AZ 복구 절차를 Runbook으로 관리 |
| 인증 | TLS + IAM 또는 SASL/SCRAM |
| Topic 관리 | Terraform 또는 GitOps로 선언 |
| 모니터링 | consumer lag, broker disk, under-replicated partitions |
| 데이터 손실 정책 | RPO 0 목표. producer `acks=all`, 적절한 replication factor, `min.insync.replicas`를 설정해 승인된 메시지 손실을 막는 방향으로 설계 |

`acks=all`은 Kafka producer가 메시지를 보낼 때 모든 필요한 replica가 기록을 확인해야 성공으로 처리하는 옵션입니다. 데이터 손실을 줄이는 대신 장애 상황에서는 쓰기 지연이나 실패가 늘 수 있습니다. 이 프로젝트는 복구 시간보다 데이터 손실 0을 우선하므로 강한 durability 설정을 우선합니다.

### 10.4 ClickHouse

권장안은 EC2 self-managed입니다. 현재 단계에서는 비용을 낮추는 구성을 우선하고, 최종 시연 전 장애 전환을 더 확실하게 보여줄 수 있는 구성으로 바꿉니다.

판단 이유는 다음과 같습니다.

- ClickHouse는 stateful 분석 DB라서 EKS 위에 올리면 PV, operator, upgrade, 노드 장애 처리까지 Kubernetes 운영 부담이 커집니다.
- EC2 self-managed는 초기 구조가 단순하고, EBS/io2 또는 gp3, S3 백업, systemd, AMI 기반 복구로 DevOps가 장애 범위를 더 명확하게 통제할 수 있습니다.
- 현재 아키텍처가 2AZ active/passive이고 평상시 1개 AZ만 운영하는 방향이므로, 지금은 active AZ의 단일 EC2 primary와 S3 백업/AMI 복구 경로를 우선합니다. 최종 시연 전에는 replica 또는 warm standby를 붙이는 방식으로 전환합니다.

따라서 1차 구축은 `EC2 self-managed ClickHouse`로 진행합니다. 단, ClickHouse를 원천 저장소로 보지 않고 MSK/S3/RDS에서 재생성 가능한 분석 저장소로 설계해야 합니다. 그래야 비용을 낮추는 동안에도 핵심 데이터 손실 0 목표를 지킬 수 있습니다.

| 선택지 | 장점 | 단점 |
| --- | --- | --- |
| ClickHouse Cloud | 운영 부담 적음 | 네트워크/비용/리전 확인 필요 |
| Altinity Operator on EKS | Kubernetes 친화적 | 운영 난이도 증가 |
| EC2 self-managed | 초기 운영 구조가 단순하고 비용/네트워크 통제가 쉬움 | 백업/업그레이드/장애 대응을 직접 설계해야 함 |

| 항목 | 권장 |
| --- | --- |
| 배치 | private data subnet의 active AZ |
| 현재 스펙 | `t3.large`, gp3 100 GiB, cold standby |
| 최종 시연 전 스펙 | `r7i.large`, gp3 200 GiB 이상, warm standby |
| 현재 standby | 비용 우선. standby AZ에는 즉시 실행 replica를 두지 않고 AMI/snapshot/S3 백업 기반 복구 경로 준비 |
| 최종 시연 전 standby | warm standby로 전환 |
| 스토리지 | gp3 EBS |
| 백업 | 일 1회 S3 원격 백업, 주기적 restore 테스트. 원천 이벤트는 MSK/S3/RDS에 보존 |
| 접근 | EKS Node SG에서만 허용 |
| 모니터링 | disk usage, query latency, merges, replication delay |

### 10.5 Ontotext GraphDB

GraphDB 제품은 Ontotext GraphDB로 확정합니다. FIBO를 기본 온톨로지로 import해 금융 도메인 개념과 관계의 표준 vocabulary로 사용합니다.

| 항목 | 권장 |
| --- | --- |
| 배치 | private data subnet |
| 운영 방식 | EC2 self-managed로 확정 |
| repository | `fibo` 또는 서비스명 기준 repository 생성 |
| 초기 데이터 | FIBO ontology import |
| 접근 | Ontology Service와 AI Agents Service에서만 허용 |
| 백업 | repository export 또는 파일 시스템 snapshot |
| 모니터링 | JVM memory, query latency, repository size, import 실패 |

### 10.6 S3 버킷 및 Storage Class 명세

S3는 Simple Storage Service입니다. 파일을 객체 단위로 저장하는 AWS 관리형 저장소이며, 이 아키텍처에서는 원천 데이터, 백업, Flink 복구 지점, 감사 로그, Terraform state를 나눠 저장합니다.

S3의 SSD/HDD 선택에 해당하는 개념은 `Storage Class`입니다. 자주 접근하는 데이터는 `S3 Standard`, 접근 패턴을 모르면 `S3 Intelligent-Tiering`, 오래 보관하지만 자주 읽지 않는 데이터는 `S3 Standard-IA` 또는 `Glacier` 계열로 보냅니다.

#### 버킷 종류

| 버킷 이름 패턴 | 저장 데이터 | 기본 Storage Class | Versioning | Lifecycle 정책 | 접근 주체 | 운영 중요도 |
| --- | --- | --- | --- | --- | --- | --- |
| `gops-${env}-terraform-state` | Terraform state file | `S3 Standard` | 활성화 필수 | 삭제 전환 없음. 수동 삭제 제한 | DevOps Terraform role | 매우 높음 |
| `gops-${env}-raw-market-data` | Alpaca 시장 데이터 원본, 외부 API raw payload | `S3 Standard` 또는 `Intelligent-Tiering` | 활성화 필수 | 30~90일 후 `Standard-IA`, 180~365일 후 Glacier 검토 | Market Data Ingestor, Flink, Athena | 매우 높음 |
| `gops-${env}-flink-checkpoint` | Flink checkpoint, savepoint | `S3 Standard` | 활성화 권장 | checkpoint는 7~14일 보관, savepoint는 30~90일 보관 | Flink JobManager/TaskManager | 높음 |
| `gops-${env}-backup` | ClickHouse backup, GraphDB repository export, 운영 백업 파일 | `S3 Standard` | 활성화 필수 | 30일 후 Glacier Instant Retrieval, 장기 보관은 Deep Archive 검토 | Backup/restore role, DevOps | 높음 |
| `gops-${env}-logs` | ALB access log, VPC Flow Logs, CloudTrail log | `S3 Standard` | 활성화 권장 | 30~90일 후 `Standard-IA`, 180~365일 후 Glacier | AWS logging service, 보안/운영자 | 높음 |
| `gops-${env}-athena-results` | Athena query result | `S3 Standard` | 선택 | 7~30일 후 삭제 | Athena workgroup, 데이터/운영자 | 중간 |
| `gops-${env}-artifacts` | 운영 스크립트 산출물, 임시 export, 수동 업로드 파일 | `S3 Standard` | 선택 | 30일 후 삭제 또는 IA 전환 | DevOps, batch job | 낮음 |

`raw-market-data`, `flink-checkpoint`, `backup` 버킷은 RPO 0 목표와 직접 연결됩니다. Ingestor는 외부 API에서 받은 원본 이벤트를 가능한 빨리 S3 또는 MSK에 기록하고, downstream 저장소인 ClickHouse는 이 원천 데이터에서 재생성할 수 있게 설계합니다.

#### Storage Class 정책

| Storage Class | 사용 위치 | 설명 |
| --- | --- | --- |
| `S3 Standard` | raw data 초기 저장, checkpoint, 최근 백업, Athena 결과 | 자주 읽고 쓰는 데이터에 사용 |
| `S3 Intelligent-Tiering` | 접근 패턴이 불확실한 raw data | AWS가 접근 빈도에 따라 비용 계층을 자동 조정 |
| `S3 Standard-IA` | 오래된 raw data, 오래된 로그 | 자주 읽지 않지만 필요하면 바로 복구해야 하는 데이터 |
| `S3 Glacier Instant Retrieval` | 최근 백업의 저비용 보관 | 거의 읽지 않지만 빠른 복구가 필요한 백업 |
| `S3 Glacier Flexible Retrieval` | 장기 로그, 오래된 백업 | 복구 시간이 어느 정도 허용되는 장기 보관 |
| `S3 Glacier Deep Archive` | 감사 목적 장기 보관 | 가장 저렴하지만 복구 시간이 길어 운영 즉시 복구용으로는 부적합 |

#### 공통 보안 설정

| 항목 | 설정 |
| --- | --- |
| Public access | 모든 버킷에서 Block Public Access 활성화 |
| Encryption | SSE-KMS 우선. 비용을 줄여야 하는 낮은 중요도 버킷은 SSE-S3 검토 가능 |
| Bucket ownership | Bucket owner enforced 적용 |
| Bucket policy | 필요한 IAM Role과 AWS logging service principal만 허용 |
| VPC Endpoint policy | private subnet의 필요한 role만 S3 접근 허용 |
| Object Lock | CloudTrail/log bucket은 최종 운영 전 적용 여부 검토 |
| Prefix 구조 | `source=<source>/type=<type>/dt=YYYY-MM-DD/` 형태로 Athena partition을 고려 |

## 11. 스트리밍 처리 명세

### 11.1 데이터 흐름

```text
Alpaca Algo Trader Plus API
  -> Market Data Ingestor
  -> Kafka topic
  -> Flink Job
  -> ClickHouse / PostgreSQL / Redis
  -> API Server / WebSocket Gateway / Chart Builder
  -> User
```

모의투자 주문 흐름은 별도 모드로 분리합니다.

```text
User / Strategy
  -> Trading Service
  -> orders.commands.v1 Kafka topic
  -> KIS Broker Adapter
  -> 한국투자증권 모의투자 API
  -> broker.submit-results.v1 / broker.order-events.v1 Kafka topic
  -> API Server / WebSocket Gateway
  -> User
```

### 11.2 Kafka Topic 초안

| Topic | Producer | Consumer | Retention |
| --- | --- | --- | --- |
| `market.ticks.v1` | Flink | API, Chart, WebSocket | 3일 |
| `market.candles.live.1m.v1` | Flink | API, Chart, WebSocket | 3일 |
| `market.candles.closed.v1` | Flink | API, Chart, WebSocket, ClickHouse loader | 30일 |
| `orders.commands.v1` | Backend Outbox Publisher | KIS Broker Adapter | 90일 |
| `broker.submit-results.v1` | KIS Broker Adapter Outbox | API, Audit, WebSocket | 90일 |
| `broker.order-events.v1` | KIS Poller/Reconciler | API, Audit, WebSocket | 90일 |
| `orders.dlq.v1` | Adapter/Flink/API 등 | 운영자 재처리 도구 | 180일 |

### 11.3 Flink 운영 조건

| 항목 | 명세 |
| --- | --- |
| Checkpoint | S3에 저장 |
| Savepoint | 배포 전 수동 또는 자동 생성 |
| 장애 복구 | checkpoint 기반 재시작 |
| 배포 | Flink Kubernetes Operator 또는 Managed Flink |
| 모니터링 | backpressure, checkpoint failure, job restart count |

Checkpoint는 스트림 처리 중간 상태를 주기적으로 저장하는 기능입니다. 장애가 나면 처음부터 다시 처리하지 않고 마지막 저장 지점부터 이어갈 수 있습니다.

## 12. 배포 전략

초기 권장 전략은 Kubernetes Rolling Update입니다.

| 서비스 | 전략 | 이유 |
| --- | --- | --- |
| Frontend Server | Rolling Update | 상태가 적고 롤백 쉬움 |
| API Server | Rolling Update | 일반 API 서버에 적합 |
| WebSocket Gateway | Rolling Update + connection drain | 장기 연결 보호 필요 |
| Trading Service | Rolling Update 또는 Blue/Green | 거래 중복/중단 리스크에 따라 결정 |
| Flink | Savepoint 기반 upgrade | 상태 있는 스트림 작업이므로 일반 rolling과 다름 |

Rolling Update는 기존 Pod를 한 번에 모두 죽이지 않고, 새 Pod를 조금씩 띄우며 교체하는 방식입니다. 두 버전이 잠깐 같이 떠 있을 수 있으므로 API와 DB 변경은 하위 호환성을 지켜야 합니다.

## 13. 관측 가능성 명세

관측 가능성은 장애가 났을 때 "무슨 일이 일어났는지"를 로그, 지표, 트레이스로 확인하는 능력입니다.

| 영역 | 권장 도구 | 수집 항목 |
| --- | --- | --- |
| Metrics | Prometheus + Grafana 또는 CloudWatch Container Insights | CPU, memory, request count, latency, error rate |
| Logs | CloudWatch Logs 또는 Loki | 애플리케이션 JSON 로그, ingress 로그, controller 로그 |
| Traces | OpenTelemetry + Tempo/X-Ray | 요청이 API, DB, 외부 API를 거치는 경로 |
| Alert | Alertmanager 또는 CloudWatch Alarm | 장애 조건 알림 |

### 13.1 필수 알림

| 알림 | 기준 예시 |
| --- | --- |
| API 5xx 증가 | 5분 평균 5xx rate > 2% |
| API latency 증가 | p95 latency > 1s |
| Pod CrashLoopBackOff | 1개 이상 5분 지속 |
| HPA max 도달 | max replicas 10분 이상 |
| DB connection 부족 | 사용률 80% 이상 |
| Redis memory 압박 | memory 80% 이상 또는 eviction 발생 |
| Kafka consumer lag | 서비스별 lag 임계값 초과 |
| Flink checkpoint 실패 | 연속 3회 실패 |
| 외부 API 실패 | Alpaca/OpenAI/한국투자증권 error rate 증가 |

## 14. 보안 명세

| 영역 | 작업 |
| --- | --- |
| IAM | 서비스별 최소 권한 Role 생성 |
| Pod Identity | S3/ECR/Secrets 접근이 필요한 Pod만 Role 연결 |
| Secret | Secrets Manager 사용, Git 저장 금지 |
| API Auth | 자체 JWT 사용, access/refresh secret은 Secrets Manager에 저장 |
| Network | DB/Kafka/Redis는 private subnet only |
| TLS | 외부는 ACM 인증서로 ALB TLS 종료, 내부 TLS는 서비스별 검토 |
| Image | non-root 실행, 취약점 스캔, `latest` 금지 |
| Ingress | JWT 인증은 API Server/WebSocket Gateway에서 수행. MVP는 rate limit 우선, WAF는 후속 적용 |
| Audit | EKS audit log, CloudTrail 활성화 |

## 15. 백업과 복구 명세

| 대상 | 백업 | 복구 검증 |
| --- | --- | --- |
| PostgreSQL | RDS automated backup + snapshot | 월 1회 staging 복구 리허설 |
| Redis | snapshot 또는 재생성 가능 캐시로 설계 | 캐시 유실 시 서비스 영향 확인 |
| Amazon MSK | topic retention + 필요 시 MirrorMaker/백업 | consumer 재처리 절차 문서화 |
| ClickHouse | S3 백업 또는 EC2/EBS snapshot | 샘플 테이블 복구 테스트 |
| Ontotext GraphDB | EC2 filesystem snapshot 또는 repository export | FIBO repository 복구와 재import 검증 |
| S3 | versioning + lifecycle | 삭제/오염 복구 절차 확인 |
| Kubernetes manifest | GitOps 저장소 | 클러스터 재생성 테스트 |

RTO/RPO 방향은 `RPO 0 우선`입니다. RTO는 Recovery Time Objective로 장애 후 복구까지 걸리는 목표 시간이고, RPO는 Recovery Point Objective로 장애 시 허용 가능한 데이터 손실량입니다. 이 프로젝트는 복구 시간이 조금 길어지더라도 데이터 손실을 0에 가깝게 만드는 쪽을 우선합니다.

이를 위해 원천 데이터는 RDS PostgreSQL, Amazon MSK, S3에 남기고, ClickHouse처럼 재생성 가능한 분석 저장소는 비용 우선 단계에서 replica를 늦출 수 있습니다. 단, ClickHouse에만 존재하는 데이터가 생기면 그 순간부터 replica 또는 더 강한 백업 정책이 필요합니다.

## 16. 운영 Runbook 초안

### 16.1 배포 실패

1. 배포 상태 확인: `kubectl rollout status deployment/<name> -n <namespace>`
2. 새 Pod 이벤트 확인: `kubectl describe pod <pod> -n <namespace>`
3. 로그 확인: `kubectl logs <pod> -n <namespace>`
4. readiness 실패면 `/ready`가 확인하는 DB/Redis/Kafka 상태 점검
5. 즉시 복구 필요 시 rollback: `kubectl rollout undo deployment/<name> -n <namespace>`

`kubectl`은 Kubernetes API와 대화하는 CLI입니다. `rollout status`는 Deployment 업데이트가 성공했는지 확인하고, `rollout undo`는 이전 ReplicaSet으로 되돌립니다.

### 16.2 API 장애

1. ALB target health 확인
2. API Pod readiness/liveness 확인
3. API 5xx 로그 검색
4. PostgreSQL/Redis/Kafka 연결 오류 확인
5. 최근 배포가 원인이면 rollback

### 16.3 WebSocket 장애

1. ALB idle timeout과 target deregistration delay 확인
2. active connection 수와 Pod 재시작 여부 확인
3. Redis pub/sub 또는 session store 상태 확인
4. rolling update 중 drain hook 동작 여부 확인

### 16.4 Kafka lag 증가

1. consumer group lag 확인
2. Flink backpressure 확인
3. broker disk/network 확인
4. Task Manager scale-out
5. 외부 API ingestion 폭증 여부 확인

## 17. AWS 작업 명세

이 섹션은 AWS에서 DevOps가 실제로 생성, 설정, 검증해야 하는 작업 목록입니다. 구현은 Terraform 기준으로 진행하되, 긴급 확인은 AWS Console과 AWS CLI를 병행할 수 있습니다.

### 17.1 계정/공통 설정

| 작업 | 산출물 | 완료 기준 |
| --- | --- | --- |
| AWS 계정과 결제 알림 확인 | AWS account, billing access | 예산 알림을 받을 담당자 이메일 등록 |
| 기본 Region 고정 | `ap-northeast-2` | Terraform provider와 운영 스크립트에 서울 리전 적용 |
| 리소스 태그 표준 정의 | `Project`, `Env`, `Owner`, `ManagedBy`, `CostCenter` | 모든 주요 리소스에 공통 태그 적용 |
| Terraform backend 구성 | S3 backend bucket, DynamoDB lock table 또는 대체 lock | state 파일 암호화, 버전 관리, 동시 실행 잠금 확인 |
| 운영자 접근 방식 정의 | IAM Identity Center 또는 IAM Role | 개인 access key 장기 사용 금지 |
| 비용 예산 설정 | AWS Budgets | 월 예산 초과 전 알림 수신 확인 |

Terraform backend는 Terraform 상태 파일을 원격에 저장하는 구성입니다. 상태 파일에는 "AWS에 어떤 리소스를 만들었는지"가 들어가므로, S3 versioning과 encryption을 켜고 접근 권한을 제한해야 합니다.

### 17.2 네트워크 작업

| 작업 | 산출물 | 완료 기준 |
| --- | --- | --- |
| VPC CIDR 확정 | VPC CIDR, 예: `10.20.0.0/16` | 기존 네트워크/VPN과 충돌 없음 |
| 2AZ 선택 | active AZ, standby AZ | 예: `ap-northeast-2a` active, `ap-northeast-2c` standby |
| Public subnet 생성 | AZ별 public subnet | ALB, NAT Gateway 배치 가능 |
| Private app subnet 생성 | AZ별 private app subnet | EKS Node Group 배치 가능 |
| Private data subnet 생성 | AZ별 private data subnet | RDS, MSK, Redis, ClickHouse, GraphDB 배치 가능 |
| Internet Gateway 생성 | IGW | public route table에서 인터넷 경로 확인 |
| NAT Gateway 생성 | 현재 active AZ 1개, 최종 시연 전 AZ별 1개 검토 | private app subnet의 외부 API 호출 확인 |
| Route table 분리 | public/app/data route table | data subnet에 인터넷 기본 경로 없음 |
| VPC Endpoint 생성 | S3, ECR API, ECR DKR, CloudWatch Logs, Secrets Manager, STS | private subnet에서 NAT 없이 AWS API 접근 확인 |
| VPC Flow Logs 설정 | CloudWatch Logs 또는 S3 | 네트워크 거부/이상 트래픽 추적 가능 |

현재 비용 우선 단계에서는 NAT Gateway를 active AZ에만 둘 수 있습니다. 다만 NAT Gateway가 없는 standby AZ는 장애 전환 때 외부 API 호출이나 image pull이 느려질 수 있으므로, 최종 시연 전에는 standby AZ NAT Gateway 또는 필요한 VPC Endpoint 구성을 보강합니다.

### 17.3 보안/IAM/KMS 작업

| 작업 | 산출물 | 완료 기준 |
| --- | --- | --- |
| KMS key 생성 | EBS/RDS/S3/Secrets 암호화 키 | 주요 저장소 at-rest encryption 적용 |
| EKS Cluster Role 생성 | EKS control plane IAM role | EKS cluster 생성 가능 |
| EKS Node Role 생성 | Worker node IAM role | ECR pull, CloudWatch log, CNI 권한 확인 |
| Pod Identity Role 생성 | 서비스별 IAM role | S3/Secrets/MSK 접근이 필요한 Pod만 권한 보유 |
| Secrets Manager secret 생성 | DB/API key/외부 API secret | Git에 민감정보가 남지 않음 |
| Security Group 작성 | ALB/EKS/RDS/MSK/Redis/ClickHouse/GraphDB SG | 필요한 포트만 허용 |
| CloudTrail 활성화 | Management event trail | 누가 어떤 AWS API를 호출했는지 추적 가능 |
| Rate limit 적용 | API/WebSocket rate limit 정책 | MVP에서는 rate limit 우선 적용, WAF는 후속 적용 |

KMS는 Key Management Service입니다. AWS 리소스의 데이터를 암호화할 때 쓰는 키를 관리합니다. 이 프로젝트에서는 RDS, EBS, S3, Secrets Manager의 암호화 기준을 맞추기 위해 필요합니다.

### 17.4 ECR/이미지 저장소 작업

| 작업 | 산출물 | 완료 기준 |
| --- | --- | --- |
| 서비스별 ECR repository 생성 | frontend, api, websocket, trading, ai-agents, ontology, ingestor, chart, flink image repo | 빌드 주체가 push 가능 |
| 이미지 태그 규칙 적용 | `{service}:{git-sha}` | `latest` 없이 배포 가능 |
| ECR lifecycle policy 설정 | 오래된 이미지 자동 정리 | 비용 증가 방지 |
| 이미지 취약점 스캔 활성화 | ECR scan 또는 Trivy scan | high/critical 취약점 배포 차단 기준 마련 |
| ECR VPC Endpoint 확인 | `ecr.api`, `ecr.dkr`, S3 endpoint | private node에서 image pull 가능 |

### 17.5 EKS/ASG 작업

| 작업 | 산출물 | 완료 기준 |
| --- | --- | --- |
| EKS cluster 생성 | EKS cluster | private app subnet 기반 cluster 생성 |
| Cluster endpoint 정책 설정 | public restricted + private enabled | 운영자 접속 경로 확정 |
| Managed Node Group 생성 | `ng-active-general`, `ng-active-data-processing`, `ng-standby-general` | 각 Node Group이 의도한 AZ와 subnet에 생성 |
| ASG 용량 적용 | 현재 `1/1/3`, `0/0/2`, `0/0/2` | 저비용 모드 동작 확인 |
| Launch Template 작성 | AMI, instance type, EBS, tag, user data | EBS 암호화와 공통 tag 적용 |
| Cluster Autoscaler 또는 Karpenter 설치 | autoscaler controller | pending Pod 발생 시 node scale-out 확인 |
| EKS Add-on 설치 | VPC CNI, CoreDNS, kube-proxy, EBS CSI | add-on health 정상 |
| AWS Load Balancer Controller 설치 | controller, IAM role | Kubernetes Ingress로 ALB 생성 가능 |
| External Secrets Operator 설치 | ESO controller | Secrets Manager 값을 Kubernetes Secret으로 동기화 |
| Metrics Server 설치 | metrics-server | HPA가 CPU/memory metric 조회 가능 |
| Namespace 생성 | `gops-dev`, `gops-staging`, `gops-prod`, `observability`, `ingress-system` | 환경별 배포 분리 |
| 최종 시연 전 ASG 변경 | standby min/desired 1 이상 | warm standby 전환 확인 |

EKS Managed Node Group은 내부적으로 ASG를 사용합니다. 그래서 DevOps 작업 기준으로는 Node Group 설정을 Terraform으로 관리하고, ASG는 EKS가 만든 결과물로 확인하는 흐름이 안전합니다.

### 17.6 ALB/Route 53/ACM 작업

| 작업 | 산출물 | 완료 기준 |
| --- | --- | --- |
| 도메인 확정 | `app.gops.<보유도메인>`, `api.gops.<보유도메인>`, `ws.gops.<보유도메인>` 같은 host | 서비스별 host/path 라우팅 가능. 도메인 확보 전까지 임시 ALB DNS 사용 |
| ACM 인증서 발급 | TLS certificate | Route 53 DNS validation 완료 |
| ALB Ingress 작성 | Kubernetes Ingress | ALB 자동 생성 |
| ALB health check 설정 | `/health`, `/ready` | unhealthy Pod로 트래픽이 가지 않음 |
| WebSocket idle timeout 설정 | ALB attribute `300초` | 장기 연결이 불필요하게 끊기지 않음 |
| JWT 전달 경로 확정 | HTTP Authorization header, WebSocket handshake token | API/WebSocket 인증 경로 확정 |
| Route 53 record 생성 | A/AAAA Alias record | 도메인이 ALB로 연결 |
| HTTP to HTTPS redirect 설정 | ALB listener rule | 평문 HTTP 접근 차단 |

ACM은 AWS Certificate Manager입니다. HTTPS 인증서를 발급하고 갱신해주는 서비스입니다. ALB에 ACM 인증서를 붙이면 사용자가 브라우저에서 안전하게 HTTPS로 접속할 수 있습니다.

### 17.7 RDS PostgreSQL 작업

| 작업 | 산출물 | 완료 기준 |
| --- | --- | --- |
| DB subnet group 생성 | private data subnet group | RDS가 public subnet에 생성되지 않음 |
| RDS PostgreSQL 생성 | RDS instance | EKS에서 private endpoint로 접속 가능 |
| Multi-AZ 설정 | Multi-AZ RDS | RPO 0 목표에 맞는 장애 대응 기반 확보 |
| Storage encryption 설정 | KMS encrypted storage | 저장 데이터 암호화 |
| Automated backup/PITR 설정 | backup retention 7~35일 | 특정 시점 복구 가능 |
| Parameter group 작성 | connection/log/timezone 설정 | 운영 설정 코드화 |
| Security Group 연결 | `sg-postgres` | EKS node SG에서만 `5432` 접근 |
| DB migration 실행 경로 정의 | 별도 migration job 또는 manual approval step | 앱 배포와 migration 순서가 분리됨 |
| 복구 리허설 | staging restore test | snapshot/PITR 복구 절차 확인 |

RPO 0 목표 때문에 RDS는 비용 우선 단계에서도 너무 약하게 줄이지 않는 것을 권장합니다. Single-AZ와 느슨한 백업은 비용은 낮지만 AZ 장애 때 데이터 손실 가능성을 키웁니다.

### 17.8 Amazon MSK 작업

| 작업 | 산출물 | 완료 기준 |
| --- | --- | --- |
| MSK cluster 생성 | Amazon MSK | private data subnet에서 broker 실행 |
| Broker subnet/AZ 배치 | active/standby AZ subnet | 2AZ active/passive 전략 반영 |
| 인증 방식 설정 | TLS + IAM 또는 SASL/SCRAM | Producer/Consumer 인증 성공 |
| Encryption 설정 | in-transit, at-rest encryption | 데이터 암호화 적용 |
| Topic 생성 | `market.ticks.v1`, `market.candles.live.1m.v1`, `market.candles.closed.v1`, `orders.commands.v1`, `broker.submit-results.v1`, `broker.order-events.v1`, `orders.dlq.v1` | Terraform/GitOps로 topic 선언 |
| Durability 설정 | replication factor, `min.insync.replicas`, producer `acks=all` | 승인된 메시지 손실 최소화 |
| Monitoring 설정 | broker disk, consumer lag, under-replicated partitions | 주요 지표 알림 가능 |
| Bootstrap broker secret 생성 | Secrets Manager 또는 ConfigMap | 애플리케이션이 broker 주소 사용 가능 |

MSK는 Managed Streaming for Apache Kafka입니다. Kafka broker 운영을 AWS가 맡아주는 서비스지만, topic 설계, retention, producer 설정, consumer lag 알림은 DevOps/개발팀이 직접 정해야 합니다.

### 17.9 Redis/캐시 작업

| 작업 | 산출물 | 완료 기준 |
| --- | --- | --- |
| Redis 운영 방식 확정 | ElastiCache Redis/Valkey 또는 대체안 | 캐시 장애 영향 범위 문서화 |
| Subnet group 생성 | private data subnet group | public 노출 없음 |
| Security Group 연결 | `sg-redis` | EKS node SG에서만 접근 |
| Eviction/TTL 정책 정의 | cache policy | 메모리 초과 시 서비스 영향 예측 가능 |
| Snapshot 필요 여부 결정 | snapshot policy | Redis를 영속 저장소로 쓰지 않는지 확인 |

Redis는 캐시로 쓰는 것이 기본입니다. 캐시는 유실되어도 원천 데이터에서 다시 만들 수 있어야 합니다. 주문/체결 같은 중요한 데이터는 Redis에만 저장하면 안 됩니다.

### 17.10 ClickHouse EC2 작업

| 작업 | 산출물 | 완료 기준 |
| --- | --- | --- |
| EC2 instance 생성 | 현재 `t3.large`, 시연 전 `r7i.large` 검토 | private data subnet active AZ에 배치 |
| EBS volume 생성 | gp3 100~200 GiB부터 시작 | 암호화와 snapshot 가능 |
| Security Group 연결 | `sg-clickhouse` | EKS node SG에서만 `8123/9000` 접근 |
| OS hardening | SSM, patch, no public IP | SSH public 접근 없음 |
| ClickHouse 설치 | systemd service 또는 패키지 설치 | 재부팅 후 자동 시작 |
| S3 backup 설정 | backup bucket, IAM role | 주기적 백업 파일 생성 |
| Restore 테스트 | staging restore | 샘플 테이블 복구 확인 |
| 최종 시연 전 standby 강화 | replica 또는 warm standby | active AZ 장애 시 조회 복구 가능 |

ClickHouse는 분석 저장소로 보고, 원천 데이터는 MSK/S3/RDS에 남깁니다. 이 원칙을 지키면 현재 단계에서 ClickHouse replica를 늦춰도 데이터 손실 0 목표를 유지하기 쉽습니다.

### 17.11 Ontotext GraphDB EC2 작업

| 작업 | 산출물 | 완료 기준 |
| --- | --- | --- |
| EC2 instance 생성 | 현재 `t3.large`, 시연 전 `r7i.large` 검토 | private data subnet active AZ에 배치 |
| EBS volume 생성 | gp3 100 GiB부터 시작 | FIBO import와 repository 저장 가능 |
| JVM heap 설정 | GraphDB runtime config | import 중 OOM 방지 |
| Ontotext GraphDB 설치 | EC2 systemd service | 재부팅 후 자동 시작 |
| FIBO import | GraphDB repository | Ontology Service가 SPARQL 조회 가능 |
| Security Group 연결 | `sg-graphdb` | Ontology/AI Agents에서만 접근 |
| Backup 설정 | repository export 또는 EBS snapshot | FIBO repository 복구 가능 |
| Restore 테스트 | repository reimport | 복구 시간과 절차 확인 |

### 17.12 S3/Glue/Athena 작업

| 작업 | 산출물 | 완료 기준 |
| --- | --- | --- |
| Terraform state bucket 생성 | `gops-${env}-terraform-state` | versioning/encryption 활성화 |
| 데이터 원본 bucket 생성 | `gops-${env}-raw-market-data` | Alpaca 시장 데이터 원본 보존 |
| Flink checkpoint bucket 생성 | `gops-${env}-flink-checkpoint` | Flink checkpoint/savepoint 저장과 복구 가능 |
| Backup bucket 생성 | `gops-${env}-backup` | ClickHouse/GraphDB export 저장과 restore 테스트 가능 |
| Access log bucket 생성 | `gops-${env}-logs` | ALB/VPC/CloudTrail 로그 저장 |
| Athena query result bucket 생성 | `gops-${env}-athena-results` | Athena workgroup 결과 저장 위치 지정 |
| Artifact bucket 생성 여부 결정 | `gops-${env}-artifacts` | 임시 산출물 저장 필요 여부 확정 |
| S3 lifecycle 설정 | bucket별 transition/delete policy | 비용 증가 방지 |
| S3 보안 설정 | Block Public Access, SSE-KMS, bucket policy | public 노출 방지와 암호화 적용 |
| S3 VPC Endpoint policy 작성 | endpoint policy | private subnet의 필요한 role만 S3 접근 |
| Glue Data Catalog 구성 | database/table | Athena에서 S3 데이터 조회 가능 |
| Athena workgroup 생성 | query result location, cost limit | 쿼리 결과 저장과 비용 제한 적용 |

### 17.13 외부 API 연동 작업

| 작업 | 산출물 | 완료 기준 |
| --- | --- | --- |
| Alpaca credential 저장 | `ALPACA_API_KEY`, `ALPACA_API_SECRET` | Market Data Ingestor에서 시장 데이터 API 호출 가능 |
| 한국투자증권 모의투자 credential 저장 | `KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCOUNT_NO` | KIS Broker Adapter에서 모의투자 호출 가능 |
| OpenAI credential 저장 | `OPENAI_API_KEY` | AI Agents 후속 단계에서 Secret 조회 가능 |
| JWT secret 저장 | `JWT_ACCESS_SECRET`, `JWT_REFRESH_SECRET` | API Server/WebSocket Gateway에서 토큰 서명 검증 가능 |
| NAT egress IP 확인 | NAT Gateway Elastic IP | 외부 API가 IP allowlist를 요구할 경우 제공 가능 |
| Rate limit 정책 작성 | retry/backoff/circuit breaker 기준 | API 장애/제한 시 폭주 방지 |

### 17.14 관측/알림 작업

| 작업 | 산출물 | 완료 기준 |
| --- | --- | --- |
| CloudWatch Logs group 생성 | 서비스별 log group | EKS/EC2/RDS/MSK 로그 조회 가능 |
| Container Insights 검토 | EKS metrics | Pod/node metric 확인 |
| Prometheus/Grafana 설치 | observability namespace | 서비스 대시보드 확인 |
| CloudWatch Alarm 생성 | ALB 5xx, target unhealthy, NAT error, RDS CPU/storage, MSK lag, EC2 disk | 장애 조건 알림 |
| SNS 알림 채널 생성 | email/Slack webhook 연동 | 알림 수신 확인 |
| CloudTrail log 보존 | S3/CloudWatch | AWS API 감사 가능 |
| AWS Config 검토 | config recorder/rules | 보안 그룹 public open 같은 drift 탐지 |

### 17.15 백업/복구/시연 전 전환 작업

| 작업 | 산출물 | 완료 기준 |
| --- | --- | --- |
| RDS restore 리허설 | staging restored DB | PITR/snapshot 복구 성공 |
| MSK consumer 재처리 리허설 | replay runbook | topic offset 조정과 재처리 확인 |
| ClickHouse restore 리허설 | restored table | S3 backup/EBS snapshot 복구 성공 |
| GraphDB restore 리허설 | restored repository | FIBO repository 재import 확인 |
| standby AZ 전환 리허설 | failover runbook | standby Node Group scale-up 후 서비스 접근 가능 |
| 최종 시연 전 ASG 변경 | warm standby ASG values | standby min/desired 1 이상 적용 |
| 최종 시연 전 ClickHouse 강화 | replica 또는 warm standby | 장애 시 분석 조회 복구 가능 |
| Rollback 절차 검증 | deployment rollback log | 이전 이미지로 되돌리기 성공 |

### 17.16 AWS 작업 완료 기준

| 영역 | 완료 기준 |
| --- | --- |
| 네트워크 | public/private app/private data subnet, route table, NAT, VPC Endpoint가 Terraform으로 재현 가능 |
| 보안 | 모든 민감정보가 Secrets Manager에 있고, DB/MSK/Redis/ClickHouse/GraphDB는 public 접근 불가 |
| EKS | ECR image pull, ALB Ingress, HPA, ASG scale-out이 동작 |
| 데이터 | RDS/MSK/S3 원천 데이터 보존 경로가 있고 RPO 0 방향 설정 적용 |
| EC2 데이터 서비스 | ClickHouse와 GraphDB가 private EC2에서 실행되고 백업/복구 테스트 완료 |
| 관측 | 장애 알림이 실제 수신되고 주요 대시보드에서 원인 추적 가능 |
| 시연 준비 | standby AZ와 ClickHouse 강화 전환이 문서화되고 리허설 완료 |

## 18. IaC 작업 분해

Terraform 기준 작업 단위 예시입니다. Terraform은 인프라를 코드로 선언하고 생성/수정/삭제하는 도구입니다.

| 모듈 | 생성 리소스 |
| --- | --- |
| `network` | VPC, subnets, route tables, IGW, NAT Gateway, VPC Endpoints |
| `security` | security groups, IAM roles, KMS keys |
| `eks` | EKS cluster, node groups, add-ons, Pod Identity |
| `ecr` | 서비스별 ECR repositories |
| `storage` | S3 buckets, lifecycle, versioning |
| `database` | RDS PostgreSQL, ElastiCache, EC2 self-managed ClickHouse, Ontotext GraphDB |
| `streaming` | Amazon MSK. 네트워크는 private data subnet 사용 |
| `observability` | CloudWatch, Prometheus/Grafana, alarms |
| `dns` | Route 53 records, ACM certificates |

## 19. Kubernetes 작업 분해

Helm 또는 Kustomize 기준으로 구성합니다.

Helm은 Kubernetes YAML을 템플릿으로 관리하는 패키지 도구입니다. 같은 앱을 dev/staging/prod에 배포할 때 값만 바꿔 재사용하기 좋습니다.

Kustomize는 YAML 원본을 두고 환경별 patch를 얹는 도구입니다. 템플릿 문법을 많이 쓰지 않고 Kubernetes 기본 스타일에 가깝게 관리할 수 있습니다.

| 작업 | 산출물 |
| --- | --- |
| Namespace 생성 | `namespaces/*.yaml` |
| 공통 설정 | `ConfigMap`, `SecretStore`, `ExternalSecret` |
| 서비스 배포 | 서비스별 `Deployment`, `Service`, `HPA`, `PDB` |
| Ingress | ALB Ingress 또는 Gateway API |
| Observability | ServiceMonitor, log annotation, dashboard |
| Flink | FlinkDeployment, checkpoint/savepoint 설정 |

## 20. 운영 전 체크리스트

### 인프라

- [x] VPC CIDR은 `10.20.0.0/16`으로 확정
- [x] Region은 서울(`ap-northeast-2`)로 확정
- [x] 2AZ active/passive 운영으로 확정
- [x] public/private-app/private-data subnet CIDR 확정
- [x] NAT Gateway는 개발 단계 AZ-a만 배치, 시연 전 AZ-b 추가로 확정
- [x] VPC Endpoint 목록은 S3, ECR API, ECR DKR, CloudWatch Logs, Secrets Manager, STS로 확정
- [x] Route 53 host는 `app`, `api`, `ws` 분리. 도메인 전까지 임시 ALB DNS 사용

### EKS

- [x] Cluster endpoint는 public 제한 + private 활성화로 확정
- [x] Node Group은 개발 `t3.large`, 시연 전 `m7i.large`로 확정
- [x] 현재 저비용 ASG min/desired/max 확정
- [x] 최종 시연 전 warm standby ASG min/desired/max로 변경 확정
- [ ] EKS Node Group Launch Template 작성
- [ ] AWS Load Balancer Controller 설치
- [ ] EBS CSI Driver 설치
- [ ] Cluster Autoscaler 또는 Karpenter 설치
- [ ] EKS Pod Identity 설정

### 애플리케이션

- [ ] 서비스별 Dockerfile 준비
- [ ] `/health`, `/ready` 엔드포인트 구현
- [ ] 환경변수 목록 확정
- [x] DB migration은 앱 시작 시 자동 실행하지 않는 것으로 확정
- [ ] WebSocket drain 처리 구현
- [x] API 인증 방식은 자체 JWT로 확정
- [ ] JWT access/refresh token 발급/검증 구현
- [ ] WebSocket JWT 인증 구현
- [ ] 외부 API timeout/retry/rate limit 구현
- [x] 프론트엔드는 EKS에서 서빙하기로 확정

### 데이터

- [x] PostgreSQL은 RDS PostgreSQL로 확정
- [x] Redis 운영 방식은 AWS ElastiCache Redis로 확정
- [x] Kafka는 Amazon MSK로 확정
- [x] ClickHouse는 현재 EC2 self-managed 저비용 구성, 최종 시연 전 standby 강화
- [x] GraphDB는 EC2에 Ontotext GraphDB + FIBO 배포로 확정
- [x] ClickHouse는 개발 `t3.large`/gp3 100GiB/일 1회 백업/cold standby, 시연 전 `r7i.large`/gp3 200GiB 이상/warm standby로 확정
- [ ] Ontotext GraphDB 세부 스펙은 MVP 제외 후속 단계에서 확정
- [x] RPO는 데이터 손실 0 우선 방향으로 확정
- [x] 백업/복구 리허설은 주요 릴리스 전 1회, 시연 1주 전 1회, 이후 월 1회로 확정

### 보안/관측

- [ ] Secrets Manager secret 생성
- [ ] 서비스별 IAM Role 최소 권한 작성
- [ ] CloudTrail/EKS audit log 활성화
- [ ] 로그/메트릭/트레이스 수집 확인
- [ ] 주요 알림 룰 생성
- [x] MVP는 rate limit 우선 적용, WAF는 후속 적용으로 확정

## 21. 미확정 항목

MVP 범위에서 남은 미확정 운영 선택지는 없다. 아래 항목은 MVP 제외 또는 후속 단계에서 다룬다.

| 질문 | 왜 필요한가 |
| --- | --- |
| OpenAI 사용량, rate limit, 비용 알림 기준 | AI Agents 고도화는 MVP 제외 후속 단계입니다. |
| Ontotext GraphDB instance type, JVM heap, EBS 크기 | 온톨로지/GraphRAG는 MVP 제외 후속 단계입니다. |

RTO는 Recovery Time Objective로, 장애 후 몇 분 안에 복구해야 하는지를 뜻합니다. RPO는 Recovery Point Objective로, 장애 때 최대 몇 분/시간치 데이터 손실을 허용하는지를 뜻합니다. 현재 방향은 RTO보다 RPO를 우선하며, 데이터 손실 0을 목표로 합니다.

## 22. 1차 구현 순서

1. VPC, subnet, route table, NAT Gateway, VPC Endpoint를 Terraform으로 생성합니다.
2. EKS cluster와 Managed Node Group을 private app subnet에 생성합니다.
3. ECR repository를 서비스별로 생성하고 이미지 push 절차를 정합니다.
4. AWS Load Balancer Controller, EBS CSI Driver, External Secrets Operator, observability stack을 설치합니다.
5. RDS PostgreSQL, Redis, Kafka, EC2 self-managed ClickHouse를 private data subnet에 배치합니다. Ontotext GraphDB는 후속 단계에서 배치합니다.
6. 서비스별 Deployment, Service, HPA, PDB, Ingress를 작성합니다.
7. staging에 먼저 배포하고 smoke test와 장애/롤백 리허설을 수행합니다.
8. prod 배포 전 백업/복구, 알림, runbook을 검증합니다.

## 23. DevOps 산출물 목록

| 산출물 | 파일/시스템 예시 |
| --- | --- |
| 인프라 코드 | `infra/terraform/*` |
| Kubernetes manifest | `k8s/base`, `k8s/overlays/staging`, `k8s/overlays/prod` |
| Helm chart | `charts/gops-services` |
| 운영 문서 | `docs/runbook.md`, `docs/rollback.md` |
| Secret 목록 | Secrets Manager path 설계 문서 |
| 대시보드 | Grafana dashboard JSON 또는 Terraform-managed dashboard |
| 알림 룰 | PrometheusRule 또는 CloudWatch Alarm |
