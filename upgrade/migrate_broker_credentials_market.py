#!/usr/bin/env python
"""
Per-Account Market-Data Credentials Migration Script for OpenAlgo

Adds `auth.broker_api_key_market` and `auth.broker_api_secret_market`
(nullable, encrypted at rest): each connected broker account's own
market-data feed credential, for the XTS-family brokers that need a
separate quote/depth login (compositedge, rmoney, fivepaisaxts, ibulls,
iifl, jainamxts, wisdom) -- mirrors how per-account broker_api_key/
broker_api_secret already work for the trading credential (see
migrate_multi_account.py).

NULL means "no per-account market credential set yet" -- as of this
migration, utils.config.get_broker_api_key_market/get_broker_api_secret_market
still fall back to the instance-wide default when NULL, so this is purely
additive and safe to run without a data backfill: no existing behavior
changes until (a) each XTS-family broker's get_feed_token() is updated to
resolve per-account first, and (b) a separate backfill copies the current
instance-wide value into every existing account before the fallback itself
is removed. See the SaaS per-account-credentials plan for that sequencing.

Idempotent: safe to run multiple times and safe on a fresh install.

Usage:
    cd upgrade
    uv run migrate_broker_credentials_market.py           # Apply migration
    uv run migrate_broker_credentials_market.py --status  # Check status
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

MIGRATION_NAME = "broker_credentials_market"
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


NEW_COLUMNS = ("broker_api_key_market", "broker_api_secret_market")


def _existing_columns(engine) -> set:
    inspector = inspect(engine)
    return {col["name"] for col in inspector.get_columns("auth")}


def upgrade():
    """Apply the migration."""
    try:
        logger.info(f"Starting migration: {MIGRATION_NAME} (v{MIGRATION_VERSION})")
        engine = get_engine()
        existing = _existing_columns(engine)

        with engine.connect() as conn:
            for column in NEW_COLUMNS:
                if column in existing:
                    logger.info(f"Column already exists: auth.{column}")
                    continue
                conn.execute(text(f"ALTER TABLE auth ADD COLUMN {column} TEXT"))
                conn.commit()
                logger.info(f"Added column: auth.{column}")

        logger.info(f"Migration {MIGRATION_NAME} completed")
        return True
    except Exception as e:
        logger.exception(f"Migration failed: {e}")
        return False


def status():
    """Check migration status."""
    try:
        engine = get_engine()
        existing = _existing_columns(engine)
        missing = [c for c in NEW_COLUMNS if c not in existing]
        if not missing:
            logger.info(f"Migration {MIGRATION_NAME} already applied")
            return True
        logger.info(f"Missing auth columns: {', '.join(missing)}")
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
