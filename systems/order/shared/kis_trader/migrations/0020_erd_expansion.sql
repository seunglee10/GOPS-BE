-- Safe expansion migration for the GOPS ERD hardening program.
-- Contract/removal steps intentionally remain in later, separately approved migrations.

CREATE OR REPLACE FUNCTION gops_deterministic_uuid(namespace_text TEXT, value_text TEXT)
RETURNS UUID
LANGUAGE SQL
IMMUTABLE
STRICT
AS $$
    SELECT (
        substr(md5(namespace_text || ':' || value_text), 1, 8) || '-' ||
        substr(md5(namespace_text || ':' || value_text), 9, 4) || '-' ||
        substr(md5(namespace_text || ':' || value_text), 13, 4) || '-' ||
        substr(md5(namespace_text || ':' || value_text), 17, 4) || '-' ||
        substr(md5(namespace_text || ':' || value_text), 21, 12)
    )::uuid
$$;

CREATE OR REPLACE FUNCTION gops_try_timestamptz(value_text TEXT)
RETURNS TIMESTAMPTZ
LANGUAGE plpgsql
IMMUTABLE
AS $$
BEGIN
    IF value_text IS NULL OR btrim(value_text) = '' OR
       btrim(value_text) !~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$' THEN
        RETURN NULL;
    END IF;
    RETURN value_text::timestamptz;
EXCEPTION WHEN OTHERS THEN
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION gops_try_date(value_text TEXT)
RETURNS DATE
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    parsed DATE;
BEGIN
    IF value_text IS NULL OR value_text !~ '^\d{4}-\d{2}-\d{2}$' THEN
        RETURN NULL;
    END IF;
    parsed := value_text::date;
    IF to_char(parsed, 'YYYY-MM-DD') <> value_text THEN
        RETURN NULL;
    END IF;
    RETURN parsed;
EXCEPTION WHEN OTHERS THEN
    RETURN NULL;
END
$$;

CREATE TABLE IF NOT EXISTS app_users (
    app_user_id UUID PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT app_users_status_check CHECK (status IN ('active', 'suspended', 'deleted'))
);

CREATE TABLE IF NOT EXISTS user_identities (
    identity_id UUID PRIMARY KEY,
    app_user_id UUID NOT NULL REFERENCES app_users(app_user_id),
    provider TEXT NOT NULL,
    provider_subject TEXT NOT NULL,
    email TEXT,
    email_verified BOOLEAN NOT NULL DEFAULT false,
    display_name TEXT,
    picture_url TEXT,
    last_login_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT user_identities_provider_subject_unique UNIQUE (provider, provider_subject)
);

CREATE OR REPLACE FUNCTION gops_ensure_app_user_identity(
    identity_provider TEXT,
    identity_subject TEXT,
    identity_email TEXT DEFAULT NULL,
    identity_email_verified BOOLEAN DEFAULT false,
    identity_display_name TEXT DEFAULT NULL,
    identity_picture_url TEXT DEFAULT NULL
)
RETURNS UUID
LANGUAGE plpgsql
AS $$
DECLARE
    resolved_user_id UUID;
BEGIN
    IF identity_provider IS NULL OR btrim(identity_provider) = '' OR
       identity_subject IS NULL OR btrim(identity_subject) = '' THEN
        RAISE EXCEPTION 'provider and provider subject are required';
    END IF;

    SELECT app_user_id INTO resolved_user_id
    FROM user_identities
    WHERE provider = identity_provider AND provider_subject = identity_subject;

    IF resolved_user_id IS NULL THEN
        -- The subject-only namespace intentionally lets legacy_sub and google
        -- resolve to the same internal user during the compatibility window.
        resolved_user_id := gops_deterministic_uuid('gops-app-user', identity_subject);
        INSERT INTO app_users (app_user_id)
        VALUES (resolved_user_id)
        ON CONFLICT (app_user_id) DO UPDATE SET updated_at = now();

        INSERT INTO user_identities (
            identity_id, app_user_id, provider, provider_subject, email,
            email_verified, display_name, picture_url, last_login_at
        ) VALUES (
            gops_deterministic_uuid('gops-identity:' || identity_provider, identity_subject),
            resolved_user_id, identity_provider, identity_subject, identity_email,
            identity_email_verified, identity_display_name, identity_picture_url, now()
        )
        ON CONFLICT (provider, provider_subject) DO UPDATE SET
            email = COALESCE(EXCLUDED.email, user_identities.email),
            email_verified = EXCLUDED.email_verified,
            display_name = COALESCE(EXCLUDED.display_name, user_identities.display_name),
            picture_url = COALESCE(EXCLUDED.picture_url, user_identities.picture_url),
            last_login_at = now(),
            updated_at = now()
        RETURNING app_user_id INTO resolved_user_id;
    ELSE
        UPDATE user_identities SET
            email = COALESCE(identity_email, email),
            email_verified = identity_email_verified,
            display_name = COALESCE(identity_display_name, display_name),
            picture_url = COALESCE(identity_picture_url, picture_url),
            last_login_at = now(),
            updated_at = now()
        WHERE provider = identity_provider AND provider_subject = identity_subject;
    END IF;
    RETURN resolved_user_id;
