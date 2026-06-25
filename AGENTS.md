# Repository Agent Rules

1. 코드 수정은 사용자가 명시적으로 요청했을 때만 한다.
2. 사용자가 문서 작업을 요청한 경우 코드 파일은 수정하지 않는다.
3. 코드 수정이 필요해 보이면 먼저 이유와 범위를 설명하고 확인을 받는다.
4. Plan Mode에서는 repo-tracked 파일을 생성, 수정, 삭제하지 않는다.
5. KIS appkey, appsecret, access token, 계좌번호 원문을 로그/문서 예시에 쓰지 않는다.
6. Kafka 메시지와 DB 예시는 `account_alias`, `request_id`, `event_id` 중심으로 작성한다.
7. 실전 주문 경로에서는 timeout 후 같은 주문을 즉시 재POST하지 않는 것을 기본 원칙으로 둔다.
8. 주문 중복 방지는 idempotency key, DB unique constraint, reconciliation 조합으로 설명한다.
9. 국내/해외 주문은 공통 파이프라인으로 다루고 API payload 차이는 adapter 책임으로 분리한다.
10. PostgreSQL과 Kafka를 기본 목표 아키텍처로 가정한다.
11. 문서 변경 시 관련 링크와 파일명을 함께 확인한다.
12. Mermaid 다이어그램은 닫힌 코드블록으로 작성한다.
13. 테스트나 검증 명령을 실행했다면 최종 응답에 결과를 요약한다.
14. 기존 사용자 변경사항은 되돌리지 않는다.
