# 역할: 사용자가 설정한 종목/채널을 Alpaca WebSocket 구독 요청 JSON으로 만듭니다.
# 기준: config/market-data-request.json을 기준으로 MVP 구독 채널을 고정합니다.
# 우선순위: .env의 ALPACA_SYMBOLS/ALPACA_CHANNELS가 있으면 .env 값을 먼저 씁니다.
import json
import os
import re
from pathlib import Path

from alfaka.common.env import load_dotenv, parse_csv


DEFAULT_REQUEST_CONFIG = {
    "defaultSymbols": ["AAPL", "TSLA", "NVDA"],
    "defaultChannels": ["bars", "updatedBars", "trades"],
    "validChannels": ["bars", "updatedBars", "trades"],
    "symbolPattern": r"^[A-Z][A-Z0-9]{0,9}(\.[A-Z])?$",
    "companyToSymbol": {},
}


def default_config_path():
    return Path(__file__).resolve().parents[3] / "config" / "market-data-request.json"


def load_request_config():
    config_path = Path(os.getenv("ALFAKA_REQUEST_CONFIG", default_config_path()))
    if not config_path.exists():
        return DEFAULT_REQUEST_CONFIG

    with config_path.open("r", encoding="utf-8") as config_file:
        loaded_config = json.load(config_file)

    return {
        **DEFAULT_REQUEST_CONFIG,
        **loaded_config,
        "companyToSymbol": loaded_config.get("companyToSymbol") or {},
        "defaultChannels": loaded_config.get("defaultChannels") or DEFAULT_REQUEST_CONFIG["defaultChannels"],
        "defaultSymbols": loaded_config.get("defaultSymbols") or DEFAULT_REQUEST_CONFIG["defaultSymbols"],
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


def load_symbols_and_channels(company_or_symbol=None):
    load_dotenv()
    config = load_request_config()
    requested_symbol = resolve_symbol(company_or_symbol, config) if company_or_symbol else None

    if requested_symbol:
        symbols = [requested_symbol]
    else:
        symbols = parse_csv(os.getenv("ALPACA_SYMBOLS", ",".join(config["defaultSymbols"])))
        for symbol in symbols:
            validate_symbol(symbol, config)

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
