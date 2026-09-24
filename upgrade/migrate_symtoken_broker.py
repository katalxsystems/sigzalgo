#!/usr/bin/env python3
"""
Migration script to give the symtoken table a broker column.

symtoken used to hold one broker's master contract at a time, so users on
different brokers wiped each other's symbol data at every login. Rows are
now keyed by broker (SymToken.broker) and every query is scoped to one
broker (database/broker_context.py).

Changes:
- symtoken.broker column (VARCHAR(32))
- existing rows assigned to the broker that last downloaded successfully
  (the only broker the table could hold before); rows with no known broker
  are deleted and re-downloaded at that broker's next login
- composite indexes (broker, symbol, exchange), (broker, token, exchange),
  (broker, brsymbol, exchange)

Idempotent; also runs at startup via database.symbol.init_db().

Usage:
    cd upgrade
    uv run migrate_symtoken_broker.py
"""

import os
import sys

# Add parent directory to path to import modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

# Load environment from parent directory
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(env_path)

# Resolve a relative SQLite path against the project root (we run from upgrade/)
# before database.symbol builds its engine from DATABASE_URL.
_database_url = os.getenv("DATABASE_URL", "sqlite:///db/openalgo.db")
if _database_url.startswith("sqlite:///") and not _database_url.startswith("sqlite:////"):
    _parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _db_path = _database_url.replace("sqlite:///", "")
    if not os.path.isabs(_db_path):
        os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_parent_dir, _db_path)}"

# Import logger after environment is loaded
from utils.logging import get_logger

logger = get_logger(__name__)


def main():
    logger.info("Adding broker column to symtoken (per-broker master contracts)")
    logger.info("-" * 60)

    from database.symbol import ensure_broker_column

    success = ensure_broker_column()

    logger.info("-" * 60)
    if success:
        logger.info("Migration process completed!")
        return 0
    logger.error("Migration failed! Check error messages above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
