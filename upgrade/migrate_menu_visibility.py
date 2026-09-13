#!/usr/bin/env python
"""
Profile Menu Visibility Migration Script for OpenAlgo

Adds `settings.hidden_menu_items_admin` and `settings.hidden_menu_items_nonadmin`
(nullable TEXT, JSON-encoded lists of hidden profile-menu item keys -- see
config/navigation.ts's profileMenuItems[].href, e.g. "/leverage"). Lets an
admin hide specific items from the header's profile dropdown, separately for
admin and non-admin sessions -- see database.settings_db.get_hidden_menu_items/
set_hidden_menu_items.

NULL means "nothing hidden" (the pre-migration default), so this is purely
additive and safe to run without a data backfill.

Idempotent: safe to run multiple times and safe on a fresh install.

Usage:
    cd upgrade
    uv run migrate_menu_visibility.py           # Apply migration
    uv run migrate_menu_visibility.py --status  # Check status
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

MIGRATION_NAME = "menu_visibility"
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


NEW_COLUMNS = ("hidden_menu_items_admin", "hidden_menu_items_nonadmin")


def _existing_columns(engine) -> set:
    inspector = inspect(engine)
    return {col["name"] for col in inspector.get_columns("settings")}


def upgrade():
    """Apply the migration."""
    try:
        logger.info(f"Starting migration: {MIGRATION_NAME} (v{MIGRATION_VERSION})")
        engine = get_engine()
        existing = _existing_columns(engine)

        with engine.connect() as conn:
            for column in NEW_COLUMNS:
                if column in existing:
                    logger.info(f"Column already exists: settings.{column}")
                    continue
                conn.execute(text(f"ALTER TABLE settings ADD COLUMN {column} TEXT"))
                conn.commit()
                logger.info(f"Added column: settings.{column}")

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
        logger.info(f"Missing settings columns: {', '.join(missing)}")
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
