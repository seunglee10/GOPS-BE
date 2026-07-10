# GOPS Demo Simulator

토요일 시연에서 사용하는 5분짜리 Alpaca 호환 틱/뉴스 시뮬레이터다.

- SIM 토글 직후 재생 시작
- 5초 뒤 이란 휴전 붕괴 속보 공개
- 300초 동안 반도체 5종목과 에너지 3종목의 틱 재생
- 반도체 전용 초기 더미 계좌
- 사용자가 직접 누르는 반도체 매도/에너지 매수만 메모리 원장에 반영
- 실제 KIS 주문 경로와 완전히 분리

EKS에서는 `Deployment/gops-simulator`를 기본 `replicas: 0`으로 유지한다.
시연 직전에 `scripts/aws/start-dev-simulator.sh`, 종료 후에는
`scripts/aws/stop-dev-simulator.sh`를 실행한다.
