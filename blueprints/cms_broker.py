# blueprints/cms_broker.py
"""Server-to-server broker login API for the WordPress CMS (katx-core's
Katx_Algoz_Session).

The CMS hosts the whole broker login experience for its users, who never
see this server. For brokers whose login is the broker's own hosted page
(OAuth / redirect login), the flow is:

1. The CMS asks ``POST /api/cms/broker/login-url`` for the broker's login
   URL, passing the CMS's own callback URL as ``redirect_uri``.
2. The user's browser goes straight from the CMS to the broker and back to
   the CMS callback; it never visits this server.
3. The CMS forwards the callback's query parameters to
   ``POST /api/cms/broker/complete``, which runs the same per-broker token
   exchange as blueprints/brlogin.py:broker_callback and stores the token on
   the account (utils.auth_utils.persist_broker_auth).
4. ``POST /api/cms/broker/status`` reports whether the account has a stored
   broker session.

Every request is authenticated with an HMAC-SHA256 signature over a
timestamp, a single-use nonce, the method, the path and the raw body, using
CMS_SSO_SHARED_SECRET (the secret the CMS SSO bridge in blueprints/auth.py
already shares). Blank disables the endpoints. The signed body names the
platform username and the account_id; the account must belong to that user.

The broker login URL formats mirror the */initiate-oauth routes in
blueprints/brlogin.py. Keep the two in step when a broker changes its URL.
"""

import hashlib
import hmac
import os
import time
from urllib.parse import quote, urlparse

from cachetools import TTLCache
from flask import Blueprint, current_app, jsonify, request

from database.auth_db import Auth, list_broker_accounts, log_login_attempt
from limiter import limiter
from utils.auth_utils import persist_broker_auth
from utils.config import get_broker_api_key
from utils.ip_helper import get_real_ip
from utils.logging import get_logger

logger = get_logger(__name__)

CMS_SHARED_SECRET = os.getenv("CMS_SSO_SHARED_SECRET", "")

# Optional allowlist of hosts the CMS may use as the broker redirect_uri
# (comma separated, e.g. "signalz.in,www.signalz.in"). Blank allows any
# https host; the signature already proves the request came from the CMS.
CMS_BROKER_REDIRECT_HOSTS = {
    h.strip().lower() for h in os.getenv("CMS_BROKER_REDIRECT_HOSTS", "").split(",") if h.strip()
}

# Every CMS user's requests arrive from the CMS server's one IP, so this is
# a per-CMS ceiling, not a per-user one.
CMS_BROKER_RATE_LIMIT = os.getenv("CMS_BROKER_RATE_LIMIT", "120 per minute")

# A signed request is accepted for this many seconds either side of its
# timestamp; nonces are remembered for twice that, so none can be replayed.
_MAX_CLOCK_SKEW_SECONDS = 60
_used_nonces = TTLCache(maxsize=8192, ttl=_MAX_CLOCK_SKEW_SECONDS * 2)

cms_broker_bp = Blueprint("cms_broker_bp", __name__, url_prefix="/api/cms/broker")


def compute_signature(secret, timestamp, nonce, method, path, body):
    """HMAC-SHA256 (hex) the CMS signs each request with.

    The signed message is ``timestamp \\n nonce \\n METHOD \\n path \\n``
    followed by the raw request body bytes.
    """
    message = f"{timestamp}\n{nonce}\n{method.upper()}\n{path}\n".encode() + (body or b"")
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_signed_request(secret, timestamp, nonce, signature, method, path, body, now=None):
    """Check a CMS request signature, freshness and nonce.

    Returns an error string, or None when the request is valid. The nonce is
    only consumed once everything else has passed.
    """
    if not secret:
        return "CMS broker login is not configured on this server."
    if not timestamp or not nonce or not signature:
        return "Missing signature headers."
    try:
        ts = int(timestamp)
    except ValueError:
        return "Malformed timestamp."
    now = time.time() if now is None else now
    if abs(now - ts) > _MAX_CLOCK_SKEW_SECONDS:
        return "Request timestamp is outside the allowed window."
    if len(nonce) < 16 or len(nonce) > 128:
        return "Malformed nonce."

    expected = compute_signature(secret, timestamp, nonce, method, path, body)
    if not hmac.compare_digest(expected, signature):
        return "Invalid signature."

    if nonce in _used_nonces:
        return "Replayed request."
    _used_nonces[nonce] = True
    return None


