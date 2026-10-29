# 이메일 가입·인증·로그인 (D35~D39)

`MOCK_API_CONTRACT.md` 의 "D35~D39 이메일 인증" 표가 계약이다. 응답 실패 본문은
`{ "code": ..., ... }` 를 최상위에 둔다.

```text
POST /api/auth/email/signup     { email, password, consents:{tos,privacy,age,mkt} } -> 201
POST /api/auth/email/verify     { email, code }            -> 200 { user } + 세션 쿠키
POST /api/auth/email/resend     { email }                  -> 200 { resendAfterSeconds }
POST /api/auth/email/login      { email, password, remember } -> 200 { user } + 세션 쿠키
POST /api/auth/password/forgot  { email }                  -> 200 { sent: true }
POST /api/auth/password/reset   { token, password }        -> 200 { reset: true }
POST /api/auth/consent          { tos, privacy, age, mkt } -> 200 { needsConsent: false }
```

## 파일

| 파일 | 역할 |
|---|---|
| `routes.py` | FastAPI 라우터. 계약서 모양으로 응답을 만들고 세션 쿠키를 소셜 콜백과 같은 속성으로 심는다. |
| `service.py` | 규칙(코드 5분·5회, 재전송 30초, 로그인 5회 실패 시 10분 잠금, 토큰 30분, remember 30일). HTTP 를 모른다. |
| `repository.py` | `InMemoryEmailAuthRepository`(테스트·DB 없음) / `PostgresEmailAuthRepository`. `DATABASE_URL` 또는 `DATABASE_*` 가 있으면 Postgres. |
| `passwords.py` | 표준 라이브러리 `hashlib.scrypt` 해시. 의존성 추가 없음. |
| `app/services/mailer.py` | `MAIL_PROVIDER=console|ses`. console 은 로그에 코드·링크를 찍는다. |

## 규칙

- 이메일은 소문자로 정규화한다. `user_identities(provider='email', provider_subject=이메일)` 로
  들어가므로 `app_user_id` 는 소셜 로그인과 같은 함수로 만들어진다.
- 비밀번호는 8자 이상, 영문·숫자 각 1자 이상. 아니면 `422 weak_password`.
- `tos`·`privacy`·`age` 는 필수, `mkt` 는 선택. 빠지면 `422 consent_required`.
- 인증 코드 6자리, 5분, 5회 틀리면 `410 expired`. 재전송은 30초마다(`429 too_soon`).
- 인증을 못 끝낸 가입은 같은 이메일로 다시 가입할 수 있다(비밀번호 갱신 + 새 코드).
  인증이 끝난 이메일이면 `409 email_taken`.
- 로그인: 잠금 중 `423 locked` → 비밀번호 확인(`401 invalid_credentials`, 5회면 423)
  → 미인증 `403 unverified`. 없는 이메일도 같은 401 이라 존재 여부가 새지 않는다.
- `remember` 면 세션 30일. 아니면 `AUTH_SESSION_TTL_SECONDS`.
- 재설정 토큰은 32바이트 url-safe, 해시만 저장, 30분. 성공하면 그 사용자의 세션을 전부 지운다
  (세션 저장소의 `delete_sessions_for_user`). 재설정 메일을 받은 것은 이메일 소유 증명이라
  미인증 계정도 인증 완료로 바꾼다.
- `GET /api/auth/me` 의 `user` 에 `emailVerified`, `needsConsent`, `experience`(지금은 항상 null)
  가 붙는다. `needsConsent` 는 `user_consents` 행이 없을 때 true 다. 이메일 가입은 가입 때
  저장되고, Google·Kakao 첫 가입은 `POST /api/auth/consent` 로 채운다.

## 환경 변수

```text
MAIL_PROVIDER=console   # console | ses
MAIL_FROM=              # ses 일 때 SES 검증 발신 주소
AUTH_PUBLIC_BASE_URL=   # 재설정 링크 앞부분: {AUTH_PUBLIC_BASE_URL}/forgot?step=reset&token=...
```

## 테이블 (migrations/0025_email_auth.sql)

`email_credentials`, `email_verifications`(verify·reset 공용), `login_attempts`, `user_consents`.
