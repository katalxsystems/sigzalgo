import os

from flask import jsonify, make_response, request
from flask_restx import Namespace, Resource
from marshmallow import ValidationError

from database.auth_db import verify_api_key
from limiter import limiter
from services.price_breach_service import create_and_activate, deactivate
from utils.logging import get_logger

from .data_schemas import PriceBreachCreateSchema, PriceBreachDeactivateSchema

# A control action, not a data read -- each call creates/activates a live
# Flow price watch (or tears one down). Same rationale and default as the
# /api/v1/ws/* subscribe endpoints.
PRICE_BREACH_LIMIT = os.getenv("WEBSOCKET_CONTROL_LIMIT", "10 per minute")
api = Namespace("pricebreach", description="Price Breach Monitor (Flow) API")

logger = get_logger(__name__)

create_schema = PriceBreachCreateSchema()
deactivate_schema = PriceBreachDeactivateSchema()


@api.route("/create", strict_slashes=False)
class PriceBreachCreate(Resource):
    @limiter.limit(PRICE_BREACH_LIMIT)
    def post(self):
        """Create and activate a price-breach watch.

        Builds a Flow workflow with a single `priceAlert` trigger (fires
        once price goes outside [stop_loss, target1]) chained to an
        `httpRequest` action that POSTs the breach to webhook_url, then
        activates it. See docs/api/market-data/pricebreach.md, or
        scripts/price_breach_monitor.py for the equivalent session-based
        flow using the Flow editor's own routes directly.

        The watch is "once": it stops polling the instant it fires, but the
        workflow still shows active until you deactivate it -- call
        POST /pricebreach/<id>/deactivate from your webhook receiver once it
        has handled the notification.
        """
        try:
            data = create_schema.load(request.json)
            api_key = data["apikey"]

            if verify_api_key(api_key) is None:
                return make_response(
                    jsonify({"status": "error", "message": "Invalid openalgo apikey"}), 403
                )

            success, response_data, status_code = create_and_activate(
                api_key=api_key,
                symbol=data["symbol"],
                exchange=data["exchange"],
                stop_loss=data["stop_loss"],
                target1=data["target1"],
                webhook_url=data["webhook_url"],
                entry_price=data["entry_price"],
                name=data["name"],
            )
            return make_response(jsonify(response_data), status_code)

        except ValidationError as err:
            return make_response(jsonify({"status": "error", "message": err.messages}), 400)
        except Exception as e:
            logger.exception(f"Unexpected error in price breach create endpoint: {e}")
            return make_response(
                jsonify({"status": "error", "message": "An unexpected error occurred"}), 500
            )


@api.route("/<int:workflow_id>/deactivate", strict_slashes=False)
class PriceBreachDeactivate(Resource):
    @limiter.limit(PRICE_BREACH_LIMIT)
    def post(self, workflow_id):
        """Deactivate a price-breach watch by workflow id.

        Requires the same apikey the watch was created with.
        """
        try:
            data = deactivate_schema.load(request.json)
            api_key = data["apikey"]

            if verify_api_key(api_key) is None:
                return make_response(
                    jsonify({"status": "error", "message": "Invalid openalgo apikey"}), 403
                )

            success, response_data, status_code = deactivate(workflow_id, api_key)
            return make_response(jsonify(response_data), status_code)

        except ValidationError as err:
            return make_response(jsonify({"status": "error", "message": err.messages}), 400)
        except Exception as e:
            logger.exception(f"Unexpected error in price breach deactivate endpoint: {e}")
            return make_response(
                jsonify({"status": "error", "message": "An unexpected error occurred"}), 500
            )
