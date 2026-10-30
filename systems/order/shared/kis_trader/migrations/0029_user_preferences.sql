-- 사용자 화면 설정 동기화 (D34).
--
-- 프론트의 화면 설정(layout, chart, draw, cur, sideW, dwW, brand)과 투자 경험
-- (experience: new|some|pro)을 JSONB 한 행에 담는다. 서버는 구조를 검사하지 않고
-- 64KB 제한과 experience 값만 API 에서 검사한다.
-- version 은 낙관적 동시성용이다. PUT 은 보낸 version 이 지금 값과 같을 때만
-- 저장하고 1 올린다. 다르면 409 version_conflict.

CREATE TABLE IF NOT EXISTS user_preferences (
    app_user_id UUID PRIMARY KEY REFERENCES app_users(app_user_id) ON DELETE CASCADE,
    version INTEGER NOT NULL DEFAULT 1,
    data JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT user_preferences_version_positive CHECK (version >= 1),
    CONSTRAINT user_preferences_data_object CHECK (jsonb_typeof(data) = 'object')
);

COMMENT ON TABLE user_preferences IS
    '사용자 화면 설정 JSON 한 행. 기기 사이 동기화용.';
COMMENT ON COLUMN user_preferences.version IS
    '저장할 때마다 1씩 오른다. 행이 없을 때 API 는 version 0 을 돌려준다.';
COMMENT ON COLUMN user_preferences.data IS
    '프론트 화면 설정 원본 JSON(64KB 이내, API 에서 검사). data.experience 는 GET /api/auth/me 의 user.experience 가 된다.';
