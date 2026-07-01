import re


SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{0,9}(\.[A-Z])?$")


def resolve_trade_subscription_plan(
    active_symbols=None,
    watchlist_symbols=None,
    hot_symbols=None,
    max_symbols=None,
    max_watchlist_symbols=None,
    max_hot_symbols=None,
):
    active = normalize_symbols(active_symbols)
    watchlist = limit_symbols(normalize_symbols(watchlist_symbols), max_watchlist_symbols)
    hot = limit_symbols(normalize_symbols(hot_symbols), max_hot_symbols)

    ordered = []
    tiers_by_symbol = {}
    for tier, symbols in (("active", active), ("watchlist", watchlist), ("hot", hot)):
        for symbol in symbols:
            if symbol not in ordered:
                ordered.append(symbol)
            tiers_by_symbol.setdefault(symbol, []).append(tier)

    cap = parse_positive_int(max_symbols)
    if cap is not None:
        ordered = ordered[:cap]
        tiers_by_symbol = {symbol: tiers_by_symbol[symbol] for symbol in ordered}

    return {
        "symbols": ordered,
        "tiersBySymbol": tiers_by_symbol,
        "counts": {
            "active": len(active),
            "watchlist": len(watchlist),
            "hot": len(hot),
            "resolved": len(ordered),
        },
    }


def normalize_symbols(values):
    normalized = []
    seen = set()
    for value in values or []:
        if not isinstance(value, str):
            continue
        symbol = value.strip().upper()
        if not SYMBOL_PATTERN.fullmatch(symbol) or symbol in seen:
            continue
        normalized.append(symbol)
        seen.add(symbol)
    return normalized


def limit_symbols(symbols, max_symbols):
    cap = parse_non_negative_int(max_symbols)
    if cap is None:
        return symbols
    return symbols[:cap]


def parse_non_negative_int(value):
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def parse_positive_int(value):
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
