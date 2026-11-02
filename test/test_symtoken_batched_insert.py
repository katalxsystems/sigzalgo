"""Master contract downloads must not hold openalgo.db's write lock throughout.

Each broker inserted its whole master contract (well over 100k rows for Angel)
in one transaction. symtoken shares openalgo.db with every other feature, and
SQLite has one write lock per database, so for the length of that insert every
other writer queued -- past the 15s retry budget in database/__init__.py, at
which point workflow runs failed with "database is locked" and the Flow
scheduler's thread died. Inserts now commit per batch, releasing the lock
between batches.
"""

import re
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.orm import scoped_session, sessionmaker

import database.symbol as symbol
from database.broker_context import broker_scope
from database.engine_factory import create_db_engine

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def symtoken_db(tmp_path, monkeypatch):
    """symbol.db_session bound to a scratch SQLite file holding symtoken."""
    path = tmp_path / "openalgo.db"
    engine = create_db_engine(f"sqlite:///{path}")
    symbol.SymToken.__table__.create(engine)
    session = scoped_session(sessionmaker(bind=engine))
    monkeypatch.setattr(symbol, "db_session", session)
    yield path, session
    session.remove()
    engine.dispose()


def _rows(n):
    return [
        {"symbol": f"S{i}", "brsymbol": f"S{i}-EQ", "exchange": "NSE", "token": str(i)}
        for i in range(n)
    ]


def test_another_writer_gets_the_lock_between_batches(symtoken_db, monkeypatch):
    path, session = symtoken_db
    monkeypatch.setattr(symbol, "SYMTOKEN_INSERT_BATCH", 2)

    # A plain connection that does not wait: it fails at once if the lock is held.
    outside = sqlite3.connect(path, timeout=0, factory=sqlite3.Connection)
    outside.execute("CREATE TABLE order_log (x)")
    outside.commit()

    real_insert = session.bulk_insert_mappings
    batches = []

    def insert_and_interleave(model, rows):
        if batches:  # between batch 1 and batch 2
            outside.execute("INSERT INTO order_log VALUES (1)")
            outside.commit()
        batches.append(len(rows))
        real_insert(model, rows)

    monkeypatch.setattr(session, "bulk_insert_mappings", insert_and_interleave)

    with broker_scope("testbroker"):
        symbol.insert_symtokens_in_batches(_rows(5))

    assert batches == [2, 2, 1]
    assert outside.execute("SELECT COUNT(*) FROM order_log").fetchone() == (2,)
    outside.close()

    verify = sqlite3.connect(path, factory=sqlite3.Connection)
    assert verify.execute("SELECT COUNT(*), MIN(broker) FROM symtoken").fetchone() == (
        5,
        "testbroker",
    )
    verify.close()


def test_nothing_to_insert_is_a_no_op(symtoken_db):
    with broker_scope("testbroker"):
        symbol.insert_symtokens_in_batches([])


# Brokers that insert some other way, each for a stated reason.
_OWN_INSERT_LOOP = {
    "deltaexchange",  # commits every 500 rows itself
    "indmoney",  # commits every 500 rows itself
    "groww",  # commits every BATCH_SIZE rows itself
    "hdfcsecurities",  # deliberately atomic: delete + insert roll back together
}


def test_no_broker_inserts_its_whole_master_in_one_transaction():
    """A broker added or merged from upstream with the old two-liner
    (bulk_insert_mappings of every row, then one commit) brings the lock back."""
    offenders = []
    for path in sorted(REPO.glob("broker/*/database/master_contract_db.py")):
        broker = path.parent.parent.name
        if broker in _OWN_INSERT_LOOP:
            continue
        source = path.read_text(encoding="utf-8")
        if re.search(r"bulk_insert_mappings\(SymToken", source):
            offenders.append(broker)
        if re.search(r"\.to_sql\(", source) and "SYMTOKEN_INSERT_BATCH" not in source:
            offenders.append(broker)

    assert offenders == [], f"use database.symbol.insert_symtokens_in_batches: {offenders}"
