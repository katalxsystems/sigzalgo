# blueprints/broker_accounts.py
"""Multi-broker-account management API.

A platform user can own several broker accounts (including more than one
account on the same broker). This blueprint lets the logged-in user list,
add, remove, and switch between their accounts. Connecting an account's
broker session still goes through the existing OAuth/TOTP flow in
blueprints/brlogin.py — POST /api/accounts only creates the account_id and
points the browser at that flow.

Administrators may also manage other users' accounts on their behalf: list
them (``?all=1`` / ``?owner=``), create one (``owner_username`` in the body),
set credentials, generate API keys, connect, set the default and revoke.
Every such action is logged. ``/activate`` stays own-accounts-only: it points
the caller's own trading UI at the account, which would mean an admin
trading as another user.
"""

import os
import secrets

from flask import Blueprint, jsonify, request, session

from database.auth_db import (
    create_broker_account,
    get_owner_username,
    list_broker_accounts,
    set_broker_credentials,
    set_default_account,
    upsert_api_key,
    upsert_auth,
)
from utils.account_context import is_admin_session
from utils.config import get_connect_path, validate_broker_api_key_format
from utils.logging import get_logger
from utils.session import require_app_session

logger = get_logger(__name__)

broker_accounts_bp = Blueprint("broker_accounts_bp", __name__, url_prefix="/api/accounts")

VALID_BROKERS = {b.strip().lower() for b in os.getenv("VALID_BROKERS", "").split(",") if b.strip()}


def _generate_api_key():
    return secrets.token_hex(32)


def _can_manage(account_id):
    """The owner of an existing account the caller may manage: the logged-in
    user for their own account, or any owner for an administrator. None when
    the account does not exist or is someone else's (callers answer 404)."""
    owner = get_owner_username(account_id)
    if owner is None:
        return None
    if owner == session.get("user") or is_admin_session():
        return owner
    return None


def _audit(action, account_id, owner):
    """Log an administrator acting on another user's account."""
    if owner and owner != session.get("user"):
        logger.info(f"Admin {session.get('user')} {action} account {account_id} (owner {owner})")


def _find_manageable_account(account_id):
    """Like _find_owned_account, but administrators may also get another
    user's account. Returns (account dict with owner_username, owner), or
    (None, None)."""
    owner = _can_manage(account_id)
    if owner is None:
        return None, None
    for account in list_broker_accounts(owner):
        if account["account_id"] == account_id:
            return {**account, "owner_username": owner}, owner
    return None, None


def _find_owned_account(account_id):
    """Return the account dict (from list_broker_accounts) if it belongs to
    the logged-in user, else None. Used where the broker name is also
    needed, avoiding a second query beyond the ownership check."""
    for account in list_broker_accounts(session.get("user")):
        if account["account_id"] == account_id:
            return account
    return None


@broker_accounts_bp.route("", methods=["GET"])
@require_app_session
def list_accounts():
    """The caller's accounts. Administrators: ``?owner=<username>`` for one
    user's accounts, ``?all=1`` for every user's (each with owner_username)."""
    owner = (request.args.get("owner") or "").strip()
    want_all = request.args.get("all") in ("1", "true", "yes")
    if want_all or (owner and owner != session["user"]):
        if not is_admin_session():
            return jsonify({"status": "error", "message": "Administrator access required"}), 403
        from database.auth_db import Auth

        if want_all:
            owners = sorted({a.owner_username for a in Auth.query.all() if a.owner_username})
        else:
            owners = [owner]
        accounts = [
            {**account, "owner_username": o} for o in owners for account in list_broker_accounts(o)
        ]
        return jsonify({"status": "success", "data": accounts})
    accounts = list_broker_accounts(session["user"])
    return jsonify({"status": "success", "data": accounts})


@broker_accounts_bp.route("", methods=["POST"])
@require_app_session
def add_account():
    data = request.get_json(silent=True) or {}
    broker = (data.get("broker") or "").strip().lower()
    label = (data.get("label") or "").strip() or None
    broker_api_key = (data.get("broker_api_key") or "").strip() or None
    broker_api_secret = (data.get("broker_api_secret") or "").strip() or None

    if not broker:
        return jsonify({"status": "error", "message": "broker is required"}), 400
    if VALID_BROKERS and broker not in VALID_BROKERS:
        return jsonify({"status": "error", "message": f"Unsupported broker: {broker}"}), 400

    if broker_api_key:
        format_error = validate_broker_api_key_format(broker, broker_api_key)
        if format_error:
            return jsonify({"status": "error", "message": format_error}), 400

    # Administrators may create an account for another (existing) user.
    owner = (data.get("owner_username") or "").strip() or session["user"]
    if owner != session["user"]:
        if not is_admin_session():
            return jsonify({"status": "error", "message": "Administrator access required"}), 403
        from database.user_db import find_user_by_exact_username

        if not find_user_by_exact_username(owner):
            return jsonify({"status": "error", "message": f"Unknown user: {owner}"}), 404

    account_id = create_broker_account(
        owner,
        broker,
        label=label,
        broker_api_key=broker_api_key,
        broker_api_secret=broker_api_secret,
    )
    if not account_id:
        return jsonify({"status": "error", "message": "Failed to create account"}), 500

    # Carried into blueprints/brlogin.py's broker_callback, which targets
    # this account_id instead of falling back to the platform username. Not
    # for an account created on another user's behalf: the admin's own next
    # broker login must not land on it unless they ask (POST .../connect).
    if owner == session["user"]:
        session["pending_account_id"] = account_id
    else:
        _audit("created", account_id, owner)

    return jsonify(
        {
            "status": "success",
            "data": {
                "account_id": account_id,
                "broker": broker,
                "owner_username": owner,
                "connect_url": get_connect_path(broker),
            },
        }
    )