END
$$;

CREATE TABLE IF NOT EXISTS instruments (
    instrument_id UUID PRIMARY KEY,
    canonical_symbol TEXT NOT NULL,
    market TEXT,
    exchange TEXT,
    asset_type TEXT NOT NULL DEFAULT 'equity',
    currency TEXT NOT NULL DEFAULT 'USD',
    name TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT instruments_symbol_unique UNIQUE (canonical_symbol),
    CONSTRAINT instruments_status_check CHECK (status IN ('active', 'inactive', 'delisted'))
);

CREATE TABLE IF NOT EXISTS instrument_aliases (
    alias_id UUID PRIMARY KEY,
    instrument_id UUID NOT NULL REFERENCES instruments(instrument_id),
    provider TEXT NOT NULL,
    provider_symbol TEXT NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT instrument_aliases_validity_check CHECK (valid_to IS NULL OR valid_to > valid_from)
);

CREATE UNIQUE INDEX IF NOT EXISTS instrument_aliases_active_unique
    ON instrument_aliases(provider, provider_symbol) WHERE valid_to IS NULL;

CREATE OR REPLACE FUNCTION gops_ensure_instrument(
    source_symbol TEXT,
    source_market TEXT DEFAULT NULL,
    source_exchange TEXT DEFAULT NULL,
    source_provider TEXT DEFAULT 'gops'
)
RETURNS UUID
LANGUAGE plpgsql
AS $$
DECLARE
    canonical TEXT := upper(replace(btrim(source_symbol), '-', '.'));
    resolved_instrument_id UUID;
BEGIN
    IF canonical IS NULL OR canonical = '' THEN
        RETURN NULL;
    END IF;

    SELECT instrument_id INTO resolved_instrument_id
    FROM instrument_aliases
    WHERE provider = source_provider
      AND provider_symbol = upper(btrim(source_symbol))
      AND valid_to IS NULL;

    IF resolved_instrument_id IS NULL THEN
        resolved_instrument_id := gops_deterministic_uuid('gops-instrument', canonical);
        INSERT INTO instruments (instrument_id, canonical_symbol, market, exchange)
        VALUES (resolved_instrument_id, canonical, source_market, source_exchange)
        ON CONFLICT (canonical_symbol) DO UPDATE SET
            market = COALESCE(instruments.market, EXCLUDED.market),
            exchange = COALESCE(instruments.exchange, EXCLUDED.exchange),
            updated_at = now()
        RETURNING instrument_id INTO resolved_instrument_id;

        INSERT INTO instrument_aliases (alias_id, instrument_id, provider, provider_symbol)
        VALUES (
            gops_deterministic_uuid('gops-instrument-alias:' || source_provider, upper(btrim(source_symbol))),
            resolved_instrument_id, source_provider, upper(btrim(source_symbol))
        )
        ON CONFLICT (provider, provider_symbol) WHERE valid_to IS NULL DO NOTHING;
    END IF;
    RETURN resolved_instrument_id;
END
$$;

-- Typed date/time expansion columns. Legacy text columns remain the public contract.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS occurred_at_ts TIMESTAMPTZ;
ALTER TABLE stock_recommendation_runs
    ADD COLUMN IF NOT EXISTS slot_start_ts TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS market_date_value DATE,
    ADD COLUMN IF NOT EXISTS market_snapshot_at TIMESTAMPTZ;

CREATE OR REPLACE FUNCTION gops_orders_dual_write_typed_time()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.occurred_at_ts IS NULL THEN
        NEW.occurred_at_ts := gops_try_timestamptz(NEW.occurred_at);
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS orders_dual_write_typed_time ON orders;
CREATE TRIGGER orders_dual_write_typed_time
BEFORE INSERT OR UPDATE OF occurred_at, occurred_at_ts ON orders
FOR EACH ROW EXECUTE FUNCTION gops_orders_dual_write_typed_time();

