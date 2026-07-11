# 07. 프런트 작도 정렬·해설·개발 패널

## 1. v1/v2 client

`analysisAssetsApi.ts`는 `assetVersion` discriminator로 v1/v2를 정규화한다.

- v1은 현재 렌더 동작 유지 + 안전한 H-Line label/timestamp compatibility normalization
- v2는 quality, layer emptyReason, focusItems, structured key levels를 사용
- unknown version/invalid shape는 적용하지 않고 panel error로 격리
- cache/in-flight generation guard는 현재 refine 동작 유지
- asset fetch 실패가 candle chart 자체를 실패시키지 않음

## 2. 자동 anchor resolve

신규 pure module:

```text
src/chart/analysisAssetAnchorResolver.ts
  resolveAnalysisAssetDrawings(asset, currentCandles)
  -> {state: ready|deferred|dropped, readyDrawings, resolvedGeometryByDrawingId,
      deferredOldestTimestamp, dropped[], candleRangeDigest}
```

동작은 02 §7을 따른다.

- exact source candle 우선
- 동등 epoch/candle key만 canonicalize
- loaded range 밖 valid anchor는 bounded older fetch 후 재시도
- loaded range 안 missing anchor는 drop
- nearest candle/continuous timestamp fallback 금지
- drawing 하나의 anchor 중 하나라도 invalid면 drawing 전체 drop
- drop reason을 개발 console 원문이 아니라 bounded telemetry/패널 상태로 노출
- asset latest candleKey가 현재 chart latest closed candleKey보다 뒤처지면 runtime stale이다.
  stale 자산은 보존 row라도 적용하지 않고 `분석 자산 갱신 필요`를 보여준다.
- `resolvedGeometryByDrawingId`는 anchor의 canonical logical slot을 sidecar로 보관한다.
  `chart-asset:` trend/parallel projection과 semantic expansion은 slot slope를 사용한다.
  payload timestamp나 수동 drawing의 elapsed-time projection은 바꾸지 않는다.

`analysisLayerController.ts`에는 이미 사용자 작업으로 interaction mode/selection 보존 변경이
있다. resolver를 별도 module로 만들어 해당 변경을 덮지 않고, controller 호출 직전에
정규화 결과만 넘긴다.

## 3. Flag와 봉 그리드

정상 Flag:

- x = source candle slot center
- y = 02의 event별 price 규범
- label tag 위치는 기존 renderer UI를 유지

`Z`, `.000Z`, UTC offset이 같은 epoch면 같은 center가 되어야 한다. 1D/1W/1M에서 첫·중간·
마지막 봉, pan/zoom, semantic expansion on/off를 visual test로 고정한다.

자동 asset에만 fallback을 금지한다. 수동 drawing의 future/time-gap anchor, drag, edit,
select 동작은 그대로다. 02의 known-candle legacy timestamp alias는 과거 수동 00Z anchor가
같은 봉을 계속 찾게 하는 lookup 호환이며 임의 nearest-snap이 아니다.

## 4. H-Line 가격 label 제거

가격은 이미 price axis marker에 표시되므로 canvas label에는 의미만 남긴다.

- v2 compiler가 anchor 가격 token이 없는 label을 생성. `52주` 같은 의미 숫자는 허용
- v2 schema/runtime validator가 anchor 가격 문자열 포함을 거부
- rollout 중 남은 v1 asset은 프런트 compatibility normalizer가 **해당 anchor 가격과
  정확히 같은 token만** 제거
- 사용자 drawing과 일반 `textLabel`은 절대 sanitize하지 않음
- price-axis marker 렌더와 값은 그대로 유지

예:

```text
지지 183.42 · 매물대 -> 지지 · 매물대
월봉 저항 210.00    -> 월봉 저항
```

현재 dirty 변경에는 H-Line label editor 위치 보정이 포함되어 있다. 가격 제거 작업은
compiler/asset normalizer에서 하고 그 렌더 위치 변경을 건드리지 않는다.

## 5. 3개 layer 토글

기존 `구조 / 추세 / 인사이트` 버튼과 기본 ON은 유지한다.

