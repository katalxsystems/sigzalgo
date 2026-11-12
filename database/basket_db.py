# database/basket_db.py
"""
Persistence for saved Portfolio Baskets.

A basket is a named, versioned allocation: the initial composition is version
1, and each manual rebalance a user records -- add a stock, drop one, reweight
-- becomes a new version with its own effective date. Unlike the stateless
Portfolio Backtester (one POST, one analysis, nothing kept), a basket is meant
to be revisited and built up over months, so it lives here rather than in the
browser.

Mirrors database/watchlist_db.py:
- SQLite via database.engine_factory.create_db_engine() (NullPool, project-wide
  pooling policy -- see CLAUDE.md)
- Rows scoped by ``user_id``, the *platform* username (Auth.owner_username),
  not a broker account_id -- a basket is shared across every broker account
  the user owns, the same way a watchlist is.
"""

import json
from datetime import date, datetime

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, scoped_session, sessionmaker

from database.engine_factory import create_db_engine
from utils.logging import get_logger

logger = get_logger(__name__)

engine = create_db_engine()
db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()

#: A basket is a deliberate allocation, not a symbol dump -- same reasoning as
#: MAX_SYMBOLS in services/portfolio_service.py, which this cap matches.
MAX_SYMBOLS_PER_VERSION = 50

#: Enough baskets for several strategies without becoming a second watchlist.
MAX_BASKETS_PER_USER = 50

#: A basket rebalanced weekly for four years is still under this.
MAX_VERSIONS_PER_BASKET = 200

_EPSILON = 1e-9


