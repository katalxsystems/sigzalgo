"""Two brokers' master contracts coexist in symtoken without clobbering each other.

The same OpenAlgo symbol maps to a different token/brsymbol per broker, so
every lookup must resolve against the right broker's rows, and one broker's
master contract download must only replace its own rows.
"""

import types

import pytest
from sqlalchemy.exc import StatementError

from database.broker_context import (
    BrokerContextMissing,
    broker_scope,
    invalidate_present_brokers,
    scoped_to_broker_arg,
)
from database.symbol import SymToken, db_session, init_db
from database import token_db_enhanced as token_db

BROKER_A = "testbrokera"
BROKER_B = "testbrokerb"
SYMBOL = "NIFTY28MAR2420800CE"
EXCHANGE = "NFO"

ROWS = {
    BROKER_A: {"token": "A-111", "brsymbol": "NIFTY-A-20800-CE"},
    BROKER_B: {"token": "B-999", "brsymbol": "NIFTY B 20800 CE"},
}


def _download(broker, token, brsymbol):
    """What every broker plugin's master_contract_download does: delete the
    table, then bulk insert -- here inside that broker's scope."""
    with broker_scope(broker):
        SymToken.query.delete()
        db_session.commit()
        db_session.bulk_insert_mappings(
            SymToken,
            [
                {
                    "symbol": SYMBOL,
                    "brsymbol": brsymbol,
                    "name": "NIFTY",
                    "exchange": EXCHANGE,
                    "brexchange": EXCHANGE,
                    "token": token,
                    "expiry": "28-MAR-24",
                    "strike": 20800.0,
                    "lotsize": 50,
                    "instrumenttype": "CE",
                    "tick_size": 0.05,
                }
            ],
        )
        db_session.commit()


def _all_rows():
    return (
        db_session.query(SymToken.broker, SymToken.token)
        .filter(SymToken.broker.in_([BROKER_A, BROKER_B]))
        .execution_options(all_brokers=True)
        .all()
    )


@pytest.fixture
def two_brokers():
    init_db()
    for broker, row in ROWS.items():
        _download(broker, row["token"], row["brsymbol"])
    invalidate_present_brokers()
    yield
    db_session.rollback()
    for broker in ROWS:
        with broker_scope(broker):
            SymToken.query.delete()
            db_session.commit()
        token_db.clear_cache(broker)
    db_session.remove()
    invalidate_present_brokers()


def test_rows_are_stamped_with_their_broker(two_brokers):
    assert sorted(_all_rows()) == [(BROKER_A, "A-111"), (BROKER_B, "B-999")]


def test_db_lookups_resolve_per_broker(two_brokers):
    for broker, row in ROWS.items():
        assert token_db.get_token_dbquery(SYMBOL, EXCHANGE, broker=broker) == row["token"]
        assert token_db.get_br_symbol_dbquery(SYMBOL, EXCHANGE, broker=broker) == row["brsymbol"]
        assert token_db.get_oa_symbol_dbquery(row["brsymbol"], EXCHANGE, broker=broker) == SYMBOL


def test_cache_lookups_resolve_per_broker(two_brokers):
    for broker in ROWS:
        assert token_db.load_cache_for_broker(broker)
    for broker, row in ROWS.items():
        assert token_db.get_cache(broker).cache_loaded
        assert token_db.get_token(SYMBOL, EXCHANGE, broker=broker) == row["token"]
        assert token_db.get_br_symbol(SYMBOL, EXCHANGE, broker=broker) == row["brsymbol"]


def test_redownload_replaces_only_that_brokers_rows(two_brokers):
    _download(BROKER_A, "A-222", "NIFTY-A-20800-CE")
    assert sorted(_all_rows()) == [(BROKER_A, "A-222"), (BROKER_B, "B-999")]


def test_reloading_one_cache_keeps_the_other(two_brokers):
    for broker in ROWS:
        token_db.load_cache_for_broker(broker)
    _download(BROKER_A, "A-222", "NIFTY-A-20800-CE")
    token_db.load_cache_for_broker(BROKER_A)
    assert token_db.get_token(SYMBOL, EXCHANGE, broker=BROKER_A) == "A-222"
    assert token_db.get_token(SYMBOL, EXCHANGE, broker=BROKER_B) == "B-999"


def test_calling_plugin_package_selects_the_broker(two_brokers):
    # Code under broker/<name>/ is identified from the call stack.
    module = types.ModuleType(f"broker.{BROKER_B}.api.data")
    exec(
        "from database.token_db import get_token\n"
        "def lookup(symbol, exchange):\n"
        "    return get_token(symbol, exchange)\n",
        module.__dict__,
    )
    assert module.lookup(SYMBOL, EXCHANGE) == "B-999"


def test_service_broker_argument_selects_the_broker(two_brokers):
    @scoped_to_broker_arg
    def service(symbol, exchange, broker):
        return token_db.get_token(symbol, exchange)

    assert service(SYMBOL, EXCHANGE, BROKER_A) == "A-111"
    assert service(SYMBOL, EXCHANGE, broker=BROKER_B) == "B-999"


def test_lookup_without_a_broker_raises_when_ambiguous(two_brokers):
    with pytest.raises(BrokerContextMissing):
        token_db.get_token(SYMBOL, EXCHANGE)


def test_insert_without_a_broker_raises_when_ambiguous(two_brokers):
    # Raised from the column default, so SQLAlchemy wraps it in StatementError.
    with pytest.raises(StatementError) as excinfo:
        db_session.bulk_insert_mappings(
            SymToken, [{"symbol": "X", "brsymbol": "X", "exchange": "NSE", "token": "1"}]
        )
    db_session.rollback()
    assert isinstance(excinfo.value.orig, BrokerContextMissing)


def test_master_contract_values_are_normalized_for_strict_backends(two_brokers):
    # A 5paisa MCX chunk mixed NaN names with text, numeric-text strikes and
    # integer tokens; CockroachDB rejects mixed-type VALUES columns.
    nan = float("nan")
    rows = [
        {"symbol": "ZINC23DEC26385PE", "brsymbol": "ZINC 385 PE", "name": "ZINC",
         "exchange": "MCX", "token": 584323, "strike": "385", "lotsize": 5.0},
        {"symbol": "ELECDMBL30DEC26FUT", "brsymbol": "ELECDMBL", "name": nan,
         "exchange": "MCX", "token": 584325.0, "strike": nan, "lotsize": nan},
    ]
    with broker_scope(BROKER_A):
        db_session.bulk_insert_mappings(SymToken, rows)
        db_session.commit()
        stored = {
            r.symbol: r
            for r in SymToken.query.filter(SymToken.exchange == "MCX").all()
        }
    zinc, elec = stored["ZINC23DEC26385PE"], stored["ELECDMBL30DEC26FUT"]
    assert (zinc.token, zinc.strike, zinc.lotsize) == ("584323", 385.0, 5)
    assert (elec.name, elec.token, elec.strike, elec.lotsize) == (None, "584325", None, None)
