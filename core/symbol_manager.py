import MetaTrader5 as mt5


def get_available_symbols():
    """Return all symbols available in the connected MT5 terminal."""
    symbols = mt5.symbols_get()

    if symbols is None:
        print("Could not retrieve broker symbols:", mt5.last_error())
        return []

    return [symbol.name for symbol in symbols]


def find_symbol_candidates(base_symbol, available_symbols=None):
    """Find exact or likely broker-specific matches for a standard symbol."""
    if available_symbols is None:
        available_symbols = get_available_symbols()

    base = base_symbol.strip().upper()

    # Prefer an exact match.
    exact = [
        name for name in available_symbols
        if name.upper() == base
    ]

    if exact:
        return exact

    # Look for broker suffixes, such as EURUSDm or EURUSD.a.
    candidates = [
        name for name in available_symbols
        if name.upper().startswith(base)
    ]

    return candidates


def find_symbol(base_symbol, available_symbols=None):
    """
    Return a broker symbol only when the match is unambiguous.
    Returns None if no match or multiple possible matches exist.
    """
    candidates = find_symbol_candidates(
        base_symbol,
        available_symbols,
    )

    if len(candidates) == 1:
        return candidates[0]

    if len(candidates) > 1:
        print(
            f"Multiple broker matches for {base_symbol}: "
            f"{candidates}"
        )

    else:
        print(f"No broker match found for {base_symbol}")

    return None


def resolve_symbols(base_symbols):
    """Map standard symbols to unambiguous broker symbol names."""
    available = get_available_symbols()
    results = {}

    for base_symbol in base_symbols:
        results[base_symbol] = find_symbol(
            base_symbol,
            available,
        )

    return results


def activate_symbol(symbol):
    """Make a broker symbol available in Market Watch."""
    if not mt5.symbol_select(symbol, True):
        print(
            f"Could not activate {symbol}:",
            mt5.last_error(),
        )
        return False

    return True


def get_symbol_details(symbol):
    """Return important trading specifications for a broker symbol."""
    info = mt5.symbol_info(symbol)

    if info is None:
        print(
            f"Could not retrieve details for {symbol}:",
            mt5.last_error(),
        )
        return None

    return {
        "name": info.name,
        "visible": info.visible,
        "digits": info.digits,
        "point": info.point,
        "contract_size": info.trade_contract_size,
        "volume_min": info.volume_min,
        "volume_max": info.volume_max,
        "volume_step": info.volume_step,
        "tick_size": info.trade_tick_size,
        "tick_value": info.trade_tick_value,
        "currency_base": info.currency_base,
        "currency_profit": info.currency_profit,
        "trade_mode": info.trade_mode,
    }
