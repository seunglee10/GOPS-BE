# KIS 모의투자 실행 가이드

한국투자증권 Open API로 해외주식 주문과 국내주식 잔고조회/현금주문을 보내는 최소 골격입니다.

기본 실행 방식은 Docker입니다. Python 버전은 컨테이너 안에서 3.12를 사용합니다.

## 1. 준비물

계좌는 이미 있다고 가정합니다. 아래 값만 준비하세요.

- 한국투자증권 Open API 모의투자 앱키
- 한국투자증권 Open API 모의투자 앱시크릿
- 모의투자 계좌번호 앞 8자리

계좌번호가 `12345678-01` 형태라면:

- `12345678` -> 계좌번호 앞 8자리
- `01` -> 계좌상품코드

## 2. 환경파일 만들기

```bash
cp .env.example .env
```

`.env` 파일에서 모의투자 값만 먼저 채우면 됩니다.

```bash
KIS_ENV=demo

KIS_DEMO_APP_KEY=모의투자_앱키
KIS_DEMO_APP_SECRET=모의투자_앱시크릿
KIS_DEMO_ACCOUNT_NO=계좌번호_앞8자리

KIS_ACCOUNT_PRODUCT_CODE=01
KIS_DEFAULT_EXCHANGE=NASD
KIS_DEFAULT_CURRENCY=USD
```

실전투자를 하지 않을 거면 `KIS_REAL_*` 값은 비워둬도 됩니다.

## 3. Docker 이미지 빌드

```bash
docker compose build
```

정상 빌드되면 `gops-kis-trader:py3.12` 이미지가 만들어집니다.

Python 버전 확인:

```bash
docker run --rm --entrypoint python gops-kis-trader:py3.12 --version
```

예상 출력:

```text
Python 3.12.x
```

## 4. 국내주식과 해외주식 명령어 구분

이 프로젝트는 국내주식과 해외주식 명령어를 분리해서 씁니다.

| 구분 | 명령어 | 용도 |
| --- | --- | --- |
| 국내주식 | `domestic-balance` | 국내주식 잔고조회 |
| 국내주식 | `domestic-order` | 국내주식 현금주문 |
| 해외주식 | `balance` | 해외주식 잔고조회 |
| 해외주식 | `order` | 해외주식 주문 |
| 해외주식 | `ccnl` | 해외주식 주문/체결내역 조회 |

공통으로 앞부분은 같습니다.

| 부분 | 의미 |
| --- | --- |
| `docker compose run` | Compose 서비스 컨테이너를 1회 실행 |
| `--rm` | 실행이 끝난 컨테이너 삭제 |
| `kis-trader` | `compose.yaml`에 정의된 서비스 이름 |
| `--env demo` | 한국투자증권 모의투자 환경 사용 |
| `--env real` | 한국투자증권 실전투자 환경 사용 |

## 5. 국내주식: 삼성전자 주문

삼성전자 종목코드는 `005930`입니다. 아래 명령은 삼성전자 1주를 70,000원 지정가로 모의투자 매수 주문합니다.

```bash
docker compose run --rm kis-trader domestic-order --env demo --symbol 005930 --side buy --qty 1 --price 70000 --submit
```

각 부분의 의미:

| 부분 | 의미 |
| --- | --- |
| `domestic-order` | 국내주식 현금주문 API 사용 |
| `--env demo` | 모의투자 주문 |
| `--symbol 005930` | 삼성전자 종목코드 |
| `--side buy` | 매수 주문. 매도는 `sell` |
| `--qty 1` | 1주 주문 |
| `--price 70000` | 지정가 70,000원. 예시 가격이며 체결을 보장하지 않음 |
| `--submit` | 실제로 주문 API 호출. 없으면 dry-run 미리보기만 출력 |

주문 전 미리보기만 하려면 `--submit`을 빼고 실행합니다.

```bash
docker compose run --rm kis-trader domestic-order --env demo --symbol 005930 --side buy --qty 1 --price 70000
```

삼성전자 매도 주문 예시:

```bash
docker compose run --rm kis-trader domestic-order --env demo --symbol 005930 --side sell --qty 1 --price 70000 --sll-type 01 --submit
```

`--sll-type 01`은 일반매도 유형입니다.

## 6. 국내주식: 잔고조회

```bash
docker compose run --rm kis-trader domestic-balance --env demo
```

각 부분의 의미:

| 부분 | 의미 |
| --- | --- |
| `domestic-balance` | 국내주식 잔고조회 API 사용 |
| `--env demo` | 모의투자 계좌 조회 |
| `--inqr-dvsn 02` | 종목별 조회. 기본값이라 생략 가능 |
| `--inqr-dvsn 01` | 대출일별 조회 |

