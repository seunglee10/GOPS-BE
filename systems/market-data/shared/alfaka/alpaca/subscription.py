# 역할: 사용자가 설정한 종목/채널을 Alpaca WebSocket 구독 요청 JSON으로 만듭니다.
# 기준: systems/market-data/config/market-data-request.json을 기본 universe와 구독 정책의 기준으로 씁니다.
# 우선순위: .env의 ALPACA_SYMBOLS/ALPACA_CHANNELS가 있으면 .env 값을 먼저 씁니다.
import json
import os
import re
from pathlib import Path

from alfaka.common.env import load_dotenv, parse_csv
from alfaka.common.symbols import alpaca_provider_symbol, normalize_market_symbol


DEFAULT_REQUEST_CONFIG = {
    "defaultUniverse": "",
    "universeRegistryPath": "",
    "collectionSymbolSource": "on-demand",
    "defaultSymbols": [],
    "defaultSeedSymbols": [],
    "extraSymbols": [],
    "defaultChannels": ["bars", "updatedBars", "dailyBars", "statuses"],
    "activeChartChannels": ["trades"],
    "validChannels": ["bars", "updatedBars", "trades", "dailyBars", "statuses", "quotes", "corrections", "cancelErrors"],
    "symbolPattern": r"^[A-Z][A-Z0-9]{0,9}(\.[A-Z])?$",
    "companyToSymbol": {},
    "symbolMetadata": {},
}


def default_config_path():
    """repo 안의 기본 market-data 요청 설정 파일 위치를 찾습니다."""
    current_file = Path(__file__).resolve()
    for parent in current_file.parents:
        candidate = parent / "config" / "market-data-request.json"
        if candidate.exists():
            return candidate
    return current_file.parents[3] / "config" / "market-data-request.json"


def resolve_request_config_path():
    """환경변수 또는 repo 상대 경로를 실제 설정 파일 경로로 해석합니다."""
    raw_path = os.getenv("ALFAKA_REQUEST_CONFIG")
    if not raw_path:
        return default_config_path()

    config_path = Path(raw_path)
    if config_path.exists() or config_path.is_absolute():
        return config_path

    for parent in default_config_path().parents:
        repo_relative_path = parent / config_path
        if repo_relative_path.exists():
            return repo_relative_path

    return config_path


def load_request_config():
    """구독 요청 설정 JSON을 읽고 누락된 값은 안전한 기본값으로 채웁니다."""
    config_path = resolve_request_config_path()
    if not config_path.exists():
        return DEFAULT_REQUEST_CONFIG

    with config_path.open("r", encoding="utf-8") as config_file:
        loaded_config = json.load(config_file)

    return {
        **DEFAULT_REQUEST_CONFIG,
        **loaded_config,
        "companyToSymbol": loaded_config.get("companyToSymbol") or {},
        "symbolMetadata": loaded_config.get("symbolMetadata") or {},
        "defaultUniverse": loaded_config.get("defaultUniverse") or DEFAULT_REQUEST_CONFIG["defaultUniverse"],
        "universeRegistryPath": loaded_config["universeRegistryPath"] if "universeRegistryPath" in loaded_config else DEFAULT_REQUEST_CONFIG["universeRegistryPath"],
        "collectionSymbolSource": loaded_config.get("collectionSymbolSource") or DEFAULT_REQUEST_CONFIG["collectionSymbolSource"],
        "defaultChannels": loaded_config.get("defaultChannels") or DEFAULT_REQUEST_CONFIG["defaultChannels"],
        "activeChartChannels": loaded_config.get("activeChartChannels") or DEFAULT_REQUEST_CONFIG["activeChartChannels"],
        "defaultSymbols": loaded_config.get("defaultSymbols") or DEFAULT_REQUEST_CONFIG["defaultSymbols"],
        "defaultSeedSymbols": loaded_config.get("defaultSeedSymbols") or DEFAULT_REQUEST_CONFIG["defaultSeedSymbols"],
        "extraSymbols": loaded_config.get("extraSymbols") or DEFAULT_REQUEST_CONFIG["extraSymbols"],
    }