def _authenticate():
    """Verify the current request; returns (data, None) or (None, error response)."""
    error = verify_signed_request(
        CMS_SHARED_SECRET,
        request.headers.get("X-Cms-Timestamp", ""),
        request.headers.get("X-Cms-Nonce", ""),
        request.headers.get("X-Cms-Signature", ""),
        request.method,
        request.path,
        request.get_data(cache=True),
    )
    if error:
        logger.warning(f"[CMS-BROKER] Rejected request to {request.path} from {get_real_ip()}: {error}")
        status = 503 if not CMS_SHARED_SECRET else 401
        return None, (jsonify({"status": "error", "message": error}), status)

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, (jsonify({"status": "error", "message": "JSON body required."}), 400)
    return data, None


def _load_account(data):
    """The Auth row named by the request, checked against its owner and
    broker; returns (account, None) or (None, error response)."""
    username = (data.get("username") or "").strip()
    account_id = (data.get("account_id") or "").strip()
    broker = (data.get("broker") or "").strip().lower()
    if not username or not account_id or not broker:
        return None, (
            jsonify({"status": "error", "message": "username, account_id and broker are required."}),
            400,
        )

    account = Auth.query.filter_by(name=account_id).first()
    if account is None or account.owner_username != username:
        return None, (jsonify({"status": "error", "message": "Account not found."}), 404)
    if (account.broker or "").lower() != broker:
        return None, (
            jsonify({"status": "error", "message": f"Account {account_id} is not a {broker} account."}),
            400,
        )
    return account, None


def _validate_redirect_uri(redirect_uri):
    """Error string, or None when the CMS callback URL is acceptable."""
    parsed = urlparse(redirect_uri or "")
    host = (parsed.hostname or "").lower()
    if not host:
        return "redirect_uri is required."
    is_local = host in ("localhost", "127.0.0.1")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and is_local):
        return "redirect_uri must use https."
    if CMS_BROKER_REDIRECT_HOSTS and host not in CMS_BROKER_REDIRECT_HOSTS:
        return f"redirect_uri host {host} is not allowed."
    return None


def _split_key(api_key, index):
    """Part ``index`` of a ':::'-packed BROKER_API_KEY, else the whole key."""
    parts = (api_key or "").split(":::")
    if len(parts) > index and parts[index]:
        return parts[index]
    return parts[0]


# Each builder returns (login_url, error). ``redirect_uri`` is only sent for
# brokers whose authorize URL takes one; the others use the redirect URL
# registered in the broker app's own developer console. ``state`` is sent
# where the broker echoes it back; the CMS also checks its own record of the
# pending login, so brokers that drop it are still safe.


def _zerodha_url(account_id, api_key, redirect_uri, state):
    return (
        f"https://kite.trade/connect/login?v=3&api_key={api_key}"
        f"&redirect_params={quote('state=' + state, safe='')}",
        None,
    )


def _upstox_url(account_id, api_key, redirect_uri, state):
    # redirect_uri unencoded: Upstox compares it literally against the
    # registered value (see blueprints/brlogin.py:upstox_initiate_oauth).
    return (
        "https://api.upstox.com/v2/login/authorization/dialog"
        f"?response_type=code&client_id={api_key}&redirect_uri={redirect_uri}"
        f"&state={quote(state, safe='')}",
        None,
    )


def _fyers_url(account_id, api_key, redirect_uri, state):
    return (
        "https://api-t1.fyers.in/api/v3/generate-authcode"
        f"?client_id={api_key}&redirect_uri={redirect_uri}"
        f"&response_type=code&state={quote(state, safe='')}",
        None,
    )


def _paytm_url(account_id, api_key, redirect_uri, state):
    return (
        f"https://login.paytmmoney.com/merchant-login?apiKey={api_key}&state={quote(state, safe='')}",
        None,
    )


def _pocketful_url(account_id, api_key, redirect_uri, state):
    # Encoded: Pocketful's OAuth server (Ory Hydra) decodes the parameter and
    # compares it exactly with the registered URL. Unencoded, a CMS callback
    # with its own query string was cut at its first "&", so the value Hydra
    # received matched no registered URL.
    return (
        "https://trade.pocketful.in/oauth2/auth"
        f"?client_id={api_key}&redirect_uri={quote(redirect_uri, safe='')}&response_type=code"
        f"&scope={quote('orders holdings', safe='')}&state={quote(state, safe='')}",
        None,
    )


def _dhan_url(account_id, api_key, redirect_uri, state):
    if ":::" not in (api_key or ""):
        return None, "Dhan Broker App API Key must be in the format client_id:::api_key."
    from broker.dhan.api.auth_api import generate_consent, get_login_url

    consent_app_id, error = generate_consent(_split_key(api_key, 0), account_id=account_id)
    if not consent_app_id:
        return None, error or "Dhan did not return a consent id. Check the Dhan API credentials."
    login_url = get_login_url(consent_app_id)
    if not login_url:
        return None, "Could not build the Dhan login URL."
    return login_url, None


