"""
Portfolio Basket persistence: CRUD, version diffs, and per-user caps.

Swaps database.basket_db onto a fresh in-memory SQLite engine per test, same
pattern as test/test_orphaned_apikey.py, so these never touch the real
db/openalgo.db.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

import database.basket_db as basket_db


@pytest.fixture(autouse=True)
def fresh_db():
    """A clean in-memory database for every test."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    basket_db.engine = engine
    basket_db.db_session = scoped_session(
        sessionmaker(autocommit=False, autoflush=False, bind=engine)
    )
    basket_db.Base.query = basket_db.db_session.query_property()
    basket_db.Base.metadata.create_all(engine)
    yield
    basket_db.db_session.remove()


HOLDINGS_V1 = [
    {"symbol": "A", "exchange": "NSE", "weight": 60},
    {"symbol": "B", "exchange": "NSE", "weight": 40},
]


class TestCreateBasket:
    def test_creates_basket_with_first_version(self):
        result = basket_db.create_basket(
            "alice",
            "Core",
            HOLDINGS_V1,
            date(2024, 1, 1),
            benchmark="NIFTY",
        )
        assert result is not None
        assert result["name"] == "Core"
        assert result["benchmark"] == "NIFTY"
        assert len(result["versions"]) == 1
        v1 = result["versions"][0]
        assert v1["version_number"] == 1
        assert v1["change_summary"]["added"] == ["A", "B"]
        assert v1["change_summary"]["removed"] == []

    def test_rejects_duplicate_name_for_same_user(self):
        basket_db.create_basket("alice", "Core", HOLDINGS_V1, date(2024, 1, 1))
        dup = basket_db.create_basket("alice", "Core", HOLDINGS_V1, date(2024, 1, 1))
        assert dup is None

    def test_same_name_allowed_for_different_users(self):
        first = basket_db.create_basket("alice", "Core", HOLDINGS_V1, date(2024, 1, 1))
        second = basket_db.create_basket("bob", "Core", HOLDINGS_V1, date(2024, 1, 1))
        assert first is not None
        assert second is not None

    def test_rejects_empty_holdings(self):
        assert basket_db.create_basket("alice", "Empty", [], date(2024, 1, 1)) is None

    def test_enforces_basket_cap_per_user(self, monkeypatch):
        monkeypatch.setattr(basket_db, "MAX_BASKETS_PER_USER", 2)
        assert basket_db.create_basket("alice", "One", HOLDINGS_V1, date(2024, 1, 1))
        assert basket_db.create_basket("alice", "Two", HOLDINGS_V1, date(2024, 1, 1))
        assert basket_db.create_basket("alice", "Three", HOLDINGS_V1, date(2024, 1, 1)) is None


class TestAddVersion:
    def test_records_a_rebalance_as_a_new_version_with_a_diff(self):
        created = basket_db.create_basket("alice", "Core", HOLDINGS_V1, date(2024, 1, 1))
        basket_id = created["id"]

        new_holdings = [
            {"symbol": "A", "exchange": "NSE", "weight": 70},  # reweighted 60 -> 70
            {"symbol": "C", "exchange": "NSE", "weight": 30},  # added
            # B dropped entirely
        ]
        version = basket_db.add_version(
            "alice",
            basket_id,
            new_holdings,
            date(2024, 2, 1),
            note="Q1 rebalance",
        )
        assert version is not None
        assert version["version_number"] == 2
        assert version["note"] == "Q1 rebalance"
        summary = version["change_summary"]
        assert summary["added"] == ["C"]
        assert summary["removed"] == ["B"]
        assert summary["reweighted"] == [{"symbol": "A", "from": 60, "to": 70}]

        basket = basket_db.get_basket("alice", basket_id)
        assert len(basket["versions"]) == 2

    def test_rejects_effective_date_not_moving_forward(self):
        created = basket_db.create_basket("alice", "Core", HOLDINGS_V1, date(2024, 2, 1))
        basket_id = created["id"]
        same_day = basket_db.add_version("alice", basket_id, HOLDINGS_V1, date(2024, 2, 1))
        earlier = basket_db.add_version("alice", basket_id, HOLDINGS_V1, date(2024, 1, 1))
        assert same_day is None
        assert earlier is None

    def test_rejects_unknown_or_unowned_basket(self):
        created = basket_db.create_basket("alice", "Core", HOLDINGS_V1, date(2024, 1, 1))
        assert basket_db.add_version("bob", created["id"], HOLDINGS_V1, date(2024, 2, 1)) is None
        assert basket_db.add_version("alice", 999999, HOLDINGS_V1, date(2024, 2, 1)) is None

    def test_enforces_version_cap_per_basket(self, monkeypatch):
        monkeypatch.setattr(basket_db, "MAX_VERSIONS_PER_BASKET", 2)
        created = basket_db.create_basket("alice", "Core", HOLDINGS_V1, date(2024, 1, 1))
        basket_id = created["id"]
        assert basket_db.add_version("alice", basket_id, HOLDINGS_V1, date(2024, 2, 1))
        assert basket_db.add_version("alice", basket_id, HOLDINGS_V1, date(2024, 3, 1)) is None


class TestOwnershipAndLifecycle:
    def test_list_baskets_is_scoped_per_user(self):
        basket_db.create_basket("alice", "Core", HOLDINGS_V1, date(2024, 1, 1))
        basket_db.create_basket("bob", "Other", HOLDINGS_V1, date(2024, 1, 1))
        assert [b["name"] for b in basket_db.list_baskets("alice")] == ["Core"]
        assert [b["name"] for b in basket_db.list_baskets("bob")] == ["Other"]

    def test_get_basket_returns_none_for_a_different_owner(self):
        created = basket_db.create_basket("alice", "Core", HOLDINGS_V1, date(2024, 1, 1))
        assert basket_db.get_basket("bob", created["id"]) is None
        assert basket_db.get_basket("alice", created["id"]) is not None

    def test_delete_basket_removes_its_versions(self):
        created = basket_db.create_basket("alice", "Core", HOLDINGS_V1, date(2024, 1, 1))
        basket_id = created["id"]
        basket_db.add_version("alice", basket_id, HOLDINGS_V1, date(2024, 2, 1))

        assert basket_db.delete_basket("alice", basket_id) is True
        assert basket_db.get_basket("alice", basket_id) is None
        remaining = (
            basket_db.db_session.query(basket_db.PortfolioBasketVersion)
            .filter_by(basket_id=basket_id)
            .count()
        )
        assert remaining == 0

    def test_update_basket_renames_and_changes_benchmark(self):
        created = basket_db.create_basket("alice", "Core", HOLDINGS_V1, date(2024, 1, 1))
        updated = basket_db.update_basket(
            "alice",
            created["id"],
            name="Renamed",
            benchmark="SENSEX",
        )
        assert updated["name"] == "Renamed"
        assert updated["benchmark"] == "SENSEX"

    def test_update_basket_rejects_name_clash(self):
        basket_db.create_basket("alice", "Core", HOLDINGS_V1, date(2024, 1, 1))
        other = basket_db.create_basket("alice", "Other", HOLDINGS_V1, date(2024, 1, 1))
        assert basket_db.update_basket("alice", other["id"], name="Core") is None
