import json
import os

from alfaka.common.env import utc_now_iso


DEFAULT_COMPONENT_HEALTH_TTL_SECONDS = 300


def write_component_health(redis_client, redis_keys, component, **fields):
    payload = {
        "component": component,
        "status": fields.pop("status", "ok"),
        "updatedAt": utc_now_iso(),
        **fields,
    }
    redis_client.set(
        redis_keys.component_health(component),
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )
    redis_client.expire(redis_keys.component_health(component), component_health_ttl_seconds())
    return payload


def read_component_health(redis_client, redis_keys, component):
    value = redis_client.get(redis_keys.component_health(component))
    return json.loads(value) if value else None


def component_health_ttl_seconds():
    try:
        parsed = int(os.getenv("COMPONENT_HEALTH_TTL_SECONDS", str(DEFAULT_COMPONENT_HEALTH_TTL_SECONDS)))
    except ValueError:
        return DEFAULT_COMPONENT_HEALTH_TTL_SECONDS
    return parsed if parsed > 0 else DEFAULT_COMPONENT_HEALTH_TTL_SECONDS
