import base64
import hashlib
import http.client
import json
import os
import re
import secrets
from urllib.parse import quote

import jwt
from flask import Blueprint, jsonify, make_response, redirect, request, session, url_for
from flask import current_app as app

from limiter import limiter  # Import the limiter instance
from utils.auth_utils import handle_auth_failure, handle_auth_success
from utils.config import (
    build_external_url,
    get_broker_api_key,
    get_broker_api_key_market,
    get_broker_api_secret,
    get_broker_redirect_url,
    get_login_rate_limit_hour,
    get_login_rate_limit_min,
)
from utils.logging import get_logger

# Initialize logger
logger = get_logger(__name__)

LOGIN_RATE_LIMIT_MIN = get_login_rate_limit_min()
LOGIN_RATE_LIMIT_HOUR = get_login_rate_limit_hour()

brlogin_bp = Blueprint("brlogin", __name__, url_prefix="/")


@brlogin_bp.errorhandler(429)
def ratelimit_handler(e):
    return jsonify(error="Rate limit exceeded"), 429


def _sahi_totp_from_request(account_id):
    """A TOTP code or base32 seed sent with a Sahi login, from form, query or JSON.

    A seed is saved on the account (with its API secret) so later logins,
    including the scheduled one, need nothing sent. Returns the value to log
    in with, or None when the request carried none.
    """
    from blueprints.broker_accounts import TOTP_SEED_FIELDS, _pack_totp_seed
    from database.auth_db import get_broker_credentials, set_broker_credentials

    body = request.get_json(silent=True) if request.is_json else None
    sources = [request.form, request.args, body if isinstance(body, dict) else {}]
    value = None
    for source in sources:
        for field in TOTP_SEED_FIELDS:
            candidate = str(source.get(field) or "").strip()
            if candidate:
                value = candidate
                break
        if value:
            break

    if not value:
        # Field names only, never values: shows what a client calls the code.
        names = sorted({*request.form.keys(), *request.args.keys(), *(sources[2].keys())})
        logger.info(f"Sahi login request carried no TOTP. Fields sent: {names}")
        return None

    if value.isdigit() and len(value) == 6:
        return value

    _, stored = get_broker_credentials(account_id, broker="sahi")
    packed, error = _pack_totp_seed("sahi", stored, value, account_id=account_id)
    if error:
        logger.warning(f"Sahi TOTP secret sent for account {account_id} was not saved: {error}")
        return value
    if set_broker_credentials(account_id, None, packed):
        logger.info(f"Saved the Sahi TOTP secret sent with the login for account {account_id}")
    return value


