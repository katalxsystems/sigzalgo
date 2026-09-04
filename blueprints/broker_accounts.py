# blueprints/broker_accounts.py
"""Multi-broker-account management API.

A platform user can own several broker accounts (including more than one
account on the same broker). This blueprint lets the logged-in user list,
add, remove, and switch between their accounts. Connecting an account's
broker session still goes through the existing OAuth/TOTP flow in
blueprints/brlogin.py — POST /api/accounts only creates the account_id and
points the browser at that flow.
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
from utils.config import get_connect_path
from utils.logging import get_logger
from utils.session import require_app_session

logger = get_logger(__name__)

broker_accounts_bp = Blueprint("broker_accounts_bp", __name__, url_prefix="/api/accounts")

VALID_BROKERS = {
    b.strip().lower() for b in os.getenv("VALID_BROKERS", "").split(",") if b.strip()
}


def _generate_api_key():
    return secrets.token_hex(32)


def _owns(account_id):
    """Ownership check: the account must belong to the logged-in user."""
    return get_owner_username(account_id) == session.get("user")


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

    account_id = create_broker_account(
        session["user"], broker, label=label,
        broker_api_key=broker_api_key, broker_api_secret=broker_api_secret,
    )
    if not account_id:
        return jsonify({"status": "error", "message": "Failed to create account"}), 500

    # Carried into blueprints/brlogin.py's broker_callback, which targets
    # this account_id instead of falling back to the platform username.
    session["pending_account_id"] = account_id

    return jsonify(
        {
            "status": "success",
            "data": {
                "account_id": account_id,
                "broker": broker,
                "connect_url": get_connect_path(broker),
            },
        }
    )


@broker_accounts_bp.route("/<account_id>", methods=["DELETE"])
@require_app_session
def remove_account(account_id):
    if not _owns(account_id):
        return jsonify({"status": "error", "message": "Account not found"}), 404

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
    account = _find_owned_account(account_id)
    if not account:
        return jsonify({"status": "error", "message": "Account not found"}), 404

    session["pending_account_id"] = account_id
    return jsonify(
        {"status": "success", "data": {"connect_url": get_connect_path(account["broker"])}}
    )


@broker_accounts_bp.route("/<account_id>/credentials", methods=["POST"])
@require_app_session
def update_account_credentials(account_id):
    """Set/update the per-account broker app credentials (API key/secret)."""
    if not _owns(account_id):
        return jsonify({"status": "error", "message": "Account not found"}), 404

    data = request.get_json(silent=True) or {}
    broker_api_key = (data.get("broker_api_key") or "").strip() or None
    broker_api_secret = (data.get("broker_api_secret") or "").strip() or None

    if not broker_api_key and not broker_api_secret:
        return jsonify({"status": "error", "message": "Nothing to update"}), 400

    ok = set_broker_credentials(account_id, broker_api_key, broker_api_secret)
    if not ok:
        return jsonify({"status": "error", "message": "Failed to update credentials"}), 500

    return jsonify({"status": "success", "message": "Credentials updated"})


@broker_accounts_bp.route("/<account_id>/apikey", methods=["POST"])
@require_app_session
def generate_account_api_key(account_id):
    if not _owns(account_id):
        return jsonify({"status": "error", "message": "Account not found"}), 404

    api_key = _generate_api_key()
    key_id = upsert_api_key(account_id, api_key, owner_username=session["user"])
    if key_id is None:
        return jsonify({"status": "error", "message": "Failed to generate API key"}), 500

    return jsonify({"status": "success", "data": {"account_id": account_id, "api_key": api_key}})


@broker_accounts_bp.route("/<account_id>/default", methods=["POST"])
@require_app_session
def set_default(account_id):
    if not _owns(account_id):
        return jsonify({"status": "error", "message": "Account not found"}), 404

    ok = set_default_account(session["user"], account_id)
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
        {"status": "success", "data": {"active_account_id": account_id, "broker": account["broker"]}}
    )
