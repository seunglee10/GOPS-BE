# S3 Platform Contract

Current AWS bucket:

```text
gops-market-data-<aws-account-id>-ap-northeast-2-an
```

S3 stores raw archives, processed/final market data, live artifacts, and future replay/evidence material.

Leave `S3_ENDPOINT_URL` empty for real AWS S3.
Use the compose `local-s3` profile only for MinIO experiments.
