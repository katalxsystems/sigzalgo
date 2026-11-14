"""The daily sync keeps the Historify watchlist covering every basket's symbols."""

from __future__ import annotations

from datetime import date

import pytest

import services.basket_watchlist_sync_service as sync

TODAY = date(2026, 11, 16)
START = date(2026, 1, 5)


@pytest.fixture
def env(monkeypatch):
    """The sync with baskets, watchlist, state and downloads held in memory."""
    import database.historify_db as hdb
    import services.historify_service as hs

    world = {
        "needed": {},
        "watchlist": set(),
        "state": {},
        "first_stored": {},
        "jobs": [],
    }

    monkeypatch.setattr(sync, "needed_symbols", lambda: dict(world["needed"]))
    monkeypatch.setattr(
        hdb, "get_watchlist", lambda: [{"symbol": s, "exchange": e} for s, e in world["watchlist"]]
    )

    def bulk_add(items):
        for i in items:
            world["watchlist"].add((i["symbol"], i["exchange"]))
        return len(items), 0, []

    def bulk_remove(items):
        for i in items:
            world["watchlist"].discard((i["symbol"], i["exchange"]))
        return len(items), 0, []

    monkeypatch.setattr(hdb, "bulk_add_to_watchlist", bulk_add)
    monkeypatch.setattr(hdb, "bulk_remove_from_watchlist", bulk_remove)
    monkeypatch.setattr(sync, "_read_state", lambda: {k: dict(v) for k, v in world["state"].items()})

    def write_state(updates, deletes):
        world["state"].update(updates)
        for key in deletes:
            world["state"].pop(key, None)

    monkeypatch.setattr(sync, "_write_state", write_state)
    monkeypatch.setattr(sync, "_first_stored_date", lambda s, e: world["first_stored"].get((s, e)))
    monkeypatch.setattr(sync, "_api_key", lambda: "k")

    def start_job(**kwargs):
        world["jobs"].append(kwargs)
        for item in kwargs["symbols"]:
            world["first_stored"][(item["symbol"], item["exchange"])] = date.fromisoformat(
                kwargs["start_date"]
            )
        return True, {"job_id": f"j{len(world['jobs'])}"}, 200

    monkeypatch.setattr(hs, "create_and_start_job", start_job)
    return world


def test_basket_symbols_are_added_and_backfilled_from_the_basket_start(env):
    env["needed"] = {("INFY", "NSE"): START, ("NIFTY", "NSE_INDEX"): START}

    summary = sync.sync_basket_watchlist(today=TODAY)

    assert env["watchlist"] == {("INFY", "NSE"), ("NIFTY", "NSE_INDEX")}
    assert sorted(summary["added"]) == ["NSE:INFY", "NSE_INDEX:NIFTY"]
    (job,) = env["jobs"]
    assert job["start_date"] == START.isoformat() and job["incremental"] is True
    assert all(env["state"][k]["owned"] for k in env["needed"])


def test_a_second_run_changes_nothing(env):
    env["needed"] = {("INFY", "NSE"): START}
    sync.sync_basket_watchlist(today=TODAY)
    summary = sync.sync_basket_watchlist(today=TODAY)
    assert not summary["added"] and not summary["removed"] and not summary["backfill"]
    assert len(env["jobs"]) == 1


def test_a_symbol_no_basket_uses_is_removed_only_if_the_sync_added_it(env):
    env["needed"] = {("INFY", "NSE"): START}
    env["watchlist"] = {("TCS", "NSE")}  # added by hand
    sync.sync_basket_watchlist(today=TODAY)

    env["needed"] = {}  # the basket was deleted
    summary = sync.sync_basket_watchlist(today=TODAY)

    assert summary["removed"] == ["NSE:INFY"]
    assert env["watchlist"] == {("TCS", "NSE")}
    assert ("INFY", "NSE") not in env["state"]


