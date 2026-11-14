# services/basket_watchlist_sync_service.py
"""Keep the Historify watchlist covering every symbol the saved baskets need.

Basket performance reads daily closes from Historify (source="db"), and
Historify's scheduled download only fetches what is on its watchlist. This job
runs once a day, before the evening download, and reconciles the two.

What a basket needs is wider than its current holdings. run_basket_backtest
loads every symbol of every version over the basket's whole life, from version
1's effective date, and keeps only the sessions common to all of them. So:

- a symbol sold out in a later version must keep updating, or the whole basket
  stops at that symbol's last stored day;
- a newly held symbol needs history back to the basket's start, not just the
  few days a scheduled download fetches, or the backtest has no common start.

The rules:

- **Add** every symbol in any version of any basket, and each basket's
  benchmark, that is not on the watchlist.
- **Remove** only what this job added itself and no basket references any more
  (a deleted basket, a deleted version). A symbol the operator put on the
  watchlist by hand is never removed, even if no basket uses it.
- **Backfill** a needed symbol whose stored daily history starts later than its
  basket does, with an incremental download that only fetches the missing part.

Ownership and backfill attempts are kept in the ``basket_watchlist_sync`` table
in the Historify DuckDB, created on first use.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta

from utils.logging import get_logger

logger = get_logger(__name__)

SYNC_JOB_ID = "historify_basket_watchlist_sync"
INTERVAL = "D"

#: The first stored bar may fall a few days after the basket's start date
#: (weekends, holidays). Within this, history counts as already covering it.
START_TOLERANCE_DAYS = 7

#: A symbol whose history still starts late after a backfill (listed after the
#: basket started, or the broker has no older bars) is retried this often, not
#: every day.
BACKFILL_RETRY_DAYS = 7


def sync_time() -> tuple[int, int]:
    """(hour, minute) IST the daily sync runs at, from BASKET_WATCHLIST_SYNC_TIME."""
    raw = os.getenv("BASKET_WATCHLIST_SYNC_TIME", "17:45")
    try:
        hour, minute = (int(part) for part in raw.split(":"))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except ValueError:
        pass
    logger.warning(f"BASKET_WATCHLIST_SYNC_TIME={raw!r} is not HH:MM; using 17:45")
    return 17, 45


def sync_enabled() -> bool:
    return os.getenv("BASKET_WATCHLIST_SYNC_ENABLED", "TRUE").strip().upper() == "TRUE"


# --- what the baskets need -------------------------------------------------------


def needed_symbols() -> dict[tuple[str, str], date]:
    """{(symbol, exchange): earliest date any basket needs its prices from}.

    Across every user's baskets: the backtest window of a basket starts at its
    version 1 effective date for all its symbols and its benchmark.
    """
    import json

    from database.basket_db import PortfolioBasket, db_session

    needed: dict[tuple[str, str], date] = {}

    def need(symbol, exchange, start):
        symbol = (symbol or "").strip().upper()
        exchange = (exchange or "").strip().upper()
        if not symbol or not exchange:
            return
        key = (symbol, exchange)
        if key not in needed or start < needed[key]:
            needed[key] = start

    try:
        for basket in db_session.query(PortfolioBasket).all():
            versions = list(basket.versions)
            if not versions:
                continue
            start = min(v.effective_date for v in versions)
            for version in versions:
                try:
                    holdings = json.loads(version.holdings_json) or []
                except (TypeError, ValueError):
                    holdings = []
                for holding in holdings:
                    need(holding.get("symbol"), holding.get("exchange", "NSE"), start)
            if basket.benchmark:
                need(basket.benchmark, basket.benchmark_exchange or "NSE_INDEX", start)
    finally:
        db_session.remove()
    return needed


# --- sync state (Historify DuckDB) ------------------------------------------------


def _ensure_state_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS basket_watchlist_sync (
            symbol VARCHAR NOT NULL,
            exchange VARCHAR NOT NULL,
            owned BOOLEAN NOT NULL DEFAULT FALSE,
            backfill_from DATE,
            backfill_attempted_on DATE,
            PRIMARY KEY (symbol, exchange)
        )
    """)


def _read_state() -> dict[tuple[str, str], dict]:
    from database.historify_db import get_connection

    with get_connection() as conn:
        _ensure_state_table(conn)
        rows = conn.execute(
            "SELECT symbol, exchange, owned, backfill_from, backfill_attempted_on "
            "FROM basket_watchlist_sync"
        ).fetchall()
    return {
        (r[0], r[1]): {"owned": bool(r[2]), "backfill_from": r[3], "attempted_on": r[4]}
        for r in rows
    }


def _write_state(updates: dict[tuple[str, str], dict], deletes: set[tuple[str, str]]) -> None:
    if not updates and not deletes:
        return
    from database.historify_db import get_connection

    with get_connection() as conn:
        _ensure_state_table(conn)
        for (symbol, exchange), row in updates.items():
            conn.execute(
                """
                INSERT INTO basket_watchlist_sync
                    (symbol, exchange, owned, backfill_from, backfill_attempted_on)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (symbol, exchange) DO UPDATE SET
                    owned = excluded.owned,
                    backfill_from = excluded.backfill_from,
                    backfill_attempted_on = excluded.backfill_attempted_on
                """,
                [symbol, exchange, row["owned"], row["backfill_from"], row["attempted_on"]],
            )
        for symbol, exchange in deletes:
            conn.execute(
                "DELETE FROM basket_watchlist_sync WHERE symbol = ? AND exchange = ?",
                [symbol, exchange],
            )


