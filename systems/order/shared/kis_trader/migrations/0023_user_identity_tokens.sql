-- 소셜 제공자가 발급한 토큰 보관소.
--
-- user_identities 에 컬럼을 붙이지 않고 테이블을 나눈다. 신원 정보를 읽는 코드는
-- 이미 여럿인데, 같은 행에 토큰이 있으면 SELECT * 나 로그·API 응답에 딸려 나갈 수
-- 있다. 테이블이 분리돼 있으면 토큰이 필요한 코드만 명시적으로 조인한다.
--
-- 지금은 평문으로 넣는다. 실습 단계의 선택이며, 암호화로 올릴 때는 저장 계층
-- 한 곳(app/auth/token_store.py)만 고치면 되도록 컬럼 타입을 TEXT 로 둔다.
--
-- 키는 identity_id 대신 (provider, provider_subject) 를 쓴다. 파이썬이 이미 그
-- 두 값을 들고 있어서 identity_id 를 다시 계산할 필요가 없다.

CREATE TABLE IF NOT EXISTS user_identity_tokens (
    provider TEXT NOT NULL,
    provider_subject TEXT NOT NULL,
    access_token TEXT,
    refresh_token TEXT,
    access_expires_at TIMESTAMPTZ,
    refresh_expires_at TIMESTAMPTZ,
    scope TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT user_identity_tokens_pkey PRIMARY KEY (provider, provider_subject),
    CONSTRAINT user_identity_tokens_identity_fk
        FOREIGN KEY (provider, provider_subject)
        REFERENCES user_identities (provider, provider_subject)
        ON DELETE CASCADE
);

COMMENT ON TABLE user_identity_tokens IS
    '소셜 제공자 발급 토큰. 로그인 자격증명이 아니라 제공자 API 호출용이다.';
COMMENT ON COLUMN user_identity_tokens.refresh_token IS
    '수명이 긴 자격증명. 응답이나 로그에 절대 싣지 않는다.';
