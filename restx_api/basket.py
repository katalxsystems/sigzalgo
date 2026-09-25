"""
Portfolio Basket API: named, saved baskets with a manual-rebalance version
history, backed by portfolio.engine.run_versioned_backtest.

Every route is a POST with identifiers in the body, matching the rest of
``/api/v1`` and restx_api/strategy.py's own stated rationale: external
callers (TradingView, the Python SDK, Excel, MCP) cannot always choose a
method or set a header.

**404, never 403, for a basket that is not yours** -- same anti-enumeration
rule as restx_api/strategy.py. Ownership is resolved through the *platform*
user (database.auth_db.get_owner_username), not the broker account_id
verify_api_key returns, so one basket is visible no matter which of a user's
broker accounts the calling API key belongs to.
"""

import os

from flask import jsonify, make_response, request
from flask_restx import Namespace, Resource
from marshmallow import Schema, ValidationError, fields, validate

from database.auth_db import get_auth_token_broker, get_owner_username, verify_api_key
from limiter import limiter
from portfolio.data import BENCHMARK_EXCHANGES
from services.basket_service import (
    add_rebalance,
    create_basket,
    delete_basket,
    get_basket,
    list_baskets,
    run_basket_backtest,
    update_basket,
)
from services.portfolio_service import MAX_SYMBOLS
from utils.logging import get_logger

API_RATE_LIMIT = os.getenv("API_RATE_LIMIT", "10 per second")
BACKTEST_RATE_LIMIT = os.getenv("PORTFOLIO_API_RATE_LIMIT", "10 per minute")
api = Namespace("basket", description="Portfolio Basket API: saved, versioned baskets")

logger = get_logger(__name__)

INVALID_KEY = "Invalid openalgo apikey"
NOT_FOUND = "Basket not found"

# Cash equity and ETFs only, matching restx_api/portfolio.py's own allow-list
# and the engine's. Kept local rather than imported from restx_api.portfolio:
# a cross-submodule import there would force restx_api/__init__.py's full
# namespace registration to run, defeating the standalone-module loading
# test_basket_api.py (and test_portfolio_api.py before it) relies on.
PORTFOLIO_EXCHANGES = ["NSE", "BSE"]


class HoldingSchema(Schema):
    symbol = fields.Str(required=True, validate=validate.Length(min=1, max=64))
    exchange = fields.Str(load_default="NSE", validate=validate.OneOf(PORTFOLIO_EXCHANGES))
    weight = fields.Float(required=True, validate=validate.Range(min=0))


def _success(payload: dict | None = None, code: int = 200):
    body = {"status": "success"}
    if payload:
        body.update(payload)
    return make_response(jsonify(body), code)


def _failure(message, code: int, payload: dict | None = None):
    body = {"status": "error", "message": message}
    if payload:
        body.update(payload)
    return make_response(jsonify(body), code)


def _resolve_owner(api_key: str):
    """Validate the key and resolve the *platform* owner it belongs to.

    Returns ``(user_id, error_response)``; exactly one is meaningful. An
    orphaned key -- valid, but its account_id has no matching Auth row (a
    pre-multi-account key, or a removed account) -- cannot be mapped to an
    owner and is refused rather than silently scoped to nothing.
    """
    account_id = verify_api_key(api_key)
    if account_id is None:
        return None, _failure(INVALID_KEY, 403)

    owner = get_owner_username(account_id)
    if owner is None:
        return None, _failure(
            "Could not resolve the account owner for this API key. "
            "Reconnect your broker account and try again.",
            403,
        )
    return owner, None


class BasketCreateSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    name = fields.Str(required=True, validate=validate.Length(min=1, max=120))
    holdings = fields.List(
        fields.Nested(HoldingSchema),
        required=True,
        validate=validate.Length(min=1, max=MAX_SYMBOLS),
    )
    effective_date = fields.Str(required=True)
    benchmark = fields.Str(load_default=None, allow_none=True)
    benchmark_exchange = fields.Str(
        load_default="NSE_INDEX", validate=validate.OneOf(BENCHMARK_EXCHANGES)
    )
    cost_model = fields.Str(
        load_default="indian_equity", validate=validate.OneOf(["indian_equity", "flat_bps"])
    )
    brokerage_pct = fields.Float(load_default=0.0, validate=validate.Range(min=0, max=0.05))
    cost_exchange = fields.Str(load_default="NSE", validate=validate.OneOf(PORTFOLIO_EXCHANGES))
    charges = fields.Dict(
        keys=fields.Str(),
        values=fields.Dict(keys=fields.Str(), values=fields.Float(allow_none=True)),
        load_default=dict,
    )
    gst_rate = fields.Float(
        load_default=None, allow_none=True, validate=validate.Range(min=0, max=1)
    )
    cost_bps = fields.Float(load_default=0.0, validate=validate.Range(min=0, max=1000))
    slippage = fields.Float(load_default=0.0, validate=validate.Range(min=0, max=0.1))
    initial_capital = fields.Float(load_default=100000.0, validate=validate.Range(min=1))
    note = fields.Str(load_default=None, allow_none=True)


class BasketIdSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    basket_id = fields.Int(required=True)


class BasketRebalanceSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    basket_id = fields.Int(required=True)
    holdings = fields.List(
        fields.Nested(HoldingSchema),
        required=True,
        validate=validate.Length(min=1, max=MAX_SYMBOLS),
    )
    effective_date = fields.Str(required=True)
    note = fields.Str(load_default=None, allow_none=True)


class BasketUpdateSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    basket_id = fields.Int(required=True)
    name = fields.Str(load_default=None, allow_none=True, validate=validate.Length(min=1, max=120))
    benchmark = fields.Str(load_default=None, allow_none=True)
    benchmark_exchange = fields.Str(
        load_default=None, allow_none=True, validate=validate.OneOf(BENCHMARK_EXCHANGES)
    )
    initial_capital = fields.Float(
        load_default=None, allow_none=True, validate=validate.Range(min=1)
    )


class BasketBacktestSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    basket_id = fields.Int(required=True)
    end_date = fields.Str(load_default=None, allow_none=True)
    risk_free_rate = fields.Float(load_default=0.0, validate=validate.Range(min=0, max=0.5))
    source = fields.Str(load_default="db", validate=validate.OneOf(["db", "api"]))


create_schema = BasketCreateSchema()
id_schema = BasketIdSchema()
rebalance_schema = BasketRebalanceSchema()
update_schema = BasketUpdateSchema()
backtest_schema = BasketBacktestSchema()


def _dispatch(ok: bool, payload: dict, status: int):
    return make_response(jsonify(payload), status)


@api.route("/create", strict_slashes=False)
class BasketCreate(Resource):
    @limiter.limit(API_RATE_LIMIT)
    def post(self):
        """Create a basket with its first version."""
        try:
            data = create_schema.load(request.json or {})
        except ValidationError as err:
            return _failure(err.messages, 400)

        user_id, error = _resolve_owner(data.pop("apikey"))
        if error:
            return error

        _ok, payload, status = create_basket(
            user_id,
            data["name"],
            data["holdings"],
            data["effective_date"],
            benchmark=data["benchmark"],
            benchmark_exchange=data["benchmark_exchange"],
            cost_config={
                "cost_model": data["cost_model"],
                "brokerage_pct": data["brokerage_pct"],
                "cost_exchange": data["cost_exchange"],
                "charges": data["charges"],
                "gst_rate": data["gst_rate"],
                "cost_bps": data["cost_bps"],
                "slippage": data["slippage"],
            },
            initial_capital=data["initial_capital"],
            note=data["note"],
        )
        return _dispatch(_ok, payload, status)


