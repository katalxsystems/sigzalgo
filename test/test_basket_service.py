"""
services.basket_service.run_basket_backtest, end to end against a real (but
in-memory) basket and a synthetic price matrix.

load_prices is the only thing stubbed -- everything else (the DB round trip,
run_versioned_backtest, the analytics reused from portfolio_service) runs for
real, so this is the seam test_basket_engine.py (pure engine) and
test_basket_api.py (mocked service layer) both stop short of.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

import database.basket_db as basket_db
import services.basket_service as basket_service
from portfolio.data import PriceMatrix


@pytest.fixture(autouse=True)
def fresh_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    basket_db.engine = engine
    basket_db.db_session = scoped_session(
        sessionmaker(autocommit=False, autoflush=False, bind=engine)
    )
    basket_db.Base.query = basket_db.db_session.query_property()
    basket_db.Base.metadata.create_all(engine)
    yield
    basket_db.db_session.remove()


def matrix(data: dict[str, list[float]], start: str) -> PriceMatrix:
    index = pd.bdate_range(start, periods=len(next(iter(data.values()))))
    closes = pd.DataFrame(data, index=index)
    return PriceMatrix(closes=closes, source="db", start=index[0].date(), end=index[-1].date())


def test_run_basket_backtest_reports_versions_and_equity(monkeypatch):
    created = basket_db.create_basket(
        "alice",
        "Core",
        [
            {"symbol": "A", "exchange": "NSE", "weight": 60},
            {"symbol": "B", "exchange": "NSE", "weight": 40},
        ],
        date(2024, 1, 1),
    )
    basket_id = created["id"]
    basket_db.add_version(
        "alice",
        basket_id,
        [
            {"symbol": "A", "exchange": "NSE", "weight": 70},
            {"symbol": "C", "exchange": "NSE", "weight": 30},
        ],  # B dropped, C added
        date(2024, 1, 3),
        note="Q1 rebalance",
    )

    prices = matrix(
        {
            "A": [100, 101, 102, 103, 104],
            "B": [50, 51, 52, 53, 54],
            "C": [200, 201, 202, 203, 204],
        },
        start="2024-01-01",
    )
    monkeypatch.setattr(basket_service, "load_prices", lambda *_a, **_k: prices)

    ok, payload, status = basket_service.run_basket_backtest("alice", basket_id)

    assert ok is True
    assert status == 200
    assert len(payload["versions"]) == 2
    assert payload["versions"][0]["change_summary"]["added"] == ["A", "B"]
    assert payload["versions"][1]["change_summary"]["added"] == ["C"]
    assert payload["versions"][1]["change_summary"]["removed"] == ["B"]
    assert payload["versions"][1]["note"] == "Q1 rebalance"
    # The value reported for the second rebalance matches the equity curve at
    # that date -- the two must not be able to disagree.
    equity_by_date = {row["date"]: row["value"] for row in payload["equity"]}
    v2_date = payload["versions"][1]["effective_date"]
    assert payload["versions"][1]["value_at_rebalance"] == pytest.approx(
        equity_by_date[v2_date], abs=0.01
    )
    assert payload["equity"][0]["value"] == pytest.approx(100_000.0)
    assert payload["benchmark_equity"] == []


def test_run_basket_backtest_missing_basket_is_404(monkeypatch):
    monkeypatch.setattr(
        basket_service,
        "load_prices",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("prices should never be loaded for a missing basket")
        ),
    )

    ok, payload, status = basket_service.run_basket_backtest("alice", 999999)

    assert ok is False
    assert status == 404


def test_run_basket_backtest_includes_benchmark_when_set(monkeypatch):
    created = basket_db.create_basket(
        "alice",
        "WithBench",
        [{"symbol": "A", "exchange": "NSE", "weight": 100}],
        date(2024, 1, 1),
        benchmark="NIFTY",
        benchmark_exchange="NSE_INDEX",
    )
    basket_id = created["id"]

    holding_prices = matrix({"A": [100, 101, 102, 103, 104]}, start="2024-01-01")
    bench_prices = matrix({"NIFTY": [1000, 1010, 1005, 1020, 1030]}, start="2024-01-01")

    def fake_load_prices(symbols, *_args, **_kwargs):
        return bench_prices if symbols == ["NIFTY"] else holding_prices

    monkeypatch.setattr(basket_service, "load_prices", fake_load_prices)

    ok, payload, status = basket_service.run_basket_backtest("alice", basket_id)

    assert ok is True
    assert status == 200
    assert len(payload["benchmark_equity"]) > 0
    assert payload["benchmark_equity"][0]["value"] == pytest.approx(100_000.0)
