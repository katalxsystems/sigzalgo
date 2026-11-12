# services/broker_auto_login_service.py
"""
Broker Auto-Login Service

For a self-hosted, single-user OpenAlgo instance running unattended Flow
workflows (price-breach watches, scheduled strategies): if the broker session
for the account those workflows use expires or is revoked mid-day (daily
~3 AM IST rollover, a manual logout, a crash), everything relying on it goes
silently dead until a human opens the browser and reconnects. This service
periodically checks one specific account (named in .env, not auto-discovered
-- see the module security note) and re-authenticates without a browser,
using the broker's TOTP login API, persisting the new session the same way a
normal login would. Two independent triggers:

1. Opportunistic recovery, every AUTO_LOGIN_CHECK_INTERVAL_MINUTES: only when
   the session is actually down AND at least one active Flow workflow
   depends on it -- catches a mid-day crash/revoke without waiting for the
   next day's forced login, but never logs in for no reason.
2. Unconditional daily login at AUTO_LOGIN_FORCE_TIME (IST, default 09:00),
   regardless of whether the old session still looks valid or any workflow
   is active -- guarantees a fresh session is ready before the trading day
   starts even if nothing happened to notice it was dead. Catches up if the
   process was restarted after that time (see _should_force_login_now), the
   same way should_download_master_contract() catches up on a missed smart
   download; fires once per IST calendar day, not once per check.

Only brokers with a fully programmatic (no OAuth redirect) TOTP or
password+TOTP login qualify -- currently fivepaisa and mstock, both of which
already have such a function in broker/<name>/api/auth_api.py. Most brokers
(Zerodha, Upstox, Angel's web flow, etc.) require a real browser round trip
and cannot be auto-logged-in this way; for those, unattended recovery from an
expired session simply is not possible without the user reconnecting.

Configuration (.env, all prefixed AUTO_LOGIN_ -- see .sample.env):
    AUTO_LOGIN_ENABLED=true
    AUTO_LOGIN_ACCOUNT_ID=<Auth.name -- the account_id from Profile > Accounts>
    AUTO_LOGIN_CLIENT_CODE=<broker login client code / email>
    AUTO_LOGIN_PIN=<broker PIN or password>
    AUTO_LOGIN_TOTP_SECRET=<base32 TOTP seed, NOT a 6-digit code>
    AUTO_LOGIN_CHECK_INTERVAL_MINUTES=5   (optional, default 5)
    AUTO_LOGIN_FORCE_TIME=09:00            (optional, default 09:00 IST)

SECURITY: AUTO_LOGIN_PIN and AUTO_LOGIN_TOTP_SECRET are as sensitive as the
broker session they can mint -- anyone who can read .env can log in as this
account. This is consistent with this instance's existing threat model (see
CLAUDE.md: self-hosted, single-user, server access already equals full
control) but is a strictly larger blast radius than the credentials already
in .env, which cannot themselves complete a login. Never log the PIN, the
TOTP secret, or a generated TOTP code -- only the broker's own error message
on failure.

The account must have been connected through the normal UI at least once
before auto-login can manage it: this service reads the existing Auth row
(for its broker name and owner) rather than creating one, the same way the
existing per-account broker_api_key/secret resolution already requires an
account_id that exists (see utils/config.py).
"""

import atexit
import logging
import os
import threading
from datetime import datetime
from typing import Callable

from database.broker_context import scoped_to_broker_arg
from utils.env_config import env_int
from utils.logging import get_logger

logger = get_logger(__name__)


def _broker_login_fivepaisa(
    client_code: str, pin: str, totp_code: str, account_id: str
) -> tuple[str | None, str | None, str | None]:
    """Returns (auth_token, feed_token, error). fivepaisa has no feed token
    distinct from the auth token."""
    from broker.fivepaisa.api.auth_api import authenticate_broker

    auth_token, error = authenticate_broker(client_code, pin, totp_code, account_id=account_id)
    return auth_token, None, error


def _broker_login_mstock(
    client_code: str, pin: str, totp_code: str, account_id: str
) -> tuple[str | None, str | None, str | None]:
    """mstock's clientcode comes from BROKER_API_KEY (per-account or .env),
    not from AUTO_LOGIN_CLIENT_CODE -- client_code is accepted for a uniform
    signature but unused, matching authenticate_with_totp's own contract."""
    from broker.mstock.api.auth_api import authenticate_with_totp

    return authenticate_with_totp(pin, totp_code, account_id=account_id)


# Brokers with a fully programmatic TOTP login. Add an entry here (plus a
# thin adapter above matching this signature) as more brokers grow one.
_TOTP_LOGIN_FUNCS: dict[str, Callable[[str, str, str, str], tuple[str | None, str | None, str | None]]] = {
    "fivepaisa": _broker_login_fivepaisa,
    "mstock": _broker_login_mstock,
}


