import os


DEFAULT_CRYPTO_SYMBOLS = ("BTCUSD",)
QUOTE_CURRENCIES = ("USD", "USDT", "USDC")


def configured_crypto_symbols(environ=None):
    """환경변수에서 crypto로 취급할 내부 심볼 목록을 읽습니다."""
    environ = environ or os.environ
    raw_value = environ.get("ALPACA_CRYPTO_SYMBOLS", ",".join(DEFAULT_CRYPTO_SYMBOLS))
    symbols = [normalize_market_symbol(item) for item in raw_value.split(",") if item.strip()]
    return tuple(symbol for symbol in symbols if symbol)


def normalize_market_symbol(value):
    """앱 내부에서 쓰는 심볼 표기(BTCUSD처럼 슬래시 없는 대문자)로 맞춥니다."""
    symbol = str(value or "").strip().upper()
    if "/" in symbol:
        return symbol.replace("/", "")
    return symbol


def is_crypto_symbol(value, environ=None):
    """주어진 심볼이 crypto 수집 대상으로 설정되어 있는지 확인합니다."""
    return normalize_market_symbol(value) in set(configured_crypto_symbols(environ))


def alpaca_provider_symbol(value, config=None, environ=None):
    """내부 심볼을 Alpaca API가 요구하는 provider 심볼로 변환합니다."""
    symbol = normalize_market_symbol(value)
    configured_symbol = ((config or {}).get("symbolMetadata") or {}).get(symbol, {}).get("alpacaSymbol")
    if configured_symbol:
        return configured_symbol
    if not is_crypto_symbol(symbol, environ):
        return symbol
    for quote_currency in QUOTE_CURRENCIES:
        if symbol.endswith(quote_currency) and len(symbol) > len(quote_currency):
            return f"{symbol[:-len(quote_currency)]}/{quote_currency}"
    return symbol


def normalize_provider_symbol(value):
    """Alpaca에서 받은 provider 심볼을 GOPS 내부 심볼로 되돌립니다."""
    return normalize_market_symbol(value)
