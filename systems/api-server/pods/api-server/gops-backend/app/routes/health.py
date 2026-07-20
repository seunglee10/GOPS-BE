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


@router.get("/api/health")
@router.get("/health")
async def health() -> dict[str, str]:
    """backend 프로세스가 살아 있는지 확인하는 기본 health 응답을 반환합니다."""
    return {"status": "ok", "service": "gops-backend"}


@router.get("/health/config")
def runtime_config() -> dict[str, object]:
    """운영자가 현재 런타임 환경설정 상태를 점검할 수 있는 요약을 반환합니다."""
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
            "rawPrefix": os.getenv("S3_RAW_PREFIX") or "",
            "finalPrefix": os.getenv("S3_FINAL_PREFIX") or "",
            "manifestPrefix": os.getenv("S3_MANIFEST_PREFIX") or "",
            "livePrefixEnabled": False,
        },
        "canonical": canonical_config(),
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
            "components": pipeline_component_health(),
        },
        "warnings": runtime_config_warnings(),
    }


def log_runtime_config() -> None:
    """프로세스 시작 후 한 번만 runtime config 요약을 로그로 남깁니다."""
    global _runtime_config_logged
    if _runtime_config_logged:
        return
    _runtime_config_logged = True
    print(f"GOPS runtime config: {json.dumps(runtime_config(), ensure_ascii=False, sort_keys=True)}", flush=True)


def presence(value: str | None) -> str:
    """민감한 값은 노출하지 않고 설정 여부만 SET/EMPTY로 표시합니다."""
    return "SET" if value else "EMPTY"


def alpaca_credential_source() -> str:
    """실제로 Alpaca credential을 어디서 읽을 수 있는지 판단합니다."""
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
    """환경변수에 설정된 Alpaca credential source 값을 검증해 반환합니다."""
    try:
        return resolve_alpaca_credential_source()
    except ValueError:
        return "invalid"


def runtime_config_warnings() -> list[str]:
    """현재 설정이 권장 런타임 계약에서 벗어난 항목을 warning 코드로 모읍니다."""
    warnings = []
    if os.getenv("ALFAKA_REQUEST_CONFIG") not in {None, "", "systems/market-data/config/market-data-request.json"}:
        warnings.append("stale_request_config_path")
    # Hybrid chart runtime intentionally keeps an S&P500 baseline universe for
    # bars/updatedBars/dailyBars/statuses. It is only a warning when the
    # baseline collection source is not the registry/universe contract.
    if os.getenv("ALPACA_UNIVERSE") and os.getenv("ALPACA_COLLECTION_SYMBOL_SOURCE") not in {"registry", "universe"}:
        warnings.append("alpaca_universe_without_registry_source")
    channels = {item.strip() for item in (os.getenv("ALPACA_CHANNELS") or "").split(",") if item.strip()}
    if channels and "dailyBars" not in channels:
        warnings.append("alpaca_channels_missing_dailyBars")
    if channels and "statuses" not in channels:
        warnings.append("alpaca_channels_missing_statuses")
    if os.getenv("S3_PROCESSED_FORMAT") not in {None, "", "parquet"}:
        warnings.append("s3_processed_format_not_parquet")
    if (os.getenv("HISTORICAL_ADJUSTMENT") or "split").lower() != "split":
        warnings.append("historical_adjustment_not_split")
    if env_bool("ALLOW_NON_CANONICAL_HISTORICAL_ADJUSTMENT", default=False):
        warnings.append("noncanonical_historical_adjustment_allowed")
    if not env_bool("CLICKHOUSE_REQUIRE_CANONICAL_CANDLES", default=True):
        warnings.append("clickhouse_canonical_filter_disabled")
    if not env_bool("S3_REQUIRE_CANONICAL_PROCESSED_CANDLES", default=True):
        warnings.append("s3_canonical_manifest_filter_disabled")
    profiles = set(configured_feed_profiles())
    allowed_profiles = {"sip", "boats", "overnight", "test", "crypto-us"}
    if any(profile not in allowed_profiles for profile in profiles):
        warnings.append("invalid_alpaca_feed_profile")
    expected_profiles = {"sip", "boats"}
    if profiles and not expected_profiles.issubset(profiles):
        warnings.append("alpaca_feed_profiles_missing_24_5_profile")
    if configured_alpaca_credential_source() == "invalid":
        warnings.append("invalid_alpaca_credential_source")
    return warnings


def canonical_config() -> dict[str, object]:
    """캔들 정규화와 S3/ClickHouse canonical 필터 설정을 요약합니다."""
    return {
        "historicalAdjustment": os.getenv("HISTORICAL_ADJUSTMENT") or "split",
        "allowNonCanonicalHistoricalAdjustment": env_bool(
            "ALLOW_NON_CANONICAL_HISTORICAL_ADJUSTMENT",
            default=False,
        ),
        "clickhouseRequireCanonicalCandles": env_bool(
            "CLICKHOUSE_REQUIRE_CANONICAL_CANDLES",
            default=True,
        ),
        "s3RequireCanonicalProcessedCandles": env_bool(
            "S3_REQUIRE_CANONICAL_PROCESSED_CANDLES",
            default=True,
        ),
        "s3ProcessedFormat": os.getenv("S3_PROCESSED_FORMAT") or "parquet",
    }


def env_bool(name: str, *, default: bool) -> bool:
    """환경변수 문자열을 bool 값으로 해석하고 없으면 기본값을 사용합니다."""
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.lower() in {"1", "true", "yes"}


def configured_feed_profiles() -> list[str]:
    """단일 또는 다중 Alpaca feed profile 설정을 문자열 목록으로 반환합니다."""
    profiles = csv_values(os.getenv("ALPACA_FEED_PROFILES"))
    if profiles:
        return profiles
    return [os.getenv("ALPACA_FEED_PROFILE") or os.getenv("ALPACA_FEED") or "sip"]


def csv_values(value: str | None) -> list[str]:
    """쉼표로 구분된 환경변수 값을 공백 제거된 문자열 목록으로 바꿉니다."""
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def pipeline_component_health() -> dict[str, object]:
    """Redis에 기록된 market ingestor/processor health 정보를 읽어 반환합니다."""
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        return {"available": False, "reason": "redis_url_not_configured"}
    try:
        import redis
        from alfaka.common.redis_keys import RedisKeyBuilder
        from alfaka.common.runtime_health import read_component_health

        client = redis.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=0.2, socket_timeout=0.2)
        keys = RedisKeyBuilder()
        ingestor_names = []
        for profile in configured_feed_profiles():
            component_profile = "boats" if profile == "overnight" else profile
            name = f"market-ingestor-{component_profile}"
            if name not in ingestor_names:
                ingestor_names.append(name)
        names = [*ingestor_names, "market-processor"]
        return {
            "available": True,
            "items": {
                name: redact_component_health(read_component_health(client, keys, name))
                for name in names
            },
        }
    except Exception:
        return {"available": False, "reason": "redis_health_probe_failed"}


def redact_component_health(payload):
    """health payload에서 화면에 보여도 되는 필드만 골라 반환합니다."""
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
        "currentMarketSession",
        "lastSourceEventId",
        "lastFeed",
        "lastFeedProfile",
        "lastResult",
        "alpacaError",
        "error",
    }
    result = {key: payload.get(key) for key in allowed if key in payload}
    for key in ("alpacaError", "error"):
        if result.get(key):
            result[key] = str(result[key])[:300]
    return result