CREATE OR REPLACE FUNCTION gops_recommendation_runs_dual_write_typed_time()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.slot_start_ts IS NULL THEN
        NEW.slot_start_ts := gops_try_timestamptz(NEW.slot_start);
    END IF;
    IF NEW.market_date_value IS NULL THEN
        NEW.market_date_value := gops_try_date(NEW.market_date);
    END IF;
    IF NEW.market_snapshot_at IS NULL THEN
        NEW.market_snapshot_at := gops_try_timestamptz(NEW.market_snapshot_time);
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS recommendation_runs_dual_write_typed_time ON stock_recommendation_runs;
CREATE TRIGGER recommendation_runs_dual_write_typed_time
BEFORE INSERT OR UPDATE OF slot_start, slot_start_ts, market_date, market_date_value,
    market_snapshot_time, market_snapshot_at ON stock_recommendation_runs
FOR EACH ROW EXECUTE FUNCTION gops_recommendation_runs_dual_write_typed_time();

ALTER TABLE orders
    ADD CONSTRAINT orders_occurred_at_ts_required CHECK (occurred_at_ts IS NOT NULL) NOT VALID;
ALTER TABLE stock_recommendation_runs
    ADD CONSTRAINT recommendation_runs_slot_start_ts_required CHECK (slot_start_ts IS NOT NULL) NOT VALID,
    ADD CONSTRAINT recommendation_runs_market_date_value_required CHECK (market_date_value IS NOT NULL) NOT VALID,
    ADD CONSTRAINT recommendation_runs_market_snapshot_at_required CHECK (market_snapshot_at IS NOT NULL) NOT VALID;

-- Internal user identity columns. Triggers provide dual-write compatibility.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS app_user_id UUID;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS app_user_id UUID;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS app_user_id UUID;
ALTER TABLE trade_conditions ADD COLUMN IF NOT EXISTS app_user_id UUID;
ALTER TABLE user_notification_preferences ADD COLUMN IF NOT EXISTS app_user_id UUID;
ALTER TABLE user_recommendation_score_profiles ADD COLUMN IF NOT EXISTS app_user_id UUID;
ALTER TABLE user_investment_profiles ADD COLUMN IF NOT EXISTS app_user_id UUID;
ALTER TABLE user_investment_profile_history ADD COLUMN IF NOT EXISTS app_user_id UUID;
ALTER TABLE user_layout_presets ADD COLUMN IF NOT EXISTS app_user_id UUID;
ALTER TABLE user_portfolio_snapshots ADD COLUMN IF NOT EXISTS app_user_id UUID;
ALTER TABLE user_portfolio_snapshot_history ADD COLUMN IF NOT EXISTS app_user_id UUID;
ALTER TABLE trade_decision_check_events ADD COLUMN IF NOT EXISTS app_user_id UUID;
ALTER TABLE order_coach_fill_history ADD COLUMN IF NOT EXISTS app_user_id UUID;
ALTER TABLE stock_recommendation_runs ADD COLUMN IF NOT EXISTS app_user_id UUID;
ALTER TABLE paper_accounts ADD COLUMN IF NOT EXISTS app_user_id UUID;
ALTER TABLE paper_account_runs ADD COLUMN IF NOT EXISTS app_user_id UUID;
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS app_user_id UUID;
ALTER TABLE paper_orders ADD COLUMN IF NOT EXISTS app_user_id UUID;
ALTER TABLE paper_order_events ADD COLUMN IF NOT EXISTS app_user_id UUID;
ALTER TABLE paper_cash_ledger ADD COLUMN IF NOT EXISTS app_user_id UUID;

CREATE OR REPLACE FUNCTION gops_assign_app_user_id_from_legacy()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE legacy_value TEXT;
BEGIN
    IF NEW.app_user_id IS NULL THEN
        legacy_value := to_jsonb(NEW)->>TG_ARGV[0];
        IF legacy_value IS NOT NULL AND btrim(legacy_value) <> '' THEN
            NEW.app_user_id := gops_ensure_app_user_identity('legacy_sub', legacy_value);
        END IF;
    END IF;
    RETURN NEW;
END
$$;