def _noren_url(base_url):
    # Shoonya, Zebu and TradeSmart: BROKER_API_KEY is userid:::client_id.
    def build(account_id, api_key, redirect_uri, state):
        return f"{base_url}?client_id={_split_key(api_key, 1)}", None

    return build


LOGIN_URL_BUILDERS = {
    "zerodha": _zerodha_url,
    "upstox": _upstox_url,
    "fyers": _fyers_url,
    "paytm": _paytm_url,
    "pocketful": _pocketful_url,
    "dhan": _dhan_url,
    "shoonya": _noren_url("https://api.shoonya.com/OAuthlogin/authorize/oauth"),
    "zebu": _noren_url("https://go.mynt.in/OAuthlogin/authorize/oauth"),
    "tradesmart": _noren_url("https://v2api.tradesmartonline.in/OAuthlogin/authorize/oauth"),
}


def _first(params, *names):
    for name in names:
        value = params.get(name)
        if value:
            return str(value)
    return None


def exchange_callback(broker, account_id, params, redirect_uri):
    """Run ``broker``'s token exchange for the broker callback ``params``.

    Mirrors the per-broker branches of blueprints/brlogin.py:broker_callback.

    Returns:
        (auth_token, feed_token, user_id, error_message)
    """
    if params.get("error"):
        description = params.get("error_description") or ""
        return None, None, None, f"{broker} login was not completed: {params['error']} {description}".strip()

    auth_function = current_app.broker_auth_functions.get(f"{broker}_auth")
    if not auth_function:
        return None, None, None, f"{broker} is not enabled on this server."

    feed_token = None
    user_id = None

    if broker == "zerodha":
        code = _first(params, "request_token")
        if not code:
            return None, None, None, "Zerodha did not return a request_token."
        auth_token, error = auth_function(code, account_id=account_id)
        if auth_token:
            auth_token = f"{get_broker_api_key(account_id, broker=broker)}:{auth_token}"
    elif broker == "upstox":
        code = _first(params, "code")
        if not code:
            return None, None, None, "Upstox did not return an authorization code."
        auth_token, error = auth_function(code, account_id=account_id, redirect_uri=redirect_uri)
    elif broker == "fyers":
        code = _first(params, "auth_code")
        if not code:
            return None, None, None, "Fyers did not return an auth_code."
        auth_token, error = auth_function(code, account_id=account_id)
    elif broker == "paytm":
        code = _first(params, "requestToken", "request_token")
        if not code:
            return None, None, None, "Paytm Money did not return a requestToken."
        auth_token, feed_token, error = auth_function(code, account_id=account_id)
    elif broker == "pocketful":
        code = _first(params, "code")
        if not code:
            return None, None, None, "Pocketful did not return an authorization code."
        auth_token, feed_token, user_id, error = auth_function(
            code, params.get("state"), account_id=account_id, redirect_uri=redirect_uri
        )
    elif broker == "dhan":
        token_id = _first(params, "tokenId", "token_id", "token")
        if not token_id:
            return None, None, None, "Dhan did not return a tokenId."
        result = auth_function(token_id, account_id=account_id)
        if len(result) == 3:
            auth_token, user_id, error = result
        else:
            auth_token, error = result
        if auth_token:
            from broker.dhan.api.funds import test_auth_token

            is_valid, validation_error = test_auth_token(auth_token)
            if not is_valid:
                return None, None, None, f"Dhan login could not be verified: {validation_error}"
    elif broker in ("shoonya", "zebu", "tradesmart"):
        code = _first(params, "code", "request_token", "request-token")
        if not code:
            return None, None, None, f"{broker} did not return an authorization code."
        auth_token, error = auth_function(code, account_id=account_id)
    else:
        return None, None, None, f"{broker} login is not supported through the CMS."

    if not auth_token:
        return None, None, None, error or f"{broker} login failed."
    return auth_token, feed_token, user_id, None


def _audit(username, status, broker, failure_reason=None):
    log_login_attempt(
        username,
        get_real_ip(),
        request.headers.get("User-Agent", ""),
        status=status,
        login_type="cms_broker",
        broker=broker,
        failure_reason=failure_reason[:255] if failure_reason else None,
    )


