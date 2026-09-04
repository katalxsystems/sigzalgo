# utils/config.py

import os

from dotenv import load_dotenv

# Load environment variables from .env file with override=True to ensure values are updated
load_dotenv(override=True)


def get_broker_api_key(account_id: str | None = None) -> str | None:
    """
    Retrieve the broker API key: DB-first, .env fallback.

    Multi-account support stores a credential pair per connected broker
    account (database.auth_db.Auth.broker_api_key/broker_api_secret) so two
    accounts on the same broker can use different app registrations. When
    ``account_id`` is given and that account has a DB-stored key, it wins.
    Otherwise this falls back to the instance-wide default, itself DB-first:
    database.settings_db's Settings row (set from Profile > Broker, takes
    effect immediately) and only then the legacy ``.env`` value, which keeps
    non-interactive scripts and not-yet-migrated installs unchanged.

    Returns:
        str | None: The broker API key, or None if not set anywhere.
    """
    if account_id:
        try:
            from database.auth_db import get_broker_credentials

            api_key, _ = get_broker_credentials(account_id)
            if api_key:
                return api_key
        except Exception:
            from utils.logging import get_logger

            get_logger(__name__).exception(
                f"Error reading DB-stored broker_api_key for account {account_id}"
            )
    return _instance_broker_setting("broker_api_key") or os.getenv("BROKER_API_KEY")


def get_broker_api_secret(account_id: str | None = None) -> str | None:
    """
    Retrieve the broker API secret: DB-first, .env fallback.

    See get_broker_api_key() for the resolution order.

    Returns:
        str | None: The broker API secret, or None if not set anywhere.
    """
    if account_id:
        try:
            from database.auth_db import get_broker_credentials

            _, api_secret = get_broker_credentials(account_id)
            if api_secret:
                return api_secret
        except Exception:
            from utils.logging import get_logger

            get_logger(__name__).exception(
                f"Error reading DB-stored broker_api_secret for account {account_id}"
            )
    return _instance_broker_setting("broker_api_secret") or os.getenv("BROKER_API_SECRET")


def _instance_broker_setting(field: str) -> str | None:
    """Read one field from the instance-wide broker Settings row (DB-backed,
    Profile > Broker) — used as the layer between per-account overrides and
    the legacy .env fallback. Returns None (not raises) on any DB error so
    callers can fall through to .env unconditionally."""
    try:
        from database.settings_db import get_broker_settings

        settings = get_broker_settings()
        return settings.get(field) if settings else None
    except Exception:
        from utils.logging import get_logger

        get_logger(__name__).exception(f"Error reading instance broker setting {field!r}")
        return None


def get_broker_api_key_market() -> str | None:
    """Retrieve the market-data-specific broker API key (used by the XTS-
    family brokers' separate market-feed login): DB-first, .env fallback.

    Unlike get_broker_api_key(), this has no per-account layer — the market
    feed credential is a single instance-wide pair, not something that
    varies per connected trading account.
    """
    return _instance_broker_setting("broker_api_key_market") or os.getenv("BROKER_API_KEY_MARKET")


def get_broker_api_secret_market() -> str | None:
    """Retrieve the market-data-specific broker API secret. See
    get_broker_api_key_market() for the resolution order."""
    return _instance_broker_setting(
        "broker_api_secret_market"
    ) or os.getenv("BROKER_API_SECRET_MARKET")


def get_broker_redirect_url() -> str | None:
    """Retrieve the instance's OAuth redirect URL: DB-first, .env fallback."""
    return _instance_broker_setting("redirect_url") or os.getenv("REDIRECT_URL")


# Brokers whose login is real external OAuth (an authorize page on the
# broker's own domain) rather than an in-app credential form. These get
# redirected through a server-side /<broker>/initiate-oauth endpoint
# (blueprints/brlogin.py) that builds the broker's login URL using the
# resolved per-account API key; every other broker goes straight to its
# /<broker>/callback credential-entry route.
OAUTH_INITIATE_BROKERS = frozenset(
    {
        "arrow",
        "compositedge",
        "dhan",
        "flattrade",
        "fyers",
        "hdfcsky",
        "paytm",
        "pocketful",
        "upstox",
        "zerodha",
    }
)


def get_connect_path(broker: str) -> str:
    """Where the browser should be sent to start connecting this broker."""
    return (
        f"/{broker}/initiate-oauth" if broker in OAUTH_INITIATE_BROKERS else f"/{broker}/callback"
    )


def get_login_rate_limit_min() -> str:
    """
    Retrieve the rate limit for logins per minute.

    Returns:
        str: The rate limit string (e.g., '5 per minute').
    """
    return os.getenv("LOGIN_RATE_LIMIT_MIN", "5 per minute")


def get_login_rate_limit_hour() -> str:
    """
    Retrieve the rate limit for logins per hour.

    Returns:
        str: The rate limit string (e.g., '25 per hour').
    """
    return os.getenv("LOGIN_RATE_LIMIT_HOUR", "25 per hour")


def get_host_server() -> str:
    """
    Retrieve the host server URL.

    Returns:
        str: The host server URL string.
    """
    return os.getenv("HOST_SERVER", "http://127.0.0.1:5000")