DO $$
DECLARE item RECORD;
BEGIN
    FOR item IN SELECT * FROM (VALUES
        ('orders', 'user_sub'), ('alerts', 'user_sub'), ('notifications', 'user_sub'),
        ('trade_conditions', 'user_sub'), ('user_notification_preferences', 'user_sub'),
        ('user_recommendation_score_profiles', 'user_sub'), ('user_investment_profiles', 'user_sub'),
        ('user_investment_profile_history', 'user_sub'), ('user_layout_presets', 'user_sub'),
        ('user_portfolio_snapshots', 'user_sub'), ('user_portfolio_snapshot_history', 'user_sub'),
        ('trade_decision_check_events', 'user_sub'), ('order_coach_fill_history', 'user_sub'),
        ('stock_recommendation_runs', 'user_sub'), ('paper_accounts', 'user_id'),
        ('paper_account_runs', 'user_id'), ('paper_positions', 'user_id'), ('paper_orders', 'user_id'),
        ('paper_order_events', 'user_id'), ('paper_cash_ledger', 'user_id')
    ) AS v(table_name, legacy_column)
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS assign_app_user_id ON %I', item.table_name);
        EXECUTE format(
            'CREATE TRIGGER assign_app_user_id BEFORE INSERT OR UPDATE ON %I '
            'FOR EACH ROW EXECUTE FUNCTION gops_assign_app_user_id_from_legacy(%L)',
            item.table_name, item.legacy_column
        );
    END LOOP;
END
$$;

ALTER TABLE orders ADD CONSTRAINT orders_app_user_fk FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id) NOT VALID;
ALTER TABLE alerts ADD CONSTRAINT alerts_app_user_fk FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id) NOT VALID;
ALTER TABLE notifications ADD CONSTRAINT notifications_app_user_fk FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id) NOT VALID;
ALTER TABLE trade_conditions ADD CONSTRAINT trade_conditions_app_user_fk FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id) NOT VALID;
ALTER TABLE user_notification_preferences ADD CONSTRAINT notification_preferences_app_user_fk FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id) NOT VALID;
ALTER TABLE user_recommendation_score_profiles ADD CONSTRAINT score_profiles_app_user_fk FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id) NOT VALID;
ALTER TABLE user_investment_profiles ADD CONSTRAINT investment_profiles_app_user_fk FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id) NOT VALID;
ALTER TABLE user_investment_profile_history ADD CONSTRAINT investment_profile_history_app_user_fk FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id) NOT VALID;
ALTER TABLE user_layout_presets ADD CONSTRAINT layout_presets_app_user_fk FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id) NOT VALID;
ALTER TABLE user_portfolio_snapshots ADD CONSTRAINT portfolio_snapshots_app_user_fk FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id) NOT VALID;
ALTER TABLE user_portfolio_snapshot_history ADD CONSTRAINT portfolio_snapshot_history_app_user_fk FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id) NOT VALID;
ALTER TABLE trade_decision_check_events ADD CONSTRAINT decision_check_events_app_user_fk FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id) NOT VALID;
ALTER TABLE order_coach_fill_history ADD CONSTRAINT coach_fill_history_app_user_fk FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id) NOT VALID;
ALTER TABLE stock_recommendation_runs ADD CONSTRAINT recommendation_runs_app_user_fk FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id) NOT VALID;
ALTER TABLE paper_accounts ADD CONSTRAINT paper_accounts_app_user_fk FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id) NOT VALID;
ALTER TABLE paper_account_runs ADD CONSTRAINT paper_account_runs_app_user_fk FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id) NOT VALID;
ALTER TABLE paper_positions ADD CONSTRAINT paper_positions_app_user_fk FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id) NOT VALID;
ALTER TABLE paper_orders ADD CONSTRAINT paper_orders_app_user_fk FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id) NOT VALID;
ALTER TABLE paper_order_events ADD CONSTRAINT paper_order_events_app_user_fk FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id) NOT VALID;
ALTER TABLE paper_cash_ledger ADD CONSTRAINT paper_cash_ledger_app_user_fk FOREIGN KEY (app_user_id) REFERENCES app_users(app_user_id) NOT VALID;

-- Canonical instrument columns. Symbol remains the immutable display/audit snapshot.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS instrument_id UUID;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS instrument_id UUID;
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS instrument_id UUID;
ALTER TABLE paper_orders ADD COLUMN IF NOT EXISTS instrument_id UUID;
ALTER TABLE stock_recommendation_items ADD COLUMN IF NOT EXISTS instrument_id UUID;
ALTER TABLE stock_recommendation_evidence_candidates ADD COLUMN IF NOT EXISTS instrument_id UUID;
ALTER TABLE order_coach_fill_history ADD COLUMN IF NOT EXISTS instrument_id UUID;

