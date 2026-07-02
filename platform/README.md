# GOPS Platform Contracts

`platform/` documents runtime dependencies.
It should not contain application logic.

```text
kafka/       topic contract and local/MSK transition notes
redis/       cache endpoint contract
postgres/    order DB endpoint and migration contract
clickhouse/  chart serving projection contract
s3/          durable object-storage contract
secrets/     Secrets Manager contract
```

Local development may use Docker Compose.
AWS/EKS may later use single pods or managed services depending on cost and operational choices.
