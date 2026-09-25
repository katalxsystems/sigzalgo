import math
import os

from sqlalchemy import (
    Column,
    Float,
    Index,
    Integer,
    String,
    and_,
    event,
    inspect,
    or_,
    text,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session, scoped_session, sessionmaker, with_loader_criteria
from sqlalchemy.types import TypeDecorator

from database.broker_context import (
    BrokerContextMissing,
    current_broker,
    invalidate_present_brokers,
    present_brokers,
    require_broker,
)
from database.engine_factory import create_db_engine
from utils.logging import get_logger

logger = get_logger(__name__)


def _escape_like(term: str) -> str:
    """Escape LIKE wildcard characters to prevent unintended broad matching."""
    return term.replace("%", r"\%").replace("_", r"\_")

DATABASE_URL = os.getenv("DATABASE_URL")
# The one engine/session/model for symtoken. Every broker plugin's
# master_contract_db imports these instead of declaring its own copy, so the
# broker scoping below applies to all of them.
engine = create_db_engine(DATABASE_URL)
db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()


def _missing(value):
    """None, NaN (pandas' missing value) or an empty string."""
    return value is None or (isinstance(value, float) and math.isnan(value)) or value == ""


class _TextValue(TypeDecorator):
    """String column that accepts what broker master-contract DataFrames
    produce: NaN becomes NULL and numbers become text (584323 -> "584323",
    584323.0 -> "584323").

    CockroachDB/Postgres reject a multi-row INSERT whose VALUES mix types in
    one column ("VALUES types float and string cannot be matched"); SQLite
    silently accepted it, so these mixes went unnoticed.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return value if isinstance(value, str) else str(value)


class _FloatValue(TypeDecorator):
    """Float column: NaN/empty -> NULL, numeric text ("385") -> float."""

    impl = Float
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if _missing(value):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


class _IntValue(TypeDecorator):
    """Integer column: NaN/empty -> NULL, 5.0 / "5" -> 5."""

    impl = Integer
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if _missing(value):
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _broker_for_insert():
    # Stamped on every inserted row: the broker whose master contract is
    # being downloaded (broker_scope in utils.auth_utils, or the calling
    # plugin's own package).
    return require_broker()


class SymToken(Base):
    __tablename__ = "symtoken"
    id = Column(Integer, primary_key=True)
    # Which broker's master contract this row belongs to. token/brsymbol/
    # brexchange are broker-specific, so several brokers' rows coexist and
    # every query is scoped to one broker (see _scope_symtoken_to_broker).
    broker = Column(String(32), nullable=False, default=_broker_for_insert)
    # Value-normalizing types: see _TextValue.
    symbol = Column(_TextValue, nullable=False, index=True)
    brsymbol = Column(_TextValue, nullable=False, index=True)
    name = Column(_TextValue)
    exchange = Column(_TextValue, index=True)
    brexchange = Column(_TextValue, index=True)
    token = Column(_TextValue, index=True)
    expiry = Column(_TextValue)
    strike = Column(_FloatValue)
    lotsize = Column(_IntValue)
    instrumenttype = Column(_TextValue)
    tick_size = Column(_FloatValue)
    contract_value = Column(_FloatValue)

    # Composite indices for improved search performance
    __table_args__ = (
        Index("idx_symbol_exchange", "symbol", "exchange"),
        Index("idx_symbol_name", "symbol", "name"),
        Index("idx_brsymbol_exchange", "brsymbol", "exchange"),
        Index("idx_symtoken_broker_symbol_exchange", "broker", "symbol", "exchange"),
        Index("idx_symtoken_broker_token_exchange", "broker", "token", "exchange"),
        Index("idx_symtoken_broker_brsymbol_exchange", "broker", "brsymbol", "exchange"),
    )


_SYMTOKEN_MAPPER = SymToken.__mapper__


@event.listens_for(Session, "do_orm_execute")
def _scope_symtoken_to_broker(state):
    """Add ``WHERE symtoken.broker = <current broker>`` to every ORM SELECT,
    UPDATE and DELETE that touches SymToken, from any session.

    This is what keeps each broker plugin's existing download code
    (``SymToken.query.delete()``, its "skip tokens that already exist"
    check) and every lookup confined to one broker's rows. Opt out with
    ``.execution_options(all_brokers=True)`` for deliberate cross-broker
    work (migrations, per-broker counts).
    """
    if not (state.is_select or state.is_update or state.is_delete):
        return
    if state.is_column_load or state.is_relationship_load:
        return
    if state.execution_options.get("all_brokers"):
        return
    try:
        if _SYMTOKEN_MAPPER not in state.all_mappers:
            return
    except Exception:
        return
    broker = current_broker()
    if broker is None:
        if len(present_brokers()) > 1:
            raise BrokerContextMissing(
                "SymToken query without a broker on an instance holding several "
                "brokers' master contracts. Pass broker=..., or run it inside "
                "broker_scope(broker)."
            )
        return
    state.statement = state.statement.options(
        with_loader_criteria(SymToken, lambda cls: cls.broker == broker, include_aliases=True)
    )


def enhanced_search_symbols(
    query: str | None, exchange: str | None = None, limit: int | None = None
) -> list[SymToken]:
    """
    Enhanced search function that searches across multiple fields
    and supports partial matching with multiple terms.

    If both query and exchange are empty, returns no results to avoid full-table scans.
    If query is empty/None but exchange is provided, returns all rows for that exchange
    (subject to limit) — useful for "show me everything in NSE" workflows.

    Args:
        query: Search query string (may be None/empty for exchange-only search)
        exchange: Exchange to filter by
        limit: Optional cap on number of results (None = no cap)

    Returns:
        List[SymToken]: List of matching SymToken objects
    """
    try:
        # Split the query into terms and clean them
        terms = [term.strip().upper() for term in (query or "").split() if term.strip()]

        # Refuse to scan the full table without any filter — caller must scope by exchange
        # if there is no query.
        if not terms and not exchange:
            return []

        # Base query
        base_query = SymToken.query

        # If exchange is specified, filter by it
        if exchange:
            base_query = base_query.filter(SymToken.exchange == exchange)

        # Create conditions for each term
        all_conditions = []
        for term in terms:
            safe_term = _escape_like(term)
            # Number detection for more accurate strike price and token searches
            try:
                num_term = float(term)
                term_conditions = or_(
                    SymToken.symbol.ilike(f"%{safe_term}%", escape="\\"),
                    SymToken.brsymbol.ilike(f"%{safe_term}%", escape="\\"),
                    SymToken.name.ilike(f"%{safe_term}%", escape="\\"),
                    SymToken.token.ilike(f"%{safe_term}%", escape="\\"),
                    SymToken.strike == num_term,
                )
            except ValueError:
                term_conditions = or_(
                    SymToken.symbol.ilike(f"%{safe_term}%", escape="\\"),
                    SymToken.brsymbol.ilike(f"%{safe_term}%", escape="\\"),
                    SymToken.name.ilike(f"%{safe_term}%", escape="\\"),
                    SymToken.token.ilike(f"%{safe_term}%", escape="\\"),
                )
            all_conditions.append(term_conditions)

        # Combine all conditions with AND
        if all_conditions:
            final_query = base_query.filter(and_(*all_conditions))
        else:
            final_query = base_query

        # Execute query — apply limit if caller specified one
        if limit is not None and limit > 0:
            results = final_query.limit(limit).all()
        else:
            results = final_query.all()
        return results

    except BrokerContextMissing:
        raise
    except Exception as e:
        logger.exception(f"Error in enhanced search: {str(e)}")
        return []


def fno_search_symbols_db(
    query: str = None,
    exchange: str = None,
    expiry: str = None,
    instrumenttype: str = None,  # "FUT", "CE", or "PE"
    strike_min: float = None,
    strike_max: float = None,
    underlying: str = None,
    limit: int = 10000,
) -> list[dict]:
    """
    FNO-specific search function using direct database queries.
    This is the fallback when cache is not available.

    Can search with just filters (no query required) - useful for:
    - "Show all NIFTY futures" (underlying=NIFTY, instrumenttype=FUT)
    - "Show all weekly expiry options" (expiry=26-DEC-24)

    Args:
        query (str, optional): Search query string (optional if filters are provided)
        exchange (str, optional): Exchange to filter by (NFO, BFO, MCX, CDS)
        expiry (str, optional): Expiry date filter (e.g., "26-DEC-24")
        instrumenttype (str, optional): "FUT" for futures, "CE" for calls, "PE" for puts
        strike_min (float, optional): Minimum strike price
        strike_max (float, optional): Maximum strike price
        underlying (str, optional): Underlying symbol name (e.g., "NIFTY")
        limit (int, optional): Maximum results to return (default 500)

    Returns:
        List[dict]: List of matching symbol dictionaries
    """
    try:
        # Base query
        base_query = SymToken.query

        # Filter by exchange
        if exchange:
            base_query = base_query.filter(SymToken.exchange == exchange)

        # Filter by underlying name
        if underlying:
            base_query = base_query.filter(SymToken.name.ilike(underlying.strip().upper()))

        # Filter by expiry date
        if expiry:
            base_query = base_query.filter(SymToken.expiry == expiry.strip())

        # Filter by instrument type (FUT, CE, PE) - based on symbol suffix
        if instrumenttype:
            inst_type = instrumenttype.strip().upper()
            if inst_type == "FUT":
                # Symbol ends with FUT (e.g., NIFTY26DEC24FUT)
                base_query = base_query.filter(SymToken.symbol.ilike("%FUT"))
            elif inst_type == "CE":
                # Symbol ends with CE (e.g., NIFTY26DEC2424000CE)
                base_query = base_query.filter(SymToken.symbol.ilike("%CE"))
            elif inst_type == "PE":
                # Symbol ends with PE (e.g., NIFTY26DEC2424000PE)
                base_query = base_query.filter(SymToken.symbol.ilike("%PE"))

        # Filter by strike price range (for options)
        if strike_min is not None:
            base_query = base_query.filter(SymToken.strike >= strike_min)
        if strike_max is not None:
            base_query = base_query.filter(SymToken.strike <= strike_max)

        # Create conditions for each search term (if query provided)
        primary_term = None
        if query:
            terms = [term.strip().upper() for term in query.split() if term.strip()]
            primary_term = terms[0] if terms else None
            all_conditions = []
            for term in terms:
                safe_term = _escape_like(term)
                try:
                    num_term = float(term)
                    term_conditions = or_(
                        SymToken.symbol.ilike(f"%{safe_term}%", escape="\\"),
                        SymToken.brsymbol.ilike(f"%{safe_term}%", escape="\\"),
                        SymToken.name.ilike(f"%{safe_term}%", escape="\\"),
                        SymToken.token.ilike(f"%{safe_term}%", escape="\\"),
                        SymToken.strike == num_term,
                    )
                except ValueError:
                    term_conditions = or_(
                        SymToken.symbol.ilike(f"%{safe_term}%", escape="\\"),
                        SymToken.brsymbol.ilike(f"%{safe_term}%", escape="\\"),
                        SymToken.name.ilike(f"%{safe_term}%", escape="\\"),
                        SymToken.token.ilike(f"%{safe_term}%", escape="\\"),
                    )
                all_conditions.append(term_conditions)

            # Combine all conditions with AND
            if all_conditions:
                base_query = base_query.filter(and_(*all_conditions))

        # Apply database-level ordering and limit for performance
        # Order by symbol for consistent results
        base_query = base_query.order_by(SymToken.symbol)

        # Apply limit at database level - critical for performance with large datasets
        if limit:
            base_query = base_query.limit(limit)

        # Execute query with limit already applied
        results = base_query.all()

        # Import freeze qty function (uses in-memory cache, fast)
        from database.qty_freeze_db import get_freeze_qty_for_option

        # Convert to dictionaries - now only processing limited results
        results_dicts = [
            {
                "symbol": r.symbol,
                "brsymbol": r.brsymbol,
                "name": r.name,
                "exchange": r.exchange,
                "brexchange": r.brexchange,
                "token": r.token,
                "expiry": r.expiry,
                "strike": r.strike,
                "lotsize": r.lotsize,
                "instrumenttype": r.instrumenttype,
                "tick_size": r.tick_size,
                "freeze_qty": get_freeze_qty_for_option(r.symbol, r.exchange),
            }
            for r in results
        ]

        # Only apply Python-level sorting if there's a search query
        # For filter-only queries (FNO chain discovery), DB ordering is sufficient
        if primary_term:

            def sort_key(r):
                """Sort results by relevance: exact match, prefix match, then alphabetical."""
                name = r["name"] or ""
                symbol = r["symbol"] or ""
                # Priority 1: Exact match on name/underlying
                name_exact = 0 if name.upper() == primary_term else 1
                # Priority 2: Name starts with search term
                name_starts = 0 if name.upper().startswith(primary_term) else 1
                # Priority 3: Symbol starts with search term
                symbol_starts = 0 if symbol.upper().startswith(primary_term) else 1
                # Priority 4: Alphabetical by symbol
                return (name_exact, name_starts, symbol_starts, symbol)

            results_dicts.sort(key=sort_key)

        return results_dicts

    except BrokerContextMissing:
        raise
    except Exception as e:
        logger.exception(f"Error in FNO search: {str(e)}")
        return []


def get_distinct_expiries(exchange: str = None, underlying: str = None) -> list[str]:
    """
    Get distinct expiry dates for FNO symbols.

    Args:
        exchange (str, optional): Exchange to filter by (NFO, BFO, MCX, CDS)
        underlying (str, optional): Underlying symbol name (e.g., "NIFTY")

    Returns:
        List[str]: List of distinct expiry dates sorted chronologically
    """
    try:
        from datetime import datetime

        from sqlalchemy import distinct

        query = db_session.query(distinct(SymToken.expiry))

        if exchange:
            query = query.filter(SymToken.exchange == exchange)

        if underlying:
            query = query.filter(SymToken.name.ilike(underlying.strip().upper()))

        # Only get non-null expiries
        query = query.filter(SymToken.expiry.isnot(None))
        query = query.filter(SymToken.expiry != "")

        results = query.all()
        expiries = [r[0] for r in results if r[0]]

        # Sort expiries chronologically
        def parse_expiry(exp_str):
            """Parse an expiry date string into a datetime for chronological sorting."""
            try:
                return datetime.strptime(exp_str, "%d-%b-%y")
            except ValueError:
                try:
                    return datetime.strptime(exp_str, "%d-%b-%Y")
                except ValueError:
                    return datetime.max

        expiries.sort(key=parse_expiry)
        return expiries

    except BrokerContextMissing:
        raise
    except Exception as e:
        logger.exception(f"Error fetching distinct expiries: {str(e)}")
        return []


def get_distinct_underlyings(exchange: str = None) -> list[str]:
    """
    Get distinct underlying names for FNO symbols.

    Args:
        exchange (str, optional): Exchange to filter by (NFO, BFO, MCX, CDS)

    Returns:
        List[str]: List of distinct underlying names sorted alphabetically
    """
    try:
        from sqlalchemy import distinct

        query = db_session.query(distinct(SymToken.name))

        if exchange:
            query = query.filter(SymToken.exchange == exchange)

        # Only get non-null names
        query = query.filter(SymToken.name.isnot(None))
        query = query.filter(SymToken.name != "")

        results = query.all()
        underlyings = sorted([r[0] for r in results if r[0]])
        return underlyings

    except BrokerContextMissing:
        raise
    except Exception as e:
        logger.exception(f"Error fetching distinct underlyings: {str(e)}")
        return []


def init_db():
    """Initialize the master contract database tables.

    Creates the ``symtoken`` table if it does not already exist,
    using the shared ``db_init_helper`` for consistent startup logging,
    then brings an existing table up to the per-broker schema.
    """
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "Master Contract DB", logger)
    ensure_broker_column()


def ensure_broker_column() -> bool:
    """Idempotently migrate an existing symtoken table to per-broker rows.

    Adds the ``broker`` column, backfills it with the broker whose contract
    the table currently holds (the most recent successful download -- before
    this change the table only ever held one broker), and creates the
    composite indexes. Rows whose broker can't be determined are deleted;
    that broker's next login re-downloads them. Also run by
    upgrade/migrate_symtoken_broker.py.
    """
    try:
        insp = inspect(engine)
        if "symtoken" not in insp.get_table_names():
            return True
        columns = {c["name"] for c in insp.get_columns("symtoken")}
        if "broker" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE symtoken ADD COLUMN broker VARCHAR(32)"))
            logger.info("Added symtoken.broker column")

        with engine.begin() as conn:
            unassigned = conn.execute(
                text("SELECT COUNT(*) FROM symtoken WHERE broker IS NULL")
            ).scalar()
        if unassigned:
            from database.master_contract_status_db import get_last_downloaded_broker

            owner = get_last_downloaded_broker()
            # In id chunks, each its own transaction: one statement over a
            # 100k+ row table exceeds CockroachDB's per-transaction lock budget
            # (ConfigurationLimitExceeded: "locking too many rows"), the same
            # limit the plugins' chunked deletes work around.
            chunk_size = 2000
            while True:
                with engine.begin() as conn:
                    ids = [
                        row[0]
                        for row in conn.execute(
                            text("SELECT id FROM symtoken WHERE broker IS NULL LIMIT :n"),
                            {"n": chunk_size},
                        )
                    ]
                    if not ids:
                        break
                    if owner:
                        conn.execute(
                            SymToken.__table__.update()
                            .where(SymToken.__table__.c.id.in_(ids))
                            .values(broker=owner)
                        )
                    else:
                        conn.execute(
                            SymToken.__table__.delete().where(SymToken.__table__.c.id.in_(ids))
                        )
            if owner:
                logger.info(f"Assigned {unassigned} existing symtoken rows to broker {owner!r}")
            else:
                logger.warning(
                    f"Deleted {unassigned} symtoken rows with no known broker; "
                    "they are re-downloaded at that broker's next login"
                )

        for index in SymToken.__table__.indexes:
            if index.name and index.name.startswith("idx_symtoken_broker_"):
                index.create(bind=engine, checkfirst=True)
        invalidate_present_brokers()
        return True
    except Exception:
        logger.exception("Error migrating symtoken to per-broker rows")
        return False