@brlogin_bp.route("/<broker>/callback", methods=["POST", "GET"])
@limiter.limit(LOGIN_RATE_LIMIT_MIN)
@limiter.limit(LOGIN_RATE_LIMIT_HOUR)
def broker_callback(broker, para=None):
    logger.info(f"Broker callback initiated for: {broker}")
    logger.debug("Session keys: %s", sorted(session.keys()))
    logger.info(f"Session has user key: {'user' in session}")

    # Special handling for brokers that come from external auth and might lose session
    if broker in ("compositedge", "rmoney", "iiflcapital") and "user" not in session:
        # Session will be established after successful auth token validation
        logger.info(f"{broker} callback without session - will establish session after auth")
    # Special handling for mstock POST - check session but provide better error instead of redirect
    elif broker == "mstock" and request.method == "POST" and "user" not in session:
        # Redirect to broker selection page with error message instead of login
        return redirect(url_for("auth.broker_login"))
    else:
        # Check if user is not in session first for other brokers
        if "user" not in session:
            logger.warning(f"User not in session for {broker} callback, redirecting to login")
            return redirect(url_for("auth.login"))

    # The broker account_id this login is connecting. "pending_account_id" is
    # set by the account-management UI (POST /api/accounts, then redirect
    # here) when adding a specific account. Without it — a plain password
    # login or the CMS SSO hand-off, neither of which route through that UI
    # — fall back to this platform user's default account for THIS broker
    # (scoped, because their overall default account may be a different
    # broker entirely, whose credentials would be wrong here). Only when
    # the user has no account at all for this broker does this drop back to
    # the platform username, reproducing pre-multi-account behavior (one
    # implicit account per user, account_id == username).
    account_id = session.get("pending_account_id")
    if not account_id:
        from database.auth_db import get_default_account_id_for_broker

        account_id = get_default_account_id_for_broker(session.get("user"), broker)
    if not account_id:
        # Neither an explicit selection nor an existing account for this
        # broker. The bare platform username is the pre-multi-account
        # fallback identifier, but it may already name a REAL Auth row for
        # a DIFFERENT broker -- a legacy single-account connection from
        # before this user adopted multi-account. Reusing that row here
        # would silently authenticate THIS broker with the OTHER broker's
        # stored app-registration credentials (observed: a user's
        # username-named Angel row made a fresh fivepaisa login fail with
        # Angel's BROKER_API_KEY format, since get_broker_api_key looks up
        # by name only and doesn't know which broker it's being asked for).
        # Only take the bare-username fallback when that's actually safe --
        # no such row exists yet, or it does and already belongs to this
        # same broker; otherwise mint a proper multi-account id, the same
        # scheme database.auth_db.create_broker_account uses, so this
        # broker gets its own row instead of colliding with the other one.
        username = session.get("user")
        from database.auth_db import Auth

        existing = Auth.query.filter_by(name=username).first() if username else None
        if not existing or existing.broker == broker:
            account_id = username
        else:
            import secrets as _secrets

            account_id = f"{username}_{broker}_{_secrets.token_hex(4)}"
    logger.info(f"Resolved account_id={account_id!r} for broker={broker}")

    if session.get("logged_in") and not session.get("pending_account_id"):
        # Single-account-era shortcut: already fully logged in and this
        # isn't a multi-account "connect a specific account" flow (no
        # pending_account_id), so there's nothing new to do here.
        #
        # Deliberately does NOT fire when pending_account_id is set: that
        # means the account-management UI is targeting a specific account,
        # which may well be a different one than whatever set logged_in=True
        # (session["logged_in"] is intentionally kept True even after that
        # OTHER account's broker token dies — see revoke_user_tokens /
        # issue #1400 — so it does not mean "no other account needs
        # connecting"). Redirecting to the dashboard here would silently
        # bounce the user away before they ever see this broker's login
        # form, with no error and nothing in the network tab to explain it.
        session["broker"] = broker
        return redirect(url_for("dashboard_bp.dashboard"))

    # Every broker's login needs this account's own app credential (API key,
    # secret, or market-feed key -- which one varies by broker). There is no
    # instance-wide fallback (see utils.config.get_broker_api_key), so fail
    # here with a clear message instead of letting the broker reject the
    # login -- or, like Angel, accept it and then answer "Invalid API Key"
    # on every funds/quotes call afterwards. Skipped for the session-less
    # external-auth callbacks above, which have no account to check yet.
    if "user" in session and not _account_has_broker_credentials(account_id, broker):
        error_message = (
            f"Broker API key not configured for this {broker} account. "
            f"Set it in Profile > Accounts."
        )
        logger.error(f"{error_message} (account_id={account_id!r})")
        return handle_auth_failure(error_message, forward_url="broker.html")

    broker_auth_functions = app.broker_auth_functions
    auth_function = broker_auth_functions.get(f"{broker}_auth")

    if not auth_function:
        return jsonify(error="Broker authentication function not found."), 404

    # Initialize optional outputs used by different broker auth flows
    feed_token = None
    user_id = None

    if broker == "fivepaisa":
        if request.method == "GET":
            # Redirect to React TOTP page
            return redirect("/broker/fivepaisa/totp")

        elif request.method == "POST":
            clientcode = request.form.get("userid") or request.form.get("clientid")
            broker_pin = request.form.get("pin")
            totp_code = request.form.get("totp")

            auth_token, error_message = auth_function(
                clientcode, broker_pin, totp_code, account_id=account_id
            )
            forward_url = "broker.html"

    elif broker == "angel":
        if request.method == "GET":
            # Redirect to React TOTP page
            return redirect("/broker/angel/totp")

        elif request.method == "POST":
            clientcode = request.form.get("userid") or request.form.get("clientid")
            broker_pin = request.form.get("pin")
            totp_code = request.form.get("totp")
            # to store user_id in the DB
            user_id = clientcode
            auth_token, feed_token, error_message = auth_function(
                clientcode, broker_pin, totp_code, account_id=account_id
            )
            forward_url = "broker.html"

    elif broker == "mstock":
        if request.method == "GET":
            # Redirect to React TOTP page
            return redirect("/broker/mstock/totp")

        elif request.method == "POST":
            # Check if user session is lost
            if "user" not in session:
                logger.error(
                    "mstock POST - Session lost; cookie names: %s",
                    sorted(request.cookies.keys()),
                )
                return jsonify(
                    {"status": "error", "message": "Session expired. Please login again."}
                ), 401

            # Import mstock TOTP authentication function
            from broker.mstock.api.auth_api import authenticate_with_totp

            # Get password and TOTP from form
            password = request.form.get("password")
            totp_code = request.form.get("totp")

            if not password:
                return jsonify({"status": "error", "message": "Password is required."}), 400
            if not totp_code:
                return jsonify({"status": "error", "message": "TOTP code is required."}), 400

            # Single-step authentication with password + TOTP
            auth_token, feed_token, error_message = authenticate_with_totp(
                password, totp_code, account_id=account_id
            )

            if error_message:
                return jsonify({"status": "error", "message": error_message}), 401

            # Authentication successful
            logger.info("mStock TOTP authentication successful")
            return handle_auth_success(
                auth_token, account_id, broker, feed_token=feed_token, user_id=None,
                owner_username=session["user"],
            )

    elif broker == "aliceblue":
        # New OAuth redirect flow:
        # 1. GET without authCode → redirect to AliceBlue login page with appcode
        # 2. GET with authCode + userId (callback) → authenticate and get session
        authCode = request.args.get("authCode")
        userId = request.args.get("userId")

        if authCode and userId:
            # Callback from AliceBlue with authorization code
            logger.info(
                "AliceBlue OAuth callback received (authCode present: %s, userId present: %s)",
                bool(authCode),
                bool(userId),
            )
            auth_token, client_id, error_message = auth_function(
                userId, authCode, account_id=account_id
            )
            user_id = client_id or userId  # clientId from API response, fallback to OAuth userId
            feed_token = None  # AliceBlue doesn't use a separate feed token
            forward_url = "broker.html"
        else:
            # Initial visit — redirect to AliceBlue login page
            logger.info("Redirecting to AliceBlue login page")
            appcode = get_broker_api_key(account_id, broker=broker)
            if not appcode:
                return handle_auth_failure(
                    "BROKER_API_KEY (appCode) not configured in environment",
                    forward_url="broker.html",
                )
            aliceblue_login_url = f"https://ant.aliceblueonline.com/?appcode={appcode}"
            return redirect(aliceblue_login_url)

    elif broker == "fivepaisaxts":
        code = "fivepaisaxts"
        logger.debug("FivePaisaXTS broker - authentication initiated")

        # Fetch auth token, feed token and user ID
        auth_token, feed_token, user_id, error_message = auth_function(
            code, account_id=account_id
        )
        forward_url = "broker.html"

    elif broker == "compositedge":
        # For Compositedge, check if we need to handle a special case where session might be lost
        if "user" not in session:
            # Check if this is coming from a valid OAuth callback
            # Log the issue but try to continue if we have valid data
            logger.warning(
                "Session 'user' key missing in Compositedge callback, attempting to recover"
            )

        try:
            # Get the raw data from the request
            if request.method == "POST":
                # Handle form data
                if request.headers.get("Content-Type") == "application/x-www-form-urlencoded":
                    raw_data = request.get_data().decode("utf-8")

                    # Extract session data from form
                    if raw_data.startswith("session="):
                        from urllib.parse import unquote

                        session_data = unquote(raw_data[8:])  # Remove 'session=' and URL decode

                    else:
                        session_data = raw_data
                else:
                    session_data = request.get_data().decode("utf-8")

            else:
                session_data = request.args.get("session")

            if not session_data:
                return jsonify({"error": "No session data received"}), 400

            # Parse the session data
            try:
                # Try to clean the data if it's malformed
                if isinstance(session_data, str):
                    # Remove any leading/trailing whitespace
                    session_data = session_data.strip()

                    session_json = json.loads(session_data)

                    # Handle double-encoded JSON
                    if isinstance(session_json, str):
                        session_json = json.loads(session_json)

                else:
                    session_json = session_data

            except json.JSONDecodeError:
                # This is the broker's session/access-token container. Never
                # reflect it into browser or proxy diagnostics.
                return jsonify({"error": "Invalid session JSON"}), 400

            # Extract access token
            access_token = session_json.get("accessToken")
            # print(f'Access token is {access_token}')

            if not access_token:
                return jsonify({"error": "No access token found"}), 400

            # Fetch auth token, feed token and user ID
            auth_token, feed_token, user_id, error_message = auth_function(
                access_token, account_id=account_id
            )

            # print(f'Auth token is {auth_token}')
            # print(f'Feed token is {feed_token}')
            # print(f'User ID is {user_id}')
            forward_url = "broker.html"

        except Exception as exc:
            # Broker SDK exceptions can quote the entire session payload. Keep
            # the response and application log to non-secret classification.
            logger.error("Could not process Compositedge callback (%s)", type(exc).__name__)
            return jsonify({"error": "Could not process broker callback"}), 500

    elif broker == "fyers":
        code = request.args.get("auth_code")
        logger.debug("Fyers broker - auth_code present: %s", bool(code))
        auth_token, error_message = auth_function(code, account_id=account_id)
        forward_url = "broker.html"

    elif broker == "tradejini":
        if request.method == "GET":
            # Redirect to React TOTP page
            return redirect("/broker/tradejini/totp")

        elif request.method == "POST":
            password = request.form.get("password")
            twofa = request.form.get("twofa")
            twofatype = request.form.get("twofatype")

            # Get auth token using individual token service
            auth_token, error_message = auth_function(
                password=password, twofa=twofa, twofa_type=twofatype, account_id=account_id
            )

            if auth_token:
                return handle_auth_success(
                    auth_token, account_id, broker, owner_username=session["user"]
                )
            else:
                return jsonify({"status": "error", "message": error_message}), 401

        forward_url = "broker.html"

    elif broker == "icici":
        full_url = request.full_path
        from utils.url_redaction import redact_url_credentials

        logger.debug(f"ICICI broker - Full URL: {redact_url_credentials(full_url)}")
        code = request.args.get("apisession")
        logger.debug("ICICI broker - apisession present: %s", bool(code))
        auth_token, error_message = auth_function(code)
        forward_url = "broker.html"

    elif broker == "ibulls":
        code = "ibulls"
        logger.debug("Indiabulls broker - authentication initiated")

        # Fetch auth token, feed token and user ID
        auth_token, feed_token, user_id, error_message = auth_function(
            code, account_id=account_id
        )
        forward_url = "broker.html"

    elif broker == "iifl":
        code = "iifl"
        logger.debug("IIFL broker - authentication initiated")

        # Fetch auth token, feed token and user ID
        auth_token, feed_token, user_id, error_message = auth_function(
            code, account_id=account_id
        )
        forward_url = "broker.html"

    elif broker == "iiflcapital":
        # IIFL Capital uses redirect login and callback params authCode + clientId
        callback_args = request.values.to_dict(flat=True)
        auth_code = (
            callback_args.get("authCode")
            or callback_args.get("authcode")
            or callback_args.get("auth_code")
            or callback_args.get("code")
        )
        client_id = (
            callback_args.get("clientId")
            or callback_args.get("clientid")
            or callback_args.get("client_id")
            or callback_args.get("clientCode")
            or callback_args.get("clientcode")
        )

        # Some callback variants may not include clientId explicitly.
        # Fall back to BROKER_API_KEY to avoid false failures.
        if not client_id:
            broker_api_key = (get_broker_api_key(account_id, broker=broker) or "").strip()
            if ":::" in broker_api_key:
                client_id = broker_api_key.split(":::", 1)[0].strip()
            elif broker_api_key:
                client_id = broker_api_key

        if request.method == "GET":
            # Initial hit from OpenAlgo broker page has no callback parameters.
            if not callback_args:
                referrer = (request.headers.get("Referer") or "").lower()
                if "iiflcapital.com" in referrer:
                    logger.warning(
                        "IIFL Capital callback returned without auth params after broker login. "
                        "This usually indicates redirect URL mismatch/whitelisting issue."
                    )
                    return handle_auth_failure(
                        "IIFL Capital callback was received without auth parameters. "
                        "Please verify the exact callback URL is whitelisted in IIFL "
                        "and matches REDIRECT_URL (including protocol, host, port, and path).",
                        forward_url="broker.html",
                    )

                from broker.iiflcapital.api.auth_api import get_login_url

                login_url = get_login_url(account_id=account_id)
                if not login_url:
                    return handle_auth_failure(
                        "IIFL Capital login URL could not be generated. "
                        "Please verify BROKER_API_KEY and REDIRECT_URL.",
                        forward_url="broker.html",
                    )
                return redirect(login_url)

            # Callback reached OpenAlgo but required params were not provided.
            if not auth_code or not client_id:
                logger.warning(
                    "IIFL Capital callback missing required params. "
                    f"Received keys: {list(callback_args.keys())}"
                )
                return handle_auth_failure(
                    "IIFL Capital callback did not include required auth parameters. "
                    "Please verify callback URL registration and try again.",
                    forward_url="broker.html",
                )

        auth_token, error_message = auth_function(auth_code, client_id, account_id=account_id)
        forward_url = "broker.html"

    elif broker == "jainamxts":
        code = "jainamxts"
        logger.debug("JainamXTS broker - authentication initiated")

        # Fetch auth token, feed token and user ID
        auth_token, feed_token, user_id, error_message = auth_function(
            code, account_id=account_id
        )
        forward_url = "broker.html"

    elif broker == "dhan":
        auth_token = None
        error_message = None
        forward_url = "broker.html"

        if request.method == "GET":
            # Handle OAuth callback with tokenId
            from utils.url_redaction import redact_url_credentials

            logger.info(
                "Dhan callback received with parameters: %s",
                sorted(request.args.keys()),
            )
            logger.info(
                f"Dhan callback - Full URL: {redact_url_credentials(request.url)}"
            )

            # Log if we're coming from a redirect
            referrer = request.headers.get("Referer", "No referrer")
            logger.info(
                "Dhan callback - Referrer: %s",
                redact_url_credentials(referrer),
            )

            # Check for tokenId in various possible parameter names
            token_id = (
                request.args.get("tokenId")
                or request.args.get("token_id")
                or request.args.get("token")
            )

            if token_id:
                # Step 3: Consume consent with tokenId
                logger.debug("Dhan broker - tokenId present: %s", bool(token_id))
                # auth_function now returns (auth_token, user_id, error_message)
                auth_result = auth_function(token_id, account_id=account_id)

                # Handle both old format (2 values) and new format (3 values)
                if len(auth_result) == 3:
                    auth_token, user_id, error_message = auth_result
                else:
                    auth_token, error_message = auth_result
                    user_id = None

                # Validate authentication by testing funds API before proceeding
                if auth_token:
                    # Import the funds function to test authentication
                    from broker.dhan.api.funds import test_auth_token

                    is_valid, validation_error = test_auth_token(auth_token)

                    if not is_valid:
                        logger.error(f"Dhan authentication validation failed: {validation_error}")
                        return handle_auth_failure(
                            f"Authentication validation failed: {validation_error}",
                            forward_url="broker.html",
                        )

                    logger.info("Dhan authentication validation successful")
                    # Set forward_url for successful authentication
                    forward_url = "broker.html"
                    # The auth_token will be handled by the common success flow below
                else:
                    # Authentication failed
                    return handle_auth_failure(
                        error_message or "Authentication failed", forward_url="broker.html"
                    )
            else:
                # First time coming from broker.html - redirect to initiate OAuth
                # This avoids showing the form and directly starts OAuth if we have a stored client ID
                return redirect("/dhan/initiate-oauth")

        elif request.method == "POST":
            # This should only handle direct access token submission now
            # OAuth flow is handled by /dhan/initiate-oauth
            access_token = request.form.get("access_token")

            if access_token:
                # Direct token authentication
                logger.info("Processing direct access token for Dhan")
                auth_token, error_message = auth_function(access_token, account_id=account_id)

                if auth_token:
                    # Validate authentication by testing funds API
                    from broker.dhan.api.funds import test_auth_token

                    is_valid, validation_error = test_auth_token(auth_token)

                    if is_valid:
                        logger.info("Dhan direct token authentication successful")
                        forward_url = "broker.html"
                        # The auth_token will be handled by the common success flow below
                    else:
                        logger.error(f"Dhan direct token validation failed: {validation_error}")
                        return jsonify(
                            {
                                "status": "error",
                                "message": f"Token validation failed: {validation_error}",
                            }
                        ), 401
                else:
                    return jsonify(
                        {"status": "error", "message": error_message or "Invalid access token"}
                    ), 401
            else:
                # If no access token provided, return error
                return jsonify(
                    {
                        "status": "error",
                        "message": "Please provide either Client ID for OAuth or Access Token for direct login",
                    }
                ), 400
    elif broker == "indmoney":
        # Two credential shapes are supported (docs 04-authentication-users),
        # read from this account's own credentials (multi-account):
        #   API secret set -> a manually generated 24h access token; use it
        #     directly. This keeps existing accounts working unchanged.
        #   API secret blank -> TOTP flow. The API key holds the static Client
        #     ID (sent as x-api-key); the user supplies MPIN and a live TOTP
        #     code, and POST /generate/token mints a fresh token.
        # Detected from the credentials themselves rather than a new flag.
        manual_token = (get_broker_api_secret(account_id, broker="indmoney") or "").strip()
        indmoney_client_id = (get_broker_api_key(account_id, broker="indmoney") or "").strip()

        if request.method == "GET":
            if manual_token:
                # auth_function validates the token against /user/profile first,
                # so a placeholder or an expired paste cannot be stored as if it
                # were a working session.
                logger.debug("IndMoney broker - trying the account's stored access token")
                auth_token, error_message = auth_function("indmoney", account_id=account_id)
                forward_url = "broker.html"

                if not auth_token and indmoney_client_id:
                    logger.warning(
                        "IndMoney: BROKER_API_SECRET is not a usable access token "
                        f"({error_message}); falling back to MPIN + TOTP login"
                    )
                    return redirect("/broker/indmoney/totp")

            elif indmoney_client_id:
                # Redirect to React TOTP page
                return redirect("/broker/indmoney/totp")

            else:
                return handle_auth_failure(
                    "IndMoney is not configured. Set BROKER_API_KEY to the Client ID from "
                    "indstocks.com > API Trading > Access Tokens, and leave BROKER_API_SECRET "
                    "blank to log in with MPIN + TOTP.",
                    forward_url="broker.html",
                )

        elif request.method == "POST":
            from broker.indmoney.api.auth_api import authenticate_broker_totp

            mpin = request.form.get("mpin")
            totp_code = request.form.get("totp")

            if not mpin or not totp_code:
                return jsonify(
                    {"status": "error", "message": "Please provide both MPIN and TOTP code"}
                ), 400

            logger.info("IndMoney TOTP authentication initiated")
            auth_token, error_message = authenticate_broker_totp(
                mpin, totp_code, account_id=account_id
            )
            forward_url = "broker.html"

            if auth_token:
                logger.info("IndMoney authentication successful, auth_token received")
            else:
                logger.error(f"IndMoney authentication failed: {error_message}")

    elif broker == "deltaexchange":
        code = "deltaexchange"
        logger.debug("DeltaExchange broker - authentication initiated")
        auth_token, error_message = auth_function(code, account_id=account_id)
        forward_url = "broker.html"

    elif broker == "dhan_sandbox":
        code = "dhan_sandbox"
        logger.debug("Dhan Sandbox broker - authentication initiated")
        auth_token, error_message = auth_function(code, account_id=account_id)
        forward_url = "broker.html"

    elif broker == "groww":
        code = "groww"
        logger.debug("Groww broker - authentication initiated")
        auth_token, error_message = auth_function(code, account_id=account_id)
        forward_url = "broker.html"

    elif broker == "sahi":
        # Sahi mints the day's token from this account's stored API key and
        # secret. A TOTP-protected key also needs a code: either the account's
        # saved base32 seed generates it, or the login request carries one
        # (a six-digit code, or the seed itself, which is then saved).
        auth_token, error_message = auth_function(
            "sahi", account_id=account_id, totp=_sahi_totp_from_request(account_id)
        )
        forward_url = "broker.html"

    elif broker == "wisdom":
        code = "wisdom"
        logger.debug("Wisdom broker - authentication initiated")
        auth_token, feed_token, user_id, error_message = auth_function(
            code, account_id=account_id
        )
        forward_url = "broker.html"

    elif broker == "zebu":
        code = request.args.get("code")
        if code:
            logger.debug("Zebu broker - OAuth code present: %s", bool(code))
            auth_token, error_message = auth_function(code, account_id=account_id)
            forward_url = "broker.html"
        else:
            # Initial visit — redirect to Zebu OAuth login page
            logger.info("Redirecting to Zebu OAuth login page")
            # BROKER_API_KEY format: userid:::client_id
            full_api_key = get_broker_api_key(account_id, broker=broker)
            if not full_api_key:
                return handle_auth_failure(
                    "BROKER_API_KEY not configured in environment",
                    forward_url="broker.html",
                )
            client_id = full_api_key.split(":::")[1]  # OAuth client_id
            zebu_login_url = f"https://go.mynt.in/OAuthlogin/authorize/oauth?client_id={client_id}"
            return redirect(zebu_login_url)

    elif broker == "shoonya":
        code = request.args.get("code")
        if code:
            logger.debug("Shoonya broker - OAuth callback received")
            auth_token, error_message = auth_function(code, account_id=account_id)
            forward_url = "broker.html"
        else:
            # Initial visit — redirect to Shoonya OAuth login page
            logger.info("Redirecting to Shoonya OAuth login page")
            # BROKER_API_KEY format: userid:::client_id
            full_api_key = get_broker_api_key(account_id, broker=broker)
            if not full_api_key:
                return handle_auth_failure(
                    "BROKER_API_KEY not configured in environment",
                    forward_url="broker.html",
                )
            parts = full_api_key.split(":::", 1)
            if len(parts) != 2 or not parts[1]:
                return handle_auth_failure(
                    "BROKER_API_KEY must be in format userid:::client_id",
                    forward_url="broker.html",
                )
            client_id = parts[1]  # OAuth client_id
            shoonya_login_url = f"https://api.shoonya.com/OAuthlogin/authorize/oauth?client_id={client_id}"
            return redirect(shoonya_login_url)

    elif broker == "firstock":
        if request.method == "GET":
            # Redirect to React TOTP page
            return redirect("/broker/firstock/totp")

        elif request.method == "POST":
            userid = request.form.get("userid")
            password = request.form.get("password")
            totp_code = request.form.get("totp")

            auth_token, error_message = auth_function(
                userid, password, totp_code, account_id=account_id
            )
            forward_url = "broker.html"

    elif broker == "nubra":
        # Nubra logs in with a phone OTP, which is a two-step exchange: the GET
        # dispatches the OTP and mints a temp_token, the POST redeems that token
        # together with the code the user received. The temp_token is held in
        # the Flask session between the two -- a signed (not encrypted) cookie,
        # so nothing beyond this single-use, ~30s token belongs in it.
        if request.method == "GET":
            from broker.nubra.api.auth_api import request_login_otp

            temp_token, masked_phone, error_message = request_login_otp(account_id=account_id)
            if error_message:
                return handle_auth_failure(error_message, forward_url="broker.html")

            session["nubra_temp_token"] = temp_token
            session["nubra_masked_phone"] = masked_phone
            logger.info(f"Nubra login OTP dispatched to {masked_phone}")

            # Redirect to the React OTP page. Reloading that page does NOT
            # resend -- only this GET dispatches an OTP, so a new code means
            # starting the Nubra login again from the broker page.
            return redirect("/broker/nubra/totp")

        elif request.method == "POST":
            # The shared React login component posts the code as "totp"
            otp_code = request.form.get("otp") or request.form.get("totp")

            if not otp_code:
                return jsonify({"status": "error", "message": "OTP is required."}), 400

            # Single-use: drop the token so a failed attempt cannot silently
            # replay a stale one -- the user reloads to get a fresh OTP.
            temp_token = session.pop("nubra_temp_token", None)
            session.pop("nubra_masked_phone", None)

            if temp_token:
                # Second half of the SMS-OTP login the GET above started.
                auth_token, feed_token, error_message = auth_function(
                    otp_code, temp_token, account_id=account_id
                )
            else:
                # No OTP was requested: the code is the account's TOTP (the
                # CMS broker login posts it straight here).
                from broker.nubra.api.auth_api import authenticate_broker_totp

                auth_token, feed_token, error_message = authenticate_broker_totp(
                    otp_code, account_id=account_id
                )
            forward_url = "broker.html"

    elif broker == "samco":
        if request.method == "GET":
            # Connect page: exchanges the configured API key/secret for a session
            # token, then verifies the static IP via /ip/whoami
            return redirect("/broker/samco/auth")

        elif request.method == "POST":
            # Daily login: POST /session/token with the OAuth app's apiKey + apiSecret
            auth_token, error_message = auth_function(account_id=account_id)
            forward_url = "broker.html"

    elif broker == "motilal":
        if request.method == "GET":
            # Redirect to React TOTP page
            return redirect("/broker/motilal/totp")

        elif request.method == "POST":
            userid = request.form.get("userid")
            password = request.form.get("password")
            totp_code = request.form.get("totp")
            date_of_birth = request.form.get("dob")

            # to store user_id (Motilal client code) in the DB - the market data
            # feed authenticates with it and dealer calls send it as clientcode
            user_id = userid
            auth_token, feed_token, error_message = auth_function(
                userid, password, totp_code, date_of_birth, account_id=account_id
            )
            forward_url = "broker.html"

    elif broker == "flattrade":
        code = request.args.get("code")
        client = request.args.get("client")  # Flattrade returns client ID as well
        logger.debug(
            "Flattrade broker - OAuth code present: %s, client present: %s",
            bool(code),
            bool(client),
        )
        auth_token, error_message = auth_function(
            code, account_id=account_id
        )  # Only pass the code parameter (+account_id)
        forward_url = "broker.html"

    elif broker == "tradesmart":
        # TradeSmart (Noren v2) OAuth — mirrors shoonya/zebu. BROKER_API_KEY
        # format is userid:::client_id.
        code = (
            request.args.get("code")
            or request.args.get("request_token")
            or request.args.get("request-token")
        )
        # Manual fallback: paste a pre-minted access_token directly (bypasses
        # GenAcsTok). Use when the OAuth redirect is unavailable — navigate to
        #   /tradesmart/callback?access_token=<TOKEN>&uid=<CLIENT_ID>
        manual_token = request.args.get("access_token") or request.form.get("access_token")
        if manual_token:
            from broker.tradesmart.api.baseurl import resolve_uid

            manual_uid = request.args.get("uid") or request.form.get("uid") or resolve_uid()
            auth_token = f"{manual_uid}:::{manual_token}" if manual_uid else manual_token
            error_message = None
            logger.info("TradeSmart broker - manual access_token accepted")
            forward_url = "broker.html"
        elif code:
            # OAuth callback: exchange code (+checksum) for an access token.
            logger.debug("TradeSmart broker - OAuth callback received")
            auth_token, error_message = auth_function(code, account_id=account_id)
            forward_url = "broker.html"
        else:
            # Initial visit — redirect to the TradeSmart OAuth login page.
            logger.info("Redirecting to TradeSmart OAuth login page")
            full_api_key = get_broker_api_key(account_id, broker=broker)
            if not full_api_key:
                return handle_auth_failure(
                    "BROKER_API_KEY not configured in environment",
                    forward_url="broker.html",
                )
            parts = full_api_key.split(":::", 1)
            client_id = parts[1] if len(parts) == 2 and parts[1] else parts[0]
            tradesmart_login_url = (
                "https://v2api.tradesmartonline.in/OAuthlogin/authorize/oauth"
                f"?client_id={client_id}"
            )
            return redirect(tradesmart_login_url)

    elif broker == "kotak":
        logger.debug(f"Kotak broker - The Broker is {broker}")
        if request.method == "GET":
            # Redirect to React TOTP page
            return redirect("/broker/kotak/totp")

        elif request.method == "POST":
            # New TOTP authentication flow
            mobile_number = request.form.get("mobile") or request.form.get("mobilenumber")
            totp = request.form.get("totp")
            mpin = request.form.get("mpin")

            # Validate inputs
            if not mobile_number or not totp or not mpin:
                error_message = "Please provide Mobile Number, TOTP, and MPIN"
                return jsonify({"status": "error", "message": error_message}), 400

            logger.info(f"Kotak TOTP authentication initiated for mobile: {mobile_number[:5]}***")

            # Call the new authenticate_broker function
            auth_token, error_message = auth_function(
                mobile_number, totp, mpin, account_id=account_id
            )
            forward_url = "broker.html"

            if auth_token:
                logger.info("Kotak authentication successful, auth_token received")
            else:
                logger.error(f"Kotak authentication failed: {error_message}")

    elif broker == "paytm":
        request_token = request.args.get("requestToken")
        logger.debug("Paytm broker - request token present: %s", bool(request_token))
        auth_token, feed_token, error_message = auth_function(
            request_token, account_id=account_id
        )
        forward_url = "broker.html"

    elif broker == "pocketful":
        # Handle the OAuth2 authorization code from the callback
        auth_code = request.args.get("code")
        state = request.args.get("state")
        error = request.args.get("error")
        error_description = request.args.get("error_description")

        # Check if there was an error in the OAuth process
        if error:
            error_msg = f"OAuth error: {error}. {error_description if error_description else ''}"
            logger.error(error_msg)
            return handle_auth_failure(error_msg, forward_url="broker.html")

        # Check if authorization code was provided
        if not auth_code:
            error_msg = "Authorization code not provided"
            logger.error(error_msg)
            return handle_auth_failure(error_msg, forward_url="broker.html")

        logger.debug(
            "Pocketful broker - authorization code present: %s, state present: %s",
            bool(auth_code),
            bool(state),
        )
        # Exchange auth code for access token and fetch client_id
        auth_token, feed_token, user_id, error_message = auth_function(
            auth_code, state, account_id=account_id
        )
        forward_url = "broker.html"

    elif broker == "definedge":
        if request.method == "GET":
            # Trigger OTP generation and redirect to React page
            api_token = get_broker_api_key(account_id, broker=broker)
            api_secret = get_broker_api_secret(account_id, broker=broker)

            # Import the step1 function to trigger OTP
            from broker.definedge.api.auth_api import login_step1

            try:
                step1_response = login_step1(api_token, api_secret, account_id=account_id)
                if step1_response and "otp_token" in step1_response:
                    # Store OTP token in session for later use
                    session["definedge_otp_token"] = step1_response["otp_token"]
                    otp_message = step1_response.get("message", "OTP has been sent successfully")
                    logger.info(f"Definedge OTP triggered: {otp_message}")
                    # Redirect to React TOTP page
                    return redirect("/broker/definedge/totp")
                else:
                    error_msg = "Failed to send OTP. Please check your API credentials."
                    response_keys = (
                        sorted(step1_response.keys())
                        if isinstance(step1_response, dict)
                        else []
                    )
                    logger.error(
                        "Definedge OTP generation failed; response keys: %s",
                        response_keys,
                    )
                    return jsonify({"status": "error", "message": error_msg}), 500
            except Exception as e:
                error_msg = f"Error sending OTP: {str(e)}"
                logger.exception(f"Definedge OTP generation error: {e}")
                return jsonify({"status": "error", "message": error_msg}), 500

        elif request.method == "POST":
            action = request.form.get("action")

            # Handle OTP resend request
            if action == "resend":
                api_token = get_broker_api_key(account_id, broker=broker)
                api_secret = get_broker_api_secret(account_id, broker=broker)

                from broker.definedge.api.auth_api import login_step1

                try:
                    step1_response = login_step1(api_token, api_secret, account_id=account_id)
                    if step1_response and "otp_token" in step1_response:
                        session["definedge_otp_token"] = step1_response["otp_token"]
                        otp_message = "OTP has been resent successfully"
                        logger.info("Definedge OTP resent successfully")
                        return jsonify({"status": "success", "message": otp_message})
                    else:
                        return jsonify({"status": "error", "message": "Failed to resend OTP"})
                except Exception as e:
                    logger.exception(f"Definedge OTP resend error: {e}")
                    return jsonify({"status": "error", "message": str(e)})

            # Handle OTP verification
            else:
                otp_code = request.form.get("otp")
                otp_token = session.get("definedge_otp_token")

                if not otp_token:
                    # Need to regenerate OTP token
                    return jsonify(
                        {
                            "status": "error",
                            "message": "Session expired. Please refresh the page to get a new OTP.",
                        }
                    ), 401

                # Get api_secret for authentication
                api_secret = get_broker_api_secret(account_id, broker=broker)

                # Use authenticate_broker for OTP verification
                from broker.definedge.api.auth_api import authenticate_broker

                try:
                    # Call authenticate_broker with OTP token and code
                    auth_token, feed_token, user_id, error_message = authenticate_broker(
                        otp_token, otp_code, api_secret, account_id=account_id
                    )

                    if auth_token:
                        # Clear the OTP token from session
                        session.pop("definedge_otp_token", None)

                except Exception as e:
                    logger.exception(f"Definedge OTP verification error: {e}")
                    auth_token = None
                    feed_token = None
                    user_id = None
                    error_message = str(e)

                forward_url = "broker.html"

    elif broker == "rmoney":
        try:
            # Extract session data from XTS OAuth callback
            session_data = None
            if request.method == "POST":
                raw_data = request.get_data().decode("utf-8")
                if request.headers.get("Content-Type") == "application/x-www-form-urlencoded":
                    if raw_data.startswith("session="):
                        from urllib.parse import unquote

                        session_data = unquote(raw_data[8:])
                    else:
                        session_data = raw_data
                else:
                    session_data = raw_data
            else:
                session_data = request.args.get("session")

            if session_data:
                # XTS OAuth returns the full login session with token directly
                session_json = json.loads(session_data)
                if isinstance(session_json, str):
                    session_json = json.loads(session_json)

                # The session already contains the final auth token and userID
                auth_token = session_json.get("token")
                user_id = session_json.get("userID")

                if not auth_token:
                    logger.error(f"RMoney callback - No token in session. Keys: {list(session_json.keys())}")
                    return jsonify({"error": "No token found in session data"}), 400

                logger.info(
                    "RMoney OAuth authentication successful (user ID present: %s)",
                    bool(user_id),
                )

                # Get feed token for market data
                from broker.rmoney.api.auth_api import get_feed_token

                feed_token, feed_user_id, feed_error = get_feed_token(account_id)
                if feed_error:
                    logger.warning(f"RMoney feed token error: {feed_error}")
                    feed_token = None
                if not user_id:
                    user_id = feed_user_id

                error_message = None
                forward_url = "broker.html"
            else:
                # No session data - initial request, redirect to RMoney OAuth login
                from broker.rmoney.baseurl import INTERACTIVE_URL as RMONEY_INTERACTIVE_URL

                BROKER_API_KEY_LOCAL = get_broker_api_key(account_id, broker=broker)
                # Built from HOST_SERVER, not the request Host header: this URL
                # is handed to the broker as the OAuth return address, so a
                # poisoned Host would send the callback (and its credentials)
                # to an attacker-controlled origin.
                callback_url = build_external_url(
                    url_for("brlogin.broker_callback", broker="rmoney")
                )
                oauth_url = f"{RMONEY_INTERACTIVE_URL}/thirdparty?appKey={BROKER_API_KEY_LOCAL}&returnURL={callback_url}"
                return redirect(oauth_url)

        except json.JSONDecodeError as e:
            return jsonify({"error": f"Invalid session data format: {str(e)}"}), 400
        except Exception as e:
            logger.exception(f"RMoney callback error: {e}")
            return jsonify({"error": f"Error processing request: {str(e)}"}), 500

    elif broker == "arrow":
        # Arrow redirects back with `request-token` (hyphen, per its docs). The
        # generic branch below only checks `request_token`/`code`, so handle the
        # hyphenated spelling (and other plausible variants) explicitly so the
        # request token is never silently dropped.
        code = (
            request.args.get("request-token")
            or request.args.get("request_token")
            or request.args.get("requestToken")
            or request.args.get("code")
        )
        logger.debug(f"Arrow broker - request token present: {bool(code)}")
        auth_token, error_message = auth_function(code, account_id=account_id)
        forward_url = "broker.html"

    elif broker == "hdfcsky":
        # HDFC Sky's docs describe the redirect only as carrying "a Request
        # Token" without naming the query parameter, so accept every plausible
        # spelling rather than silently dropping the token.
        code = (
            request.args.get("request_token")
            or request.args.get("requestToken")
            or request.args.get("request-token")
            or request.args.get("code")
        )
        logger.debug(f"HDFC Sky broker - request token present: {bool(code)}")
        auth_token, error_message = auth_function(code, account_id=account_id)
        forward_url = "broker.html"

    elif broker == "hdfcsecurities":
        # InvestRight's docs describe the redirect only as carrying "a Request
        # Token" without naming the query parameter, so accept every plausible
        # spelling rather than silently dropping the token.
        code = (
            request.args.get("request_token")
            or request.args.get("requestToken")
            or request.args.get("request-token")
            or request.args.get("code")
        )
        logger.debug(f"HDFC Securities broker - request token present: {bool(code)}")
        auth_token, error_message = auth_function(code)
        forward_url = "broker.html"

    else:
        code = request.args.get("code") or request.args.get("request_token")
        logger.debug("Generic broker - callback code present: %s", bool(code))
        auth_token, error_message = auth_function(code, account_id=account_id)
        forward_url = "broker.html"

    if auth_token:
        # Store broker in session
        session["broker"] = broker
        logger.info(f"Successfully connected broker: {broker}")
        if broker == "zerodha":
            auth_token = f"{get_broker_api_key(account_id, broker=broker)}:{auth_token}"
        if broker == "dhan":
            auth_token = f"{auth_token}"

        # For brokers that have user_id and feed_token from authenticate_broker
        if broker in ["angel", "compositedge", "pocketful", "definedge", "dhan", "motilal", "rmoney", "iiflcapital"]:
            # For OAuth brokers, these can lose the Flask session mid-redirect.
            # There is no safe user to attribute the callback to in that case
            # (multiple platform users may exist) — fail closed rather than
            # guessing, matching the behavior every other broker already has
            # below when "user" is missing from the session.
            if broker in ("compositedge", "rmoney", "iiflcapital") and "user" not in session:
                logger.error(f"No session user found for {broker} callback")
                return handle_auth_failure(
                    "No user account found. Please login first.", forward_url="broker.html"
                )

            # Pass the feed token and user_id to handle_auth_success
            return handle_auth_success(
                auth_token, account_id, broker, feed_token=feed_token, user_id=user_id,
                owner_username=session["user"],
            )
        elif broker == "paytm":
            # Paytm has feed_token (public_access_token) but no user_id
            return handle_auth_success(
                auth_token, account_id, broker, feed_token=feed_token,
                owner_username=session["user"],
            )
        else:
            # Pass just the feed token to handle_auth_success (other brokers don't have feed_token or user_id)
            return handle_auth_success(
                auth_token, account_id, broker, feed_token=feed_token,
                owner_username=session["user"],
            )
    else:
        return handle_auth_failure(error_message, forward_url=forward_url)