CREATE OR REPLACE FUNCTION gops_assign_instrument_id_from_symbol()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    symbol_value TEXT;
    market_value TEXT;
    exchange_value TEXT;
BEGIN
    IF NEW.instrument_id IS NULL THEN
        symbol_value := to_jsonb(NEW)->>TG_ARGV[0];
        market_value := CASE WHEN TG_NARGS > 1 THEN to_jsonb(NEW)->>TG_ARGV[1] ELSE NULL END;
        exchange_value := CASE WHEN TG_NARGS > 2 THEN to_jsonb(NEW)->>TG_ARGV[2] ELSE NULL END;
        NEW.instrument_id := gops_ensure_instrument(symbol_value, market_value, exchange_value);
    END IF;
    RETURN NEW;
END
$$;

DO $$
DECLARE item RECORD;
BEGIN
    FOR item IN SELECT * FROM (VALUES
        ('orders', 'symbol', 'market', 'exchange'),
        ('alerts', 'symbol', NULL, NULL),
        ('paper_positions', 'symbol', NULL, NULL),
        ('paper_orders', 'symbol', 'market', 'exchange'),
        ('stock_recommendation_items', 'symbol', NULL, NULL),
        ('stock_recommendation_evidence_candidates', 'symbol', NULL, NULL),
        ('order_coach_fill_history', 'symbol', NULL, NULL)
    ) AS v(table_name, symbol_column, market_column, exchange_column)
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS assign_instrument_id ON %I', item.table_name);
        IF item.market_column IS NULL THEN
            EXECUTE format(
                'CREATE TRIGGER assign_instrument_id BEFORE INSERT OR UPDATE ON %I '
                'FOR EACH ROW EXECUTE FUNCTION gops_assign_instrument_id_from_symbol(%L)',
                item.table_name, item.symbol_column
            );
        ELSE
            EXECUTE format(
                'CREATE TRIGGER assign_instrument_id BEFORE INSERT OR UPDATE ON %I '
                'FOR EACH ROW EXECUTE FUNCTION gops_assign_instrument_id_from_symbol(%L, %L, %L)',
                item.table_name, item.symbol_column, item.market_column, item.exchange_column
            );
        END IF;
    END LOOP;
END
$$;

ALTER TABLE orders ADD CONSTRAINT orders_instrument_fk FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id) NOT VALID;
ALTER TABLE alerts ADD CONSTRAINT alerts_instrument_fk FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id) NOT VALID;
ALTER TABLE paper_positions ADD CONSTRAINT paper_positions_instrument_fk FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id) NOT VALID;
ALTER TABLE paper_orders ADD CONSTRAINT paper_orders_instrument_fk FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id) NOT VALID;
ALTER TABLE stock_recommendation_items ADD CONSTRAINT recommendation_items_instrument_fk FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id) NOT VALID;
ALTER TABLE stock_recommendation_evidence_candidates ADD CONSTRAINT evidence_candidates_instrument_fk FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id) NOT VALID;
ALTER TABLE order_coach_fill_history ADD CONSTRAINT coach_fill_history_instrument_fk FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id) NOT VALID;

-- trade_conditions.order_id historically points to paper_orders. Preserve it as
-- a compatibility alias while introducing the correctly named physical FK.
ALTER TABLE trade_conditions ADD COLUMN IF NOT EXISTS paper_order_id TEXT;
CREATE OR REPLACE FUNCTION gops_trade_conditions_dual_write_paper_order_id()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.paper_order_id IS NULL AND NEW.order_id IS NOT NULL THEN
        NEW.paper_order_id := NEW.order_id;
    ELSIF NEW.order_id IS NULL AND NEW.paper_order_id IS NOT NULL THEN
        NEW.order_id := NEW.paper_order_id;
    END IF;
    RETURN NEW;
END
$$;
DROP TRIGGER IF EXISTS trade_conditions_dual_write_paper_order_id ON trade_conditions;
CREATE TRIGGER trade_conditions_dual_write_paper_order_id
BEFORE INSERT OR UPDATE OF order_id, paper_order_id ON trade_conditions
FOR EACH ROW EXECUTE FUNCTION gops_trade_conditions_dual_write_paper_order_id();
ALTER TABLE trade_conditions ADD CONSTRAINT trade_conditions_paper_order_fk
    FOREIGN KEY (paper_order_id) REFERENCES paper_orders(order_id) NOT VALID;