def resolve_symbol(value, config=None):
    """회사명 별칭이나 사용자 입력을 검증된 내부 심볼로 변환합니다."""
    config = config or load_request_config()
    cleaned = value.strip()
    if not cleaned:
        return None

    mapped_symbol = config["companyToSymbol"].get(cleaned.lower())
    symbol = normalize_market_symbol(mapped_symbol or cleaned)
    validate_symbol(symbol, config)
    return symbol


def validate_symbol(symbol, config):
    """설정된 정규식 규칙에 맞는 심볼인지 검사합니다."""
    if not re.fullmatch(config["symbolPattern"], symbol):
        raise ValueError(f"지원하지 않는 회사명 또는 심볼입니다: {symbol}")


def validate_channels(channels, config):
    """요청 채널이 Alpaca 수집 설정에서 허용된 채널인지 검사합니다."""
    valid_channels = set(config["validChannels"])
    invalid_channels = [channel for channel in channels if channel not in valid_channels]
    if invalid_channels:
        raise ValueError(f"지원하지 않는 Alpaca 채널입니다: {', '.join(invalid_channels)}")


def configured_universe_name(config=None):
    """환경변수와 설정 파일을 합쳐 현재 universe 이름을 결정합니다."""
    config = config or load_request_config()
    return (os.getenv("ALPACA_UNIVERSE", "") or config["defaultUniverse"]).strip()


def configured_universe_symbols(config=None):
    """선택된 universe에 포함된 수집 후보 심볼 목록을 반환합니다."""
    config = config or load_request_config()
    universe_name = configured_universe_name(config).lower()
    if not universe_name:
        return []
    configured_universes = config.get("universes") or {}
    if universe_name in configured_universes:
        return _validated_symbol_list(configured_universes[universe_name], config, f"ALPACA_UNIVERSE:{universe_name}")
    registry_symbols = load_universe_registry_symbols(config)
    if registry_symbols:
        return _validated_symbol_list(registry_symbols, config, f"ALPACA_UNIVERSE_REGISTRY_PATH:{universe_name}")
    raise ValueError(f"지원하지 않는 ALPACA_UNIVERSE입니다: {universe_name}")


def load_universe_registry_symbols(config=None):
    """registry 파일에 저장된 universe 심볼을 읽고 없으면 기본 심볼을 반환합니다."""
    config = config or load_request_config()
    registry = load_universe_registry(config)
    values = registry.get("symbols") if isinstance(registry, dict) else None
    return values or config.get("defaultSymbols") or []


def load_universe_registry(config=None):
    """universe registry JSON 파일을 읽어 심볼 묶음 설정으로 반환합니다."""
    config = config or load_request_config()
    registry_path = resolve_universe_registry_path(config)
    if not registry_path or not registry_path.exists():
        return {"symbols": config.get("defaultSymbols") or []}
    with registry_path.open("r", encoding="utf-8") as registry_file:
        return json.load(registry_file)


def resolve_universe_registry_path(config=None):
    """registry 경로를 절대 경로나 설정 파일 기준 상대 경로로 해석합니다."""
    config = config or load_request_config()
    raw_path = os.getenv("ALPACA_UNIVERSE_REGISTRY_PATH") or config.get("universeRegistryPath")
    if not raw_path:
        return None
    registry_path = Path(raw_path)
    if registry_path.exists() or registry_path.is_absolute():
        return registry_path
    request_config_path = resolve_request_config_path()
    for parent in (request_config_path.parent, *request_config_path.parents):
        candidate = parent / registry_path
        if candidate.exists():
            return candidate
    return registry_path


def configured_collection_symbols(config=None):
    """수집기가 시작할 때 기본으로 구독할 심볼 목록을 결정합니다."""
    config = config or load_request_config()
    raw_symbols = os.getenv("ALPACA_COLLECTION_SYMBOLS")
    if raw_symbols is not None:
        values = parse_csv(raw_symbols)
        return _validated_symbol_list(values, config, "ALPACA_COLLECTION_SYMBOLS") if values else []

    source = (os.getenv("ALPACA_COLLECTION_SYMBOL_SOURCE") or config.get("collectionSymbolSource") or "seed").strip().lower()
    if source in {"on-demand", "ondemand", "none", "empty"}:
        return []
    if source == "universe":
        return configured_universe_symbols(config)
    if source == "registry":
        values = load_universe_registry_symbols(config)
        return _validated_symbol_list(values, config, "ALPACA_UNIVERSE_REGISTRY_PATH") if values else []
    if source == "seed":
        return configured_seed_symbols(config)
    if source == "defaultsymbols":
        values = config.get("defaultSymbols") or []
        return _validated_symbol_list(values, config, "defaultSymbols") if values else []
    raise ValueError(f"지원하지 않는 ALPACA_COLLECTION_SYMBOL_SOURCE입니다: {source}")


