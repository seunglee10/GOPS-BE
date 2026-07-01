import json
import os

from fastapi import APIRouter
from alfaka.common.secrets import (
    ALPACA_CREDENTIAL_SOURCE_AUTO,
    ALPACA_CREDENTIAL_SOURCE_AWS,
    ALPACA_CREDENTIAL_SOURCE_LOCAL,
    local_alpaca_credentials,
    resolve_alpaca_credential_source,
)

router = APIRouter()
_runtime_config_logged = False


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "gops-backend"}


@router.get("/health/config")
def runtime_config() -> dict[str, object]:
    components = pipeline_component_health()
    return {
        "status": "ok",
        "aws": {
            "region": os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ap-northeast-2",
            "accessKeyId": presence(os.getenv("AWS_ACCESS_KEY_ID")),
            "secretAccessKey": presence(os.getenv("AWS_SECRET_ACCESS_KEY")),
            "sessionToken": presence(os.getenv("AWS_SESSION_TOKEN")),
        },
        "s3": {
            "bucket": os.getenv("S3_BUCKET") or "",
            "endpoint": presence(os.getenv("S3_ENDPOINT_URL")),
            "endpointMode": "real-aws" if not os.getenv("S3_ENDPOINT_URL") else "custom-endpoint",
        },
        "alpaca": {
            "localKeyId": presence(os.getenv("APCA_API_KEY_ID")),
            "localSecretKey": presence(os.getenv("APCA_API_SECRET_KEY")),
            "secretName": presence(os.getenv("ALPACA_SECRET_NAME")),
            "configuredCredentialSource": configured_alpaca_credential_source(),
            "credentialSource": alpaca_credential_source(),
            "feedProfile": os.getenv("ALPACA_FEED_PROFILE") or os.getenv("ALPACA_FEED") or "sip",
            "feedProfiles": configured_feed_profiles(),
        },
        "pipeline": {
            "components": components,
        },
        "warnings": runtime_config_warnings(components),
    }


def log_runtime_config() -> None:
    global _runtime_config_logged
    if _runtime_config_logged:
        return
    _runtime_config_logged = True
    print(f"GOPS runtime config: {json.dumps(runtime_config(), ensure_ascii=False, sort_keys=True)}", flush=True)


def presence(value: str | None) -> str:
    return "SET" if value else "EMPTY"


def alpaca_credential_source() -> str:
    try:
        configured = resolve_alpaca_credential_source()
    except ValueError:
        return "invalid"
    if configured == ALPACA_CREDENTIAL_SOURCE_LOCAL:
        return "local-env" if all(local_alpaca_credentials()) else "missing"
    if configured == ALPACA_CREDENTIAL_SOURCE_AWS:
        return "aws-secrets-manager" if os.getenv("ALPACA_SECRET_NAME") else "missing"
    if configured == ALPACA_CREDENTIAL_SOURCE_AUTO and all(local_alpaca_credentials()):
        return "local-env"
    if os.getenv("ALPACA_SECRET_NAME"):
        return "aws-secrets-manager"
    return "missing"


def configured_alpaca_credential_source() -> str:
    try:
        return resolve_alpaca_credential_source()
    except ValueError:
        return "invalid"


def runtime_config_warnings(pipeline_components: dict[str, object] | None = None) -> list[str]:
    warnings = []
    if os.getenv("ALFAKA_REQUEST_CONFIG") not in {None, "", "systems/market-data/config/market-data-request.json"}:
        warnings.append("stale_request_config_path")
    if os.getenv("ALPACA_UNIVERSE") not in {None, "", "sp500"}:
        warnings.append("alpaca_universe_not_sp500")
    channels = {item.strip() for item in (os.getenv("ALPACA_CHANNELS") or "").split(",") if item.strip()}
    if channels and "dailyBars" not in channels:
        warnings.append("alpaca_channels_missing_dailyBars")
    if channels and "statuses" not in channels:
        warnings.append("alpaca_channels_missing_statuses")
    if os.getenv("S3_PROCESSED_FORMAT") not in {None, "", "parquet"}:
        warnings.append("s3_processed_format_not_parquet")
    profiles = set(configured_feed_profiles())
    allowed_profiles = {"sip", "iex", "boats", "overnight", "test"}
    if any(profile not in allowed_profiles for profile in profiles):
        warnings.append("invalid_alpaca_feed_profile")
    active_profile = os.getenv("ALPACA_FEED_PROFILE") or os.getenv("ALPACA_FEED") or "sip"
    if profiles and active_profile not in profiles:
        warnings.append("alpaca_feed_profile_not_listed")
    if configured_alpaca_credential_source() == "invalid":
        warnings.append("invalid_alpaca_credential_source")
    if pipeline_components and pipeline_components.get("available") is True:
        if pipeline_components.get("missing"):
            warnings.append("pipeline_component_missing")
        if pipeline_components.get("unhealthy"):
            warnings.append("pipeline_component_unhealthy")
    return warnings


def configured_feed_profiles() -> list[str]:
    profiles = csv_values(os.getenv("ALPACA_FEED_PROFILES"))
    if profiles:
        return profiles
    return [os.getenv("ALPACA_FEED_PROFILE") or os.getenv("ALPACA_FEED") or "sip"]


def csv_values(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def pipeline_component_health() -> dict[str, object]:
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        return {"available": False, "reason": "redis_url_not_configured"}
    try:
        import redis
        from alfaka.common.redis_keys import RedisKeyBuilder
        from alfaka.common.runtime_health import read_component_health

        client = redis.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=0.2, socket_timeout=0.2)
        keys = RedisKeyBuilder()
        names = pipeline_required_component_names()
        items = {
            name: redact_component_health(read_component_health(client, keys, name))
            for name in names
        }
        return {
            "available": True,
            "required": names,
            "items": items,
            **pipeline_component_summary(items),
        }
    except Exception:
        return {"available": False, "reason": "redis_health_probe_failed"}


def pipeline_required_component_names() -> list[str]:
    configured = unique_values(csv_values(os.getenv("PIPELINE_REQUIRED_COMPONENTS")))
    if configured:
        return configured
    return unique_values([
        *(f"market-ingestor-{profile}" for profile in configured_feed_profiles()),
        "market-processor",
    ])


def unique_values(values: list[str]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def pipeline_component_summary(items: dict[str, object]) -> dict[str, object]:
    missing = sorted(name for name, payload in items.items() if not payload)
    unhealthy = sorted(
        name for name, payload in items.items()
        if isinstance(payload, dict) and payload.get("status") not in {None, "ok"}
    )
    return {
        "healthy": not missing and not unhealthy,
        "missing": missing,
        "unhealthy": unhealthy,
    }


def redact_component_health(payload):
    if not payload:
        return None
    allowed = {
        "component",
        "status",
        "updatedAt",
        "feedProfile",
        "alpacaFeed",
        "websocketUrl",
        "supportedSessions",
        "channels",
        "symbolCount",
        "lastChannel",
        "lastSymbol",
        "lastEventTime",
        "lastMarketSession",
        "lastSourceEventId",
        "lastFeed",
        "lastFeedProfile",
        "lastResult",
        "lastEventAt",
        "heartbeatResult",
        "lastError",
        "alpacaError",
        "error",
    }
    result = {key: payload.get(key) for key in allowed if key in payload}
    for key in ("alpacaError", "error", "lastError"):
        if result.get(key):
            result[key] = str(result[key])[:300]
    return result
