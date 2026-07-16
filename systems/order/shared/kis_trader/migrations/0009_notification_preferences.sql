CREATE TABLE IF NOT EXISTS user_notification_preferences (
    user_sub TEXT PRIMARY KEY,
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,
    company_overrides JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT user_notification_preferences_settings_object
        CHECK (jsonb_typeof(settings) = 'object'),
    CONSTRAINT user_notification_preferences_company_overrides_object
        CHECK (jsonb_typeof(company_overrides) = 'object')
);