# --- the sync ---------------------------------------------------------------------


def _first_stored_date(symbol: str, exchange: str) -> date | None:
    from database.historify_db import get_data_range

    data_range = get_data_range(symbol, exchange, INTERVAL)
    if not data_range or not data_range.get("first_timestamp"):
        return None
    return datetime.fromtimestamp(data_range["first_timestamp"]).date()


def _needs_backfill(first_stored: date | None, start: date, state: dict | None, today: date) -> bool:
    if first_stored is not None and first_stored <= start + timedelta(days=START_TOLERANCE_DAYS):
        return False
    if not state or state.get("backfill_from") != start or not state.get("attempted_on"):
        return True
    return (today - state["attempted_on"]).days >= BACKFILL_RETRY_DAYS


def _api_key() -> str | None:
    from database.auth_db import get_first_available_api_key
    from services.historify_scheduler_service import get_historify_scheduler

    try:
        key = get_historify_scheduler().api_key
    except Exception:
        key = None
    return key or get_first_available_api_key()


def sync_basket_watchlist(dry_run: bool = False, today: date | None = None) -> dict:
    """Reconcile the Historify watchlist with what the baskets need.

    Returns a summary: added, removed and backfilled symbols as "EXCH:SYM".
    With ``dry_run`` nothing is changed and the summary is what would be done.
    """
    from database.historify_db import (
        bulk_add_to_watchlist,
        bulk_remove_from_watchlist,
        get_watchlist,
    )

    today = today or date.today()
    needed = needed_symbols()
    on_watchlist = {
        (str(i["symbol"]).upper(), str(i["exchange"]).upper()) for i in get_watchlist()
    }
    state = _read_state()

    to_add = sorted(set(needed) - on_watchlist)
    to_remove = sorted(
        key for key, row in state.items() if row["owned"] and key not in needed and key in on_watchlist
    )
    # Owned rows whose symbol is gone from both the watchlist and the baskets.
    forgotten = {key for key, row in state.items() if key not in needed and key not in to_remove}

    backfill: dict[date, list[tuple[str, str]]] = {}
    for key, start in needed.items():
        if _needs_backfill(_first_stored_date(*key), start, state.get(key), today):
            backfill.setdefault(start, []).append(key)

    summary = {
        "status": "success",
        "dry_run": dry_run,
        "needed": len(needed),
        "added": [f"{e}:{s}" for s, e in to_add],
        "removed": [f"{e}:{s}" for s, e in to_remove],
        "backfill": {
            start.isoformat(): [f"{e}:{s}" for s, e in sorted(keys)]
            for start, keys in sorted(backfill.items())
        },
        "backfill_jobs": [],
    }
    if dry_run:
        return summary

    updates: dict[tuple[str, str], dict] = {}

    if to_add:
        added, _skipped, failed = bulk_add_to_watchlist(
            [{"symbol": s, "exchange": e} for s, e in to_add]
        )
        failed_keys = {(f.get("symbol"), f.get("exchange")) for f in failed}
        for key in to_add:
            if key in failed_keys:
                continue
            row = dict(state.get(key) or {"backfill_from": None, "attempted_on": None})
            row["owned"] = True
            updates[key] = row
        if failed:
            logger.warning(f"Basket watchlist sync could not add {len(failed)} symbol(s): {failed}")

    if to_remove:
        bulk_remove_from_watchlist([{"symbol": s, "exchange": e} for s, e in to_remove])

    if backfill:
        api_key = _api_key()
        if not api_key:
            logger.warning(
                "Basket watchlist sync: no API key, so the missing basket history was not "
                "downloaded. Generate an API key, and it is picked up on the next run."
            )
        else:
            from services.historify_service import create_and_start_job

            for start, keys in sorted(backfill.items()):
                ok, response, _status = create_and_start_job(
                    job_type="custom",
                    symbols=[{"symbol": s, "exchange": e} for s, e in keys],
                    interval=INTERVAL,
                    start_date=start.isoformat(),
                    end_date=today.isoformat(),
                    api_key=api_key,
                    config={"source": "basket_watchlist_sync"},
                    incremental=True,
                )
                if not ok:
                    logger.warning(
                        f"Basket watchlist sync could not start the history download from "
                        f"{start}: {response.get('message')}"
                    )
                    continue
                summary["backfill_jobs"].append(response.get("job_id"))
                for key in keys:
                    row = updates.get(key) or dict(
                        state.get(key) or {"owned": False, "backfill_from": None, "attempted_on": None}
                    )
                    row["backfill_from"] = start
                    row["attempted_on"] = today
                    updates[key] = row

    _write_state(updates, set(to_remove) | forgotten)

    logger.info(
        f"Basket watchlist sync: {len(needed)} symbol(s) needed, added {len(summary['added'])}, "
        f"removed {len(summary['removed'])}, history downloads started "
        f"{len(summary['backfill_jobs'])}"
    )
    return summary


def run_scheduled_sync() -> None:
    """APScheduler entry point. Never raises: a failed sync waits for tomorrow."""
    if not sync_enabled():
        return
    try:
        sync_basket_watchlist()
    except Exception:
        logger.exception("Basket watchlist sync failed")
