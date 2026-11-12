# scripts/price_breach_webhook_receiver_example.py
"""Example receiver for scripts/price_breach_monitor.py's breach notification.

This is what your "external application" runs — a small HTTP server that
OpenAlgo's `httpRequest` action node POSTs to when a price-breach workflow
fires. It is deliberately minimal: swap the `handle_breach()` body for
whatever your application actually does (page someone, square off a
position, write to a database, ...).

The one piece that is not optional: a Flow workflow cannot deactivate
itself — /flow/api/workflows/<id>/deactivate needs a session cookie, and an
httpRequest node has no way to carry one (see price_breach_monitor.py's
module docstring for why). So the receiver, which already has to run
somewhere with its own credentials, is what closes the loop.

Run it, then point --webhook-url at it when you create the watch:

    uv run python scripts/price_breach_webhook_receiver_example.py \\
        --base-url http://127.0.0.1:5000 --username myuser --password 'mypassword' \\
        --port 8000

    uv run python scripts/price_breach_monitor.py create \\
        --base-url http://127.0.0.1:5000 --username myuser --password 'mypassword' \\
        --symbol RELIANCE --exchange NSE --stop-loss 1160 --target1 1220 \\
        --webhook-url http://<this-machine>:8000/breach-webhook

Needs Flask installed (already a dependency of the main app; if running this
standalone elsewhere: `uv add flask`).
"""

from __future__ import annotations

import argparse
import os
import sys

import requests
from flask import Flask, jsonify, request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from price_breach_monitor import LoginError, deactivate_workflow, login  # noqa: E402

app = Flask(__name__)

# Populated from CLI args in main() before app.run(). A real application
# would refresh/re-login on session expiry rather than logging in once at
# startup and holding the session indefinitely.
_config: dict = {}


def handle_breach(payload: dict) -> None:
    """Replace this with your actual response to a breach.

    `payload` is exactly the JSON body price_breach_monitor.py's httpRequest
    node sends: workflow_id, symbol, exchange, entry_price, stop_loss,
    target1, breach_price, triggered_at.
    """
    print(
        f"[BREACH] {payload.get('symbol')}@{payload.get('exchange')}: "
        f"price {payload.get('breach_price')} breached "
        f"[{payload.get('stop_loss')}, {payload.get('target1')}] "
        f"at {payload.get('triggered_at')}"
    )
    # e.g.: page on-call, square off the position via your own order-placement
    # code, write an audit row, etc.


@app.route("/breach-webhook", methods=["POST"])
def breach_webhook():
    payload = request.get_json(silent=True) or {}

    handle_breach(payload)

    workflow_id = payload.get("workflow_id")
    if workflow_id is not None:
        try:
            deactivate_workflow(_config["session"], _config["base_url"], int(workflow_id))
            print(f"Deactivated workflow {workflow_id}.")
        except (RuntimeError, requests.RequestException) as exc:
            # Do not fail the webhook response over this — the breach was
            # already handled above. Log it and let an operator deactivate
            # manually (scripts/price_breach_monitor.py deactivate ...).
            print(f"Could not deactivate workflow {workflow_id}: {exc}", file=sys.stderr)
    else:
        print("No workflow_id in payload; nothing to deactivate.", file=sys.stderr)

    return jsonify({"status": "ok"}), 200


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True, help="OpenAlgo base URL, e.g. http://127.0.0.1:5000")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--totp", help="6-digit code, only if this account requires TOTP for login")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    session = requests.Session()
    try:
        login(session, args.base_url, args.username, args.password, args.totp)
    except LoginError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1

    _config["session"] = session
    _config["base_url"] = args.base_url

    print(f"Listening on {args.host}:{args.port}/breach-webhook ...")
    app.run(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