def _oauth_account_id():
    """The broker account this initiate-oauth request is connecting.
    Same resolution as broker_callback: "pending_account_id" is set by the
    account-management UI (POST /api/accounts, or POST
    /api/accounts/<id>/connect) right before redirecting here; without it,
    this falls back to the platform username (one implicit account per
    user, account_id == username)."""
    return session.get("pending_account_id") or session.get("user")


def _oauth_redirect_uri(broker: str) -> str:
    """REDIRECT_URL is one fixed value in .env, historically written for
    the instance's single configured broker (e.g. .../upstox/callback).
    Rewrite its trailing /<broker>/callback segment so any broker gets its
    own correct callback URL on this same server."""
    base = re.sub(r"/[^/]+/callback/?$", "", get_broker_redirect_url() or "")
    return f"{base}/{broker}/callback"


def _js_redirect_response(url: str, broker_label: str = "broker") -> str:
    """An HTML page that immediately navigates to an external URL via JS,
    which reliably performs the redirect where a raw 302 to a
    cross-origin URL can be finicky in some browser/proxy setups."""
    return f'''
    <html>
    <head>
        <title>Redirecting to {broker_label}...</title>
    </head>
    <body>
        <p>Redirecting to {broker_label} login page...</p>
        <script>
            window.location.href = "{url}";
        </script>
    </body>
    </html>
    '''


