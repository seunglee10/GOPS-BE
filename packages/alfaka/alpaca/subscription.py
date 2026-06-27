# 역할: 사용자가 설정한 종목/채널을 Alpaca WebSocket 구독 요청 JSON으로 만듭니다.
# 기준: config/market-data-request.json을 기본 universe와 구독 정책의 기준으로 씁니다.
# 우선순위: .env의 ALPACA_SYMBOLS/ALPACA_CHANNELS가 있으면 .env 값을 먼저 씁니다.
import json
import os
import re
from pathlib import Path

from alfaka.common.env import load_dotenv, parse_csv


DEFAULT_REQUEST_CONFIG = {
    "defaultUniverse": "semiconductor-100",
    "defaultSymbols": ["AAPL", "TSLA", "NVDA"],
    "defaultSeedSymbols": ["NVDA", "AMD", "AVGO", "TSM", "ASML", "AMAT", "MU"],
    "defaultChannels": ["bars", "updatedBars", "dailyBars", "statuses"],
    "activeChartChannels": ["trades"],
    "validChannels": ["bars", "updatedBars", "trades", "dailyBars", "statuses", "quotes", "corrections", "cancelErrors"],
    "symbolPattern": r"^[A-Z][A-Z0-9]{0,9}(\.[A-Z])?$",
    "companyToSymbol": {},
    "symbolMetadata": {},
}


def default_config_path():
    return Path(__file__).resolve().parents[3] / "config" / "market-data-request.json"


def resolve_request_config_path():
    raw_path = os.getenv("ALFAKA_REQUEST_CONFIG")
    if not raw_path:
        return default_config_path()

    config_path = Path(raw_path)
    if config_path.exists() or config_path.is_absolute():
        return config_path

    repo_relative_path = default_config_path().parents[1] / config_path
    if repo_relative_path.exists():
        return repo_relative_path

    return config_path


def load_request_config():
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
        "defaultChannels": loaded_config.get("defaultChannels") or DEFAULT_REQUEST_CONFIG["defaultChannels"],
        "activeChartChannels": loaded_config.get("activeChartChannels") or DEFAULT_REQUEST_CONFIG["activeChartChannels"],
        "defaultSymbols": loaded_config.get("defaultSymbols") or DEFAULT_REQUEST_CONFIG["defaultSymbols"],
        "defaultSeedSymbols": loaded_config.get("defaultSeedSymbols") or DEFAULT_REQUEST_CONFIG["defaultSeedSymbols"],
    }


def resolve_symbol(value, config=None):
    config = config or load_request_config()
    cleaned = value.strip()
    if not cleaned:
        return None

    mapped_symbol = config["companyToSymbol"].get(cleaned.lower())
    symbol = (mapped_symbol or cleaned).upper()
    validate_symbol(symbol, config)
    return symbol


def validate_symbol(symbol, config):
    if not re.fullmatch(config["symbolPattern"], symbol):
        raise ValueError(f"지원하지 않는 회사명 또는 심볼입니다: {symbol}")


def validate_channels(channels, config):
    valid_channels = set(config["validChannels"])
    invalid_channels = [channel for channel in channels if channel not in valid_channels]
    if invalid_channels:
        raise ValueError(f"지원하지 않는 Alpaca 채널입니다: {', '.join(invalid_channels)}")


def configured_universe_name(config=None):
    config = config or load_request_config()
    return (os.getenv("ALPACA_UNIVERSE", "") or config["defaultUniverse"]).strip()


def configured_universe_symbols(config=None):
    config = config or load_request_config()
    universe_name = configured_universe_name(config)
    if universe_name != "semiconductor-100":
        raise ValueError(f"지원하지 않는 ALPACA_UNIVERSE입니다: {universe_name}")
    return _validated_symbol_list(config.get("defaultSymbols") or [], config, "ALPACA_UNIVERSE")


def configured_seed_symbols(config=None):
    config = config or load_request_config()
    raw_symbols = os.getenv("ALPACA_SYMBOLS")
    values = parse_csv(raw_symbols) if raw_symbols is not None else list(config.get("defaultSeedSymbols") or [])
    if not values:
        raise ValueError("ALPACA_SYMBOLS가 비어 있습니다. 기본 수집/watch list seed 심볼을 CSV로 설정하세요.")
    return _validated_symbol_list(values, config, "ALPACA_SYMBOLS")


def _validated_symbol_list(values, config, source_name):
    symbols = []
    seen = set()
    for value in values:
        if not isinstance(value, str):
            continue
        symbol = value.strip().upper()
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
    load_dotenv()
    config = load_request_config()
    requested_symbol = resolve_symbol(company_or_symbol, config) if company_or_symbol else None

    if requested_symbol:
        symbols = [requested_symbol]
    else:
        symbols = configured_seed_symbols(config)

    channels = parse_csv(os.getenv("ALPACA_CHANNELS", ",".join(config["defaultChannels"])))
    validate_channels(channels, config)
    return symbols, channels


def build_subscription_request(symbols, channels):
    request = {"action": "subscribe"}
    for channel in channels:
        request[channel] = symbols
    return request


def build_request_from_env(company_or_symbol=None):
    symbols, channels = load_symbols_and_channels(company_or_symbol)
    return build_subscription_request(symbols, channels)


def print_request(company_or_symbol=None):
    request = build_request_from_env(company_or_symbol)
    print(json.dumps(request, indent=2, ensure_ascii=False))
