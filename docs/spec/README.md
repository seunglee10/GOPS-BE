# GOPS Spec Index

이 폴더는 팀원별 스펙 문서를 Codex가 참고하기 쉽도록 도메인별로 정리한 색인이다.

## 읽는 기준

- 현재 문서들은 완성본이 아니라 구현 중 바뀔 수 있는 참고 스펙이다.
- 기존 스펙 본문은 보존했고, 이번 정리에서는 파일명과 디렉터리 구조만 바꾸었다.
- 우리 담당 구현의 1차 기준은 [10-chart/gops-chart-spec.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/spec/10-chart/gops-chart-spec.md)다.
- 장기 구현에서는 다른 팀원 스펙도 제품 전체의 제약 조건으로 함께 확인한다.
- 프론트엔드 설계와 UI 디자인은 차트 스펙에서 시작하되, 시장 데이터, 주문, 인프라 경계를 함께 고려한다.

## 디렉터리 구조

| 경로 | 역할 |
| --- | --- |
| [00-integrated/gops-integrated-spec.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/spec/00-integrated/gops-integrated-spec.md) | 팀원별 스펙을 하나로 종합한 통합 초안 |
| [10-chart/gops-chart-spec.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/spec/10-chart/gops-chart-spec.md) | 차트, Chart Document, Command Engine, LLM 제안, 렌더링, 프론트엔드 시작점 |
| [20-market-data/market-data-pipeline-spec.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/spec/20-market-data/market-data-pipeline-spec.md) | Alpaca 수집, Kafka/Flink 처리, Redis/S3/ClickHouse 저장, WebSocket/Chart API |
| [30-orders/order-system-reliability-security-spec.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/spec/30-orders/order-system-reliability-security-spec.md) | 주문 시스템 신뢰성, 멱등성, KIS Adapter, 보안 기능명세 |
| [30-orders/order-security-reliability-milestones.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/spec/30-orders/order-security-reliability-milestones.md) | 주문 경로 보안/신뢰성 마일스톤 |
| [40-infrastructure/devops-architecture-spec.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/spec/40-infrastructure/devops-architecture-spec.md) | AWS, EKS, 네트워크, 데이터 계층, 운영, 배포 명세 |

## 이전 경로에서 변경된 경로

| 이전 경로 | 현재 경로 |
| --- | --- |
| `docs/spec/gops-integrated-spec.md` | `docs/spec/00-integrated/gops-integrated-spec.md` |
| `docs/spec/GOPS_CHART_SPEC.md` | `docs/spec/10-chart/gops-chart-spec.md` |
| `docs/spec/market-data-pipeline-spec.md` | `docs/spec/20-market-data/market-data-pipeline-spec.md` |
| `docs/spec/order_system_reliability_security_spec.md` | `docs/spec/30-orders/order-system-reliability-security-spec.md` |
| `docs/spec/security-reliability-milestones.md` | `docs/spec/30-orders/order-security-reliability-milestones.md` |
| `docs/spec/devops-architecture-spec.md` | `docs/spec/40-infrastructure/devops-architecture-spec.md` |

## 참고 순서

1. 프로젝트 방향은 [../README.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/README.md)를 먼저 읽는다.
2. 전체 기술 경계는 [00-integrated/gops-integrated-spec.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/spec/00-integrated/gops-integrated-spec.md)를 확인한다.
3. 우리 담당 구현은 [10-chart/gops-chart-spec.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/spec/10-chart/gops-chart-spec.md)를 기준으로 삼는다.
4. 데이터 입력과 저장 경계가 필요하면 [20-market-data/market-data-pipeline-spec.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/spec/20-market-data/market-data-pipeline-spec.md)를 확인한다.
5. 거래 연결, 주문 상태, 보안 제약이 필요하면 [30-orders](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/spec/30-orders)를 확인한다.
6. 배포, 운영, 인프라 제약은 [40-infrastructure/devops-architecture-spec.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/spec/40-infrastructure/devops-architecture-spec.md)를 확인한다.

## 주의

기존 스펙 본문 내부의 상대 링크나 과거 경로 표현은 원문 보존을 위해 일괄 수정하지 않았다. 문서 탐색은 이 색인을 기준으로 한다.
