# Secrets Platform Contract

Do not commit secret values.

AWS Secrets Manager names:

```text
dev/alpaca
dev/kis
dev/google-oauth
```

`dev/alpaca` must contain:

```json
{"APCA_API_KEY_ID":"...","APCA_API_SECRET_KEY":"..."}
```

`dev/kis` must contain KIS demo credentials.

GOPS Google login can use direct environment secrets on the `gops-backend` pod:

```text
GOOGLE_OAUTH_CLIENT_ID
GOOGLE_OAUTH_CLIENT_SECRET
AUTH_SESSION_SECRET
```

For AWS/EKS, prefer `GOOGLE_OAUTH_SECRET_NAME` and keep the values in AWS
Secrets Manager. The secret JSON may use:

```json
{"GOOGLE_OAUTH_CLIENT_ID":"...","GOOGLE_OAUTH_CLIENT_SECRET":"...","AUTH_SESSION_SECRET":"..."}
```

It may also keep Google's downloaded OAuth client shape:

```json
{"web":{"client_id":"...","client_secret":"..."},"AUTH_SESSION_SECRET":"..."}
```

Do not commit OAuth client secrets or session secrets.
