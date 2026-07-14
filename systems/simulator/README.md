# GOPS Demo Simulator

토요일 시연에서 사용하는 5분짜리 Alpaca 호환 체결·호가·뉴스 시뮬레이터다.

- 기본 시나리오: `saturday-demo-amd-iff-oke`
- AMD, IFF, OKE를 종목별 300 체결 + 300 호가로 재생
- 시장 조망부터 장 마감·복기까지 8단계 수동 이동
- T+03:30 지정학 이벤트 뒤 AMD 약세, OKE 강세로 전환
- IFF 삼각 수렴·돌파, AMD 위험 관리, OKE 수혜 시나리오
- 시뮬레이터 메모리 원장과 영구 가상계좌(paper) 모두 실제 KIS와 분리
- 모든 합성 이벤트에 `simulator.source=gops-simulator`와
  `marketSession=regular`를 붙이고 ClickHouse·raw/processed S3 영구 적재에서는 제외

## Operator Flow

1. `scripts/aws/start-dev-simulator.sh`를 실행한다.
2. GOPS 상단에서 LIVE를 SIM으로 전환한다.
3. 상단의 `다음 시연 단계` 버튼 또는 시뮬레이터 제어 화면의 단계 버튼으로 진행한다.
4. `지정학 이벤트` 단계에서 가짜 속보와 AMD 하락·OKE 상승을 확인한다.
5. 속보의 `대응 레이아웃 적용`을 눌러 포트폴리오 대응 패널을 연다.
6. 종료 뒤 `scripts/aws/stop-dev-simulator.sh`를 실행한다.

종료 스크립트는 시뮬레이터를 LIVE 모드로 되돌린 뒤 AMD/IFF/OKE의 Redis 임시
캔들·체결·호가·오더플로우를 제거한다. 프런트는 SIM→LIVE 전환을 감지하면 브라우저
차트 런타임을 비우고 실시간 스냅샷과 WebSocket을 다시 연결한다.

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
