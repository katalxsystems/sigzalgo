#!/usr/bin/env python
"""
Multi-Broker-Account Migration Script for OpenAlgo

Adds support for one platform user owning multiple broker accounts
(including several accounts on the same broker):

1. `auth` table: adds owner_username, account_label, is_default,
   broker_api_key, broker_api_secret columns. Backfills
   owner_username = name and is_default = True for every existing row
   (pre-migration installs have exactly one account per username, which
   becomes that user's default account).
2. `api_keys` table: adds an account_id column (backfilled from the legacy
   user_id column, one key per existing account) and removes the old
   UNIQUE constraint on user_id — a platform user can now own many API
   keys, one per broker account, instead of exactly one.

Idempotent: safe to run multiple times and safe on a fresh install.

Usage:
    cd upgrade
    uv run migrate_multi_account.py           # Apply migration
    uv run migrate_multi_account.py --status  # Check status
"""

import argparse
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text

from utils.logging import get_logger

logger = get_logger(__name__)

MIGRATION_NAME = "multi_account_support"
MIGRATION_VERSION = "001"

parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(parent_dir, ".env"))


def get_engine():
    """Get main database engine"""
    database_url = os.getenv("DATABASE_URL", "sqlite:///db/openalgo.db")

    if database_url.startswith("sqlite:///"):
        db_path = database_url.replace("sqlite:///", "")
        if not os.path.isabs(db_path):
            db_path = os.path.join(parent_dir, db_path)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        database_url = f"sqlite:///{db_path}"

    return create_engine(database_url)


AUTH_COLUMNS = [
    ("owner_username", "VARCHAR(255)"),
    ("account_label", "VARCHAR(255)"),
    ("is_default", "BOOLEAN DEFAULT 0"),
    ("broker_api_key", "TEXT"),
    ("broker_api_secret", "TEXT"),
]


def _add_auth_columns(engine):
    inspector = inspect(engine)
    existing_columns = {col["name"] for col in inspector.get_columns("auth")}

    added = 0
    with engine.connect() as conn:
        for col_name, col_type in AUTH_COLUMNS:
            if col_name not in existing_columns:
                conn.execute(text(f"ALTER TABLE auth ADD COLUMN {col_name} {col_type}"))
                logger.info(f"Added column: auth.{col_name}")
                added += 1
            else:
                logger.info(f"Column already exists: auth.{col_name}")
        conn.commit()
    return added


def _backfill_auth(engine):
    with engine.connect() as conn:
        result = conn.execute(
            text("UPDATE auth SET owner_username = name WHERE owner_username IS NULL")
        )
        conn.execute(
            text(
                "UPDATE auth SET is_default = 1 "
                "WHERE (is_default IS NULL OR is_default = 0) "
                "AND owner_username = name"
            )
        )
        conn.commit()
        logger.info(f"Backfilled owner_username for {result.rowcount} existing auth row(s)")

    with engine.connect() as conn:
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS idx_auth_owner_username ON auth(owner_username)")
        )
        conn.commit()


def _api_keys_has_user_id_unique_constraint(engine, inspector):
    """Detect whether api_keys.user_id still carries its old UNIQUE constraint."""
    try:
        for uc in inspector.get_unique_constraints("api_keys"):
            if list(uc.get("column_names", [])) == ["user_id"]:
                return True
    except NotImplementedError:
        pass
    # SQLite often represents a Column(unique=True) as an autoindex rather
    # than a named unique constraint the inspector's get_unique_constraints
    # picks up — check indexes too.
    for idx in inspector.get_indexes("api_keys"):
        if idx.get("unique") and list(idx.get("column_names", [])) == ["user_id"]:
            return True
    return False


