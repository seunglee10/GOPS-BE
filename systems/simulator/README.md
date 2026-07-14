# GOPS Demo Simulator

토요일 시연에서 사용하는 5분짜리 Alpaca 호환 체결·호가·뉴스 시뮬레이터다.

- 기본 시나리오: `saturday-demo-amd-iff-oke`
- AMD와 OKE를 종목별 300 체결 + 300 호가로 재생
- `시장 조망 → 지정학 이벤트 → 장 마감·복기` 3단계 수동 이동
- T+03:30 지정학 이벤트 알림 뒤 5초 후 AMD는 약 1분간 완만한 약세, OKE는 더 느린 점진 강세로 전환
- SIM 전환 직후에는 자연스러운 가격 흐름, 첫 다음 버튼부터 이벤트 대응 시나리오
- 시뮬레이터 메모리 원장과 영구 가상계좌(paper) 모두 실제 KIS와 분리
- 모든 합성 이벤트에 `simulator.source=gops-simulator`와
  `marketSession=regular`를 붙이고 ClickHouse·raw/processed S3 영구 적재에서는 제외

## Operator Flow

1. `scripts/aws/start-dev-simulator.sh`를 실행한다.
2. GOPS 상단에서 LIVE를 SIM으로 전환한다.
3. AMD와 OKE의 자연스러운 체결·호가가 시작되는 것을 확인한다.
4. 상단의 첫 `다음 시연 단계` 버튼을 눌러 지정학 이벤트를 즉시 시작한다.
5. 기존 GOPS 알림의 `근거 보기`를 눌러 포트폴리오 대응 패널을 연다.
6. 종료 뒤 `scripts/aws/stop-dev-simulator.sh`를 실행한다.

시뮬레이터 시작 전에는 AMD/OKE의 Redis 캔들·체결·호가·오더플로우를
원본 스냅샷으로 보관한다. 상단 토글이나 종료 스크립트가 LIVE 모드로 되돌아가면
시뮬레이션 임시 상태를 제거한 뒤 이 스냅샷을 복원한다. 프런트는 SIM→LIVE 전환을
감지하면 브라우저 차트 런타임을 비우고 복원된 스냅샷과 WebSocket을 다시 연결한다.

제어 API는 `PUT /api/control/phase`에 `{"phase":"breaking-event"}` 형식으로
단계 ID를 받는다. 프런트 백엔드 프록시는 `PUT /api/simulator/phase`다.

시나리오 파일은
`data/scenarios/saturday-demo-amd-iff-oke/`에 있으며 다음 명령으로 같은 데이터를
결정론적으로 다시 만들 수 있다.

```sh
PYTHONPATH=systems/simulator .venv/bin/python -m gops_simul.tools.build_saturday_demo
```

EKS에서는 `Deployment/gops-simulator`를 기본 `replicas: 0`으로 유지한다.
시연 직전에 `scripts/aws/start-dev-simulator.sh`, 종료 후에는
`scripts/aws/stop-dev-simulator.sh`를 실행한다.
