-- 소셜 계정 연결(account linking).
--
-- 지금까지 gops_ensure_app_user_identity 는 처음 보는 identity 를 만나면 언제나
-- subject 에서 유도한 UUID 로 새 app_user 를 만들었다. 그래서 같은 사람이 구글과
-- 카카오로 각각 로그인하면 GOPS 계정이 둘로 갈라진다.
--
-- link_to_app_user_id 를 받아 "이 사용자에게 붙여라"는 지시를 표현한다. 이 인자는
-- 이미 로그인한 세션에서 시작한 연결 요청에만 채워지고, 평범한 로그인에는 NULL 이라
-- 기존 동작이 그대로 유지된다.
--
-- 이메일 기반 자동 병합은 넣지 않는다. 카카오는 비즈니스 앱 전환 전까지 이메일을
-- 주지 않아 비교할 값이 없고, 이메일이 같다는 사실은 소유권 증명이 아니다.

-- CREATE OR REPLACE 로 인자를 늘리면 교체가 아니라 오버로드가 생겨서 기존
-- 2~6인자 호출이 모호해진다. 옛 시그니처를 먼저 지운다.
DROP FUNCTION IF EXISTS gops_ensure_app_user_identity(TEXT, TEXT, TEXT, BOOLEAN, TEXT, TEXT);

CREATE OR REPLACE FUNCTION gops_ensure_app_user_identity(
    identity_provider TEXT,
    identity_subject TEXT,
    identity_email TEXT DEFAULT NULL,
    identity_email_verified BOOLEAN DEFAULT false,
    identity_display_name TEXT DEFAULT NULL,
    identity_picture_url TEXT DEFAULT NULL,
    link_to_app_user_id UUID DEFAULT NULL
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

    -- (1) 이미 아는 identity.
    IF resolved_user_id IS NOT NULL THEN
        IF link_to_app_user_id IS NOT NULL AND link_to_app_user_id <> resolved_user_id THEN
            PERFORM 1 FROM app_users WHERE app_user_id = link_to_app_user_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'link target app_user % does not exist', link_to_app_user_id;
            END IF;

            -- 옮겨도 되는 경우는 "이 로그인 수단 하나로만 존재하던 계정"뿐이다.
            -- 다른 수단이 붙어 있으면 남의 계정에서 떼어오는 시도와 구별되지 않는다.
            -- legacy_sub 는 트리거가 만든 그림자라 로그인 수단으로 세지 않는다.
            IF EXISTS (
                SELECT 1
                FROM user_identities
                WHERE app_user_id = resolved_user_id
                  AND provider <> 'legacy_sub'
                  AND NOT (provider = identity_provider AND provider_subject = identity_subject)
            ) THEN
                RAISE EXCEPTION 'social identity belongs to an account with other sign-in methods'
                    USING ERRCODE = 'unique_violation';
            END IF;

            -- legacy_sub 그림자까지 함께 옮긴다. 그래야 이후 user_sub 로 들어오는
            -- 쓰기가 합쳐진 계정으로 귀속된다. (이미 쓰인 행의 app_user_id 는
            -- 그대로 남는다 — 기존 데이터 이전은 별도 작업이다.)
            UPDATE user_identities
            SET app_user_id = link_to_app_user_id,
                updated_at = now()
            WHERE app_user_id = resolved_user_id;

            UPDATE app_users
            SET status = 'deleted',
                updated_at = now()
            WHERE app_user_id = resolved_user_id;

            resolved_user_id := link_to_app_user_id;
        END IF;

        UPDATE user_identities SET
            email = COALESCE(identity_email, email),
            email_verified = identity_email_verified,
            display_name = COALESCE(identity_display_name, display_name),
            picture_url = COALESCE(identity_picture_url, picture_url),
            last_login_at = now(),
            updated_at = now()
        WHERE provider = identity_provider AND provider_subject = identity_subject;

        RETURN resolved_user_id;
    END IF;

    -- (2) 처음 보는 identity 를 지정된 사용자에게 붙인다.
    IF link_to_app_user_id IS NOT NULL THEN
        PERFORM 1 FROM app_users WHERE app_user_id = link_to_app_user_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'link target app_user % does not exist', link_to_app_user_id;
        END IF;
        resolved_user_id := link_to_app_user_id;
    ELSE
        -- (3) 평범한 최초 로그인. 0020 과 동일하게 subject 에서 유도한다.
        resolved_user_id := gops_deterministic_uuid('gops-app-user', identity_subject);
    END IF;

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

    RETURN resolved_user_id;
END
$$;
