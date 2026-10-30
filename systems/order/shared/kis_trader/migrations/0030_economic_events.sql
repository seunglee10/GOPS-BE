-- 경제지표 발표 일정(D28 캘린더의 kind=econ).
--
-- economic-calendar 잡(systems/market-data/jobs/economic-calendar)이 Finnhub
-- 경제 캘린더에서 미국·한국 일정을 받아 6시간마다 upsert 한다. 발표가 끝나면
-- actual 이 채워진다. 실적 일정(kind=earn)은 ClickHouse yahoo_earnings_estimates
-- 를 그대로 읽으므로 여기에 넣지 않는다.
--
-- event_date·event_time_local 은 한국 시간(Asia/Seoul) 기준이다. 목업 캘린더가
-- 미국 CPI 를 "오후 9시 30분" 처럼 한국 시간으로 보여 주기 때문이다.
-- actual·forecast·previous 는 공급자가 준 숫자를 문자열로 두고 단위는 unit 에 따로 둔다.

CREATE TABLE IF NOT EXISTS economic_events (
    event_id TEXT PRIMARY KEY,
    event_date DATE NOT NULL,
    event_time_local TEXT,
    region TEXT NOT NULL CHECK (region IN ('kr', 'us')),
    title_ko TEXT,
    title_en TEXT NOT NULL,
    importance TEXT NOT NULL DEFAULT 'normal' CHECK (importance IN ('major', 'watch', 'normal')),
    actual TEXT,
    forecast TEXT,
    previous TEXT,
    unit TEXT,
    source TEXT NOT NULL DEFAULT 'finnhub',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_economic_events_date_region
    ON economic_events (event_date, region);

COMMENT ON TABLE economic_events IS
    '경제지표 발표 일정. economic-calendar 잡이 Finnhub 에서 받아 upsert 한다. 날짜·시각은 한국 시간.';
COMMENT ON COLUMN economic_events.event_id IS
    '공급자:국가:발표시각(UTC):지표명 해시. 같은 발표는 같은 id 로 다시 들어온다.';
COMMENT ON COLUMN economic_events.event_time_local IS
    '한국 시간 HH:MM. 공급자가 시각을 주지 않으면 NULL.';
COMMENT ON COLUMN economic_events.title_ko IS
    '한글 지표 이름. 매핑표에 없는 지표는 NULL 이고 API 는 title_en 을 쓴다.';
COMMENT ON COLUMN economic_events.importance IS
    'major | watch | normal. Finnhub impact high→major, medium→watch, 나머지 normal. 주요 지표는 매핑표가 하한을 둔다.';