@brlogin_bp.route("/dhan/initiate-oauth", methods=["GET", "POST"])
@limiter.limit(LOGIN_RATE_LIMIT_MIN)
@limiter.limit(LOGIN_RATE_LIMIT_HOUR)
def dhan_initiate_oauth():
    """Handle Dhan OAuth initiation"""
    # Check if user is not in session first
    if "user" not in session:
        return redirect(url_for("auth.login"))

    account_id = _oauth_account_id()

    # Get client_id from BROKER_API_KEY (format: client_id:::api_key), DB-first
    # for the target account, falling back to .env
    broker_api_key = get_broker_api_key(account_id, broker="dhan")
    client_id = None

    if broker_api_key and ":::" in broker_api_key:
        client_id, _ = broker_api_key.split(":::")

    if not client_id:
        error_message = "Client ID not found in BROKER_API_KEY. Please configure BROKER_API_KEY as 'client_id:::api_key' in .env"
        logger.error(error_message)
        return handle_auth_failure(error_message, forward_url="broker.html")

    logger.info(f"Initiating Dhan OAuth flow with client ID: {client_id}")

    # Import the required functions
    from broker.dhan.api.auth_api import generate_consent, get_login_url

    # Generate consent with the client ID
    consent_app_id, error = generate_consent(client_id, account_id=account_id)

    if consent_app_id:
        # Store consent_app_id in session
        session["consent_app_id"] = consent_app_id

        # Get the login URL
        login_url = get_login_url(consent_app_id)
        if login_url:
            # ``consentAppId`` in the URL is a single-use OAuth credential.
            # The browser needs the URL below, but application logs do not.
            logger.info("Redirecting to Dhan OAuth login URL (consent credential redacted)")
            return _js_redirect_response(login_url, "Dhan")
        else:
            error_message = "Failed to generate login URL"
            logger.error(error_message)
            return handle_auth_failure(error_message, forward_url="broker.html")
    else:
        error_message = (
            error or "Failed to generate consent. Please check your API credentials and Client ID."
        )
        logger.error(error_message)
        return handle_auth_failure(error_message, forward_url="broker.html")


