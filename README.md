# KIS 해외주식 모의투자 실행 가이드

한국투자증권 Open API로 해외주식 모의투자 주문을 보내는 최소 골격입니다.

기본 실행 방식은 Docker입니다. Python 버전은 컨테이너 안에서 3.12를 사용합니다.

## 1. 준비물

계좌는 이미 있다고 가정합니다. 아래 값만 준비하세요.

- 한국투자증권 Open API 모의투자 앱키
- 한국투자증권 Open API 모의투자 앱시크릿
- 모의투자 해외주식 계좌번호 앞 8자리

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

## 4. 주문 전에 잔고조회부터 확인

먼저 API 키와 계좌 설정이 맞는지 잔고조회로 확인합니다.

```bash
docker compose run --rm kis-trader balance --env demo --exchange NASD --currency USD
```

정상이라면 JSON 응답이 출력됩니다. 여기서 오류가 나면 보통 `.env`의 앱키, 앱시크릿, 계좌번호가 잘못된 경우입니다.

## 5. 주문 전송 없이 미리보기

실제 주문을 보내기 전에 dry-run으로 주문 body와 TR ID를 확인합니다.

```bash
docker compose run --rm kis-trader order --env demo --symbol AAPL --side buy --qty 1 --price 145.00
```

이 명령은 주문을 보내지 않습니다. `dry_run: true`가 나오면 정상입니다.

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

## 6. 모의투자 주문 보내기

`--submit`을 붙이면 실제로 모의투자 주문 API를 호출합니다.

```bash
docker compose run --rm kis-trader order --env demo --symbol AAPL --side buy --qty 1 --price 145.00 --submit
```

주문 후 바로 주문체결내역까지 조회하려면:

```bash
docker compose run --rm kis-trader order --env demo --symbol AAPL --side buy --qty 1 --price 145.00 --submit --poll-ccnl
```

## 7. 주문체결내역 조회

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
