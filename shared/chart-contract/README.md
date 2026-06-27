# GOPS Shared Chart Contract

이 디렉터리는 사용자 UI와 LLM Agent가 함께 사용하는 chart tool contract의 기준 위치다.

현재는 개발 기준선이므로 JSON schema와 capability manifest reference를 둔다. TypeScript와 Python 코드는 이 contract를 mirror한다.

Current mirrors:

- `frontend/src/chart/types.ts`
- `frontend/src/chart/capabilities.ts`
- `backend/app/contracts/chart.py`

Contract rules:

- LLM은 `ChartDocument`를 직접 수정하지 않고 `ChartProposal`을 반환한다.
- 사용자 UI와 LLM Agent는 같은 `ChartCommand` type을 사용한다.
- command payload는 JSON-serializable이어야 한다.
- command validation을 통과하지 못하면 chart state를 변경하지 않는다.
- proposal 하나는 chart-local history에서 하나의 undo/redo 단위가 된다.
- layout+chart mixed proposal은 Workspace-level grouped history가 준비되기 전까지 제외한다.
- Drawing/annotation command는 pixel coordinate가 아니라 data-coordinate anchor를 사용한다.
- `horizontalLine`은 가격 레벨 자체가 의미이므로 timestamp 없는 price-only anchor를 허용한다. 다른 drawing은 도구별 registry가 요구하는 time/value anchor를 따라야 한다.
- LLM drawing/comparison proposal은 preview-first이며, apply 전에는 `ChartDocument.drawings`를 변경하지 않는다.
- 새 LLM drawing proposal은 기존 pending preview를 덮어쓴다.
- `ChartPendingPreview.visible`은 preview layer 표시 여부다. 숨긴 preview는 pending 상태로 남지만 apply할 수 없고, 다시 표시한 뒤 적용한다.
- Applied preview는 일반 editable drawing object가 되며 chart-local undo/redo 대상이다.
- shared canonical chart command schema는 runtime이 이해하는 전체 command set이다. backend OpenAI generation schema는 LLM이 직접 생성할 수 있는 안전 subset이며, OpenAI strict JSON schema 제약에 맞춰 nullable-required 형태를 사용할 수 있다.

Current status:

```text
Chart Tool Runtime V1 core implementation baseline + validation hardening backlog
```

Next target:

```text
Validation hardening: browser regression, multi-chart scenario, reference behavior comparison, real provider transition policy
```