def test_a_symbol_already_on_the_watchlist_by_hand_is_never_removed(env):
    env["watchlist"] = {("INFY", "NSE")}
    env["needed"] = {("INFY", "NSE"): START}
    sync.sync_basket_watchlist(today=TODAY)
    env["needed"] = {}
    sync.sync_basket_watchlist(today=TODAY)
    assert ("INFY", "NSE") in env["watchlist"]


def test_history_already_covering_the_start_is_not_downloaded_again(env):
    env["needed"] = {("INFY", "NSE"): START}
    env["first_stored"] = {("INFY", "NSE"): date(2026, 1, 7)}  # within holiday tolerance
    sync.sync_basket_watchlist(today=TODAY)
    assert env["jobs"] == []


def test_a_short_history_is_retried_weekly_not_daily(env, monkeypatch):
    env["needed"] = {("NEWCO", "NSE"): START}
    monkeypatch.setattr(sync, "_first_stored_date", lambda s, e: date(2026, 6, 1))  # listed later
    sync.sync_basket_watchlist(today=TODAY)
    sync.sync_basket_watchlist(today=date(2026, 11, 17))
    assert len(env["jobs"]) == 1
    sync.sync_basket_watchlist(today=date(2026, 11, 23))
    assert len(env["jobs"]) == 2


def test_an_earlier_basket_start_backfills_again(env):
    env["needed"] = {("INFY", "NSE"): START}
    sync.sync_basket_watchlist(today=TODAY)
    env["needed"] = {("INFY", "NSE"): date(2025, 6, 2)}  # a new, older basket holds it
    sync.sync_basket_watchlist(today=TODAY)
    assert [j["start_date"] for j in env["jobs"]] == ["2026-01-05", "2025-06-02"]


def test_a_dry_run_changes_nothing(env):
    env["needed"] = {("INFY", "NSE"): START}
    summary = sync.sync_basket_watchlist(dry_run=True, today=TODAY)
    assert summary["added"] == ["NSE:INFY"] and summary["backfill"]
    assert not env["watchlist"] and not env["jobs"] and not env["state"]


def test_needed_symbols_covers_every_version_and_the_benchmark(monkeypatch):
    import json
    import types

    import database.basket_db as bdb

    versions = [
        types.SimpleNamespace(
            effective_date=date(2026, 3, 2),
            holdings_json=json.dumps([{"symbol": "tcs", "exchange": "NSE", "weight": 1}]),
        ),
        types.SimpleNamespace(
            effective_date=START,
            holdings_json=json.dumps([{"symbol": "INFY", "weight": 1}]),  # exchange defaults
        ),
    ]
    basket = types.SimpleNamespace(versions=versions, benchmark="NIFTY", benchmark_exchange=None)

    class FakeSession:
        def query(self, _model):
            return types.SimpleNamespace(all=lambda: [basket])

        def remove(self):
            pass

    monkeypatch.setattr(bdb, "db_session", FakeSession())
    assert sync.needed_symbols() == {
        ("TCS", "NSE"): START,
        ("INFY", "NSE"): START,
        ("NIFTY", "NSE_INDEX"): START,
    }


def test_the_state_table_round_trips(monkeypatch, tmp_path):
    import duckdb

    import database.historify_db as hdb

    path = str(tmp_path / "h.duckdb")

    class _Conn:
        def __enter__(self):
            self.c = duckdb.connect(path)
            return self.c

        def __exit__(self, *a):
            self.c.close()

    monkeypatch.setattr(hdb, "get_connection", lambda: _Conn())
    sync._write_state(
        {("INFY", "NSE"): {"owned": True, "backfill_from": START, "attempted_on": TODAY}}, set()
    )
    sync._write_state(
        {("INFY", "NSE"): {"owned": True, "backfill_from": START, "attempted_on": date(2026, 11, 20)}},
        set(),
    )
    assert sync._read_state() == {
        ("INFY", "NSE"): {"owned": True, "backfill_from": START, "attempted_on": date(2026, 11, 20)}
    }
    sync._write_state({}, {("INFY", "NSE")})
    assert sync._read_state() == {}
