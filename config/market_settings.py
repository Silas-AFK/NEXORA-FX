
import json
from pathlib import Path


# Settings file location
SETTINGS_FILE = (
    Path(__file__).resolve().parent
    / "market_settings.json"
)


# Default currency pairs
DEFAULT_SYMBOLS = [
    "EURUSD",
    "GBPUSD",
    "USDJPY",
]


# Available pairs for selection
AVAILABLE_SYMBOLS = [
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
    "USDCAD",
    "USDCHF",
    "NZDUSD",
    "EURJPY",
    "GBPJPY",
    "AUDJPY",
    "EURGBP",
    "XAUUSD",
]


def load_symbols():
    """Load saved currency pair selections."""

    if not SETTINGS_FILE.exists():
        return DEFAULT_SYMBOLS.copy()

    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as file:
            settings = json.load(file)

        symbols = settings.get("symbols", DEFAULT_SYMBOLS)

        valid_symbols = [
            symbol
            for symbol in symbols
            if symbol in AVAILABLE_SYMBOLS
        ]

        return valid_symbols or DEFAULT_SYMBOLS.copy()

    except (json.JSONDecodeError, OSError):
        return DEFAULT_SYMBOLS.copy()


def save_symbols(symbols):
    """Save selected currency pairs."""

    valid_symbols = [
        symbol
        for symbol in symbols
        if symbol in AVAILABLE_SYMBOLS
    ]

    settings = {
        "symbols": valid_symbols
    }

    SETTINGS_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with open(SETTINGS_FILE, "w", encoding="utf-8") as file:
        json.dump(settings, file, indent=4)

    return True