@broker_accounts_bp.route("/<account_id>", methods=["DELETE"])
@require_app_session
def remove_account(account_id):
    owner = _can_manage(account_id)
    if owner is None:
        return jsonify({"status": "error", "message": "Account not found"}), 404
    _audit("revoked", account_id, owner)

    inserted_id = upsert_auth(account_id, "", "", revoke=True)
    if inserted_id is None:
        return jsonify({"status": "error", "message": "Failed to revoke account"}), 500

    if session.get("pending_account_id") == account_id:
        session.pop("pending_account_id", None)
    if session.get("active_account_id") == account_id:
        session.pop("active_account_id", None)

    return jsonify({"status": "success", "message": "Account revoked"})


@broker_accounts_bp.route("/<account_id>/connect", methods=["POST"])
@require_app_session
def connect_account(account_id):
    """Point the browser at this account's broker login, whether that's an
    external OAuth authorize page or the in-app credential form.

    Also (re)sets session["pending_account_id"], which blueprints/brlogin.py
    reads to know which account a subsequent /<broker>/callback or
    /<broker>/initiate-oauth request belongs to. Without this refresh, an
    older not-yet-connected account's "Connect" button would still target
    whichever account_id was last created in this browser session — POST
    /api/accounts already sets it once, but only at creation time.
    """
    account, owner = _find_manageable_account(account_id)
    if not account:
        return jsonify({"status": "error", "message": "Account not found"}), 404

    # For an administrator connecting another user's account, the broker login
    # (that user's credentials/TOTP) completes in the admin's browser session.
    _audit("started connecting", account_id, owner)
    session["pending_account_id"] = account_id
    return jsonify(
        {"status": "success", "data": {"connect_url": get_connect_path(account["broker"])}}
    )


@broker_accounts_bp.route("/<account_id>/credentials", methods=["POST"])
@require_app_session
def update_account_credentials(account_id):
    """Set/update the per-account broker app credentials (API key/secret)."""
    account, owner = _find_manageable_account(account_id)
    if not account:
        return jsonify({"status": "error", "message": "Account not found"}), 404

    data = request.get_json(silent=True) or {}
    broker_api_key = (data.get("broker_api_key") or "").strip() or None
    broker_api_secret = (data.get("broker_api_secret") or "").strip() or None

    if not broker_api_key and not broker_api_secret:
        return jsonify({"status": "error", "message": "Nothing to update"}), 400

    # Some brokers (5paisa, Flattrade, Dhan) pack more than one credential
    # into a single ":::"-delimited BROKER_API_KEY. Reject a malformed value
    # here, at save time, instead of letting it silently fail deep inside a
    # broker plugin the next time it's actually used (see
    # utils.config.validate_broker_api_key_format).
    if broker_api_key:
        format_error = validate_broker_api_key_format(account["broker"], broker_api_key)
        if format_error:
            return jsonify({"status": "error", "message": format_error}), 400

    ok = set_broker_credentials(account_id, broker_api_key, broker_api_secret)
    if not ok:
        return jsonify({"status": "error", "message": "Failed to update credentials"}), 500
    _audit("updated credentials of", account_id, owner)

    return jsonify({"status": "success", "message": "Credentials updated"})


@broker_accounts_bp.route("/<account_id>/apikey", methods=["POST"])
@require_app_session
def generate_account_api_key(account_id):
    owner = _can_manage(account_id)
    if owner is None:
        return jsonify({"status": "error", "message": "Account not found"}), 404

    api_key = _generate_api_key()
    # Filed under the account's owner, also when an administrator generates it.
    key_id = upsert_api_key(account_id, api_key, owner_username=owner)
    if key_id is None:
        return jsonify({"status": "error", "message": "Failed to generate API key"}), 500
    _audit("generated an API key for", account_id, owner)

    return jsonify({"status": "success", "data": {"account_id": account_id, "api_key": api_key}})


@broker_accounts_bp.route("/<account_id>/default", methods=["POST"])
@require_app_session
def set_default(account_id):
    owner = _can_manage(account_id)
    if owner is None:
        return jsonify({"status": "error", "message": "Account not found"}), 404

    ok = set_default_account(owner, account_id)
    if ok:
        _audit("set as default", account_id, owner)
    if not ok:
        return jsonify({"status": "error", "message": "Failed to set default account"}), 500

    return jsonify({"status": "success", "message": "Default account updated"})


@broker_accounts_bp.route("/<account_id>/activate", methods=["POST"])
@require_app_session
def activate_account(account_id):
    """Switch which account the interactive web UI (dashboard, charting,
    scalping terminal) operates on for the rest of this browser session.
    Does not affect API-key-based requests, which are always routed by the
    key itself regardless of this setting.

    Updates user_session_key/broker (the session keys the dashboard/orders
    code actually reads via database.auth_db.get_auth_token et al.) and not
    just active_account_id, so the switch takes effect immediately.
    """
    account = _find_owned_account(account_id)
    if not account:
        return jsonify({"status": "error", "message": "Account not found"}), 404
    if not account["connected"]:
        return jsonify({"status": "error", "message": "Account is not connected"}), 400

    session["active_account_id"] = account_id
    session["user_session_key"] = account_id
    session["broker"] = account["broker"]
    return jsonify(
        {
            "status": "success",
            "data": {"active_account_id": account_id, "broker": account["broker"]},
        }
    )
