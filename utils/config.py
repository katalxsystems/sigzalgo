# utils/config.py

import os

from dotenv import load_dotenv

# Load environment variables from .env file with override=True to ensure values are updated
load_dotenv(override=True)

# Brokers whose BROKER_API_KEY packs more than one credential into a single
# ":::"-delimited string, and how many "::: " separators (== parts - 1) that
# requires. Shared by both credential-save routes (the instance-wide
# blueprints/broker_credentials.py and the per-account
# blueprints/broker_accounts.py) so a malformed value is rejected at save
# time with a clear message, instead of surfacing as a cryptic unpack error
# deep in a broker plugin the next time it's actually used.
COMPOUND_BROKER_API_KEY_FORMATS = {
    "fivepaisa": (2, "'User_Key:::User_ID:::client_id'"),
    "flattrade": (1, "'client_id:::api_key'"),
    "dhan": (1, "'client_id:::api_key'"),
}


def validate_broker_api_key_format(broker: str, broker_api_key: str) -> str | None:
    """Check a broker_api_key against its broker's required ":::"-delimited
    shape, if it has one. Returns an error message to show the caller, or
    None when the value is fine (including brokers with no compound
    requirement, where any non-empty value passes).
    """
    spec = COMPOUND_BROKER_API_KEY_FORMATS.get((broker or "").strip().lower())
    if not spec:
        return None
    required_separators, example = spec
    if broker_api_key.count(":::") != required_separators:
        broker_label = broker.strip().capitalize()
        return f"{broker_label} API key must be in format: {example}"
    return None


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


def get_broker_api_key_market(account_id: str | None = None) -> str | None:
    """Retrieve the market-data-specific broker API key (used by the XTS-
    family brokers' separate market-feed login: compositedge, rmoney,
    fivepaisaxts, ibulls, iifl, jainamxts, wisdom).

    Per-account first (database.auth_db.Auth.broker_api_key_market), falling
    back to the instance-wide default during the per-account migration --
    see get_broker_api_key() for the same resolution shape. The
    instance-wide fallback here is transitional: it goes away once every
    XTS-family broker's get_feed_token() threads account_id and the
    one-time backfill migration has populated broker_api_key_market for
    every existing account (see the SaaS per-account credentials plan).
    """
    if account_id:
        try:
            from database.auth_db import get_broker_credentials_market

            api_key, _ = get_broker_credentials_market(account_id)
            if api_key:
                return api_key
        except Exception:
            from utils.logging import get_logger

            get_logger(__name__).exception(
                f"Error reading DB-stored broker_api_key_market for account {account_id}"
            )
    return _instance_broker_setting("broker_api_key_market") or os.getenv("BROKER_API_KEY_MARKET")


def get_broker_api_secret_market(account_id: str | None = None) -> str | None:
    """Retrieve the market-data-specific broker API secret. See
    get_broker_api_key_market() for the resolution order."""
    if account_id:
        try:
            from database.auth_db import get_broker_credentials_market

            _, api_secret = get_broker_credentials_market(account_id)
            if api_secret:
                return api_secret
        except Exception:
            from utils.logging import get_logger

            get_logger(__name__).exception(
                f"Error reading DB-stored broker_api_secret_market for account {account_id}"
            )
    return _instance_broker_setting("broker_api_secret_market") or os.getenv(
        "BROKER_API_SECRET_MARKET"
    )


def resolve_broker_api_key(
    auth_token: str | None, broker: str, account_id: str | None = None
) -> tuple[str | None, str | None]:
    """Resolve (api_key, api_secret) for a broker plugin's per-request calls
    (order placement, funds, quotes) that must resend the app-level
    BROKER_API_KEY/SECRET on every call, not just at login.

    Reverse-looks-up account_id from auth_token when not already known
    (database.auth_db.get_account_id_from_auth_token), then resolves through
    get_broker_api_key/get_broker_api_secret -- the same per-account-first
    resolution login already uses. This is the shared helper broker plugins
    should call instead of reading os.getenv("BROKER_API_KEY") directly: see
    broker/fivepaisa/api/order_api.py's _get_5paisa_credentials() for the
    original hand-written version of this pattern, before it was
    generalized here.
    """
    if account_id is None and auth_token:
        from database.auth_db import get_account_id_from_auth_token

        account_id = get_account_id_from_auth_token(auth_token, broker=broker)
    return get_broker_api_key(account_id), get_broker_api_secret(account_id)


def resolve_broker_api_key_market(
    auth_token: str | None, broker: str, account_id: str | None = None
) -> tuple[str | None, str | None]:
    """Market-data-feed counterpart of resolve_broker_api_key() -- for
    XTS-family brokers' get_feed_token() (compositedge, rmoney, fivepaisaxts,
    ibulls, iifl, jainamxts, wisdom), which needs its own app key/secret
    pair, separate from the trading credential. Same reverse-lookup-from-
    auth_token mechanism; resolves through get_broker_api_key_market/
    get_broker_api_secret_market instead.
    """
    if account_id is None and auth_token:
        from database.auth_db import get_account_id_from_auth_token

        account_id = get_account_id_from_auth_token(auth_token, broker=broker)
    return get_broker_api_key_market(account_id), get_broker_api_secret_market(account_id)


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
