"""Shared SQLAlchemy engine factory.

Centralizes engine creation so every database engine in the project follows the
same connection-pooling policy. This exists to enforce one rule across the whole
codebase, including all broker ``master_contract_db.py`` modules and every
``database/*_db.py`` module (see CLAUDE.md and the SQLite -> CockroachDB
migration notes for why this centralization matters beyond just the policy
below: a second backend only has to change dialect logic in one place):

    All SQLite engines MUST use NullPool.

With NullPool each operation gets a fresh connection that is closed immediately
after use, so no file descriptors to the SQLite database file accumulate. A real
pool (the SQLAlchemy default QueuePool, or an explicit ``pool_size``/``max_overflow``)
holds connections open per worker thread and leaks descriptors over the lifetime
of the long-running Gunicorn/eventlet process.

StaticPool must NOT be used for SQLite: a single shared connection causes
"bad parameter or other API misuse" and "cannot commit - SQL statements in
progress" errors when concurrent requests corrupt the shared cursor state.

For non-SQLite backends (e.g. PostgreSQL) a normal connection pool is used.

SQLite pragmas (WAL journal mode, synchronous=NORMAL) are applied to every
connection process-wide by the listener in ``database/__init__.py`` — they do
not need to be set here.

See CLAUDE.md "SQLite Connection Pooling (NullPool)" and the reference
implementation in ``database/auth_db.py``.
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool


def create_db_engine(
    database_url=None,
    *,
    pool_size=50,
    max_overflow=100,
    pool_timeout=10,
    echo=False,
    sqlite_connect_args=None,
    pool_kwargs=None,
):
    """Create a SQLAlchemy engine with the project-wide pooling policy.

    Args:
        database_url: SQLAlchemy database URL. Falls back to the ``DATABASE_URL``
            environment variable when not provided.
        pool_size, max_overflow, pool_timeout: Non-SQLite pool sizing. Most
            callers use the defaults; a couple of modules (OAuth, sandbox) run
            smaller pools (``pool_size=20, max_overflow=40``) against the same
            engine — pass those explicitly rather than special-casing them here,
            so the NullPool-vs-pooled *decision* still lives in exactly one
            place even though the pool *size* varies per caller.
        echo: SQLAlchemy engine echo, applied to both branches. Default False
            matches ``create_engine``'s own default; only master_contract_status_db
            and user_db pass it explicitly today, and both pass False.
        sqlite_connect_args: Extra kwargs merged into the SQLite branch's
            ``connect_args`` (which always includes ``check_same_thread=False``).
            Used by master_contract_status_db for ``{"timeout": 30}``.
        pool_kwargs: Extra kwargs merged into the non-SQLite branch's
            ``create_engine`` call only -- e.g. telegram_db/whatsapp_db pass
            ``{"pool_pre_ping": True, "pool_recycle": 3600}`` for their
            long-lived bot-polling connections. Deliberately not applied to the
            SQLite branch: NullPool has no persistent connections to ping or
            recycle, and the original per-module code never passed these there.

    Returns:
        A configured SQLAlchemy ``Engine``. SQLite URLs use ``NullPool`` with
        ``check_same_thread=False``; other backends use a QueuePool.
    """
    database_url = database_url or os.getenv("DATABASE_URL")

    if database_url and "sqlite" in database_url:
        # SQLite: NullPool so each checkout creates a fresh connection that is
        # closed immediately. Session cleanup is handled by app.py
        # teardown_appcontext. StaticPool must NOT be used (see module docstring).
        connect_args = {"check_same_thread": False}
        if sqlite_connect_args:
            connect_args.update(sqlite_connect_args)
        return create_engine(
            database_url,
            poolclass=NullPool,
            connect_args=connect_args,
            echo=echo,
        )

    # Non-SQLite backends (e.g. PostgreSQL, CockroachDB): use a real connection pool.
    kwargs = {
        "pool_size": pool_size,
        "max_overflow": max_overflow,
        "pool_timeout": pool_timeout,
        "echo": echo,
    }
    if pool_kwargs:
        kwargs.update(pool_kwargs)
    return create_engine(database_url, **kwargs)
