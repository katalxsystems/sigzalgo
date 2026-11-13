"""Sahi login: exchange the API key and secret for the day's access token.

Sahi has no redirect. The trader creates an API key in the Sahi web app (key
id ``pk_...``, secret ``sahi_sk_...``), approves it once per trading day in the
API Dashboard, and the token endpoint on the auth host mints a Bearer token
valid until the next 06:00 IST boundary (broker-api-docs/sahi-api-docs,
Authentication).

    POST https://apis.sahi.com/api/v1/apikeys/token
    {"key_id": "pk_...", "secret": "sahi_sk_...", "totp": "123456"}
    -> {"access_token": "...", "token_type": "Bearer", "expires_in": 65275,
        "expires_at": "...T06:00:00+05:30", "scope": "orders.write ..."}

A key created with two-factor protection is refused without ``totp``
("This API key needs a six-digit totp code"). For such a key the account's
API secret is stored as ``sahi_sk_...:::<base32 TOTP seed>`` and the code is
generated here at login, so no one has to type it.

The account's broker API key holds the key id and its API secret the secret.
"""

import pyotp

from broker.sahi.api.baseurl import AUTH_BASE_URL, USER_AGENT
from broker.sahi.api.rate_limiter import sahi_request
from utils.broker_backpressure import BROKER_BUSY_MESSAGE, BrokerBusyError
from utils.config import get_broker_api_key, get_broker_api_secret
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)

TOKEN_PATH = "/api/v1/apikeys/token"
TOTP_SEPARATOR = ":::"


def normalize_totp_seed(seed):
    """A base32 TOTP seed with the spaces and lower case authenticator apps show.

    Returns the cleaned seed, or None when it is not valid base32.
    """
    cleaned = "".join((seed or "").split()).upper().rstrip("=")
    if not cleaned:
        return None
    try:
        pyotp.TOTP(cleaned).now()
    except Exception:
        return None
    return cleaned


def split_secret(stored_secret):
    """(api_secret, totp_seed or None) from a stored ``secret[:::seed]`` value."""
    secret, _, seed = (stored_secret or "").partition(TOTP_SEPARATOR)
    return secret.strip(), (seed.strip() or None)


def _login_error(status_code, body, sent_totp=False):
    """A login failure in words a trader can act on.

    The auth host answers with {"code", "message"} where code is PascalCase:
    InvalidRequest (400), Unauthorized or Locked (401), Forbidden (403, key not
    approved today) and TooManyRequests (429).
    """
    code = body.get("code") if isinstance(body, dict) else None
    reason = body.get("message") if isinstance(body, dict) else None

    if isinstance(reason, str) and "totp" in reason.lower():
        if sent_totp:
            return (
                "Sahi did not accept the authenticator code. Check that the TOTP secret saved "
                "on this broker account belongs to this API key, and that the server clock is "
                "correct, then log in again."
            )
        return (
            "This Sahi API key is protected by an authenticator code, and no TOTP secret is "
            "saved on this broker account. Add the key's TOTP secret (the base32 text shown "
            "when you set up the authenticator), then log in again."
        )
    if status_code == 403 or code == "Forbidden":
        return (
            "Sahi did not issue an access token because the API key is not approved for today. "
            "Open Sahi web, go to Profile, API Dashboard, and click Approve on the key, then log in again."
        )
    if code == "Locked":
        return (
            "Sahi reports that your account is frozen or locked. Contact Sahi support to unlock it."
        )
    if status_code == 401:
        return (
            "Sahi did not accept the API key and secret. Check BROKER_API_KEY (the key id starting "
            "with pk_) and BROKER_API_SECRET (the secret starting with sahi_sk_), and that the key "
            "has not been deleted in the Sahi API Dashboard."
        )
    if status_code == 429:
        return "Sahi is limiting login attempts. Wait a minute, then log in again."
    if status_code is not None and status_code >= 500:
        return "Sahi's login service is not responding right now. Try again in a few minutes."
    message = "Sahi did not issue an access token"
    if reason:
        message += f": {reason}"
    return message + ". Check the API key and secret in your OpenAlgo settings."


def authenticate_broker(code=None, account_id=None, totp=None):
    """Mint the day's Sahi access token from the API key and secret.

    Args:
        code: Unused. The login route passes a placeholder.
        account_id: The broker account logging in. Its own stored API key and
            secret are used (see utils.config.get_broker_api_key).
        totp: Optional, from the login request: a six-digit code used as is,
            or a base32 seed. Takes precedence over the account's saved seed.

    Returns:
        tuple: (access_token, None) on success, (None, error_message) on failure.
    """
    key_id = (get_broker_api_key(account_id, broker="sahi") or "").strip()
    secret, seed = split_secret(get_broker_api_secret(account_id, broker="sahi"))
    if not key_id or not secret:
        return None, (
            "Sahi needs BROKER_API_KEY (the API key id, pk_...) and BROKER_API_SECRET "
            "(the API secret, sahi_sk_...). Add both in your OpenAlgo settings."
        )

    payload = {"key_id": key_id, "secret": secret}
    totp = (totp or "").strip()
    if totp.isdigit() and len(totp) == 6:
        payload["totp"] = totp
        seed = None
        logger.info(f"Sahi login for account {account_id}: using the code sent with the login")
    elif totp:
        seed = totp
    if seed:
        cleaned = normalize_totp_seed(seed)
        if not cleaned:
            return None, (
                "The TOTP secret saved for this Sahi account is not valid. Enter the base32 "
                "secret shown when you set up the authenticator, not a six-digit code."
            )
        payload["totp"] = pyotp.TOTP(cleaned).now()
        # TEMPORARY: diagnosing a refused Sahi TOTP login. Remove once fixed;
        # generated codes must not be logged in normal operation.
        logger.info(f"TEMPORARY Sahi TOTP debug for account {account_id}: code={payload['totp']}")
    if "totp" not in payload:
        logger.info(
            f"Sahi login for account {account_id}: no TOTP secret saved and none sent "
            f"with the login, sending key and secret only"
        )
    elif seed:
        logger.info(f"Sahi login for account {account_id}: sending an authenticator code")

    client = get_httpx_client()
    try:
        response = sahi_request(
            client,
            "POST",
            f"{AUTH_BASE_URL}{TOKEN_PATH}",
            "auth",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            timeout=30,
        )
    except BrokerBusyError as e:
        logger.warning(f"Sahi login not sent, rate limit queue full: {e}")
        return None, str(e) or BROKER_BUSY_MESSAGE
    except Exception:
        logger.exception("Sahi login request failed")
        return None, "Could not reach Sahi to log in. Check your internet connection and try again."

    try:
        body = response.json()
    except ValueError:
        body = {}

    if response.status_code == 200 and isinstance(body, dict) and body.get("access_token"):
        logger.info(
            f"Sahi access token issued, expires at {body.get('expires_at')}, scopes: {body.get('scope')}"
        )
        return body["access_token"], None

    logger.error(
        f"Sahi login refused: HTTP {response.status_code}, code={body.get('code') if isinstance(body, dict) else None}, "
        f"message={body.get('message') if isinstance(body, dict) else None}"
    )
    return None, _login_error(response.status_code, body, sent_totp="totp" in payload)
