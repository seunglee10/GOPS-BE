-- Operator-run, idempotent migration. Review table sizes and backups before execution.
-- initdb SQL은 빈 볼륨에서만 실행되므로, 이미 떠 있는 ClickHouse에는 이 파일을 직접 실행해야
-- 보존 정책 제거가 반영된다. trade_ticks / quote_ticks의 21일 TTL은 의도적으로 유지한다.
--
-- 실행 예:
--   clickhouse-client --multiquery < scripts/local/migrate-remove-news-retention-ttl.sql
--   (k8s) kubectl exec -n alfaka-market-data clickhouse-0 -- clickhouse-client --multiquery < ...

ALTER TABLE market_data.news_articles
    REMOVE TTL;

ALTER TABLE market_data.news_article_localizations
    REMOVE TTL;

ALTER TABLE market_data.news_company_daily_summaries
    REMOVE TTL;

ALTER TABLE market_data.yahoo_analyst_summaries
    REMOVE TTL;

-- 확인: 아래 쿼리 결과의 4개 테이블 모두 ttl_expression이 비어 있어야 한다.
-- SELECT name, delete_ttl_expression
-- FROM system.tables
-- WHERE database = 'market_data'
--   AND name IN ('news_articles', 'news_article_localizations', 'news_company_daily_summaries', 'yahoo_analyst_summaries');