def _account_has_broker_credentials(account_id, broker: str) -> bool:
    """Whether this account has any app credential of its own for ``broker``."""
    if not account_id:
        return False
    return bool(
        get_broker_api_key(account_id, broker=broker)
        or get_broker_api_secret(account_id, broker=broker)
        or get_broker_api_key_market(account_id, broker=broker)
    )


def _require_account_api_key(broker: str):
    """Common initiate-oauth prologue: require a logged-in session and a
    resolved broker_api_key for the account being connected. Returns
    (account_id, api_key, None) on success, or (None, None, error_response)
    to return immediately."""
    if "user" not in session:
        return None, None, redirect(url_for("auth.login"))

    account_id = _oauth_account_id()
    api_key = get_broker_api_key(account_id, broker=broker)
    if not api_key:
        error_message = f"Broker API key not configured for this {broker} account."
        logger.error(error_message)
        return None, None, handle_auth_failure(error_message, forward_url="broker.html")

    return account_id, api_key, None


@brlogin_bp.route("/compositedge/initiate-oauth", methods=["GET", "POST"])
@limiter.limit(LOGIN_RATE_LIMIT_MIN)
@limiter.limit(LOGIN_RATE_LIMIT_HOUR)
def compositedge_initiate_oauth():
    _account_id, api_key, error_response = _require_account_api_key("compositedge")
    if error_response:
        return error_response

    return_url = _oauth_redirect_uri("compositedge")
    # Deliberately unencoded (matches the original client-side URL
    # construction this replaced) -- Upstox's OAuth endpoint does a literal
    # string comparison against its registered redirect_uri rather than
    # URL-decoding first, so percent-encoding here breaks the match even
    # though it's the more spec-compliant choice. Other brokers' endpoints
    # aren't confirmed either way; keeping all of them unencoded for
    # consistency with what's known to have worked before.
    login_url = f"https://xts.compositedge.com/interactive/thirdparty?appKey={api_key}&returnURL={return_url}"
    return _js_redirect_response(login_url, "CompositEdge")


