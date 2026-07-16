-- Yahoo 실적 일정의 발표 시각과 EPS 결과를 기존 테이블에 안전하게 확장합니다.
ALTER TABLE market_data.yahoo_earnings_estimates ADD COLUMN IF NOT EXISTS event_at Nullable(DateTime64(3, 'UTC')) AFTER analyst_count;
ALTER TABLE market_data.yahoo_earnings_estimates ADD COLUMN IF NOT EXISTS actual_value Nullable(Float64) AFTER event_at;
ALTER TABLE market_data.yahoo_earnings_estimates ADD COLUMN IF NOT EXISTS surprise_percent Nullable(Float64) AFTER actual_value;
ALTER TABLE market_data.yahoo_earnings_estimates ADD COLUMN IF NOT EXISTS event_session LowCardinality(String) DEFAULT 'unknown' AFTER surprise_percent;
ALTER TABLE market_data.yahoo_earnings_estimates ADD COLUMN IF NOT EXISTS event_status LowCardinality(String) DEFAULT 'scheduled' AFTER event_session;
