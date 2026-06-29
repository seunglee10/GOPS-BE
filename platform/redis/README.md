# Redis Platform Contract

Current local stage:

```text
docker-compose redis
REDIS_URL=redis://redis:6379/0
REDIS_KEY_PREFIX=
```

AWS/EKS can later point `REDIS_URL` at ElastiCache, Valkey, or another Redis-compatible endpoint.

The API server also stores Google login sessions in Redis when `AUTH_ENABLED=true`.
Session keys use `AUTH_REDIS_KEY_PREFIX` (`gops:auth` by default) and TTLs, so no
separate Redis deployment is required for auth.