v2 상태:

| 상태 | 버튼 |
| --- | --- |
| asset 없음 | disabled, `분석 자산 없음` |
| layer에 quality-pass drawing 있음 | enabled |
| 정상 no-draw | disabled, `현재 기준을 통과한 작도 없음` |
| data insufficient | disabled, `데이터 품질 확인 필요` |
| I만 LLM degraded | S/T 정상, I disabled `인사이트 생성 축약` |

토글은 사용자 drawing을 건드리지 않고 `chart-asset:` group만 처리한다. clear-all 보호,
selection/mode 보존, external history 동작은 현재 refine/dirty 변경을 유지한다.

## 6. 초기 history와 off-viewport anchor

화면 밖 anchor가 있다는 이유만으로 line을 숨기지 않는다. v2 selected drawing의 가장 오래된
anchor가 현재 candle array 밖이면:

1. current relevance·quality가 이미 통과했는지 확인
2. 기존 older-range loader로 필요한 범위만 요청
3. 요청 cap은 asset displayBars의 1.5배
4. 응답 후 exact resolve해서 적용
5. cap을 넘거나 데이터가 없으면 drawing drop

asset 때문에 모든 심볼의 기본 candle limit를 늘리지는 않는다. 의미 있는 selected drawing이
필요할 때만 bounded fetch한다.

resolve state 전이:

- `deferred`에서는 applied terminal key를 기록하지 않고 older fetch만 시작한다.
- 현재 적용된 자산이 runtime fresh면 fetch 동안 유지할 수 있다. stale이면 즉시 숨기고 pending
  상태를 보여준다.
- 응답으로 oldest candle 또는 candle identity set이 바뀌면 `candleRangeDigest`가 바뀌어 같은
  asset을 다시 resolve한다.
- applied key는 `asset generatedAt/buildDigest + candleRangeDigest + terminal state`를 포함하고
  `ready|dropped`에서만 기록한다.
- symbol/interval request generation guard가 늦게 도착한 older-fetch 응답의 재적용을 막는다.

## 7. 자동 지표

- volume profile + volume 기존 always-on 유지
- v2 recommended는 최대 1개
- asset 적용 시 현재 사용자가 명시적으로 끈/설정한 layer 정책을 침범하지 않는 기존 규칙 유지
- commentary focus item에 추천 이유와 관찰점을 표시
- interval 전환 시 해당 interval recommendation만 적용

## 8. 해설 패널 정보 구조

위에서 아래 순서:

```text
[1D] [분석 기준] [품질/신뢰도] [데이터 상태]
Headline
현재 구조
차트에서 볼 것
  [구조 아이콘] 저항 · 매물대
    무엇: 최근 두 번 반응한 상단 zone입니다.
    왜: 현재가와 가까운 다음 확인 구간입니다.
    볼 것: 종가 돌파 뒤 재접촉을 지키는지 확인하세요.
  [추세 아이콘] 상승 지지 추세
    ...
확인 조건
무효화 조건
상위 주기 / 반대 근거
데이터 한계
```

요구사항:

- `focusItems`만 “차트에서 볼 것”에 렌더
- focus item의 drawing layer가 OFF면 작은 off 상태 표시
- focus item 클릭/keyboard activation은 해당 layer를 켜고 drawing을 짧게 강조한다.
  geometry를 변경하거나 viewport를 강제 이동하지 않는다.
- 표시 drawing 없는 정상 asset은 no-draw 이유와 다음 확인 조건을 보여준다.
- flat v1 commentary는 기존 UI로 fallback
- `GlossaryText`는 headline/body/focus/condition/caveat 전부에 적용
- `commentary.enrichment`는 계속 null

`ChartPanel`이 analysis layer controller를 소유하고 parent를 통해 CommentaryPanel에
`onFocusDrawing({drawingIds, layerKey})` callback을 전달한다. callback은 현재 asset ID에
속한 drawing만 허용하고 `layer enable → 존재 ID filter → transient highlight` 순서로
실행한다. CommentaryPanel이 별도 drawing state를 만들거나 asset을 재적용하지 않는다.

