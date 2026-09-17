import os

from flask import jsonify, make_response, request
from flask_restx import Namespace, Resource
from marshmallow import ValidationError

from database.auth_db import verify_api_key
from limiter import limiter
from services.price_breach_service import create_and_activate, deactivate_by_call_id, deactivate_one
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
        """Create and activate a price-breach watch (up to two workflows).

        Builds one Flow workflow with a `priceAlert` trigger watching
        `outside_channel` [stop_loss, target1] -- its webhook payload carries
        `breach_type` of "stop_loss" or "target1" depending on which bound
        was actually crossed -- and, unless active_price already equals
        entry_price, a second one watching for price crossing back through
        entry_price (`breach_type` "entry_price"), in whichever direction is
        a genuine "re-cross" given where price was (active_price) when this
        call was made. See docs/api/market-data/pricebreach.md, or
        scripts/price_breach_monitor.py for the equivalent session-based
        flow using the Flow editor's own routes directly.

        Each watch is "once" while it is live and notifies webhook_url when it
        fires -- no separate deactivate call is needed from your webhook
        receiver either way, but the two watches deactivate at different
        scopes: sl_target firing (a real stop-loss/target breach) ends the
        call and deactivates both workflows; entry_recross firing (price just
        came back to entry) deactivates only itself, since price can still go
        on to hit stop_loss or target1 afterwards. If active_price already
        equals entry_price, no entry_recross watch is created (there is no
        direction to watch for) -- instead an entry_price breach notification
        is sent to webhook_url immediately, in the background.

        mentor_id is echoed back in every webhook payload alongside call_id,
        and -- unless name is given -- both workflows are named from
        call_id, mentor_id, and symbol (the "script").
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
                call_id=data["call_id"],
                mentor_id=data["mentor_id"],
                symbol=data["symbol"],
                exchange=data["exchange"],
                active_price=data["active_price"],
                entry_price=data["entry_price"],
                stop_loss=data["stop_loss"],
                target1=data["target1"],
                webhook_url=data["webhook_url"],
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


@api.route("/<string:call_id>/deactivate", strict_slashes=False)
class PriceBreachDeactivate(Resource):
    @limiter.limit(PRICE_BREACH_LIMIT)
    def post(self, call_id):
        """Deactivate a price-breach call by call_id.

        Tears down both of the call's workflows (sl_target and, if it
        exists, entry_recross) together -- call_id is the identifier you
        passed to /api/v1/pricebreach/create, not either workflow_id from
        its response.

        Not usually needed -- sl_target firing already deactivates both
        workflows on its own (entry_recross firing deactivates only itself;
        see POST /api/v1/pricebreach/workflow/<workflow_id>/deactivate).
        Exposed for manual/operator use (e.g. canceling a call before it
        fires). Requires the same apikey
        the call was created with.
        """
        try:
            data = deactivate_schema.load(request.json)
            api_key = data["apikey"]

            if verify_api_key(api_key) is None:
                return make_response(
                    jsonify({"status": "error", "message": "Invalid openalgo apikey"}), 403
                )

            success, response_data, status_code = deactivate_by_call_id(call_id, api_key)
            return make_response(jsonify(response_data), status_code)

        except ValidationError as err:
            return make_response(jsonify({"status": "error", "message": err.messages}), 400)
        except Exception as e:
            logger.exception(f"Unexpected error in price breach deactivate endpoint: {e}")
            return make_response(
                jsonify({"status": "error", "message": "An unexpected error occurred"}), 500
            )


@api.route("/workflow/<int:workflow_id>/deactivate", strict_slashes=False)
class PriceBreachDeactivateWorkflow(Resource):
    @limiter.limit(PRICE_BREACH_LIMIT)
    def post(self, workflow_id):
        """Deactivate a single price-breach workflow by its own workflow_id,
        without touching any sibling workflow created for the same call_id.

        This is the scope the entry_recross watch's own deactivate node uses:
        price coming back to entry does not end the call (it can still go on
        to hit stop_loss or target1), so only that watch is retired. Compare
        with POST /api/v1/pricebreach/<call_id>/deactivate, which tears down
        every workflow for a call_id together -- the scope the sl_target
        watch uses, since a real stop-loss/target breach does end the call.

        Requires the apikey the workflow was created with.
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
            logger.exception(f"Unexpected error in price breach workflow deactivate endpoint: {e}")
            return make_response(
                jsonify({"status": "error", "message": "An unexpected error occurred"}), 500
            )
