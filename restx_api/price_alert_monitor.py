import os

from flask import jsonify, make_response, request
from flask_restx import Namespace, Resource
from marshmallow import ValidationError

from database.auth_db import verify_api_key
from limiter import limiter
from services.price_alert_service import create_and_activate
from services.price_breach_service import deactivate_one
from utils.logging import get_logger

from .data_schemas import PriceAlertCreateSchema, PriceAlertDeactivateSchema

# Same control-action rate limit price_breach_monitor.py uses: each call
# creates/activates or tears down a live Flow price watch, not a data read.
PRICE_ALERT_LIMIT = os.getenv("WEBSOCKET_CONTROL_LIMIT", "10 per minute")
api = Namespace("pricealert", description="Price Alert (Flow) API")

logger = get_logger(__name__)

create_schema = PriceAlertCreateSchema()
deactivate_schema = PriceAlertDeactivateSchema()


@api.route("/create", strict_slashes=False)
class PriceAlertCreate(Resource):
    @limiter.limit(PRICE_ALERT_LIMIT)
    def post(self):
        """Create and activate a single-condition price alert (one Flow workflow).

        Builds a `priceAlert` trigger (condition/price/trigger/expiration)
        wired to an `httpRequest` node that POSTs to webhook_url when the
        condition is met, then arms it. A `trigger: "once"` alert (the
        default) deactivates its own workflow the instant it fires -- Flow's
        own behavior, nothing extra to call. An `every_time` alert keeps
        watching until its `expiration` or a manual
        POST /api/v1/pricealert/<workflow_id>/deactivate.

        condition is one of: above, below, crosses_above, crosses_below.
        See docs/api/market-data/pricealert.md.
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
                condition=data["condition"],
                price=data["price"],
                webhook_url=data["webhook_url"],
                trigger=data["trigger"],
                expiration=data["expiration"],
                message=data["message"],
                name=data["name"],
            )
            return make_response(jsonify(response_data), status_code)

        except ValidationError as err:
            return make_response(jsonify({"status": "error", "message": err.messages}), 400)
        except Exception as e:
            logger.exception(f"Unexpected error in price alert create endpoint: {e}")
            return make_response(
                jsonify({"status": "error", "message": "An unexpected error occurred"}), 500
            )


@api.route("/<int:workflow_id>/deactivate", strict_slashes=False)
class PriceAlertDeactivate(Resource):
    @limiter.limit(PRICE_ALERT_LIMIT)
    def post(self, workflow_id):
        """Deactivate one price alert by its workflow_id.

        Not usually needed -- a "once" alert already deactivates itself when
        it fires, and an "every_time" alert is meant to keep running until
        its own expiration. Exposed for manual/operator use (e.g. canceling
        an alert before it fires). Requires the same apikey the alert was
        created with (services.price_breach_service.deactivate_one() is
        reused as-is here: it is already generic over any priceAlert-backed
        workflow_id, not pricebreach-specific).
        """
        try:
            data = deactivate_schema.load(request.json)
            api_key = data["apikey"]

            if verify_api_key(api_key) is None:
                return make_response(
                    jsonify({"status": "error", "message": "Invalid openalgo apikey"}), 403
                )

            success, message, status_code = deactivate_one(workflow_id, api_key)
            return make_response(
                jsonify({"status": "success" if success else "error", "message": message}),
                status_code,
            )

        except ValidationError as err:
            return make_response(jsonify({"status": "error", "message": err.messages}), 400)
        except Exception as e:
            logger.exception(f"Unexpected error in price alert deactivate endpoint: {e}")
            return make_response(
                jsonify({"status": "error", "message": "An unexpected error occurred"}), 500
            )