@brlogin_bp.route("/flattrade/initiate-oauth", methods=["GET", "POST"])
@limiter.limit(LOGIN_RATE_LIMIT_MIN)
@limiter.limit(LOGIN_RATE_LIMIT_HOUR)
def flattrade_initiate_oauth():
    _account_id, api_key, error_response = _require_account_api_key("flattrade")
    if error_response:
        return error_response

    # BROKER_API_KEY for Flattrade is stored "client_id:::app_key" (same
    # convention as Dhan) — only the app_key half is used in the login URL.
    app_key = api_key.split(":::")[1] if ":::" in api_key else api_key
    login_url = f"https://auth.flattrade.in/?app_key={app_key}"
    return _js_redirect_response(login_url, "Flattrade")


@brlogin_bp.route("/fyers/initiate-oauth", methods=["GET", "POST"])
@limiter.limit(LOGIN_RATE_LIMIT_MIN)
@limiter.limit(LOGIN_RATE_LIMIT_HOUR)
def fyers_initiate_oauth():
    _account_id, api_key, error_response = _require_account_api_key("fyers")
    if error_response:
        return error_response

    redirect_uri = _oauth_redirect_uri("fyers")
    login_url = (
        "https://api-t1.fyers.in/api/v3/generate-authcode"
        f"?client_id={api_key}&redirect_uri={redirect_uri}"
        "&response_type=code&state=2e9b44629ebb28226224d09db3ffb47c"
    )
    return _js_redirect_response(login_url, "Fyers")


