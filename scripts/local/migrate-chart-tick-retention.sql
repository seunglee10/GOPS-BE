-- Operator-run, idempotent migration. Review table sizes and backups before execution.
ALTER TABLE market_data.trade_ticks
    MODIFY TTL event_time + INTERVAL 21 DAY DELETE;

ALTER TABLE market_data.quote_ticks
    MODIFY TTL event_time + INTERVAL 21 DAY DELETE;