@api.route("/list", strict_slashes=False)
class BasketList(Resource):
    @limiter.limit(API_RATE_LIMIT)
    def post(self):
        """List this user's baskets, newest first."""
        api_key = (request.json or {}).get("apikey")
        if not api_key:
            return _failure(INVALID_KEY, 403)
        user_id, error = _resolve_owner(api_key)
        if error:
            return error

        _ok, payload, status = list_baskets(user_id)
        return _dispatch(_ok, payload, status)


@api.route("/get", strict_slashes=False)
class BasketGet(Resource):
    @limiter.limit(API_RATE_LIMIT)
    def post(self):
        """One basket with its full version history."""
        try:
            data = id_schema.load(request.json or {})
        except ValidationError as err:
            return _failure(err.messages, 400)

        user_id, error = _resolve_owner(data["apikey"])
        if error:
            return error

        _ok, payload, status = get_basket(user_id, data["basket_id"])
        return _dispatch(_ok, payload, status)


@api.route("/rebalance", strict_slashes=False)
class BasketRebalance(Resource):
    @limiter.limit(API_RATE_LIMIT)
    def post(self):
        """Add a manual rebalance: a new version with an updated composition."""
        try:
            data = rebalance_schema.load(request.json or {})
        except ValidationError as err:
            return _failure(err.messages, 400)

        user_id, error = _resolve_owner(data["apikey"])
        if error:
            return error

        _ok, payload, status = add_rebalance(
            user_id,
            data["basket_id"],
            data["holdings"],
            data["effective_date"],
            note=data["note"],
        )
        return _dispatch(_ok, payload, status)


@api.route("/update", strict_slashes=False)
class BasketUpdate(Resource):
    @limiter.limit(API_RATE_LIMIT)
    def post(self):
        """Rename a basket or change its benchmark / capital settings."""
        try:
            data = update_schema.load(request.json or {})
        except ValidationError as err:
            return _failure(err.messages, 400)

        user_id, error = _resolve_owner(data["apikey"])
        if error:
            return error

        fields_to_update = {
            key: data[key]
            for key in ("name", "benchmark", "benchmark_exchange", "initial_capital")
            if data.get(key) is not None
        }
        if not fields_to_update:
            return _failure("no fields to update", 400)

        _ok, payload, status = update_basket(user_id, data["basket_id"], **fields_to_update)
        return _dispatch(_ok, payload, status)


@api.route("/delete", strict_slashes=False)
class BasketDelete(Resource):
    @limiter.limit(API_RATE_LIMIT)
    def post(self):
        """Delete a basket and its whole version history."""
        try:
            data = id_schema.load(request.json or {})
        except ValidationError as err:
            return _failure(err.messages, 400)

        user_id, error = _resolve_owner(data["apikey"])
        if error:
            return error

        _ok, payload, status = delete_basket(user_id, data["basket_id"])
        return _dispatch(_ok, payload, status)


@api.route("/backtest", strict_slashes=False)
class BasketBacktest(Resource):
    @limiter.limit(BACKTEST_RATE_LIMIT)
    def post(self):
        """Run the basket's whole rebalance history and compare it to its benchmark."""
        try:
            data = backtest_schema.load(request.json or {})
        except ValidationError as err:
            return _failure(err.messages, 400)

        api_key = data.pop("apikey")
        user_id, error = _resolve_owner(api_key)
        if error:
            return error

        auth_token = feed_token = broker = None
        if data["source"] == "api":
            auth_token, feed_token, broker = get_auth_token_broker(api_key, include_feed_token=True)
            if auth_token is None:
                return _failure(
                    "No broker session for source='api'. Log in to your broker, "
                    "or use source='db' to backtest from local history.",
                    403,
                )

        _ok, payload, status = run_basket_backtest(
            user_id,
            data["basket_id"],
            end_date=data["end_date"],
            risk_free_rate=data["risk_free_rate"],
            source=data["source"],
            api_key=api_key,
            auth_token=auth_token,
            feed_token=feed_token,
            broker=broker,
        )
        return _dispatch(_ok, payload, status)
