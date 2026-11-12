"""Expiry of a broker's JWT access token, read locally.

Some brokers (5paisa) issue the daily access token as a JWT whose ``exp``
claim says when it stops working. Reading that claim is a base64 decode, not a
broker call, so a feed or quote poller can tell an expired session apart from
a transient failure without asking the broker and logging its refusal.
"""

import base64
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")


def jwt_expiry(token: str | None) -> float | None:
    """The ``exp`` claim of a JWT as a Unix timestamp, without verifying it.

    Returns None for anything that is not a JWT carrying a numeric ``exp``,
    so callers treat an unreadable token as "unknown", never as expired.
    """
    if not token or token.count(".") != 2:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return float(exp) if exp is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def expired_at(token: str | None, now: float | None = None) -> str | None:
    """When the token expired, formatted in IST, or None if it has not (or
    its expiry cannot be read)."""
    exp = jwt_expiry(token)
    if exp is None or (now if now is not None else time.time()) < exp:
        return None
    return datetime.fromtimestamp(exp, _IST).strftime("%d %b %H:%M IST")
