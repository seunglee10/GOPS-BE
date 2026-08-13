-- Safety gate: execute `SET gops.rls_activation_confirmed = 'yes';` in this
-- session only after the five-trading-day ownership validation is complete.
DO $$
BEGIN
    IF current_setting('gops.rls_activation_confirmed', true) IS DISTINCT FROM 'yes' THEN
        RAISE EXCEPTION 'RLS activation not confirmed';
    END IF;
END
$$;

DO $$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'orders', 'alerts', 'notifications', 'trade_conditions',
        'user_notification_preferences', 'user_recommendation_score_profiles',
        'user_investment_profiles', 'user_investment_profile_history',
        'user_layout_presets', 'user_portfolio_snapshots',
        'user_portfolio_snapshot_history', 'trade_decision_check_events',
        'order_coach_fill_history', 'stock_recommendation_runs',
        'paper_accounts', 'paper_account_runs', 'paper_positions',
        'paper_orders', 'paper_order_events', 'paper_cash_ledger'
    ] LOOP
        EXECUTE format('DROP POLICY IF EXISTS user_isolation ON %I', table_name);
        EXECUTE format(
            'CREATE POLICY user_isolation ON %I USING (' ||
            'pg_has_role(current_user, ''gops_worker'', ''member'') OR ' ||
            'pg_has_role(current_user, ''gops_migration'', ''member'') OR ' ||
            'app_user_id = nullif(current_setting(''app.current_user_id'', true), '''')::uuid' ||
            ') WITH CHECK (' ||
            'pg_has_role(current_user, ''gops_worker'', ''member'') OR ' ||
            'pg_has_role(current_user, ''gops_migration'', ''member'') OR ' ||
            'app_user_id = nullif(current_setting(''app.current_user_id'', true), '''')::uuid' ||
            ')',
            table_name
        );
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
    END LOOP;
END
$$;
