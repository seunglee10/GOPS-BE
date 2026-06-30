# Secrets Platform Contract

Do not commit secret values.

AWS Secrets Manager names:

```text
dev/alpaca
dev/kis
oauth/google
/gops/prod/agent-orchestrator/openai/api-key
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

`/gops/prod/agent-orchestrator/openai/api-key` is read by the AWS/EKS
ExternalSecret manifests and becomes Kubernetes Secret
`alfaka-openai-secret` key `OPENAI_API_KEY`.
The EKS cluster must run External Secrets Operator before applying these
manifests.

Expected SecretString shape:

```json
{"OPENAI_API_KEY":"sk-..."}
```

Do not commit OpenAI API keys.
