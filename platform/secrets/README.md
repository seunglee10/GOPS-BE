# Secrets Platform Contract

Do not commit secret values.

AWS Secrets Manager names:

```text
dev/alpaca
dev/kis
```

`dev/alpaca` must contain:

```json
{"APCA_API_KEY_ID":"...","APCA_API_SECRET_KEY":"..."}
```

`dev/kis` must contain KIS demo credentials.

GOPS Google login uses these environment secrets on the `gops-backend` pod:

```text
GOOGLE_OAUTH_CLIENT_ID
GOOGLE_OAUTH_CLIENT_SECRET
AUTH_SESSION_SECRET
```

Do not commit OAuth client secrets or session secrets.
