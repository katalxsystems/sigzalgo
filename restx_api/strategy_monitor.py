import os

from flask import jsonify, make_response, request
from flask_restx import Namespace, Resource
from marshmallow import ValidationError

from limiter import limiter
from services.strategy_monitor_service import deactivate, upsert
from utils.logging import get_logger

from .data_schemas import StrategyMonitorDeactivateSchema, StrategyMonitorUpsertSchema

# A control action, not a data read: each call arms or disarms a live Flow
# schedule. Same rationale and default as the price-breach endpoints.
STRATEGY_MONITOR_LIMIT = os.getenv("WEBSOCKET_CONTROL_LIMIT", "10 per minute")
api = Namespace("strategymonitor", description="Strategy target/stoploss monitor (Flow) API")

logger = get_logger(__name__)

upsert_schema = StrategyMonitorUpsertSchema()
deactivate_schema = StrategyMonitorDeactivateSchema()


@api.route("/upsert", strict_slashes=False)
class StrategyMonitorUpsert(Resource):
    @limiter.limit(STRATEGY_MONITOR_LIMIT)
    def post(self):
        """Create or update a strategy's monitor and arm it.

        The monitor keeps the levels sent here and checks them on a schedule:
        combined strategy P&L against target/stoploss, and the underlying's
        spot against upper/lower spot stoploss. On a breach it POSTs one
        notification to webhook_url and then deactivates itself. Calling this
        again for the same strategy replaces the levels and re-arms it.
        See docs/api/flow/strategymonitor.md.
        """
        try:
            data = upsert_schema.load(request.json or {})
            api_key = data.pop("apikey")
            strategy = data.pop("strategy")
            _success, response_data, status_code = upsert(api_key, strategy, **data)
            return make_response(jsonify(response_data), status_code)
        except ValidationError as err:
            return make_response(jsonify({"status": "error", "message": err.messages}), 400)
        except Exception as e:
            logger.exception(f"Unexpected error in strategy monitor upsert endpoint: {e}")
            return make_response(
                jsonify({"status": "error", "message": "An unexpected error occurred"}), 500
            )


@api.route("/deactivate", strict_slashes=False)
class StrategyMonitorDeactivate(Resource):
    @limiter.limit(STRATEGY_MONITOR_LIMIT)
    def post(self):
        """Disarm a strategy's monitor. Idempotent.

        Also what the monitor calls on itself after a delivered notification.
        Requires the apikey the monitor was created with.
        """
        try:
            data = deactivate_schema.load(request.json or {})
            _success, response_data, status_code = deactivate(data["apikey"], data["strategy"])
            return make_response(jsonify(response_data), status_code)
        except ValidationError as err:
            return make_response(jsonify({"status": "error", "message": err.messages}), 400)
        except Exception as e:
            logger.exception(f"Unexpected error in strategy monitor deactivate endpoint: {e}")
            return make_response(
                jsonify({"status": "error", "message": "An unexpected error occurred"}), 500
            )
