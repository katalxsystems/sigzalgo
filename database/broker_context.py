"""Which broker's master contract (SymToken rows, symbol cache) a lookup uses.

The symtoken table holds every broker's contracts side by side, keyed by
``SymToken.broker``, because users on one instance may be on different
brokers. ``token``/``brsymbol``/``brexchange`` differ per broker, so every
lookup must resolve against the right broker's rows -- a wrong broker
silently maps a symbol to a different instrument.

``current_broker()`` resolves it, first match wins:

1. an explicit ``broker`` argument;
2. an enclosing ``broker_scope(broker)`` block -- background work with no
   request (master contract download, sandbox engine, schedulers);
3. the calling broker plugin: code under ``broker/<name>/`` is inherently
   single-broker, so the nearest ``broker.<name>.*`` frame on the stack
   names it (this covers plugin API, mapping and streaming code without
   threading a parameter through 200+ plugin files);
4. the current Flask request -- ``g.symtoken_broker``, set from the
   logged-in session or the API key (see database.auth_db.get_auth_token_broker);
5. the only broker present in symtoken, if there is exactly one (a
   single-broker instance behaves exactly as before).

When none applies and several brokers are present, callers raise
``BrokerContextMissing`` rather than guess.
"""

import functools
import inspect
import sys
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar

from utils.logging import get_logger

logger = get_logger(__name__)

_scoped_broker: ContextVar[str | None] = ContextVar("symtoken_broker", default=None)

# How far up the stack to look for a calling broker plugin. Plugin call
# chains are shallow; the bound keeps the walk cheap on hot lookup paths.
_MAX_FRAMES = 60


class BrokerContextMissing(RuntimeError):
    """Raised when a symbol lookup can't tell which broker it is for."""


@contextmanager
def broker_scope(broker: str | None):
    """Run the enclosed block against ``broker``'s master contract."""
    token = _scoped_broker.set(broker)
    try:
        yield
    finally:
        _scoped_broker.reset(token)


def _broker_from_stack() -> str | None:
    frame = sys._getframe(2)
    for _ in range(_MAX_FRAMES):
        if frame is None:
            return None
        module = frame.f_globals.get("__name__", "")
        if module.startswith("broker."):
            parts = module.split(".", 2)
            if len(parts) > 1 and parts[1]:
                return parts[1]
        frame = frame.f_back
    return None


def _broker_from_request() -> str | None:
    try:
        from flask import g, has_request_context

        if has_request_context():
            return getattr(g, "symtoken_broker", None)
    except Exception:
        return None
    return None


def set_request_broker(broker: str | None) -> None:
    """Record the broker of the account serving the current request."""
    if not broker:
        return
    try:
        from flask import g, has_request_context

        if has_request_context():
            g.symtoken_broker = broker
    except Exception:
        logger.exception("Could not record request broker")


# Distinct brokers in symtoken, refreshed at most every _PRESENT_TTL seconds
# and immediately after a download (invalidate_present_brokers).
_PRESENT_TTL = 60.0
_present_lock = threading.Lock()
_present_cache: tuple[float, frozenset[str]] | None = None


def present_brokers() -> frozenset[str]:
    """Brokers that currently have rows in symtoken."""
    global _present_cache
    now = time.monotonic()
    cached = _present_cache
    if cached and now - cached[0] < _PRESENT_TTL:
        return cached[1]
    with _present_lock:
        cached = _present_cache
        if cached and time.monotonic() - cached[0] < _PRESENT_TTL:
            return cached[1]
        try:
            # Core on its own short-lived connection: this can run from inside
            # the ORM scoping hook while another query is mid-flight, so it
            # must neither reuse nor remove() the scoped session.
            from sqlalchemy import select

            from database.symbol import SymToken, engine

            with engine.connect() as conn:
                rows = conn.execute(select(SymToken.broker).distinct()).all()
            brokers = frozenset(r[0] for r in rows if r[0])
        except Exception:
            logger.exception("Could not read brokers present in symtoken")
            brokers = cached[1] if cached else frozenset()
        _present_cache = (time.monotonic(), brokers)
        return brokers


def invalidate_present_brokers() -> None:
    global _present_cache
    _present_cache = None


