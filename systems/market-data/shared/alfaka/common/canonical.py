"""Canonical candle metadata shared by historical, stream, and serving paths."""

CANONICAL_VERSION = "v2"
LEGACY_VERSION = "legacy"
CANONICAL_HISTORICAL_ADJUSTMENT = "split"
LIVE_PRICE_ADJUSTMENT = "live"
UNKNOWN_PRICE_ADJUSTMENT = "unknown"
SERVING_PRICE_ADJUSTMENTS = (CANONICAL_HISTORICAL_ADJUSTMENT, LIVE_PRICE_ADJUSTMENT)
HISTORICAL_SERVING_PRICE_ADJUSTMENTS = (CANONICAL_HISTORICAL_ADJUSTMENT,)


def normalize_price_adjustment(value, default=UNKNOWN_PRICE_ADJUSTMENT):
    normalized = str(value or default).strip().lower()
    return normalized or default


def normalize_canonical_version(value, default=LEGACY_VERSION):
    normalized = str(value or default).strip().lower()
    return normalized or default


def canonical_version_for_adjustment(adjustment):
    normalized = normalize_price_adjustment(adjustment)
    return CANONICAL_VERSION if normalized in SERVING_PRICE_ADJUSTMENTS else LEGACY_VERSION


def candle_metadata(adjustment=None, canonical_version=None):
    normalized_adjustment = normalize_price_adjustment(adjustment)
    normalized_version = normalize_canonical_version(
        canonical_version,
        default=canonical_version_for_adjustment(normalized_adjustment),
    )
    return {
        "priceAdjustment": normalized_adjustment,
        "canonicalVersion": normalized_version,
    }


def historical_adjustment_from_env(environ):
    requested = normalize_price_adjustment(environ.get("HISTORICAL_ADJUSTMENT"), default=CANONICAL_HISTORICAL_ADJUSTMENT)
    if requested == CANONICAL_HISTORICAL_ADJUSTMENT:
        return requested
    allow_noncanonical = str(environ.get("ALLOW_NON_CANONICAL_HISTORICAL_ADJUSTMENT") or "").lower() in {"1", "true", "yes"}
    return requested if allow_noncanonical else CANONICAL_HISTORICAL_ADJUSTMENT


def is_serving_canonical(price_adjustment, canonical_version):
    return (
        normalize_canonical_version(canonical_version) == CANONICAL_VERSION
        and normalize_price_adjustment(price_adjustment) in SERVING_PRICE_ADJUSTMENTS
    )


def is_historical_canonical(price_adjustment, canonical_version):
    return (
        normalize_canonical_version(canonical_version) == CANONICAL_VERSION
        and normalize_price_adjustment(price_adjustment) in HISTORICAL_SERVING_PRICE_ADJUSTMENTS
    )