def configured_seed_symbols(config=None):
    """명시적으로 seed 된 심볼 목록을 환경변수 또는 설정 파일에서 읽습니다."""
    config = config or load_request_config()
    raw_symbols = os.getenv("ALPACA_SYMBOLS")
    values = parse_csv(raw_symbols) if raw_symbols is not None else list(config.get("defaultSeedSymbols") or [])
    if not values:
        return []
    return _validated_symbol_list(values, config, "ALPACA_SYMBOLS")


def configured_benchmark_symbols(config=None):
    """추천 기준지수로 영구 수집하되 일반 universe에는 섞지 않을 심볼입니다."""
    config = config or load_request_config()
    values = parse_csv(os.getenv("ALPACA_BENCHMARK_SYMBOLS", ""))
    return _validated_symbol_list(values, config, "ALPACA_BENCHMARK_SYMBOLS") if values else []


def _validated_symbol_list(values, config, source_name):
    """여러 출처에서 온 심볼 목록을 중복 제거하고 내부 표기로 정규화합니다."""
    symbols = []
    seen = set()
    for value in values:
        if not isinstance(value, str):
            continue
        symbol = normalize_market_symbol(value)
        if not symbol:
            continue
        try:
            validate_symbol(symbol, config)
        except ValueError as exc:
            raise ValueError(f"{source_name}에 유효하지 않은 심볼이 있습니다: {symbol}") from exc
        if symbol not in seen:
            symbols.append(symbol)
            seen.add(symbol)
    if not symbols:
        raise ValueError(f"{source_name}에서 사용할 수 있는 심볼이 없습니다.")
    return symbols


def load_symbols_and_channels(company_or_symbol=None):
    """CLI/환경변수 입력을 합쳐 최종 심볼 목록과 채널 목록을 만듭니다."""
    load_dotenv()
    config = load_request_config()
    requested_symbol = resolve_symbol(company_or_symbol, config) if company_or_symbol else None

    if requested_symbol:
        symbols = [requested_symbol]
    else:
        symbols = list(dict.fromkeys([
            *configured_collection_symbols(config),
            *configured_benchmark_symbols(config),
        ]))

    channels = parse_csv(os.getenv("ALPACA_CHANNELS", ",".join(config["defaultChannels"])))
    validate_channels(channels, config)
    return symbols, channels


def build_subscription_request(symbols, channels, action="subscribe", config=None):
    """내부 심볼 목록을 Alpaca WebSocket subscribe/unsubscribe 요청으로 만듭니다."""
    config = config or load_request_config()
    request = {"action": action}
    if not symbols:
        return request
    provider_symbols = alpaca_subscription_symbols(symbols, config)
    for channel in channels:
        request[channel] = provider_symbols
    return request


def alpaca_subscription_symbols(symbols, config=None):
    """GOPS 내부 심볼을 Alpaca 구독용 provider 심볼로 변환하고 중복을 제거합니다."""
    config = config or load_request_config()
    provider_symbols = []
    seen = set()
    for symbol in symbols:
        provider_symbol = alpaca_provider_symbol(symbol, config)
        if provider_symbol in seen:
            continue
        provider_symbols.append(provider_symbol)
        seen.add(provider_symbol)
    return provider_symbols


def build_request_from_env(company_or_symbol=None):
    """현재 환경 설정만으로 Alpaca WebSocket 구독 요청을 구성합니다."""
    symbols, channels = load_symbols_and_channels(company_or_symbol)
    return build_subscription_request(symbols, channels)


def print_request(company_or_symbol=None):
    """디버깅용으로 최종 구독 요청 JSON을 콘솔에 출력합니다."""
    request = build_request_from_env(company_or_symbol)
    print(json.dumps(request, indent=2, ensure_ascii=False))
