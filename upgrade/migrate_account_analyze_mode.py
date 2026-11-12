#!/usr/bin/env python
"""
Per-Account Analyzer Mode Migration Script for OpenAlgo

Adds `auth.analyze_mode` (nullable boolean): each connected broker account's
own Analyzer/Sandbox override, independent of every other account on the
same instance. NULL means "this account has never set its own toggle, fall
back to the instance-wide database.settings_db.Settings.analyze_mode default"
-- see database.settings_db.get_analyze_mode()'s docstring for the full
resolution order (mirrors how per-account broker_api_key/broker_api_secret
already fall back to the instance-wide .env pair).

No backfill needed: NULL is exactly the right value for every existing row
(no account had its own override before this column existed), so this
migration is just the ALTER TABLE.

Idempotent: safe to run multiple times and safe on a fresh install.

Usage:
    cd upgrade
    uv run migrate_account_analyze_mode.py           # Apply migration
    uv run migrate_account_analyze_mode.py --status  # Check status
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

MIGRATION_NAME = "account_analyze_mode"
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


def _has_column(engine) -> bool:
    inspector = inspect(engine)
    existing_columns = {col["name"] for col in inspector.get_columns("auth")}
    return "analyze_mode" in existing_columns


def upgrade():
    """Apply the migration."""
    try:
        logger.info(f"Starting migration: {MIGRATION_NAME} (v{MIGRATION_VERSION})")
        engine = get_engine()

        if _has_column(engine):
            logger.info("Column already exists: auth.analyze_mode")
        else:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE auth ADD COLUMN analyze_mode BOOLEAN"))
                conn.commit()
            logger.info("Added column: auth.analyze_mode")

        logger.info(f"Migration {MIGRATION_NAME} completed")
        return True
    except Exception as e:
        logger.exception(f"Migration failed: {e}")
        return False


def status():
    """Check migration status."""
    try:
        engine = get_engine()
        if _has_column(engine):
            logger.info(f"Migration {MIGRATION_NAME} already applied")
            return True
        logger.info("Missing auth column: analyze_mode")
        logger.info("Migration needed")
        return False
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
