-- D13 뉴스 분류. 값은 market|corp|earn|rate|etf|global, 빈 문자열은 "아직 분류 안 됨"(읽을 때 같은 규칙으로 계산)이다.
-- ADD COLUMN 은 메타데이터만 바꾸므로 기존 파트를 다시 쓰지 않는다. 정렬 키·엔진은 그대로다.
ALTER TABLE market_data.news_articles ADD COLUMN IF NOT EXISTS category LowCardinality(String) DEFAULT '';
ALTER TABLE market_data.news_article_localizations ADD COLUMN IF NOT EXISTS category LowCardinality(String) DEFAULT '';

-- SELECT * 뷰는 만든 시점의 컬럼 목록을 고정하므로 새 컬럼이 보이도록 0001 과 같은 정의로 다시 만든다.
CREATE OR REPLACE VIEW market_data.news_articles_latest AS
SELECT * FROM market_data.news_articles
ORDER BY inserted_at DESC, cityHash64(toString(tuple(*))) DESC
LIMIT 1 BY symbol, published_at, article_id;

CREATE OR REPLACE VIEW market_data.news_article_localizations_latest AS
SELECT * FROM market_data.news_article_localizations
ORDER BY localized_at DESC, inserted_at DESC, cityHash64(toString(tuple(*))) DESC
LIMIT 1 BY symbol, locale, published_at, article_id;
