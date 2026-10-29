-- 종목 한글 이름·거래소 한글 표기.
--
-- 목업은 종목을 "엔비디아", "마이크로소프트" 처럼 한글로 보여 준다. 영문
-- 이름은 공급자(Alpaca) 메타데이터에서 오지만 한글 이름은 어디서도 오지 않아
-- 저장소가 직접 들고 있어야 한다.
--
-- instruments 에 FK 를 걸지 않고 symbol 을 키로 둔다. 한글 이름은 종목 등록
-- (symbol-registry-sync) 보다 먼저 적재될 수 있고, instruments 행이 아직
-- 없어도 API 가 nameKo 를 돌려줘야 하기 때문이다. 원본은
-- systems/market-data/shared/market_data/reference/instrument_names_ko.json 이며
-- instrument-names-ko-seed 잡이 같은 내용을 여기에 upsert 한다.

CREATE TABLE IF NOT EXISTS instrument_names_ko (
    symbol TEXT PRIMARY KEY,
    name_ko TEXT NOT NULL,
    exchange_ko TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE instrument_names_ko IS
    '종목 한글 이름. symbol 은 instruments.canonical_symbol 과 같은 표기(예: BRK.B)지만 FK 는 두지 않는다.';
COMMENT ON COLUMN instrument_names_ko.exchange_ko IS
    '거래소 한글 표기. 나스닥 | NYSE | CBOE. 모르면 NULL.';