def current_broker(broker: str | None = None) -> str | None:
    """Resolve the broker a symbol lookup is for (see module docstring).

    Returns None when it can't be determined; the single-broker fallback
    means that only happens with zero or several brokers present.
    """
    if broker:
        return broker
    scoped = _scoped_broker.get()
    if scoped:
        return scoped
    from_stack = _broker_from_stack()
    if from_stack:
        return from_stack
    from_request = _broker_from_request()
    if from_request:
        return from_request
    present = present_brokers()
    if len(present) == 1:
        return next(iter(present))
    return None


def require_broker(broker: str | None = None) -> str:
    """current_broker(), raising BrokerContextMissing when it is ambiguous."""
    resolved = current_broker(broker)
    if resolved:
        return resolved
    present = sorted(present_brokers())
    where = (
        f"this instance holds several brokers' master contracts ({', '.join(present)})"
        if present
        else "no broker's master contract is loaded yet"
    )
    raise BrokerContextMissing(
        f"Can't tell which broker this symbol lookup or insert is for: {where}. "
        "Pass broker=..., or run it inside broker_scope(broker)."
    )


def scoped_to_broker_arg(func):
    """Decorator for service functions that take the resolved broker as a
    ``broker`` or ``broker_name`` argument (after get_auth_token_broker):
    run the call inside that broker's scope. This keeps their symbol lookups
    right outside a request too -- the sandbox engine and schedulers call
    these services from background threads.
    """
    signature = inspect.signature(func)
    param = "broker" if "broker" in signature.parameters else "broker_name"

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            broker = signature.bind_partial(*args, **kwargs).arguments.get(param)
        except TypeError:
            broker = None
        if not isinstance(broker, str) or not broker:
            return func(*args, **kwargs)
        with broker_scope(broker):
            return func(*args, **kwargs)

    return wrapper


def scoped_to_account_broker(cls):
    """Class decorator: run every public method of a per-account class
    (``self.user_id`` is the account id) inside that account's broker scope.

    For the sandbox managers, which do symbol lookups both in requests and in
    background threads (execution engine, square-off, catch-up) where nothing
    else says which broker the account is on.
    """
    for name, attr in list(vars(cls).items()):
        if name.startswith("__") or not inspect.isfunction(attr):
            continue
        setattr(cls, name, _account_scoped(attr))
    return cls


_UNSET = object()


def _account_scoped(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        broker = getattr(self, "_symtoken_broker", _UNSET)
        if broker is _UNSET:
            broker = broker_for_account(getattr(self, "user_id", None))
            self._symtoken_broker = broker
        if not broker:
            return method(self, *args, **kwargs)
        with broker_scope(broker):
            return method(self, *args, **kwargs)

    return wrapper


def scoped_to_order_account(method):
    """Method decorator for ``method(self, order, ...)``: run it inside the
    broker scope of ``order.user_id``'s account."""

    @functools.wraps(method)
    def wrapper(self, order, *args, **kwargs):
        broker = broker_for_account(getattr(order, "user_id", None))
        if not broker:
            return method(self, order, *args, **kwargs)
        with broker_scope(broker):
            return method(self, order, *args, **kwargs)

    return wrapper


# account_id -> (monotonic time, broker). Sandbox execution resolves this per
# order on every tick, so it is cached briefly. Bounded by the number of
# accounts on the instance.
_ACCOUNT_BROKER_TTL = 60.0
_account_broker_cache: dict[str, tuple[float, str | None]] = {}


def broker_for_account(account_id: str | None) -> str | None:
    """The broker of a connected account (Auth.name == account_id)."""
    if not account_id:
        return None
    cached = _account_broker_cache.get(account_id)
    if cached and time.monotonic() - cached[0] < _ACCOUNT_BROKER_TTL:
        return cached[1]
    try:
        from database.auth_db import Auth

        row = Auth.query.filter_by(name=account_id).first()
        broker = row.broker if row and row.broker else None
    except Exception:
        logger.exception(f"Could not resolve broker for account {account_id!r}")
        return None
    _account_broker_cache[account_id] = (time.monotonic(), broker)
    return broker