@brlogin_bp.route("/upstox/initiate-oauth", methods=["GET", "POST"])
@limiter.limit(LOGIN_RATE_LIMIT_MIN)
@limiter.limit(LOGIN_RATE_LIMIT_HOUR)
def upstox_initiate_oauth():
    _account_id, api_key, error_response = _require_account_api_key("upstox")
    if error_response:
        return error_response

    redirect_uri = _oauth_redirect_uri("upstox")
    login_url = (
        "https://api.upstox.com/v2/login/authorization/dialog"
        f"?response_type=code&client_id={api_key}"
        f"&redirect_uri={redirect_uri}"
    )
    return _js_redirect_response(login_url, "Upstox")


@brlogin_bp.route("/zerodha/initiate-oauth", methods=["GET", "POST"])
@limiter.limit(LOGIN_RATE_LIMIT_MIN)
@limiter.limit(LOGIN_RATE_LIMIT_HOUR)
def zerodha_initiate_oauth():
    _account_id, api_key, error_response = _require_account_api_key("zerodha")
    if error_response:
        return error_response

    # Kite Connect uses the redirect URL registered in the app's own Kite
    # Connect console — no redirect_uri query param.
    login_url = f"https://kite.trade/connect/login?api_key={api_key}"
    return _js_redirect_response(login_url, "Zerodha")