@cms_broker_bp.route("/login-url", methods=["POST"])
@limiter.limit(CMS_BROKER_RATE_LIMIT)
def login_url():
    """Body: username, account_id, broker, redirect_uri, state.
    Returns {"status": "success", "data": {"login_url": ...}}."""
    data, error_response = _authenticate()
    if error_response:
        return error_response
    account, error_response = _load_account(data)
    if error_response:
        return error_response

    broker = account.broker.lower()
    builder = LOGIN_URL_BUILDERS.get(broker)
    if not builder:
        return jsonify(
            {"status": "error", "message": f"{broker} does not use a broker-hosted login page."}
        ), 400

    redirect_uri = (data.get("redirect_uri") or "").strip()
    redirect_error = _validate_redirect_uri(redirect_uri)
    if redirect_error:
        return jsonify({"status": "error", "message": redirect_error}), 400

    state = (data.get("state") or "").strip()
    if not state:
        return jsonify({"status": "error", "message": "state is required."}), 400

    api_key = get_broker_api_key(account.name, broker=broker)
    if not api_key:
        return jsonify(
            {"status": "error", "message": f"No broker app API key is saved for this {broker} account."}
        ), 400

    try:
        url, error = builder(account.name, api_key, redirect_uri, state)
    except Exception:
        logger.exception(f"[CMS-BROKER] Building the {broker} login URL failed for {account.name}")
        return jsonify({"status": "error", "message": f"Could not start the {broker} login."}), 500
    if not url:
        return jsonify({"status": "error", "message": error}), 400

    # The redirect_uri is logged so it can be compared with the one registered
    # on the broker's app, which a mismatch error never shows.
    logger.info(
        f"[CMS-BROKER] Issued {broker} login URL for account {account.name}, "
        f"redirect_uri={redirect_uri}"
    )
    return jsonify({"status": "success", "data": {"login_url": url}})


@cms_broker_bp.route("/complete", methods=["POST"])
@limiter.limit(CMS_BROKER_RATE_LIMIT)
def complete():
    """Body: username, account_id, broker, redirect_uri, params (the broker
    callback's query/form parameters). Exchanges them for a broker token and
    stores it on the account."""
    data, error_response = _authenticate()
    if error_response:
        return error_response
    account, error_response = _load_account(data)
    if error_response:
        return error_response

    username = account.owner_username
    broker = account.broker.lower()
    params = data.get("params")
    if not isinstance(params, dict):
        return jsonify({"status": "error", "message": "params must be an object."}), 400
    params = {str(k): v for k, v in params.items() if isinstance(v, (str, int))}

    redirect_uri = (data.get("redirect_uri") or "").strip() or None
    if redirect_uri:
        redirect_error = _validate_redirect_uri(redirect_uri)
        if redirect_error:
            return jsonify({"status": "error", "message": redirect_error}), 400

    try:
        auth_token, feed_token, user_id, error = exchange_callback(
            broker, account.name, params, redirect_uri
        )
    except Exception:
        logger.exception(f"[CMS-BROKER] {broker} token exchange raised for account {account.name}")
        auth_token, error = None, f"{broker} login failed unexpectedly."

    if not auth_token:
        logger.warning(f"[CMS-BROKER] {broker} login failed for account {account.name}: {error}")
        _audit(username, "failed", broker, error)
        return jsonify({"status": "error", "message": error}), 400

    inserted_id = persist_broker_auth(
        auth_token, account.name, broker, feed_token=feed_token, user_id=user_id,
        owner_username=username,
    )
    if not inserted_id:
        _audit(username, "failed", broker, "token_store_failed")
        return jsonify({"status": "error", "message": "Could not store the broker session."}), 500

    _audit(username, "success", broker)
    logger.info(f"[CMS-BROKER] {broker} connected for account {account.name}")
    return jsonify({"status": "success", "data": {"account_id": account.name, "connected": True}})


@cms_broker_bp.route("/status", methods=["POST"])
@limiter.limit(CMS_BROKER_RATE_LIMIT)
def status():
    """Body: username, account_id, broker. Whether the account has a stored,
    unrevoked broker session (it may still have expired at the broker's
    daily rollover; the CMS tracks that from its own connect time)."""
    data, error_response = _authenticate()
    if error_response:
        return error_response
    account, error_response = _load_account(data)
    if error_response:
        return error_response

    for entry in list_broker_accounts(account.owner_username):
        if entry["account_id"] == account.name:
            return jsonify(
                {
                    "status": "success",
                    "data": {
                        "account_id": account.name,
                        "broker": entry["broker"],
                        "connected": bool(entry["connected"]),
                        "has_api_key": bool(entry["has_api_key"]),
                    },
                }
            )
    return jsonify({"status": "error", "message": "Account not found."}), 404
