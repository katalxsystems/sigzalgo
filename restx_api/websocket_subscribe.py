import os

from flask import jsonify, make_response, request
from flask_restx import Namespace, Resource
from marshmallow import ValidationError

from database.auth_db import get_broker_name, verify_api_key
from limiter import limiter
from services.websocket_service import (
    subscribe_to_symbols,
    unsubscribe_all,
    unsubscribe_from_symbols,
)
from utils.logging import get_logger

from .data_schemas import WebSocketSubscribeSchema, WebSocketUnsubscribeAllSchema

# Separate, tighter limit than plain data reads (quotes/history): each call
# fans out into a real broker-side subscribe/unsubscribe per symbol, so it's
# priced closer to an order-control action than a read. See
# docs/audit/PUBLIC_IP_SECURITY_AUDIT_2026-04.md H3 / §2.4.
WEBSOCKET_CONTROL_LIMIT = os.getenv("WEBSOCKET_CONTROL_LIMIT", "10 per minute")
api = Namespace("ws", description="WebSocket Subscription Control API")

logger = get_logger(__name__)

subscribe_schema = WebSocketSubscribeSchema()
unsubscribe_schema = WebSocketSubscribeSchema()
unsubscribe_all_schema = WebSocketUnsubscribeAllSchema()


def _resolve_account_and_broker(api_key: str):
    """Verify the API key and resolve (account_id, broker_name).

    Returns (account_id, broker, error_response) -- error_response is None on
    success, otherwise a ready-to-return (dict, status_code) pair.
    """
    account_id = verify_api_key(api_key)
    if account_id is None:
        return None, None, ({"status": "error", "message": "Invalid openalgo apikey"}, 403)

    broker = get_broker_name(api_key)
    if not broker:
        return None, None, (
            {"status": "error", "message": "No broker configuration found for this account"},
            404,
        )

    return account_id, broker, None


@api.route("/subscribe", strict_slashes=False)
class WebSocketSubscribe(Resource):
    @limiter.limit(WEBSOCKET_CONTROL_LIMIT)
    def post(self):
        """Subscribe to real-time market data (LTP/Quote/Depth) for one or more symbols.

        The subscription is served over the account's own server-side broker
        feed -- the same one the raw WebSocket protocol (ws://<host>:8765)
        and the /websocket/test page use -- so a symbol subscribed here is
        immediately reflected for any other connection under this account,
        and stays live until explicitly unsubscribed or the account
        disconnects.
        """
        try:
            data = subscribe_schema.load(request.json)
            api_key = data["apikey"]
            symbols = data["symbols"]
            mode = data["mode"]

            account_id, broker, error = _resolve_account_and_broker(api_key)
            if error:
                return make_response(jsonify(error[0]), error[1])

            success, response_data, status_code = subscribe_to_symbols(
                account_id, broker, symbols, mode
            )
            return make_response(jsonify(response_data), status_code)

        except ValidationError as err:
            return make_response(jsonify({"status": "error", "message": err.messages}), 400)
        except Exception as e:
            logger.exception(f"Unexpected error in websocket subscribe endpoint: {e}")
            return make_response(
                jsonify({"status": "error", "message": "An unexpected error occurred"}), 500
            )


@api.route("/unsubscribe", strict_slashes=False)
class WebSocketUnsubscribe(Resource):
    @limiter.limit(WEBSOCKET_CONTROL_LIMIT)
    def post(self):
        """Unsubscribe from real-time market data for one or more symbols."""
        try:
            data = unsubscribe_schema.load(request.json)
            api_key = data["apikey"]
            symbols = data["symbols"]
            mode = data["mode"]

            account_id, broker, error = _resolve_account_and_broker(api_key)
            if error:
                return make_response(jsonify(error[0]), error[1])

            success, response_data, status_code = unsubscribe_from_symbols(
                account_id, broker, symbols, mode
            )
            return make_response(jsonify(response_data), status_code)

        except ValidationError as err:
            return make_response(jsonify({"status": "error", "message": err.messages}), 400)
        except Exception as e:
            logger.exception(f"Unexpected error in websocket unsubscribe endpoint: {e}")
            return make_response(
                jsonify({"status": "error", "message": "An unexpected error occurred"}), 500
            )


@api.route("/unsubscribe-all", strict_slashes=False)
class WebSocketUnsubscribeAll(Resource):
    @limiter.limit(WEBSOCKET_CONTROL_LIMIT)
    def post(self):
        """Unsubscribe this account from every symbol it currently has subscribed."""
        try:
            data = unsubscribe_all_schema.load(request.json)
            api_key = data["apikey"]

            account_id, broker, error = _resolve_account_and_broker(api_key)
            if error:
                return make_response(jsonify(error[0]), error[1])

            success, response_data, status_code = unsubscribe_all(account_id, broker)
            return make_response(jsonify(response_data), status_code)

        except ValidationError as err:
            return make_response(jsonify({"status": "error", "message": err.messages}), 400)
        except Exception as e:
            logger.exception(f"Unexpected error in websocket unsubscribe-all endpoint: {e}")
            return make_response(
                jsonify({"status": "error", "message": "An unexpected error occurred"}), 500
            )
