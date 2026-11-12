"""The broker account a web session is working in, and whether it is an admin.

Flow workflows and Python strategies are owned per broker account
(``Auth.name``): a user sees and manages only their active account's items,
and administrators see everyone's. These helpers answer both questions for the
current Flask request.
"""

from flask import g, session


def current_account_id():
    """The broker account the session is working in, or None when logged out.

    ``active_account_id`` is set at broker login and by the account switcher.
    API keys and ownership are per account, so this -- not the platform
    username -- is what they are keyed by. Falls back to the user's default
    account, then the username (the pre-multi-account implicit account).
    """
    username = session.get("user")
    if not username:
        return None
    account_id = session.get("active_account_id") or session.get("user_session_key")
    if not account_id:
        from database.auth_db import get_default_account_id

        account_id = get_default_account_id(username)
    return account_id or username


def is_admin_session():
    """Whether the session user is an administrator (cached per request)."""
    if "is_admin_session" not in g:
        from database.user_db import find_user_by_exact_username

        username = session.get("user")
        user = find_user_by_exact_username(username) if username else None
        g.is_admin_session = bool(user and user.is_admin)
    return g.is_admin_session


def owner_of_account(account_id):
    """Platform username owning ``account_id`` (the session user if unknown)."""
    from database.auth_db import get_owner_username

    return (get_owner_username(account_id) if account_id else None) or session.get("user")
