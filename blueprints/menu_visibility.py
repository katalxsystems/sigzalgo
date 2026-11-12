# blueprints/menu_visibility.py
"""
Profile-menu visibility API.

Lets an admin hide specific items from the header's profile dropdown
(config/navigation.ts's profileMenuItems), separately for admin and
non-admin sessions. Purely a visibility toggle, not a permissions system:
hiding an item's menu entry does not block its underlying route -- someone
who already knows the URL (or a bookmark) can still reach it. Use the
existing @admin_required-gated routes for actual access control.
"""

from flask import Blueprint, jsonify, request, session

from database.settings_db import get_hidden_menu_items, set_hidden_menu_items
from database.user_db import find_user_by_exact_username
from utils.logging import get_logger
from utils.session import admin_required, require_app_session

logger = get_logger(__name__)

menu_visibility_bp = Blueprint("menu_visibility_bp", __name__, url_prefix="/api/menu-visibility")


@menu_visibility_bp.route("", methods=["GET"])
@require_app_session
def get_my_hidden_items():
    """The calling session's own effective hidden-item list, resolved by
    their role. This is what the Navbar's profile dropdown filters against
    -- every logged-in user needs this (not just admins), so it's
    @require_app_session rather than @admin_required.
    """
    user = find_user_by_exact_username(session.get("user"))
    is_admin = bool(user and user.is_admin)
    return jsonify({"status": "success", "hidden_items": get_hidden_menu_items(is_admin)})


@menu_visibility_bp.route("/config", methods=["GET"])
@admin_required
def get_config():
    """Both role lists at once, for the admin settings UI to edit."""
    return jsonify(
        {
            "status": "success",
            "admin": get_hidden_menu_items(is_admin=True),
            "non_admin": get_hidden_menu_items(is_admin=False),
        }
    )


@menu_visibility_bp.route("/config", methods=["POST"])
@admin_required
def update_config():
    """Set which profile-menu items are hidden per role.

    Body: {"admin": ["/leverage", ...], "non_admin": [...]}. Either key may
    be omitted to leave that role's list untouched (see
    set_hidden_menu_items's semantics).
    """
    data = request.get_json(silent=True) or {}
    admin_items = data.get("admin")
    non_admin_items = data.get("non_admin")

    if admin_items is not None and not isinstance(admin_items, list):
        return jsonify({"status": "error", "message": "'admin' must be a list"}), 400
    if non_admin_items is not None and not isinstance(non_admin_items, list):
        return jsonify({"status": "error", "message": "'non_admin' must be a list"}), 400

    set_hidden_menu_items(admin_items, non_admin_items)

    return jsonify(
        {
            "status": "success",
            "message": "Menu visibility updated",
            "admin": get_hidden_menu_items(is_admin=True),
            "non_admin": get_hidden_menu_items(is_admin=False),
        }
    )