class PortfolioBasket(Base):
    """One named, saved basket."""

    __tablename__ = "portfolio_basket"

    id = Column(Integer, primary_key=True)
    user_id = Column(String(80), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    benchmark = Column(String(40), nullable=True)
    benchmark_exchange = Column(String(20), nullable=True)
    initial_capital = Column(Float, nullable=False, default=100_000.0)
    #: JSON: cost_model, brokerage_pct, cost_exchange, charges, gst_rate,
    #: cost_bps, slippage -- the same override shape PortfolioBacktestSchema
    #: already accepts, kept opaque here so this table does not need a column
    #: for every field the cost model ever grows.
    cost_config_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    versions = relationship(
        "PortfolioBasketVersion",
        back_populates="basket",
        cascade="all, delete-orphan",
        order_by="PortfolioBasketVersion.version_number",
    )

    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_basket_user_name"),)


class PortfolioBasketVersion(Base):
    """One rebalance: the basket's composition from ``effective_date`` on."""

    __tablename__ = "portfolio_basket_version"

    id = Column(Integer, primary_key=True)
    basket_id = Column(
        Integer,
        ForeignKey("portfolio_basket.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_number = Column(Integer, nullable=False)
    effective_date = Column(Date, nullable=False)
    #: JSON list[{"symbol", "exchange", "weight"}] -- symbols held from this
    #: date on. A symbol from the previous version simply absent here has
    #: been sold out entirely; see portfolio.engine.normalise_schedule_weights.
    holdings_json = Column(Text, nullable=False)
    #: JSON {"added": [...], "removed": [...], "reweighted": [{"symbol","from","to"}]},
    #: computed once at write time against the previous version and stored --
    #: not recomputed on every read, and it survives even if an earlier
    #: version is ever pruned.
    change_summary_json = Column(Text, nullable=False, default="{}")
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    basket = relationship("PortfolioBasket", back_populates="versions")

    __table_args__ = (
        UniqueConstraint("basket_id", "version_number", name="uq_basket_version_number"),
    )


def init_db():
    """Create the basket tables."""
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "Portfolio Basket DB", logger)


def ensure_basket_tables_exists():
    """Alias to match the app.py init pattern."""
    init_db()


def _normalise_holdings(holdings: list[dict]) -> list[dict]:
    """Validate and clean a holdings list into {"symbol","exchange","weight"} dicts."""
    out = []
    for h in holdings or []:
        symbol = str(h.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        exchange = str(h.get("exchange", "NSE")).strip().upper()
        weight = float(h.get("weight", 0) or 0)
        out.append({"symbol": symbol, "exchange": exchange, "weight": weight})
    return out


def _diff_holdings(previous: list[dict], current: list[dict]) -> dict:
    """
    Added / removed / reweighted, comparing two holdings lists by symbol.

    A symbol's weight is compared as given (percentage or fraction, whichever
    the caller used consistently) -- this is a display diff, not a normalised
    one, so it shows the user's own numbers back to them.
    """
    prev_by_symbol = {h["symbol"]: h["weight"] for h in previous}
    curr_by_symbol = {h["symbol"]: h["weight"] for h in current}

    added = sorted(s for s in curr_by_symbol if s not in prev_by_symbol)
    removed = sorted(s for s in prev_by_symbol if s not in curr_by_symbol)
    reweighted = [
        {"symbol": s, "from": prev_by_symbol[s], "to": curr_by_symbol[s]}
        for s in sorted(curr_by_symbol)
        if s in prev_by_symbol and abs(curr_by_symbol[s] - prev_by_symbol[s]) > _EPSILON
    ]
    return {"added": added, "removed": removed, "reweighted": reweighted}


def _serialize_version(version: PortfolioBasketVersion) -> dict:
    try:
        holdings = json.loads(version.holdings_json)
    except json.JSONDecodeError:
        holdings = []
    try:
        change_summary = json.loads(version.change_summary_json)
    except json.JSONDecodeError:
        change_summary = {"added": [], "removed": [], "reweighted": []}
    return {
        "id": version.id,
        "version_number": version.version_number,
        "effective_date": version.effective_date.isoformat(),
        "holdings": holdings,
        "change_summary": change_summary,
        "note": version.note,
        "created_at": version.created_at.isoformat() if version.created_at else None,
    }


def _serialize_basket(basket: PortfolioBasket, *, with_versions: bool = True) -> dict:
    try:
        cost_config = json.loads(basket.cost_config_json)
    except json.JSONDecodeError:
        cost_config = {}
    out = {
        "id": basket.id,
        "name": basket.name,
        "benchmark": basket.benchmark,
        "benchmark_exchange": basket.benchmark_exchange,
        "initial_capital": basket.initial_capital,
        "cost_config": cost_config,
        "version_count": len(basket.versions),
        "created_at": basket.created_at.isoformat() if basket.created_at else None,
        "updated_at": basket.updated_at.isoformat() if basket.updated_at else None,
    }
    if with_versions:
        out["versions"] = [_serialize_version(v) for v in basket.versions]
    else:
        latest = basket.versions[-1] if basket.versions else None
        out["latest_effective_date"] = latest.effective_date.isoformat() if latest else None
    return out


def list_baskets(user_id: str) -> list[dict]:
    """Every basket for a user, newest first, without their full version history."""
    try:
        rows = (
            db_session.query(PortfolioBasket)
            .filter_by(user_id=user_id)
            .order_by(PortfolioBasket.updated_at.desc())
            .all()
        )
        return [_serialize_basket(row, with_versions=False) for row in rows]
    except Exception:
        logger.exception("Could not list baskets for %s", user_id)
        db_session.rollback()
        return []


def get_basket(user_id: str, basket_id: int) -> dict | None:
    """One basket with its full version history, or None if missing/not owned."""
    try:
        basket = db_session.query(PortfolioBasket).filter_by(id=basket_id, user_id=user_id).first()
        return _serialize_basket(basket) if basket else None
    except Exception:
        logger.exception("Could not read basket %s for %s", basket_id, user_id)
        db_session.rollback()
        return None


def create_basket(
    user_id: str,
    name: str,
    holdings: list[dict],
    effective_date: date,
    *,
    benchmark: str | None = None,
    benchmark_exchange: str | None = "NSE_INDEX",
    cost_config: dict | None = None,
    initial_capital: float = 100_000.0,
    note: str | None = None,
) -> dict | None:
    """Create a basket with its first version. None on a taken name, empty
    holdings, or the per-user cap -- the caller turns that into a 409/400."""
    name = (name or "").strip()
    holdings = _normalise_holdings(holdings)[:MAX_SYMBOLS_PER_VERSION]
    if not name or not holdings:
        return None

    try:
        existing = db_session.query(PortfolioBasket).filter_by(user_id=user_id, name=name).first()
        if existing:
            return None

        count = db_session.query(PortfolioBasket).filter_by(user_id=user_id).count()
        if count >= MAX_BASKETS_PER_USER:
            logger.warning("Basket cap reached for %s (%d baskets)", user_id, count)
            return None

        basket = PortfolioBasket(
            user_id=user_id,
            name=name,
            benchmark=(benchmark or None),
            benchmark_exchange=(benchmark_exchange or "NSE_INDEX"),
            initial_capital=float(initial_capital),
            cost_config_json=json.dumps(cost_config or {}),
        )
        db_session.add(basket)
        db_session.flush()  # assign the id the version needs

        version = PortfolioBasketVersion(
            basket_id=basket.id,
            version_number=1,
            effective_date=effective_date,
            holdings_json=json.dumps(holdings),
            change_summary_json=json.dumps(_diff_holdings([], holdings)),
            note=note,
        )
        db_session.add(version)
        db_session.commit()
        return _serialize_basket(basket)
    except Exception:
        logger.exception("Could not create basket %s for %s", name, user_id)
        db_session.rollback()
        return None


def add_version(
    user_id: str,
    basket_id: int,
    holdings: list[dict],
    effective_date: date,
    *,
    note: str | None = None,
) -> dict | None:
    """
    Record a manual rebalance as a new version.

    None when the basket is missing/not owned, holdings are empty, the cap is
    reached, or ``effective_date`` does not move strictly forward from the
    latest version -- manual rebalances are a timeline, not a free-form edit.
    """
    holdings = _normalise_holdings(holdings)[:MAX_SYMBOLS_PER_VERSION]
    if not holdings:
        return None

    try:
        basket = db_session.query(PortfolioBasket).filter_by(id=basket_id, user_id=user_id).first()
        if not basket:
            return None

        count = db_session.query(PortfolioBasketVersion).filter_by(basket_id=basket.id).count()
        if count >= MAX_VERSIONS_PER_BASKET:
            logger.warning("Version cap reached for basket %s (%d versions)", basket_id, count)
            return None

        latest = (
            db_session.query(PortfolioBasketVersion)
            .filter_by(basket_id=basket.id)
            .order_by(PortfolioBasketVersion.version_number.desc())
            .first()
        )
        if latest and effective_date <= latest.effective_date:
            logger.warning(
                "Rebalance date %s does not move forward from basket %s's latest version (%s)",
                effective_date,
                basket_id,
                latest.effective_date,
            )
            return None

        previous_holdings = json.loads(latest.holdings_json) if latest else []
        version = PortfolioBasketVersion(
            basket_id=basket.id,
            version_number=(latest.version_number + 1) if latest else 1,
            effective_date=effective_date,
            holdings_json=json.dumps(holdings),
            change_summary_json=json.dumps(_diff_holdings(previous_holdings, holdings)),
            note=note,
        )
        db_session.add(version)
        basket.updated_at = datetime.utcnow()
        db_session.commit()
        return _serialize_version(version)
    except Exception:
        logger.exception("Could not add a version to basket %s for %s", basket_id, user_id)
        db_session.rollback()
        return None


def update_basket(user_id: str, basket_id: int, **fields) -> dict | None:
    """Rename a basket or change its benchmark/cost/capital settings.

    ``fields`` may include ``name``, ``benchmark``, ``benchmark_exchange``,
    ``cost_config`` (dict, replaces the stored one), ``initial_capital``.
    """
    try:
        basket = db_session.query(PortfolioBasket).filter_by(id=basket_id, user_id=user_id).first()
        if not basket:
            return None

        if "name" in fields:
            name = (fields["name"] or "").strip()
            if not name:
                return None
            clash = (
                db_session.query(PortfolioBasket)
                .filter(
                    PortfolioBasket.user_id == user_id,
                    PortfolioBasket.name == name,
                    PortfolioBasket.id != basket_id,
                )
                .first()
            )
            if clash:
                return None
            basket.name = name

        if "benchmark" in fields:
            basket.benchmark = fields["benchmark"] or None
        if "benchmark_exchange" in fields:
            basket.benchmark_exchange = fields["benchmark_exchange"] or "NSE_INDEX"
        if "cost_config" in fields:
            basket.cost_config_json = json.dumps(fields["cost_config"] or {})
        if "initial_capital" in fields and fields["initial_capital"]:
            basket.initial_capital = float(fields["initial_capital"])

        basket.updated_at = datetime.utcnow()
        db_session.commit()
        return _serialize_basket(basket, with_versions=False)
    except Exception:
        logger.exception("Could not update basket %s for %s", basket_id, user_id)
        db_session.rollback()
        return None


def delete_basket(user_id: str, basket_id: int) -> bool:
    """Remove a basket and every version in it."""
    try:
        basket = db_session.query(PortfolioBasket).filter_by(id=basket_id, user_id=user_id).first()
        if not basket:
            return False
        db_session.delete(basket)
        db_session.commit()
        return True
    except Exception:
        logger.exception("Could not delete basket %s for %s", basket_id, user_id)
        db_session.rollback()
        return False
