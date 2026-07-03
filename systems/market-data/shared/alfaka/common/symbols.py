import os


DEFAULT_CRYPTO_SYMBOLS = ("BTCUSD",)
QUOTE_CURRENCIES = ("USD", "USDT", "USDC")


def configured_crypto_symbols(environ=None):
    environ = environ or os.environ
    raw_value = environ.get("ALPACA_CRYPTO_SYMBOLS", ",".join(DEFAULT_CRYPTO_SYMBOLS))
    symbols = [normalize_market_symbol(item) for item in raw_value.split(",") if item.strip()]
    return tuple(symbol for symbol in symbols if symbol)


def normalize_market_symbol(value):
    symbol = str(value or "").strip().upper()
    if "/" in symbol:
        return symbol.replace("/", "")
    return symbol


def is_crypto_symbol(value, environ=None):
    return normalize_market_symbol(value) in set(configured_crypto_symbols(environ))


def alpaca_provider_symbol(value, config=None, environ=None):
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
    return normalize_market_symbol(value)
