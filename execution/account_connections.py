import re


ACCOUNT_TYPES = ("DEMO", "LIVE")


def normalize_account_profile(login, broker_server, account_type):
    if isinstance(login, bool):
        raise ValueError("MT5 account login must contain only digits.")
    if isinstance(login, int):
        account_login = login
    elif isinstance(login, str) and re.fullmatch(r"[0-9]+", login.strip()):
        account_login = int(login.strip())
    else:
        raise ValueError("MT5 account login must contain only digits.")
    if account_login <= 0 or account_login > 9_223_372_036_854_775_807:
        raise ValueError("MT5 account login is outside the supported range.")

    if not isinstance(broker_server, str):
        raise ValueError("Broker server is required.")
    server = broker_server.strip()
    if (
        not server
        or len(server) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in server)
    ):
        raise ValueError("Broker server must be a non-empty single-line value.")

    if not isinstance(account_type, str):
        raise ValueError("Account type must be DEMO or LIVE.")
    normalized_type = account_type.strip().upper()
    if normalized_type not in ACCOUNT_TYPES:
        raise ValueError("Account type must be DEMO or LIVE.")

    return {
        "login": account_login,
        "server": server,
        "account_type": normalized_type,
    }


def account_type_from_trade_mode(api, trade_mode):
    if trade_mode == getattr(api, "ACCOUNT_TRADE_MODE_DEMO", object()):
        return "DEMO"
    if trade_mode == getattr(api, "ACCOUNT_TRADE_MODE_REAL", object()):
        return "LIVE"
    return None


def mask_account_login(login):
    digits = str(login)
    return f"****{digits[-4:]}" if len(digits) > 4 else "****"