@brlogin_bp.route("/arrow/initiate-oauth", methods=["GET", "POST"])
@limiter.limit(LOGIN_RATE_LIMIT_MIN)
@limiter.limit(LOGIN_RATE_LIMIT_HOUR)
def arrow_initiate_oauth():
    _account_id, api_key, error_response = _require_account_api_key("arrow")
    if error_response:
        return error_response

    login_url = f"https://app.arrow.trade/app/login?appID={api_key}"
    return _js_redirect_response(login_url, "Arrow")


@brlogin_bp.route("/hdfcsky/initiate-oauth", methods=["GET", "POST"])
@limiter.limit(LOGIN_RATE_LIMIT_MIN)
@limiter.limit(LOGIN_RATE_LIMIT_HOUR)
def hdfcsky_initiate_oauth():
    _account_id, api_key, error_response = _require_account_api_key("hdfcsky")
    if error_response:
        return error_response

    login_url = f"https://developer.hdfcsky.com/oapi/v1/login?api_key={api_key}"
    return _js_redirect_response(login_url, "HDFC Sky")


@brlogin_bp.route("/hdfcsecurities/initiate-oauth", methods=["GET", "POST"])
@limiter.limit(LOGIN_RATE_LIMIT_MIN)
@limiter.limit(LOGIN_RATE_LIMIT_HOUR)
def hdfcsecurities_initiate_oauth():
    _account_id, api_key, error_response = _require_account_api_key("hdfcsecurities")
    if error_response:
        return error_response

    # InvestRight hosted login (credentials + 2FA + consent); redirects back to
    # the app's registered callback with a request token.
    login_url = f"https://developer.hdfcsec.com/oapi/v1/login?api_key={api_key}"
    return _js_redirect_response(login_url, "HDFC Securities")


@brlogin_bp.route("/paytm/initiate-oauth", methods=["GET", "POST"])
@limiter.limit(LOGIN_RATE_LIMIT_MIN)
@limiter.limit(LOGIN_RATE_LIMIT_HOUR)
def paytm_initiate_oauth():
    _account_id, api_key, error_response = _require_account_api_key("paytm")
    if error_response:
        return error_response

    # state is a literal placeholder value in the original client-side URL,
    # preserved as-is rather than guessed at.
    login_url = f"https://login.paytmmoney.com/merchant-login?apiKey={api_key}&state={{default}}"
    return _js_redirect_response(login_url, "Paytm Money")


@brlogin_bp.route("/pocketful/initiate-oauth", methods=["GET", "POST"])
@limiter.limit(LOGIN_RATE_LIMIT_MIN)
@limiter.limit(LOGIN_RATE_LIMIT_HOUR)
def pocketful_initiate_oauth():
    _account_id, api_key, error_response = _require_account_api_key("pocketful")
    if error_response:
        return error_response

    redirect_uri = _oauth_redirect_uri("pocketful")
    # Random per-request state, held server-side (tamper-resistant) instead
    # of the previous client-side localStorage copy. Not currently
    # cross-checked at the callback (broker/pocketful/api/auth_api.py just
    # forwards it along), matching pre-existing behavior — this only makes
    # the value itself harder to tamper with, it doesn't add verification.
    state = secrets.token_urlsafe(16)
    session["pocketful_oauth_state"] = state
    scope = "orders holdings"
    # client_id/redirect_uri unencoded (matches the original client-side
    # code this replaced, and Upstox's OAuth endpoint is confirmed to
    # reject a percent-encoded redirect_uri via literal string comparison —
    # not confirmed for Pocketful either way, kept consistent). scope/state
    # keep encodeURIComponent-equivalent encoding, matching the original.
    login_url = (
        "https://trade.pocketful.in/oauth2/auth"
        f"?client_id={api_key}&redirect_uri={redirect_uri}"
        f"&response_type=code&scope={quote(scope, safe='')}&state={quote(state, safe='')}"
    )
    return _js_redirect_response(login_url, "Pocketful")


# Old Kotak SMS OTP flow - deprecated in favor of TOTP authentication
# Keeping this commented for reference if needed
# @brlogin_bp.route('/<broker>/loginflow', methods=['POST','GET'])
# @limiter.limit(LOGIN_RATE_LIMIT_MIN)
# @limiter.limit(LOGIN_RATE_LIMIT_HOUR)
# def broker_loginflow(broker):
#     # This function is no longer used for Kotak TOTP authentication
#     pass


# ============================================================
# Samco Routes
# ============================================================


@brlogin_bp.route("/samco/ip-status", methods=["GET"])
@limiter.limit(LOGIN_RATE_LIMIT_MIN)
@limiter.limit(LOGIN_RATE_LIMIT_HOUR)
def samco_ip_status():
    """Report the source IP Samco sees for this host vs the registered static IPs.

    Backed by Samco's GET /ip/whoami diagnostic. There is deliberately no
    /samco/update-ip counterpart: the password-based /ip/ipRegistration and
    /ip/ipUpdate endpoints are deprecated in Trade API v3.2, and static IPs are
    now registered through the Samco Web Dashboard.
    """
    if "user" not in session:
        return jsonify({"status": "error", "message": "Not logged in"}), 401

    from broker.samco.api.auth_api import DASHBOARD_URL, get_whoami
    from database.auth_db import get_auth_token

    session_token = get_auth_token(session["user"])
    if not session_token:
        return jsonify({
            "status": "error",
            "message": "Not connected to Samco. Log in to the broker first.",
        }), 400

    data, error = get_whoami(session_token)
    if error:
        return jsonify({"status": "error", "message": error}), 400

    return jsonify({
        "status": "success",
        "src_ip": data.get("srcIp") or "",
        "primary_ip": data.get("primaryIp") or "",
        "secondary_ip": data.get("secondaryIp") or "",
        "matches": bool(data.get("matches")),
        "matched_as": data.get("matchedAs"),
        "message": data.get("statusMessage", ""),
        "dashboard_url": DASHBOARD_URL,
    })


@brlogin_bp.route("/nubra/ip-status", methods=["GET"])
@limiter.limit(LOGIN_RATE_LIMIT_MIN)
@limiter.limit(LOGIN_RATE_LIMIT_HOUR)
def nubra_ip_status():
    """Get static IP validation status for Nubra.

    Mirrors the read half of /samco/ip-status. There is deliberately no
    /nubra/update-ip counterpart: Nubra's REST V3 API exposes only
    GET /ipaddress/validate, with no endpoint to register or change the
    static IPs -- that is done through Nubra directly.
    """
    if "user" not in session:
        return jsonify({"status": "error", "message": "Not logged in"}), 401

    from broker.nubra.api.auth_api import validate_static_ip
    from database.auth_db import get_auth_token

    session_token = get_auth_token(session["user"])
    if not session_token:
        return jsonify({
            "status": "error",
            "message": "Not connected to Nubra. Log in to the broker first.",
        }), 400

    payload, error = validate_static_ip(session_token)

    if error:
        # "No IP addresses registered for user" is the expected answer for an
        # account without static IP access, not a failure to report.
        registered = "no ip addresses registered" not in error.lower()
        return jsonify({
            "status": "error",
            "message": error,
            "registered": registered,
            "editable": False,
        }), 200 if not registered else 400

    return jsonify({
        "status": "success",
        "registered": True,
        "is_matched": payload.get("is_matched", False),
        "current_ip": payload.get("current_ip_address", ""),
        "primary_ip": payload.get("primary_ip_address", ""),
        "secondary_ip": payload.get("secondary_ip_address", ""),
        # Nubra has no register/update IP API; changes go through Nubra.
        "editable": False,
        "message": (
            "Current IP matches a registered static IP."
            if payload.get("is_matched")
            else "Current IP does NOT match the registered static IPs. "
                 "Update them with Nubra to restore access."
        ),
    })
