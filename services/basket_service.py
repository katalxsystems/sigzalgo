"""
Portfolio Basket service facade: CRUD over database.basket_db, plus running a
versioned backtest across a basket's whole rebalance history.

Follows the same service-layer contract as services/portfolio_service.py --
``(success, payload, status_code)``, never raises for a caller mistake.

Deliberately reuses services.portfolio_service's private report-building
helpers (``_curve``, ``_clean``, ``_series_analytics``, ``_allocation_path``,
``_asset_returns``, ``_symbol_names``, ``_build_costs``) rather than
re-deriving the same analytics a second time -- a basket backtest and a
stateless one should never be able to disagree about what a Sharpe ratio or a
drawdown curve means.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd

from database import basket_db
from portfolio.analytics import (
    average_pairwise_correlation,
    concentration,
    correlation_matrix,
    diversification_ratio,
    summary,
)
from portfolio.attribution import attribution
from portfolio.crisis import crisis_analysis
from portfolio.data import BENCHMARK_EXCHANGES, DataError, load_prices
from portfolio.engine import ScheduleEntry, run_versioned_backtest
from portfolio.grouping import structure
from portfolio.health import portfolio_health
from portfolio.insights import build_findings
from services.portfolio_service import (
    MAX_SYMBOLS,
    _allocation_path,
    _asset_returns,
    _build_costs,
    _clean,
    _curve,
    _series_analytics,
    _symbol_names,
)
from utils.logging import get_logger

logger = get_logger(__name__)


def _not_found() -> tuple[bool, dict[str, Any], int]:
    return False, {"status": "error", "message": "Basket not found"}, 404


def create_basket(
    user_id: str,
    name: str,
    holdings: list[dict[str, Any]],
    effective_date: str,
    *,
    benchmark: str | None = None,
    benchmark_exchange: str = "NSE_INDEX",
    cost_config: dict | None = None,
    initial_capital: float = 100_000.0,
    note: str | None = None,
) -> tuple[bool, dict[str, Any], int]:
    if not holdings:
        return False, {"status": "error", "message": "no holdings supplied"}, 400
    if len(holdings) > MAX_SYMBOLS:
        return (
            False,
            {
                "status": "error",
                "message": f"{len(holdings)} holdings exceeds the {MAX_SYMBOLS} limit",
            },
            400,
        )
    try:
        parsed_date = date.fromisoformat(effective_date)
    except (TypeError, ValueError):
        return False, {"status": "error", "message": "effective_date must be YYYY-MM-DD"}, 400

    basket = basket_db.create_basket(
        user_id,
        name,
        holdings,
        parsed_date,
        benchmark=benchmark,
        benchmark_exchange=benchmark_exchange,
        cost_config=cost_config,
        initial_capital=initial_capital,
        note=note,
    )
    if basket is None:
        return (
            False,
            {
                "status": "error",
                "message": "could not create basket -- name taken, or the per-user limit was reached",
            },
            409,
        )
    return True, {"status": "success", "basket": basket}, 201


def list_baskets(user_id: str) -> tuple[bool, dict[str, Any], int]:
    return True, {"status": "success", "baskets": basket_db.list_baskets(user_id)}, 200


def get_basket(user_id: str, basket_id: int) -> tuple[bool, dict[str, Any], int]:
    basket = basket_db.get_basket(user_id, basket_id)
    if basket is None:
        return _not_found()
    return True, {"status": "success", "basket": basket}, 200


def add_rebalance(
    user_id: str,
    basket_id: int,
    holdings: list[dict[str, Any]],
    effective_date: str,
    *,
    note: str | None = None,
) -> tuple[bool, dict[str, Any], int]:
    if not holdings:
        return False, {"status": "error", "message": "no holdings supplied"}, 400
    if len(holdings) > MAX_SYMBOLS:
        return (
            False,
            {
                "status": "error",
                "message": f"{len(holdings)} holdings exceeds the {MAX_SYMBOLS} limit",
            },
            400,
        )
    try:
        parsed_date = date.fromisoformat(effective_date)
    except (TypeError, ValueError):
        return False, {"status": "error", "message": "effective_date must be YYYY-MM-DD"}, 400

    if basket_db.get_basket(user_id, basket_id) is None:
        return _not_found()

    version = basket_db.add_version(user_id, basket_id, holdings, parsed_date, note=note)
    if version is None:
        return (
            False,
            {
                "status": "error",
                "message": "could not add a rebalance -- effective_date must be strictly "
                "after the basket's latest version, or the per-basket version limit was reached",
            },
            409,
        )
    return True, {"status": "success", "version": version}, 201


def update_basket(user_id: str, basket_id: int, **fields: Any) -> tuple[bool, dict[str, Any], int]:
    basket = basket_db.update_basket(user_id, basket_id, **fields)
    if basket is None:
        if basket_db.get_basket(user_id, basket_id) is None:
            return _not_found()
        return (
            False,
            {"status": "error", "message": "could not update basket -- name may be taken"},
            409,
        )
    return True, {"status": "success", "basket": basket}, 200


def delete_basket(user_id: str, basket_id: int) -> tuple[bool, dict[str, Any], int]:
    if not basket_db.delete_basket(user_id, basket_id):
        return _not_found()
    return True, {"status": "success"}, 200


def run_basket_backtest(
    user_id: str,
    basket_id: int,
    *,
    end_date: str | None = None,
    risk_free_rate: float = 0.0,
    source: str = "db",
    api_key: str | None = None,
    auth_token: str | None = None,
    feed_token: str | None = None,
    broker: str | None = None,
) -> tuple[bool, dict[str, Any], int]:
    """
    Run the basket's whole rebalance history as one versioned backtest.

    The window always starts at version 1's ``effective_date`` -- that is what
    "the basket's history" means -- and runs to ``end_date`` (default today).
    There is no way to start partway through a basket's timeline: the engine
    requires the first schedule entry to land on the price matrix's first
    session, and mid-history is not a state a basket was ever actually in.
    """
    basket = basket_db.get_basket(user_id, basket_id)
    if basket is None:
        return _not_found()

    versions = basket["versions"]
    if not versions:
        # Cannot happen through normal create_basket/add_version, but a basket
        # with no versions has nothing to simulate.
        return False, {"status": "error", "message": "basket has no versions"}, 422

    start_date = versions[0]["effective_date"]
    end_date = end_date or date.today().isoformat()

    symbol_exchange: dict[str, str] = {}
    for version in versions:
        for holding in version["holdings"]:
            symbol_exchange.setdefault(holding["symbol"], holding.get("exchange", "NSE"))
    symbols = list(symbol_exchange.keys())
    exchanges = [symbol_exchange[s] for s in symbols]

    fetch = {
        "source": source,
        "api_key": api_key,
        "auth_token": auth_token,
        "feed_token": feed_token,
        "broker": broker,
    }

    cost_config = basket.get("cost_config") or {}
    try:
        prices = load_prices(symbols, exchanges, start_date, end_date, **fetch)
        charged = _build_costs(
            cost_model=cost_config.get("cost_model", "indian_equity"),
            brokerage_pct=cost_config.get("brokerage_pct", 0.0),
            cost_exchange=cost_config.get("cost_exchange", "NSE"),
            charge_overrides=cost_config.get("charges"),
            gst_rate=cost_config.get("gst_rate"),
            cost_bps=cost_config.get("cost_bps", 0.0),
            slippage=cost_config.get("slippage", 0.0),
        )
        schedule = [
            ScheduleEntry(
                effective_date=date.fromisoformat(v["effective_date"]),
                weights={h["symbol"]: h["weight"] for h in v["holdings"]},
                label=f"v{v['version_number']}",
            )
            for v in versions
        ]
        result = run_versioned_backtest(
            prices,
            schedule,
            costs=charged,
            initial_capital=basket["initial_capital"],
        )
    except DataError as exc:
        return False, {"status": "error", "message": str(exc)}, 422
    except ValueError as exc:
        return False, {"status": "error", "message": str(exc)}, 400
    except Exception:  # noqa: BLE001 - facade must not leak a 500 body
        logger.exception("basket backtest failed for basket %s", basket_id)
        return False, {"status": "error", "message": "Backtest failed."}, 500

    benchmark = basket.get("benchmark")
    benchmark_exchange = basket.get("benchmark_exchange") or "NSE_INDEX"
    bench_returns = None
    bench_curve: list[dict[str, Any]] = []
    if benchmark:
        try:
            bench_prices = load_prices(
                [benchmark.strip().upper()],
                benchmark_exchange,
                start_date,
                end_date,
                allowed_exchanges=BENCHMARK_EXCHANGES,
                **fetch,
            )
            bench_close = bench_prices.closes.iloc[:, 0].reindex(prices.closes.index)
            bench_close = bench_close.ffill().dropna()
            if len(bench_close) > 1:
                bench_returns = bench_close.pct_change().iloc[1:]
                bench_curve = _curve(bench_close / bench_close.iloc[0] * basket["initial_capital"])
        except DataError as exc:
            logger.warning("basket benchmark %s unavailable: %s", benchmark, exc)

    returns = result.returns
    holding_returns = prices.returns()
    # A versioned basket has no single target: the latest version is what the
    # basket currently stands for, so composition-shaped analytics (weights of
    # today, not of the whole history) are measured against it.
    latest_targets = pd.Series(result.meta["target_weights"])

    corr = correlation_matrix(holding_returns)
    metrics = _clean(summary(returns, bench_returns, rf=risk_free_rate))

    # Per-rebalance breakdown: the stored diff plus what the engine actually
    # realised on that date (value just after the rebalance, cost charged).
    version_breakdown = []
    for entry_meta, version in zip(result.meta["versions"], versions, strict=True):
        stamp = pd.Timestamp(entry_meta["effective_date"])
        value_at = float(result.equity.loc[stamp]) if stamp in result.equity.index else None
        cost_at = float(result.cost_at.get(stamp, 0.0))
        version_breakdown.append(
            {
                "version_number": version["version_number"],
                "effective_date": version["effective_date"],
                "holdings": version["holdings"],
                "change_summary": version["change_summary"],
                "note": version["note"],
                "target_weights": _clean(entry_meta["target_weights"]),
                "value_at_rebalance": _clean(value_at),
                "cost_at_rebalance": _clean(cost_at),
            }
        )

    payload = {
        "status": "success",
        "meta": {
            **result.meta,
            "source": result.source,
            "start": start_date,
            "end": end_date,
            "benchmark": benchmark,
            "risk_free_rate": risk_free_rate,
            "total_return_basis": "price",
            "data_warnings": prices.warnings,
        },
        "basket": {k: v for k, v in basket.items() if k != "versions"},
        "versions": version_breakdown,
        "equity": _curve(result.equity),
        "benchmark_equity": bench_curve,
        "metrics": metrics,
        "items": _clean(
            [
                {"symbol": symbol, **row}
                for symbol, row in result.items.to_dict(orient="index").items()
            ]
        ),
        "correlation": {
            "symbols": list(corr.columns),
            "matrix": _clean(corr.to_numpy().tolist()) if not corr.empty else [],
            "average_pairwise": _clean(average_pairwise_correlation(holding_returns)),
        },
        "diversification": _clean(
            {
                **concentration(latest_targets),
                "diversification_ratio": diversification_ratio(latest_targets, holding_returns),
            }
        ),
        "series": _series_analytics(returns, risk_free_rate, bench_returns),
        "asset_returns": _clean(_asset_returns(prices)),
        "structure": _clean(structure(latest_targets, holding_returns, _symbol_names(symbols))),
        "allocation": _clean(_allocation_path(result.weights)),
        "crisis": _clean(crisis_analysis(returns, bench_returns)),
        "health": portfolio_health(
            weights=latest_targets,
            returns=holding_returns,
            closes=prices.closes,
            sharpe=metrics.get("sharpe", float("nan")) or float("nan"),
            sortino=metrics.get("sortino", float("nan")) or float("nan"),
            max_drawdown=metrics.get("max_drawdown", float("nan")) or float("nan"),
            cost_drag=result.cost_drag,
            turnover=float(result.turnover.sum()),
        ),
        "attribution": _clean(
            attribution(
                holding_returns,
                result.weights,
                latest_targets,
                result.returns,
                bench_returns,
                result.items["contribution_pct"],
            )
        ),
        "costs": _clean(
            {
                "model": cost_config.get("cost_model", "indian_equity"),
                **{
                    ("gst" if key == "tax" else key): value
                    for key, value in result.cost_breakdown.items()
                },
                "drag": result.cost_drag,
                "turnover": float(result.turnover.sum()),
            }
        ),
        "rebalancing": {
            "rule": "manual",
            "count": len(result.rebalance_dates),
            "cost_drag": _clean(result.cost_drag),
            "turnover_total": _clean(float(result.turnover.sum())),
            "dates": [d.date().isoformat() for d in result.rebalance_dates],
        },
    }
    payload["insights"] = _clean(build_findings(payload))
    return True, payload, 200