def _account_has_active_workflow(account_id: str) -> bool:
    """Whether any active Flow workflow currently resolves to this account_id
    through its stored api_key. Auto-login exists to keep those workflows
    running -- re-authenticating an account nothing is using would just be
    noise (and an extra broker login the account holder never asked for)."""
    from database.auth_db import verify_api_key
    from database.flow_db import get_active_workflows, get_workflow_api_key

    for workflow in get_active_workflows():
        api_key = get_workflow_api_key(workflow)
        if not api_key:
            continue
        try:
            resolved_account_id = verify_api_key(api_key)
        except Exception:
            continue
        if resolved_account_id == account_id:
            return True
    return False


@scoped_to_broker_arg
def _persist_relogin(account_id: str, broker: str, auth_token: str, feed_token: str | None) -> None:
    """The DB-persisting half of utils/auth_utils.handle_auth_success(),
    without any of its Flask session/request/SocketIO calls -- this runs on a
    background thread with no request context, so none of that applies (there
    is no browser session to update; the point is that the NEXT browser
    session, and every background trigger, sees a live token again).
    """
    from database.auth_db import upsert_auth
    from database.master_contract_status_db import init_broker_status
    from utils.auth_utils import async_master_contract_download, should_download_master_contract

    inserted_id = upsert_auth(account_id, auth_token, broker, feed_token=feed_token)
    if not inserted_id:
        logger.error(f"Auto-login: failed to persist refreshed session for account {account_id}")
        return

    logger.info(f"Auto-login: refreshed session persisted for account {account_id} ({broker})")

    init_broker_status(broker)
    should_download, reason = should_download_master_contract(broker)
    logger.info(f"Auto-login: master contract check for {broker}: should_download={should_download}, reason={reason}")
    if should_download:
        thread = threading.Thread(
            target=async_master_contract_download, args=(broker,), daemon=True
        )
        thread.start()


def check_and_relogin(force: bool = False) -> bool:
    """Run one auto-login check. Returns True if a re-login was attempted
    (regardless of outcome), False if there was nothing to do. Safe to call
    on any thread; removes its own DB sessions before returning either way.

    force=True (the once-daily trigger -- see BrokerAutoLoginMonitor._loop)
    skips both the "is the session actually down" and "does an active
    workflow need it" checks: the point of the daily login is to guarantee a
    fresh session before the trading day starts, not to react to something
    already having gone wrong. AUTO_LOGIN_ENABLED and the account/broker
    validation below still apply either way.
    """
    if os.getenv("AUTO_LOGIN_ENABLED", "false").strip().lower() != "true":
        return False

    account_id = (os.getenv("AUTO_LOGIN_ACCOUNT_ID") or "").strip()
    client_code = (os.getenv("AUTO_LOGIN_CLIENT_CODE") or "").strip()
    pin = os.getenv("AUTO_LOGIN_PIN") or ""
    totp_secret = os.getenv("AUTO_LOGIN_TOTP_SECRET") or ""

    if not account_id or not pin or not totp_secret:
        logger.warning(
            "Auto-login is enabled but AUTO_LOGIN_ACCOUNT_ID / AUTO_LOGIN_PIN / "
            "AUTO_LOGIN_TOTP_SECRET is not fully configured; skipping."
        )
        return False

    attempted = False
    try:
        from database.auth_db import Auth, db_session, get_auth_token

        # Cheap, side-effect-free: a cache/DB read, not a broker call. See
        # blueprints/auth.py's /session-status route for the same check.
        # Skipped when force=True: the daily login runs regardless of
        # whether the old session still looks valid.
        if not force and get_auth_token(account_id, bypass_cache=True) is not None:
            return False  # session already live -- nothing to do

        # Checked before _account_has_active_workflow, not after: a
        # misconfigured/nonexistent AUTO_LOGIN_ACCOUNT_ID has no active
        # workflow either (nothing can resolve to an account that doesn't
        # exist), so checking workflow-need first buried this genuinely
        # actionable misconfiguration behind the same silent, DEBUG-level
        # "nothing to do" path as the ordinary "session's fine" case.
        auth_row = Auth.query.filter_by(name=account_id).first()
        if auth_row is None:
            logger.error(
                f"Auto-login: no Auth row for account {account_id!r} -- this is not a "
                "valid account_id (it should look like <username>_<broker>_<hash>, from "
                "Profile > Accounts, not a display name). Auto-login cannot do anything "
                "until AUTO_LOGIN_ACCOUNT_ID is corrected."
            )
            return False
        broker = auth_row.broker

        if not force and not _account_has_active_workflow(account_id):
            logger.debug(
                f"Auto-login: account {account_id}'s session is down, but no active "
                "workflow depends on it; not re-authenticating."
            )
            return False

        login_func = _TOTP_LOGIN_FUNCS.get(broker)
        if login_func is None:
            logger.warning(
                f"Auto-login: broker {broker!r} (account {account_id}) has no "
                f"programmatic TOTP login implemented; supported brokers: "
                f"{sorted(_TOTP_LOGIN_FUNCS)}."
            )
            return False

        import pyotp

        totp_code = pyotp.TOTP(totp_secret).now()

        attempted = True
        reason = "daily forced login" if force else "opportunistic recovery"
        logger.info(
            f"Auto-login: attempting re-authentication for account {account_id} "
            f"({broker}) -- {reason}"
        )
        auth_token, feed_token, error = login_func(client_code, pin, totp_code, account_id)

        if error or not auth_token:
            logger.error(f"Auto-login failed for account {account_id} ({broker}): {error}")
            return True

        _persist_relogin(account_id, broker, auth_token, feed_token)
        return True

    except Exception:
        logger.exception(f"Auto-login: unexpected error checking account {account_id!r}")
        return attempted
    finally:
        # No Flask app context on this thread, so teardown_appcontext never
        # fires; every scoped session this check touched would otherwise stay
        # bound to it.
        from utils.db_sessions import remove_all_scoped_sessions

        remove_all_scoped_sessions()


