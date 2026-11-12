#!/usr/bin/env python3
"""
Migration script to add instance-wide broker credential columns to the
existing settings table. These replace the .env-file-based BROKER_API_KEY /
BROKER_API_SECRET / BROKER_API_KEY_MARKET / BROKER_API_SECRET_MARKET /
REDIRECT_URL defaults (blueprints/broker_credentials.py) with DB-backed
storage that takes effect without a process restart.

Usage:
    cd upgrade
    python migrate_broker_settings.py
"""

import os
import sys

# Add parent directory to path to import modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text

# Load environment from parent directory
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(env_path)

# Import logger after environment is loaded
from utils.logging import get_logger

logger = get_logger(__name__)


def migrate_settings_table():
    """Add missing broker-credential columns to the settings table if they don't exist"""

    # Get database URL from environment
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///db/openalgo.db")

    # Adjust path for SQLite if relative (since we're in upgrade folder)
    if DATABASE_URL.startswith("sqlite:///") and not DATABASE_URL.startswith("sqlite:////"):
        db_path = DATABASE_URL.replace("sqlite:///", "")
        parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        full_db_path = os.path.join(parent_dir, db_path)
        DATABASE_URL = f"sqlite:///{full_db_path}"
        logger.info(f"Using database: {full_db_path}")

    try:
        engine = create_engine(DATABASE_URL)
        inspector = inspect(engine)

        if "settings" not in inspector.get_table_names():
            logger.info("Settings table doesn't exist. It will be created on first run.")
            return True

        existing_columns = [col["name"] for col in inspector.get_columns("settings")]
        logger.info(f"Existing columns in settings table: {existing_columns}")

        broker_columns = [
            ("broker_api_key_encrypted", "TEXT"),
            ("broker_api_secret_encrypted", "TEXT"),
            ("broker_api_key_market_encrypted", "TEXT"),
            ("broker_api_secret_market_encrypted", "TEXT"),
            ("broker_redirect_url", "VARCHAR(500)"),
        ]

        columns_added = 0
        columns_existing = 0

        with engine.connect() as conn:
            for column_name, column_def in broker_columns:
                if column_name not in existing_columns:
                    try:
                        alter_sql = text(
                            f"ALTER TABLE settings ADD COLUMN {column_name} {column_def}"
                        )
                        conn.execute(alter_sql)
                        conn.commit()
                        logger.info(f"Added column: {column_name}")
                        columns_added += 1
                    except Exception as col_error:
                        logger.warning(f"Could not add column {column_name}: {col_error}")
                else:
                    logger.info(f"Column already exists: {column_name}")
                    columns_existing += 1

        logger.info("\n Migration Summary:")
        logger.info(f"   - Columns added: {columns_added}")
        logger.info(f"   - Columns already existing: {columns_existing}")
        logger.info(f"   - Total broker columns: {len(broker_columns)}")

        if columns_added > 0:
            logger.info("\n Settings table migration completed successfully!")
            logger.info("   New broker-credential columns have been added to your database.")
        else:
            logger.info("\n No migration needed - all broker columns already exist!")

        return True

    except Exception as e:
        logger.error(f"Error during migration: {e}")
        return False


def main():
    """Main function to run the migration"""
    logger.info("=" * 60)
    logger.info("OpenAlgo Broker Settings Migration Script")
    logger.info("=" * 60)
    logger.info("This script adds instance-wide broker credential columns")
    logger.info("to the settings table, replacing .env-based BROKER_API_KEY/")
    logger.info("BROKER_API_SECRET/REDIRECT_URL storage.")
    logger.info("-" * 60)

    success = migrate_settings_table()

    logger.info("-" * 60)
    if success:
        logger.info("Migration process completed!")
        logger.info("\n Next Steps:")
        logger.info("   1. Restart your OpenAlgo application (one last time for this change)")
        logger.info("   2. Broker credentials can now be updated from Profile > Broker")
        logger.info("      without a further restart")
        return 0
    else:
        logger.error("Migration failed! Please check the error messages above.")
        logger.error("\n Troubleshooting:")
        logger.error("   1. Ensure the database file exists and is accessible")
        logger.error("   2. Check that you have write permissions to the database")
        logger.error("   3. Verify your DATABASE_URL in the .env file")
        return 1


if __name__ == "__main__":
    sys.exit(main())