-- Paper execution source of truth. The order row remains a read-optimized aggregate.
CREATE TABLE IF NOT EXISTS paper_executions (
    execution_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES paper_orders(order_id),
    execution_sequence INTEGER NOT NULL,
    quantity NUMERIC(24, 0) NOT NULL,
    price NUMERIC(24, 6) NOT NULL,
    fee NUMERIC(24, 6) NOT NULL DEFAULT 0,
    quote_event_id TEXT,
    quote_timestamp TIMESTAMPTZ,
    executed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT paper_executions_sequence_unique UNIQUE (order_id, execution_sequence),
    CONSTRAINT paper_executions_quantity_check CHECK (quantity > 0),
    CONSTRAINT paper_executions_price_check CHECK (price > 0),
    CONSTRAINT paper_executions_fee_check CHECK (fee >= 0)
);

ALTER TABLE paper_order_events ADD COLUMN IF NOT EXISTS execution_id TEXT;
ALTER TABLE paper_cash_ledger ADD COLUMN IF NOT EXISTS execution_id TEXT;
ALTER TABLE paper_order_events ADD CONSTRAINT paper_order_events_execution_fk
    FOREIGN KEY (execution_id) REFERENCES paper_executions(execution_id) NOT VALID;
ALTER TABLE paper_cash_ledger ADD CONSTRAINT paper_cash_ledger_execution_fk
    FOREIGN KEY (execution_id) REFERENCES paper_executions(execution_id) NOT VALID;

ALTER TABLE paper_orders DROP CONSTRAINT IF EXISTS paper_orders_status_check;
ALTER TABLE paper_orders ADD CONSTRAINT paper_orders_status_check
    CHECK (status IN ('pending', 'partially_filled', 'filled', 'cancelled', 'rejected')) NOT VALID;

-- Reliable outbox claim/lease metadata and consumer inbox idempotency.
ALTER TABLE outbox_events
    ADD COLUMN IF NOT EXISTS delivery_status TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS last_error TEXT,
    ADD COLUMN IF NOT EXISTS locked_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS lock_owner TEXT;

UPDATE outbox_events
SET delivery_status = 'published'
WHERE published_at IS NOT NULL AND delivery_status <> 'published';

ALTER TABLE outbox_events
    ADD CONSTRAINT outbox_events_delivery_status_check
        CHECK (delivery_status IN ('pending', 'publishing', 'retry', 'published')) NOT VALID,
    ADD CONSTRAINT outbox_events_attempt_count_check CHECK (attempt_count >= 0) NOT VALID;

CREATE TABLE IF NOT EXISTS inbox_events (
    consumer_name TEXT NOT NULL,
    event_id TEXT NOT NULL,
    payload_digest TEXT,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer_name, event_id)
);

-- Explicit recommendation version dimensions.
ALTER TABLE stock_recommendation_runs
    ADD COLUMN IF NOT EXISTS model_version TEXT;
ALTER TABLE stock_recommendation_runs
    ADD CONSTRAINT recommendation_runs_model_version_fk
        FOREIGN KEY (model_version) REFERENCES stock_recommendation_model_registry(model_version) NOT VALID;

-- Closed-value and positive-amount constraints, introduced as NOT VALID so
-- existing data can be audited before validation.
ALTER TABLE orders
    ADD CONSTRAINT orders_side_check CHECK (side IN ('buy', 'sell')) NOT VALID,
    ADD CONSTRAINT orders_qty_check CHECK (qty > 0) NOT VALID,
    ADD CONSTRAINT orders_price_check CHECK (price > 0) NOT VALID,
    ADD CONSTRAINT orders_status_check CHECK (status IN (
        'RECEIVED', 'PUBLISHED', 'REJECTED', 'RISK_REJECTED', 'SUBMITTING',
        'SUBMITTED', 'SUBMIT_FAILED_UNKNOWN', 'PARTIALLY_FILLED', 'FILLED',
        'CANCELED', 'RECONCILIATION_REQUIRED', 'FAILED'
    )) NOT VALID;

COMMENT ON COLUMN audit_logs.order_id IS
    'External or internal order identifier retained without a physical FK for audit durability.';
COMMENT ON COLUMN trade_conditions.order_id IS
    'Compatibility alias; paper_order_id is the canonical FK to paper_orders.';
COMMENT ON TABLE instruments IS
    'PostgreSQL canonical instrument source. ClickHouse market_data.* tables link logically by instrument_id.';