def _should_force_login_now(last_forced_date) -> bool:
    """True once per IST calendar day, from AUTO_LOGIN_FORCE_TIME onward.

    Comparing "now >= today's boundary" rather than "now == boundary" means a
    process that was down or a check that landed a few minutes late still
    fires the forced login later the same day instead of waiting for
    tomorrow's boundary -- the same catch-up shape as
    utils.auth_utils.should_download_master_contract().
    """
    import pytz

    force_time = os.getenv("AUTO_LOGIN_FORCE_TIME", "09:00")
    try:
        hour, minute = map(int, force_time.strip().split(":"))
    except (ValueError, AttributeError):
        logger.warning(f"AUTO_LOGIN_FORCE_TIME={force_time!r} is not HH:MM; using 09:00")
        hour, minute = 9, 0

    now_ist = datetime.now(pytz.timezone("Asia/Kolkata"))
    boundary = now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now_ist < boundary:
        return False
    return last_forced_date != now_ist.date()


class BrokerAutoLoginMonitor:
    """Singleton that runs check_and_relogin() on a fixed interval, plus an
    unconditional daily login at AUTO_LOGIN_FORCE_TIME (see _loop)."""

    _instance: "BrokerAutoLoginMonitor | None" = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._interval_seconds = env_int("AUTO_LOGIN_CHECK_INTERVAL_MINUTES", 5, minimum=1) * 60
        # Guards the once-per-day forced login -- see _should_force_login_now.
        self._last_forced_date = None

    def start(self) -> None:
        if self._running:
            return
        if os.getenv("AUTO_LOGIN_ENABLED", "false").strip().lower() != "true":
            logger.debug("Auto-login monitor not started: AUTO_LOGIN_ENABLED is not 'true'.")
            return
        self._stop_event = threading.Event()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info(
            f"Broker auto-login monitor started (checking every "
            f"{self._interval_seconds // 60} minute(s))"
        )

    def stop(self) -> None:
        if not self._running:
            return
        self._stop_event.set()
        self._running = False
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._thread = None
        logger.info("Broker auto-login monitor stopped")

    def _loop(self) -> None:
        stop_event = self._stop_event
        while not stop_event.is_set():
            try:
                if _should_force_login_now(self._last_forced_date):
                    import pytz

                    check_and_relogin(force=True)
                    # Set regardless of outcome: a failed forced login (bad
                    # TOTP secret, broker down) should retry next interval,
                    # not spin every tick until it succeeds -- once forced
                    # for today, the opportunistic path (still active for the
                    # rest of the day) covers a genuine follow-up failure.
                    self._last_forced_date = datetime.now(pytz.timezone("Asia/Kolkata")).date()
                else:
                    check_and_relogin()
            except Exception:
                logger.exception("Auto-login monitor: error in check loop")
            stop_event.wait(timeout=self._interval_seconds)


_monitor = BrokerAutoLoginMonitor()


def start_broker_auto_login() -> None:
    """Start the periodic auto-login monitor. Call once at app startup;
    a no-op if AUTO_LOGIN_ENABLED is not 'true'."""
    _monitor.start()


def get_broker_auto_login_monitor() -> BrokerAutoLoginMonitor:
    return _monitor


def _shutdown_at_exit() -> None:
    """Release the monitor thread on interpreter exit, mirroring
    flow_price_monitor_service.py / flow_order_update_monitor_service.py."""
    logging.disable(logging.CRITICAL)
    try:
        _monitor.stop()
    except Exception:
        pass


atexit.register(_shutdown_at_exit)
