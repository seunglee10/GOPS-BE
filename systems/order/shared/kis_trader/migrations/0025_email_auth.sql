-- 이메일·비밀번호 가입.
--
-- 이메일 사용자는 user_identities 에 provider='email', provider_subject=<소문자 이메일>
-- 로 들어간다. 그래서 app_user_id 는 소셜 로그인과 같은 함수(gops_ensure_app_user_identity)
-- 로 만들어지고, 세션·소유 테이블은 로그인 수단을 구분하지 않는다.
--
-- 비밀번호 해시는 user_identities 에 붙이지 않고 따로 둔다. 0023 의 토큰과 같은 이유다:
-- 신원 정보를 읽는 코드가 해시를 같이 끌고 나가지 않게 한다.

CREATE TABLE IF NOT EXISTS email_credentials (
    app_user_id UUID PRIMARY KEY REFERENCES app_users(app_user_id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    email_verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT email_credentials_email_unique UNIQUE (email)
);

COMMENT ON TABLE email_credentials IS
    '이메일 가입 사용자의 비밀번호 해시. 응답·로그에 절대 싣지 않는다.';
COMMENT ON COLUMN email_credentials.email IS
    '소문자로 정규화한 이메일. user_identities.provider_subject 와 같은 값이다.';
COMMENT ON COLUMN email_credentials.email_verified_at IS
    'NULL 이면 아직 인증 코드를 확인하지 않은 계정. 로그인은 403 unverified.';

-- 인증 코드(purpose='verify', 6자리·5분)와 비밀번호 재설정 토큰(purpose='reset',
-- 32바이트·30분)을 한 테이블에 둔다. 둘 다 "이메일로 보낸 1회용 비밀값의 해시"라
-- 구조가 같다. 원문은 저장하지 않는다.
CREATE TABLE IF NOT EXISTS email_verifications (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    purpose TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT email_verifications_purpose_check CHECK (purpose IN ('verify', 'reset'))
);

CREATE INDEX IF NOT EXISTS email_verifications_email_purpose_idx
    ON email_verifications (email, purpose, created_at DESC);
CREATE INDEX IF NOT EXISTS email_verifications_code_hash_idx
    ON email_verifications (purpose, code_hash);

COMMENT ON TABLE email_verifications IS
    '이메일로 보낸 인증 코드·재설정 토큰의 해시와 만료·시도 횟수.';

-- 로그인 실패 횟수와 잠금. 계정이 없는 이메일도 세어서 존재 여부가 새지 않게 한다.
CREATE TABLE IF NOT EXISTS login_attempts (
    email TEXT PRIMARY KEY,
    failed_count INTEGER NOT NULL DEFAULT 0,
    locked_until TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE login_attempts IS
    '이메일별 로그인 실패 횟수. 5회면 10분 잠근다.';

-- 약관 동의. 이메일 가입은 가입 요청에서, 소셜 가입은 POST /api/auth/consent 에서 채운다.
-- 행이 없으면 /api/auth/me 의 needsConsent 가 true 다.
CREATE TABLE IF NOT EXISTS user_consents (
    app_user_id UUID PRIMARY KEY REFERENCES app_users(app_user_id) ON DELETE CASCADE,
    tos BOOLEAN NOT NULL,
    privacy BOOLEAN NOT NULL,
    age BOOLEAN NOT NULL,
    mkt BOOLEAN NOT NULL DEFAULT false,
    consented_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE user_consents IS
    '약관(tos)·개인정보(privacy)·연령(age) 필수 동의와 마케팅(mkt) 선택 동의.';
