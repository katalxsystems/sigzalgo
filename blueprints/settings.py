# blueprints/settings.py

from flask import Blueprint, jsonify, session

from database.settings_db import get_analyze_mode, set_analyze_mode
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

settings_bp = Blueprint("settings_bp", __name__, url_prefix="/settings")


def _session_account_id():
    return session.get("user_session_key") or session.get("user")


@settings_bp.route("/analyze-mode")
@check_session_validity
def get_mode():
    """Get the analyze mode setting for the calling account."""
    try:
        return jsonify({"analyze_mode": get_analyze_mode(_session_account_id())})
    except Exception as e:
        logger.exception(f"Error getting analyze mode: {str(e)}")
        return jsonify({"error": "Failed to get analyze mode"}), 500


@settings_bp.route("/analyze-mode/<int:mode>", methods=["POST"])
@check_session_validity
def set_mode(mode):
    """Set the analyze mode setting for the calling account.

    Per-account (database.auth_db.Auth.analyze_mode), not instance-wide --
    see /auth/analyzer-toggle, the React SPA's equivalent route, for the
    full rationale. The sandbox execution engine and square-off scheduler
    run continuously from app startup (see app.py) rather than being
    started/stopped by this toggle, since stopping them here would break
    any other account still relying on analyze mode.
    """
    try:
        account_id = _session_account_id()
        set_analyze_mode(bool(mode), account_id)
        mode_name = "Analyze" if mode else "Live"

        return jsonify(
            {
                "success": True,
                "analyze_mode": bool(mode),
                "message": f"Switched to {mode_name} Mode",
            }
        )
    except Exception as e:
        logger.exception(f"Error setting analyze mode: {str(e)}")
        return jsonify({"error": "Failed to set analyze mode"}), 500