def _rebuild_api_keys_table_sqlite(engine):
    """SQLite can't drop a column constraint via ALTER TABLE — recreate the
    table without the UNIQUE(user_id) constraint, preserving all data."""
    logger.info("Rebuilding api_keys table to drop legacy UNIQUE(user_id) constraint...")
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE api_keys RENAME TO api_keys_old"))
        conn.execute(
            text("""
                CREATE TABLE api_keys (
                    id INTEGER NOT NULL PRIMARY KEY,
                    user_id VARCHAR NOT NULL,
                    account_id VARCHAR,
                    api_key_hash TEXT NOT NULL,
                    api_key_encrypted TEXT NOT NULL,
                    created_at DATETIME,
                    order_mode VARCHAR(20)
                )
            """)
        )
        conn.execute(
            text("""
                INSERT INTO api_keys
                    (id, user_id, account_id, api_key_hash, api_key_encrypted,
                     created_at, order_mode)
                SELECT id, user_id, account_id, api_key_hash, api_key_encrypted,
                       created_at, order_mode
                FROM api_keys_old
            """)
        )
        conn.execute(text("DROP TABLE api_keys_old"))
        conn.commit()
    logger.info("api_keys table rebuilt without UNIQUE(user_id)")


def _add_api_keys_columns(engine):
    inspector = inspect(engine)
    existing_columns = {col["name"] for col in inspector.get_columns("api_keys")}

    if "account_id" not in existing_columns:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE api_keys ADD COLUMN account_id VARCHAR"))
            conn.commit()
        logger.info("Added column: api_keys.account_id")
    else:
        logger.info("Column already exists: api_keys.account_id")

    with engine.connect() as conn:
        result = conn.execute(
            text("UPDATE api_keys SET account_id = user_id WHERE account_id IS NULL")
        )
        conn.commit()
        logger.info(f"Backfilled account_id for {result.rowcount} existing api_keys row(s)")

    inspector = inspect(engine)
    if engine.dialect.name == "sqlite" and _api_keys_has_user_id_unique_constraint(
        engine, inspector
    ):
        _rebuild_api_keys_table_sqlite(engine)
    elif engine.dialect.name != "sqlite" and _api_keys_has_user_id_unique_constraint(
        engine, inspector
    ):
        logger.warning(
            "api_keys.user_id still has a UNIQUE constraint and this is not SQLite "
            f"(dialect={engine.dialect.name}). Automatic constraint removal is not "
            "implemented for this database backend — drop it manually, e.g. "
            "Postgres: ALTER TABLE api_keys DROP CONSTRAINT <constraint_name>; "
            "MySQL: ALTER TABLE api_keys DROP INDEX <index_name>; "
            "otherwise a user's second broker account's API key upsert will fail."
        )

    with engine.connect() as conn:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_account_id "
                "ON api_keys(account_id)"
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS idx_api_keys_user_id ON api_keys(user_id)")
        )
        conn.commit()


def upgrade():
    """Apply the migration."""
    try:
        logger.info(f"Starting migration: {MIGRATION_NAME} (v{MIGRATION_VERSION})")
        engine = get_engine()

        _add_auth_columns(engine)
        _backfill_auth(engine)
        _add_api_keys_columns(engine)

        logger.info(f"Migration {MIGRATION_NAME} completed")
        return True
    except Exception as e:
        logger.exception(f"Migration failed: {e}")
        return False


def status():
    """Check migration status."""
    try:
        engine = get_engine()
        inspector = inspect(engine)

        auth_columns = {col["name"] for col in inspector.get_columns("auth")}
        missing_auth = [c for c, _ in AUTH_COLUMNS if c not in auth_columns]

        api_key_columns = {col["name"] for col in inspector.get_columns("api_keys")}
        missing_api_keys = [c for c in ("account_id",) if c not in api_key_columns]

        still_unique = _api_keys_has_user_id_unique_constraint(engine, inspector)

        if missing_auth or missing_api_keys or still_unique:
            if missing_auth:
                logger.info(f"Missing auth columns: {', '.join(missing_auth)}")
            if missing_api_keys:
                logger.info(f"Missing api_keys columns: {', '.join(missing_api_keys)}")
            if still_unique:
                logger.info("api_keys.user_id still has its legacy UNIQUE constraint")
            logger.info("Migration needed")
            return False

        logger.info(f"Migration {MIGRATION_NAME} already applied")
        return True
    except Exception as e:
        logger.exception(f"Status check failed: {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=f"Migration: {MIGRATION_NAME} (v{MIGRATION_VERSION})",
    )
    parser.add_argument("--status", action="store_true", help="Check migration status")

    args = parser.parse_args()

    success = status() if args.status else upgrade()

    sys.exit(0 if success else 1)
