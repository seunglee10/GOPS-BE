CREATE TABLE IF NOT EXISTS paper_accounts (
    user_id TEXT PRIMARY KEY,
    current_generation INTEGER NOT NULL DEFAULT 1,
    currency TEXT NOT NULL DEFAULT 'USD',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS paper_account_runs (
    user_id TEXT NOT NULL REFERENCES paper_accounts(user_id),
    generation INTEGER NOT NULL,
    starting_cash NUMERIC(24, 6) NOT NULL,
    cash_balance NUMERIC(24, 6) NOT NULL,
    reserved_cash NUMERIC(24, 6) NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ,
    PRIMARY KEY (user_id, generation),
    CHECK (starting_cash > 0),
    CHECK (cash_balance >= 0),
    CHECK (reserved_cash >= 0),
    CHECK (reserved_cash <= cash_balance)
);

CREATE TABLE IF NOT EXISTS paper_positions (
    user_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    qty NUMERIC(24, 0) NOT NULL DEFAULT 0,
    reserved_qty NUMERIC(24, 0) NOT NULL DEFAULT 0,
    average_price NUMERIC(24, 6) NOT NULL DEFAULT 0,
    realized_pnl NUMERIC(24, 6) NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, generation, symbol),
    FOREIGN KEY (user_id, generation) REFERENCES paper_account_runs(user_id, generation),
    CHECK (qty >= 0),
    CHECK (reserved_qty >= 0),
    CHECK (reserved_qty <= qty),
    CHECK (average_price >= 0)
);

CREATE TABLE IF NOT EXISTS paper_orders (
    order_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty NUMERIC(24, 0) NOT NULL,
    limit_price NUMERIC(24, 6) NOT NULL,
    exchange TEXT NOT NULL,
    order_division TEXT NOT NULL DEFAULT '00',
    status TEXT NOT NULL,
    filled_qty NUMERIC(24, 0) NOT NULL DEFAULT 0,
    fill_price NUMERIC(24, 6),
    quote_event_id TEXT,
    quote_timestamp TIMESTAMPTZ,
    reason TEXT,
    idempotency_key_hash TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    filled_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    FOREIGN KEY (user_id, generation) REFERENCES paper_account_runs(user_id, generation),
    UNIQUE (user_id, idempotency_key_hash),
    CHECK (side IN ('buy', 'sell')),
    CHECK (status IN ('pending', 'filled', 'cancelled', 'rejected')),
    CHECK (qty > 0),
    CHECK (limit_price > 0),
    CHECK (filled_qty >= 0),
    CHECK (filled_qty <= qty)
);

CREATE TABLE IF NOT EXISTS paper_order_events (
    event_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES paper_orders(order_id),
    user_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS paper_cash_ledger (
    entry_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    order_id TEXT REFERENCES paper_orders(order_id),
    event_type TEXT NOT NULL,
    cash_delta NUMERIC(24, 6) NOT NULL DEFAULT 0,
    reserved_cash_delta NUMERIC(24, 6) NOT NULL DEFAULT 0,
    cash_balance_after NUMERIC(24, 6) NOT NULL,
    reserved_cash_after NUMERIC(24, 6) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (user_id, generation) REFERENCES paper_account_runs(user_id, generation)
);

CREATE INDEX IF NOT EXISTS idx_paper_orders_user_generation_created
    ON paper_orders(user_id, generation, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_paper_orders_pending_symbol
    ON paper_orders(symbol, created_at, order_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_paper_order_events_order_created
    ON paper_order_events(order_id, created_at, event_id);
CREATE INDEX IF NOT EXISTS idx_paper_cash_ledger_user_generation
    ON paper_cash_ledger(user_id, generation, created_at, entry_id);
