"""
Versioned (manually-rebalanced) basket backtesting.

The reference case is the 2-symbol, 60/40, 5-manual-rebalance walkthrough
worked by hand: each checkpoint is its own trading session, so the engine's
bar-by-bar drift telescopes exactly into the checkpoint-to-checkpoint growth
used in the manual calculation, and the two should match to the cent.
"""

from __future__ import annotations

import pandas as pd
import pytest

from portfolio.data import PriceMatrix
from portfolio.engine import (
    Costs,
    ScheduleEntry,
    normalise_schedule_weights,
    run_versioned_backtest,
)


def matrix(data: dict[str, list[float]], start: str = "2024-01-01") -> PriceMatrix:
    index = pd.bdate_range(start, periods=len(next(iter(data.values()))))
    closes = pd.DataFrame(data, index=index)
    return PriceMatrix(closes=closes, source="db", start=index[0].date(), end=index[-1].date())


class TestNormaliseScheduleWeights:
    def test_fills_absent_symbols_with_zero(self):
        vector = normalise_schedule_weights({"A": 60, "B": 40}, ["A", "B", "C"])
        assert vector.tolist() == pytest.approx([0.6, 0.4, 0.0])

    def test_rejects_symbol_outside_universe(self):
        with pytest.raises(ValueError, match="outside the basket's universe"):
            normalise_schedule_weights({"A": 1.0, "Z": 1.0}, ["A", "B"])

    def test_rejects_zero_sum(self):
        with pytest.raises(ValueError, match="sum to zero"):
            normalise_schedule_weights({"A": 0.0}, ["A", "B"])

    def test_rejects_negative_weight(self):
        with pytest.raises(ValueError, match="long-only"):
            normalise_schedule_weights({"A": -1.0}, ["A", "B"])


class TestRunVersionedBacktest:
    def _prices(self):
        # One session per checkpoint from the hand-worked walkthrough: buy,
        # then 5 manual rebalances.
        return matrix(
            {
                "A": [1000, 1100, 1050, 1200, 1150, 1300],
                "B": [500, 480, 520, 500, 560, 540],
            }
        )

    def _schedule(self, prices: PriceMatrix) -> list[ScheduleEntry]:
        dates = [d.date() for d in prices.closes.index]
        weights = {"A": 60, "B": 40}
        return [
            ScheduleEntry(effective_date=d, weights=weights, label=f"v{i + 1}")
            for i, d in enumerate(dates)
        ]

    def test_matches_the_hand_worked_five_rebalance_example(self):
        prices = self._prices()
        schedule = self._schedule(prices)
        result = run_versioned_backtest(
            prices, schedule, costs=Costs(bps=10.0), initial_capital=100_000.0
        )

        # Independently verified (see the PR/commit that added this test):
        # drift each leg by its own bar-over-bar close ratio, reset to 60/40
        # net of a 10bps charge on traded value, five times over.
        assert result.equity.iloc[-1] == pytest.approx(122_340.88, abs=0.01)
        assert result.equity.iloc[0] == pytest.approx(100_000.0)
        # Five rebalances happened, each with nonzero turnover and cost.
        assert len(result.rebalance_dates) == 5
        assert (result.turnover > 0).all()
        assert (result.cost_at > 0).all()
        assert result.cost_at.sum() == pytest.approx(20.10, abs=0.01)
        # Cost drag is small and positive -- costs can only ever cost you.
        assert 0 < result.cost_drag < 0.001

    def test_buy_and_hold_is_the_zero_rebalance_case(self):
        prices = self._prices()
        schedule = [
            ScheduleEntry(effective_date=prices.closes.index[0].date(), weights={"A": 60, "B": 40})
        ]
        result = run_versioned_backtest(prices, schedule, costs=Costs(bps=10.0))
        assert len(result.rebalance_dates) == 0
        assert result.cost_drag == pytest.approx(0.0)
        # Pure drift: final value is the weighted sum of each leg's own growth.
        expected = 60_000 * (1300 / 1000) + 40_000 * (540 / 500)
        assert result.equity.iloc[-1] == pytest.approx(expected)

    def test_dropping_a_symbol_sells_it_out_and_it_never_returns(self):
        prices = matrix(
            {
                "A": [100, 110, 120, 130],
                "B": [50, 55, 60, 65],
                "C": [200, 210, 220, 230],
            }
        )
        dates = [d.date() for d in prices.closes.index]
        schedule = [
            ScheduleEntry(dates[0], {"A": 50, "B": 50}),
            ScheduleEntry(dates[1], {"A": 100}),  # B sold out entirely here
            ScheduleEntry(dates[2], {"A": 70, "C": 30}),  # C introduced fresh
        ]
        result = run_versioned_backtest(prices, schedule, costs=Costs())

        # Before the drop: B has weight. From the drop onward: exactly zero.
        assert result.weights.loc[prices.closes.index[0], "B"] > 0
        assert result.weights.loc[prices.closes.index[1], "B"] == pytest.approx(0.0)
        assert result.weights.loc[prices.closes.index[-1], "B"] == pytest.approx(0.0)
        # C has no weight before it's introduced, and nonzero after.
        assert result.weights.loc[prices.closes.index[0], "C"] == pytest.approx(0.0)
        assert result.weights.loc[prices.closes.index[1], "C"] == pytest.approx(0.0)
        assert result.weights.loc[prices.closes.index[-1], "C"] > 0

    def test_rejects_empty_schedule(self):
        with pytest.raises(ValueError, match="at least one entry"):
            run_versioned_backtest(self._prices(), [])

    def test_rejects_duplicate_effective_dates(self):
        prices = self._prices()
        d = prices.closes.index[0].date()
        schedule = [
            ScheduleEntry(d, {"A": 1.0}),
            ScheduleEntry(d, {"B": 1.0}),
        ]
        with pytest.raises(ValueError, match="duplicate"):
            run_versioned_backtest(prices, schedule)

    def test_rejects_first_entry_not_on_first_session(self):
        prices = self._prices()
        second_day = prices.closes.index[1].date()
        with pytest.raises(ValueError, match="first session"):
            run_versioned_backtest(prices, [ScheduleEntry(second_day, {"A": 1.0})])

    def test_rejects_symbol_with_no_price_history(self):
        prices = self._prices()
        first_day = prices.closes.index[0].date()
        with pytest.raises(ValueError, match="price history missing"):
            run_versioned_backtest(prices, [ScheduleEntry(first_day, {"ZZZ": 1.0})])