## 9. 타이포와 스타일

DESIGN.md 승인 롤만 쓴다.

| 내용 | 롤 |
| --- | --- |
| headline/본문 | `body-md` |
| section/focus title/meta/condition | `caption` |

로컬 `font-size/font-weight/line-height/letter-spacing/text-transform` 선언을 추가하지 않는다.
색은 ink/ink-muted/accent-blue와 invalidation caution만 사용한다. 새 decorative card 중첩을
만들지 않는다.

## 10. 개발 패널 요청 반영

### 상단 입력 안내

첫 줄을 좌우 정렬한다.

```text
[ ] 전체 S&P500                                      콤마로 구분
```

- 왼쪽: 기존 checkbox
- 오른쪽: `caption` 롤의 `콤마로 구분`
- 안내문 ID를 textarea `aria-describedby`에 연결
- 전체 S&P500 체크 시 textarea disabled여도 안내문은 유지

현재 parser `split(/[\s,]+/)`는 아래를 모두 지원한다.

```text
NVDA,AAPL
NVDA, AAPL
NVDA AAPL
줄바꿈
```

즉 콤마 앞뒤에 띄어쓰기가 없어도 된다. 사용자 표시는 요청대로 짧게 `콤마로 구분`만 쓴다.
parser 계약은 콤마/공백/줄바꿈 호환을 유지한다.

### 문구 변경

```text
신선 자산 스킵(시간) -> 갱신 스킵(시간)
```

API payload field `skipFreshHours`와 backend 의미는 바꾸지 않는다.

### LLM 예상 호출 수

v2는 심볼당 최대 1회이므로 확인 dialog는 `symbolCount`만 사용한다. interval 수를 곱하지
않는다. rule-only면 dialog가 없다.

### 진행 상태

`unchanged`, `saved_with_warning`, `completed_with_warnings`를 표시하고 terminal로 처리한다.
LLM warning과 실제 failed symbol을 분리해 “실패분 재실행”이 불필요한 LLM degraded를
다시 호출하지 않게 한다.

## 11. 현재 dirty worktree 통합 규칙

다음 파일은 계획 작성 시점에 사용자 변경이 있다.

```text
analysisLayerController.ts
drawings.ts
ChartPanel.tsx
styles.css
analysisAssets.test.ts
drawingTools.test.ts
tests/visual/chart-drawing-tools.spec.ts
```

변경 내용은 interaction 보존, H-Line label 위치, label editor scaling 관련이다.

- 구현 시작 전 diff를 다시 읽는다.
- checkout/reset/revert하지 않는다.
- anchor resolver는 신규 pure file로 분리한다.
- H-Line 텍스트 정책은 backend compiler와 asset-only normalizer에 둔다.
- 기존 dirty test의 기대를 삭제하지 않고 additive case로 확장한다.
- 충돌 조정은 v2 `IMPLEMENTATION_NOTES.md`에 기록한다.

## 12. 테스트

현재 custom test runners/Playwright만 사용한다.

- v1/v2/unknown asset normalization
- asset anchor exact/epoch/candle-key resolve
- invalid in-range anchor drop와 off-range bounded defer
- deferred 재진입, candleRangeDigest, stale asset fail-closed, late response generation guard
- Flag x == candle center: 1D/1W/1M, pan/zoom/expansion
- 수동 future/time-gap drawing 불변
- legacy 00Z 수동 anchor/cursor가 canonical daily candle을 계속 참조
- H-Line label에 anchor price 없음, price axis에는 가격 있음
- user H-Line label은 변경하지 않음
- empty layer 토글 tooltip/disabled
- focus item ↔ drawing ID parity와 layer highlight
- dropped candidate commentary 미노출
- `NVDA,AAPL`, `NVDA, AAPL`, whitespace 입력 동등
- `갱신 스킵(시간)`과 right-aligned `콤마로 구분` 접근성
- v2 LLM call estimate symbol count
- warning/error progress 분리
- DESIGN typography token 검사
- `npm run test:chart`, `test:layout`, `test:chart-visual`, `build`
