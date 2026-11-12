"""
The backtest itself: weights + prices + a rebalancing policy -> an equity curve.

Deliberately a drift-and-reset simulation rather than an order-level one. Held
weights drift with prices between rebalance dates and snap back to target on
them, which is what a weight-target portfolio actually does. There is no order
book because a daily-bar investor portfolio does not need one.

Costs are modelled and reported, not assumed away. A frictionless backtest
flatters every rebalancing schedule -- the more often it trades, the more it
flatters -- so turnover and the return it consumed are first-class outputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from portfolio.costs import CostSchedule, EquityCosts
from portfolio.data import PriceMatrix
from portfolio.rebalance import RebalancePolicy, calendar_dates, drifted


@dataclass(frozen=True)
class Costs:
    """
    Round-trip trading costs, as fractions of traded value.

    ``bps`` covers brokerage, exchange fees and taxes as one number; ``slippage``
    is the execution gap. Both apply to the *traded* value at each rebalance,
    not to the whole portfolio, so a small drift correction costs little.
    """

    bps: float = 0.0
    slippage: float = 0.0

    def __post_init__(self) -> None:
        for name in ("bps", "slippage"):
            value = getattr(self, name)
            if not np.isfinite(value) or value < 0:
                raise ValueError(f"flat cost {name} must be finite and non-negative")

    @property
    def total(self) -> float:
        return (self.bps / 10_000.0) + self.slippage


@dataclass
class BacktestResult:
    """Everything a tearsheet or an API response needs from one run."""

    equity: pd.Series
    weights: pd.DataFrame
    rebalance_dates: pd.DatetimeIndex
    turnover: pd.Series
    cost_drag: float
    source: str
    cost_breakdown: dict[str, float] = field(default_factory=dict)
    #: One row per holding: what it made, what it cost, what it contributed.
    items: pd.DataFrame = field(default_factory=pd.DataFrame)
    meta: dict = field(default_factory=dict)
    #: Cost charged at each rebalance date, currency terms. Parallel to
    #: ``turnover`` (a fraction) -- this is what a per-rebalance breakdown
    #: (e.g. a basket's version history) needs to show what that specific
    #: rebalance cost, rather than only the run's total.
    cost_at: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))

    @property
    def returns(self) -> pd.Series:
        """Daily portfolio returns, net of costs."""
        return self.equity.pct_change().iloc[1:]

    @property
    def total_return(self) -> float:
        return float(self.equity.iloc[-1] / self.equity.iloc[0] - 1.0)


def normalise_weights(weights: dict[str, float], symbols: list[str]) -> np.ndarray:
    """
    Order weights to match ``symbols`` and scale them to sum to 1.

    Accepts percentages or fractions -- the UI collects "40.0" meaning 40% --
    since the only thing that matters downstream is the ratio between them.
    """
    missing = [s for s in symbols if s not in weights]
    if missing:
        raise ValueError(f"no weight given for {', '.join(missing)}")
    extra = [s for s in weights if s not in symbols]
    if extra:
        raise ValueError(f"weight given for unheld symbol(s): {', '.join(extra)}")

    vector = np.array([float(weights[s]) for s in symbols], dtype=float)
    if not np.isfinite(vector).all():
        raise ValueError("weights must be finite")
    if np.any(vector < 0):
        raise ValueError("negative weights are not supported; this is a long-only engine")
    total = vector.sum()
    if total <= 0:
        raise ValueError("weights sum to zero")
    return vector / total


def _apply_rebalance_costs(
    desired: np.ndarray,
    sleeve: np.ndarray,
    value: float,
    costs: Costs | EquityCosts | CostSchedule,
    cost_breakdown: dict[str, float],
) -> tuple[float, float, int]:
    """
    Cost of trading ``sleeve`` to ``desired`` at the current ``value``.

    Pure w.r.t. its inputs except for accumulating this rebalance's lines into
    ``cost_breakdown``. Returns ``(traded, charge, orders)`` -- ``traded`` is
    the one-way turnover fraction, ``charge`` the currency cost, ``orders`` how
    many symbols actually changed. Shared by ``run_backtest`` and
    ``run_versioned_backtest`` so the two rebalancing modes cannot drift apart
    on what a rebalance costs.
    """
    traded = float(np.abs(desired - sleeve).sum()) / 2.0 / value
    if traded <= 0:
        return traded, 0.0, 0

    traded_value = traded * value
    # One order per holding whose position actually changed -- a flat
    # per-order brokerage depends on that count, not on the value.
    orders = int((np.abs(desired - sleeve) > 1e-9).sum())
    if isinstance(costs, (EquityCosts, CostSchedule)):
        lines = costs.breakdown(traded_value, traded_value, orders)
        for key, amount in lines.items():
            cost_breakdown[key] = cost_breakdown.get(key, 0.0) + amount
        charge = lines["total"]
    else:
        charge = traded_value * costs.total
        cost_breakdown["total"] += charge
        cost_breakdown["orders"] += float(orders)
    return traded, charge, orders


@dataclass(frozen=True)
class ScheduleEntry:
    """
    One version of a versioned (manually-rebalanced) basket.

    ``weights`` names only the symbols *held* as of ``effective_date`` -- a
    symbol from an earlier entry simply absent here is fully sold out on this
    rebalance. Percentages or fractions both work, same as ``weights`` in
    ``run_backtest``.
    """

    effective_date: date
    weights: dict[str, float]
    label: str = ""


def normalise_schedule_weights(weights: dict[str, float], universe: list[str]) -> np.ndarray:
    """
    Like ``normalise_weights``, but tolerant of a partial dict.

    ``universe`` is every symbol the basket has ever held across its whole
    version history, not just this version's. A symbol in ``universe`` but not
    in ``weights`` gets an explicit 0 -- that is what "sold out this
    rebalance" means to the engine, no special case required. Only the given
    weights are required to sum to a positive total; the rest are zero by
    construction, so the resulting vector still sums to 1.
    """
    unknown = [s for s in weights if s not in universe]
    if unknown:
        raise ValueError(
            f"weight given for symbol(s) outside the basket's universe: {', '.join(unknown)}"
        )

    vector = np.array([float(weights.get(s, 0.0)) for s in universe], dtype=float)
    if not np.isfinite(vector).all():
        raise ValueError("weights must be finite")
    if np.any(vector < 0):
        raise ValueError("negative weights are not supported; this is a long-only engine")
    total = vector.sum()
    if total <= 0:
        raise ValueError("weights sum to zero")
    return vector / total


def _snap_to_sessions(dates: list[date], index: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """
    Move each schedule date onto the next actual trading session on or after
    it -- a manual rebalance dated on a holiday or a weekend is not a thing
    that can happen, same reasoning as ``calendar_dates`` in rebalance.py.
    """
    snapped: list[pd.Timestamp] = []
    for d in dates:
        stamp = pd.Timestamp(d)
        candidates = index[index >= stamp]
        if len(candidates) == 0:
            raise ValueError(f"no trading session on or after {d.isoformat()}")
        snapped.append(candidates[0])
    return snapped


def run_versioned_backtest(
    prices: PriceMatrix,
    schedule: list[ScheduleEntry],
    *,
    costs: Costs | EquityCosts | CostSchedule | None = None,
    initial_capital: float = 100_000.0,
) -> BacktestResult:
    """
    Simulate a basket whose target composition changes over time.

    Unlike ``run_backtest``, there is no single fixed target and no calendar
    or drift-band policy: ``schedule`` gives the exact dates a rebalance
    happens and what the basket should look like from that date on, entry 0
    being the initial buy. Otherwise the mechanics -- drift between
    rebalances, turnover, costs, the cost-free twin for ``cost_drag`` -- are
    identical to ``run_backtest``, sharing ``_apply_rebalance_costs`` so the
    two modes cannot disagree about what a rebalance costs.
    """
    costs = costs or Costs()
    if not schedule:
        raise ValueError("schedule must have at least one entry")
    if not np.isfinite(initial_capital) or initial_capital <= 0:
        raise ValueError("initial capital must be positive and finite")

    ordered = sorted(schedule, key=lambda e: e.effective_date)
    if [e.effective_date for e in ordered] != [e.effective_date for e in schedule]:
        raise ValueError("schedule must be sorted by effective_date")
    dates = [e.effective_date for e in ordered]
    if len(set(dates)) != len(dates):
        raise ValueError("schedule has duplicate effective_date entries")

    universe: list[str] = []
    for entry in ordered:
        for symbol in entry.weights:
            if symbol not in universe:
                universe.append(symbol)
    missing_prices = [s for s in universe if s not in prices.symbols]
    if missing_prices:
        raise ValueError(
            f"price history missing for symbol(s) referenced in the schedule: "
            f"{', '.join(missing_prices)}"
        )
    # Use the price matrix's own column order so `symbols`, `target` vectors
    # and `growth` all line up positionally, same convention as run_backtest.
    symbols = [s for s in prices.symbols if s in universe]

    closes = prices.closes[symbols]
    close_values = closes.to_numpy(dtype=float)
    if not np.isfinite(close_values).all() or (close_values <= 0).any():
        raise ValueError("price matrix must contain only positive finite closes")
    index = closes.index

    targets = [normalise_schedule_weights(e.weights, symbols) for e in ordered]
    snapped = _snap_to_sessions(dates, index)
    if snapped[0] != index[0]:
        raise ValueError(
            "the first schedule entry must fall on the price matrix's first session "
            "-- load prices starting from the basket's initial buy date"
        )
    # stamp -> index into `targets`/`ordered` that becomes active on that date.
    activation = {stamp: i for i, stamp in enumerate(snapped)}
    scheduled = set(snapped[1:])

    growth = closes.div(closes.shift(1)).fillna(1.0).to_numpy()

    equity = np.empty(len(index), dtype=float)
    weight_path = np.empty((len(index), len(symbols)), dtype=float)
    turnover_at: dict[pd.Timestamp, float] = {}
    cost_at: dict[pd.Timestamp, float] = {}
    order_count = 0
    if isinstance(costs, (EquityCosts, CostSchedule)):
        cost_breakdown = dict.fromkeys(costs.breakdown(0.0, 0.0, orders=0), 0.0)
    else:
        cost_breakdown = {"total": 0.0, "orders": 0.0}

    target = targets[0]
    sleeve = target * float(initial_capital)
    price_pnl = np.zeros(len(symbols), dtype=float)
    cost_paid = np.zeros(len(symbols), dtype=float)
    gross_sleeve = sleeve.copy()

    for i, stamp in enumerate(index):
        if i > 0:
            grown = sleeve * growth[i]
            price_pnl += grown - sleeve
            sleeve = grown
            value = sleeve.sum()
            gross_sleeve = gross_sleeve * growth[i]

            if stamp in scheduled:
                target = targets[activation[stamp]]
                desired = target * value
                traded, charge, orders = _apply_rebalance_costs(
                    desired, sleeve, value, costs, cost_breakdown
                )
                if traded > 0:
                    order_count += orders
                    moved = np.abs(desired - sleeve)
                    share = moved / moved.sum() if moved.sum() > 0 else moved
                    cost_paid += charge * share
                    value -= charge
                    turnover_at[stamp] = traded
                    cost_at[stamp] = charge
                sleeve = target * value
                gross_sleeve = target * gross_sleeve.sum()
        else:
            value = sleeve.sum()

        equity[i] = value
        weight_path[i] = sleeve / value

    equity_series = pd.Series(equity, index=index, name="equity")
    gross_total = gross_sleeve.sum() / float(initial_capital) - 1.0
    net_total = equity[-1] / float(initial_capital) - 1.0

    net_pnl = price_pnl - cost_paid
    first_close = closes.iloc[0].to_numpy()
    last_close = closes.iloc[-1].to_numpy()
    items = pd.DataFrame(
        {
            "weight_target": target,
            "weight_final": weight_path[-1],
            "invested": targets[0] * float(initial_capital),
            "price_pnl": price_pnl,
            "costs": cost_paid,
            "net_pnl": net_pnl,
            "contribution_pct": net_pnl / float(initial_capital),
            "symbol_return": (last_close / first_close) - 1.0,
        },
        index=pd.Index(symbols, name="symbol"),
    )

    return BacktestResult(
        equity=equity_series,
        items=items,
        weights=pd.DataFrame(weight_path, index=index, columns=symbols),
        rebalance_dates=pd.DatetimeIndex(sorted(turnover_at)),
        turnover=pd.Series(turnover_at, dtype=float).sort_index(),
        cost_at=pd.Series(cost_at, dtype=float).sort_index(),
        cost_drag=float(gross_total - net_total),
        source=prices.source,
        cost_breakdown=cost_breakdown,
        meta={
            "symbols": symbols,
            "target_weights": dict(zip(symbols, target.tolist(), strict=True)),
            "rule": "manual",
            "drift_band": 0.0,
            "cost_model": (
                {"kind": "schedule", "name": costs.name}
                if isinstance(costs, CostSchedule)
                else {"kind": "indian_equity", "exchange": costs.exchange}
                if isinstance(costs, EquityCosts)
                else {"kind": "flat_bps", "bps": costs.bps}
            ),
            "orders": order_count,
            "slippage": getattr(costs, "slippage", 0.0),
            "initial_capital": initial_capital,
            "sessions": len(index),
            "versions": [
                {
                    "label": entry.label,
                    "effective_date": stamp.date().isoformat(),
                    "target_weights": dict(zip(symbols, vector.tolist(), strict=True)),
                }
                for entry, stamp, vector in zip(ordered, snapped, targets, strict=True)
            ],
        },
    )


def run_backtest(
    prices: PriceMatrix,
    weights: dict[str, float],
    *,
    policy: RebalancePolicy | None = None,
    costs: Costs | EquityCosts | CostSchedule | None = None,
    initial_capital: float = 100_000.0,
) -> BacktestResult:
    """
    Simulate ``weights`` over ``prices`` under ``policy``.

    Returns an equity curve net of costs, the weight path, the sessions that
    were rebalanced, per-rebalance turnover, and the total return given up to
    costs -- the last of which is what makes two rebalancing schedules
    comparable on an honest basis.
    """
    policy = policy or RebalancePolicy()
    costs = costs or Costs()
    if not np.isfinite(initial_capital) or initial_capital <= 0:
        raise ValueError("initial capital must be positive and finite")

    symbols = prices.symbols
    target = normalise_weights(weights, symbols)
    closes = prices.closes
    close_values = closes.to_numpy(dtype=float)
    if not np.isfinite(close_values).all() or (close_values <= 0).any():
        raise ValueError("price matrix must contain only positive finite closes")
    index = closes.index

    # Bar-over-bar growth per holding. Row i is the move from i-1 to i, so the
    # first row is 1.0: nothing has moved on the day the portfolio is bought.
    growth = closes.div(closes.shift(1)).fillna(1.0).to_numpy()

    scheduled = set(calendar_dates(index, policy.rule))

    equity = np.empty(len(index), dtype=float)
    weight_path = np.empty((len(index), len(symbols)), dtype=float)
    turnover_at: dict[pd.Timestamp, float] = {}
    cost_at: dict[pd.Timestamp, float] = {}
    order_count = 0
    if isinstance(costs, (EquityCosts, CostSchedule)):
        cost_breakdown = dict.fromkeys(costs.breakdown(0.0, 0.0, orders=0), 0.0)
    else:
        cost_breakdown = {"total": 0.0, "orders": 0.0}

    # Held in currency per symbol rather than as weights. Weights are a ratio
    # and cannot answer "how much did this holding actually make", which is the
    # question an investor asks first; currency can, and weights divide back
    # out of it exactly.
    sleeve = target * float(initial_capital)
    price_pnl = np.zeros(len(symbols), dtype=float)  # made or lost on price
    cost_paid = np.zeros(len(symbols), dtype=float)  # costs charged to it
    gross_sleeve = sleeve.copy()  # the same run with costs off, for the drag

    for i, stamp in enumerate(index):
        if i > 0:
            # Drift: each sleeve grows by its own bar return, so the weights
            # move on their own between rebalances.
            grown = sleeve * growth[i]
            price_pnl += grown - sleeve
            sleeve = grown
            value = sleeve.sum()
            gross_sleeve = gross_sleeve * growth[i]

            held = sleeve / value
            if stamp in scheduled or drifted(held, target, policy.drift_band):
                desired = target * value
                # A rebalance conserves value, so the rupees bought equal the
                # rupees sold. That split matters: stamp duty is charged on the
                # buy leg only, which a single blended rate cannot express.
                traded, charge, orders = _apply_rebalance_costs(
                    desired, sleeve, value, costs, cost_breakdown
                )
                if traded > 0:
                    order_count += orders
                    # Attributed by each symbol's share of the traded value, so
                    # the holding that forced the trade carries the cost.
                    moved = np.abs(desired - sleeve)
                    share = moved / moved.sum() if moved.sum() > 0 else moved
                    cost_paid += charge * share
                    value -= charge
                    turnover_at[stamp] = traded
                    cost_at[stamp] = charge
                sleeve = target * value
                # The cost-free twin rebalances on the same sessions; only the
                # charge is skipped, so the difference is costs and nothing else.
                gross_sleeve = target * gross_sleeve.sum()
        else:
            value = sleeve.sum()

        equity[i] = value
        weight_path[i] = sleeve / value

    equity_series = pd.Series(equity, index=index, name="equity")
    gross_total = gross_sleeve.sum() / float(initial_capital) - 1.0
    net_total = equity[-1] / float(initial_capital) - 1.0

    # Itemised P&L. `contribution_pct` is each holding's share of the total
    # return in percentage points, so the column sums to the portfolio return --
    # which is the property that makes it an attribution rather than a list of
    # individual performances. A holding's own return and its contribution
    # differ whenever its weight is not 100%, and diverge further under
    # rebalancing, where capital is added to laggards and taken from winners.
    net_pnl = price_pnl - cost_paid
    first_close = closes.iloc[0].to_numpy()
    last_close = closes.iloc[-1].to_numpy()
    items = pd.DataFrame(
        {
            "weight_target": target,
            "weight_final": weight_path[-1],
            "invested": target * float(initial_capital),
            "price_pnl": price_pnl,
            "costs": cost_paid,
            "net_pnl": net_pnl,
            "contribution_pct": net_pnl / float(initial_capital),
            "symbol_return": (last_close / first_close) - 1.0,
        },
        index=pd.Index(symbols, name="symbol"),
    )

    return BacktestResult(
        equity=equity_series,
        items=items,
        weights=pd.DataFrame(weight_path, index=index, columns=symbols),
        rebalance_dates=pd.DatetimeIndex(sorted(turnover_at)),
        turnover=pd.Series(turnover_at, dtype=float).sort_index(),
        cost_at=pd.Series(cost_at, dtype=float).sort_index(),
        # What costs took out of the total return, in return terms. Zero when
        # nothing traded, which is the buy-and-hold case.
        cost_drag=float(gross_total - net_total),
        source=prices.source,
        cost_breakdown=cost_breakdown,
        meta={
            "symbols": symbols,
            "target_weights": dict(zip(symbols, target.tolist(), strict=True)),
            "rule": policy.rule,
            "drift_band": policy.drift_band,
            "cost_model": (
                {"kind": "schedule", "name": costs.name}
                if isinstance(costs, CostSchedule)
                else {"kind": "indian_equity", "exchange": costs.exchange}
                if isinstance(costs, EquityCosts)
                else {"kind": "flat_bps", "bps": costs.bps}
            ),
            "orders": order_count,
            "slippage": getattr(costs, "slippage", 0.0),
            "initial_capital": initial_capital,
            "sessions": len(index),
        },
    )