대출일별로 보고 싶으면:

```bash
docker compose run --rm kis-trader domestic-balance --env demo --inqr-dvsn 01
```

## 7. 해외주식: 잔고조회

먼저 API 키와 계좌 설정이 맞는지 해외주식 잔고조회로 확인합니다.

```bash
docker compose run --rm kis-trader balance --env demo --exchange NASD --currency USD
```

각 부분의 의미:

| 부분 | 의미 |
| --- | --- |
| `balance` | 해외주식 잔고조회 API 사용 |
| `--env demo` | 모의투자 계좌 조회 |
| `--exchange NASD` | 나스닥 거래소 조회 |
| `--currency USD` | USD 기준 조회 |

정상이라면 JSON 응답이 출력됩니다. 여기서 오류가 나면 보통 `.env`의 앱키, 앱시크릿, 계좌번호가 잘못된 경우입니다.

## 8. 해외주식: 주문 전송 없이 미리보기

실제 주문을 보내기 전에 dry-run으로 주문 body와 TR ID를 확인합니다.

```bash
docker compose run --rm kis-trader order --env demo --symbol AAPL --side buy --qty 1 --price 145.00
```

이 명령은 주문을 보내지 않습니다. `dry_run: true`가 나오면 정상입니다.

각 부분의 의미:

| 부분 | 의미 |
| --- | --- |
| `order` | 해외주식 주문 API 사용 |
| `--env demo` | 모의투자 주문 |
| `--symbol AAPL` | 애플 티커 |
| `--side buy` | 매수 주문. 매도는 `sell` |
| `--qty 1` | 1주 주문 |
| `--price 145.00` | 지정가 145.00달러 |
| `--submit` 없음 | 주문을 보내지 않고 미리보기만 출력 |

예상되는 핵심 값:

```json
{
  "dry_run": true,
  "preview": {
    "env": "demo",
    "tr_id": "VTTT1002U",
    "body": {
      "OVRS_EXCG_CD": "NASD",
      "PDNO": "AAPL",
      "ORD_QTY": "1",
      "OVRS_ORD_UNPR": "145.00",
      "ORD_DVSN": "00"
    }
  }
}
```

## 9. 해외주식: 모의투자 주문 보내기

`--submit`을 붙이면 실제로 모의투자 주문 API를 호출합니다.

```bash
docker compose run --rm kis-trader order --env demo --symbol AAPL --side buy --qty 1 --price 145.00 --submit
```

`--submit`은 실제 주문 API를 호출합니다. 지정가 주문은 가격 조건이 맞아야 체결됩니다.

주문 후 바로 주문체결내역까지 조회하려면:

```bash
docker compose run --rm kis-trader order --env demo --symbol AAPL --side buy --qty 1 --price 145.00 --submit --poll-ccnl
```

`--poll-ccnl`은 주문 후 해외주식 주문/체결내역을 이어서 조회합니다.

## 10. 해외주식: 주문체결내역 조회

오늘 주문 내역을 조회합니다.

```bash
docker compose run --rm kis-trader ccnl --env demo
```

특정 날짜를 조회하려면 `YYYYMMDD` 형식으로 입력합니다.

```bash
docker compose run --rm kis-trader ccnl --env demo --start-date 20260624 --end-date 20260624
```

## 자주 바꾸는 옵션

매도 주문:

```bash
docker compose run --rm kis-trader order --env demo --symbol AAPL --side sell --qty 1 --price 145.00 --submit
```

뉴욕거래소 종목:

```bash
docker compose run --rm kis-trader order --env demo --exchange NYSE --symbol IBM --side buy --qty 1 --price 150.00 --submit
```

미국 거래소 코드:

- `NASD`: 나스닥
- `NYSE`: 뉴욕
- `AMEX`: 아멕스

## 로컬에서 실행하고 싶을 때

Docker 없이 로컬 Python 3.12 환경에서 실행하려면:

```bash
uv sync
cp .env.example .env
uv run kis-overseas balance --env demo --exchange NASD --currency USD
```

## 안전장치

- `--submit`이 없으면 주문을 보내지 않습니다.
- 기본 환경은 모의투자 `demo`입니다.
- 실전 주문은 `--env real --submit --confirm-real-order REAL_ORDER`를 모두 입력해야만 전송됩니다.
- `.env`는 Docker 이미지에 복사되지 않습니다.
- 토큰 캐시는 Docker volume인 `kis-token-cache`에 저장됩니다.
