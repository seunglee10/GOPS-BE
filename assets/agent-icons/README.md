# Agent Icons

GOPS 에이전트 UI에서 사용할 SVG 아이콘 자산이다.

## 파일 규칙

- 파일명은 `agent-01.svg`부터 `agent-12.svg`까지 순번 기반으로 관리한다.
- 원본 `Artboard N.svg`의 번호는 `agent-NN.svg`에 그대로 대응한다.
- 아직 각 아이콘의 역할이 확정되지 않았으므로 임의의 역할명은 붙이지 않는다.
- 에이전트 역할이 확정되면 별도 manifest에서 역할과 아이콘 파일을 매핑한다.

## 사용 예시

```ts
const agentIconUrl = "/assets/agent-icons/agent-01.svg";
```

React/Vite 앱 구조가 만들어진 뒤에는 이 디렉터리를 `public/assets/agent-icons`로 옮기거나, 번들 import 방식에 맞춰 참조 경로를 조정한다.
