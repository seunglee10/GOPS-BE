# AWS Runtime Values

Do not store real `.env` files or secret values here.

Put runtime values in these places:

```text
Alpaca API key/secret       AWS Secrets Manager dev/alpaca
KIS demo credentials        AWS Secrets Manager dev/kis
OpenAI API key              AWS Secrets Manager /gops/prod/agent-orchestrator/openai/api-key
Kafka bootstrap servers     infra/k8s/overlays/aws config patch
Redis URL                   infra/k8s/overlays/aws config patch
Postgres connection values  Kubernetes Secret or external secret
ClickHouse connection       infra/k8s/overlays/aws config/secret patch
S3 bucket and prefixes      infra/k8s/overlays/aws config patch
Custom image repositories   infra/k8s/overlays/aws/kustomization.yaml
IRSA role ARN               infra/k8s/overlays/aws/serviceaccount patch
```

Local development uses repo-local `.env`, which must stay ignored and uncommitted.
