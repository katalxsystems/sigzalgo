# scripts/price_breach_monitor.py
"""Create, activate, and manage a Flow price-breach watch over HTTP.

An external application's end of the "monitor EntryPrice/StopLoss/Target1"
design: log in, create a Flow workflow whose `priceAlert` trigger watches for
price going outside [StopLoss, Target1], activate it, and — the piece Flow
cannot do for itself — deactivate it again once your webhook receiver has
handled the breach notification.

Talks to the running OpenAlgo server purely over HTTP/JSON, the way any
external application would; it does not import the app or touch the database
directly. That also means it inherits the app's real auth model for these
routes: unlike /api/v1/*, blueprints/flow.py is session-cookie authenticated
(@check_session_validity), not apikey authenticated, and that decorator
requires a *connected broker* session, not just a valid password login. Use a
requests.Session() throughout so the login cookie carries to every call.

    # Create + activate a watch (defaults: outside_channel on StopLoss/Target1)
    uv run python scripts/price_breach_monitor.py create \\
        --base-url http://127.0.0.1:5000 \\
        --username myuser --password 'mypassword' \\
        --symbol RELIANCE --exchange NSE \\
        --entry 1180 --stop-loss 1160 --target1 1220 \\
        --webhook-url https://myapp.example.com/breach-webhook

    # If the account has TOTP required for login:
    uv run python scripts/price_breach_monitor.py create ... --totp 123456

    # Deactivate later (e.g. from an operator shell, or your receiver calls
    # the same two functions directly — see price_breach_webhook_receiver.py)
    uv run python scripts/price_breach_monitor.py deactivate \\
        --base-url http://127.0.0.1:5000 --username myuser --password '...' \\
        --workflow-id 42

Output note: this is an operator/integration-facing CLI, so results go to
stdout via print() rather than the application logger — the output is the
product here. Application code continues to use utils.logging.get_logger.
"""

from __future__ import annotations

import argparse
import json
import sys

try:
    import requests
except ImportError:  # pragma: no cover - guidance, not a runtime path
    print("This script needs the 'requests' package: uv add requests", file=sys.stderr)
    raise


class LoginError(RuntimeError):
    pass


def login(session: requests.Session, base_url: str, username: str, password: str, totp: str | None = None) -> None:
    """Password-login (+ TOTP if required), leaving `session` authenticated.

    Raises LoginError with a message safe to print (never includes the
    password) on any failure, including "broker not connected" — that is not
    a login failure, but the caller cannot proceed to Flow's session-gated
    routes without one, so it is surfaced the same way.
    """
    csrf_resp = session.get(f"{base_url}/auth/csrf-token", timeout=15)
    csrf_resp.raise_for_status()
    csrf_token = csrf_resp.json().get("csrf_token", "")

    resp = session.post(
        f"{base_url}/auth/login",
        data={"username": username, "password": password},
        headers={"X-CSRFToken": csrf_token},
        timeout=15,
    )
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}

    if body.get("status") == "totp_required":
        if not totp:
            raise LoginError(
                "This account requires a TOTP code for login. Re-run with --totp <code>."
            )
        totp_resp = session.post(
            f"{base_url}/auth/login/totp",
            json={"totp_code": totp},
            headers={"X-CSRFToken": csrf_token},
            timeout=15,
        )
        totp_body = totp_resp.json() if totp_resp.ok else {}
        if not totp_resp.ok or totp_body.get("status") == "error":
            raise LoginError(f"TOTP verification failed: {totp_body.get('message', totp_resp.text)}")
    elif not resp.ok or body.get("status") == "error":
        raise LoginError(f"Login failed: {body.get('message', resp.text)}")

    # Password (+ TOTP) succeeded, but blueprints/flow.py's routes require a
    # *connected broker* session (session["logged_in"]), not just a valid
    # password login. Confirm that now with a clear message instead of a
    # confusing 401 on the first workflow call.
    status_resp = session.get(f"{base_url}/auth/session-status", timeout=15)
    status = status_resp.json() if status_resp.ok else {}
    if not status.get("logged_in"):
        raise LoginError(
            "Password login succeeded, but no broker is connected on this account. "
            "Flow's workflow routes require a connected broker session - "
            "log in through the broker connect screen first, then re-run this script."
        )


def build_breach_workflow(
    name: str,
    symbol: str,
    exchange: str,
    stop_loss: float,
    target1: float,
    webhook_url: str,
    entry_price: float | None = None,
    workflow_id: int | None = None,
) -> dict:
    """One priceAlert trigger (outside_channel on [stop_loss, target1]) into
    one httpRequest action that POSTs the breach to webhook_url.

    `workflow_id` is None on first creation (the workflow does not have an id
    yet) and filled in afterwards — see `main()`'s create flow, which POSTs
    without it, then PUTs the notify node's body again once the id is known.
    Your receiver needs it to know which workflow to deactivate.

    `trigger: "once"` means the live price-monitor removes its watch the
    moment either bound is breached — it will not fire twice. That does NOT
    flip the workflow's is_active flag in the database, though (see the
    module docstring); call deactivate_workflow() once your receiver has
    handled the notification if you want the workflow to show as inactive
    too — see price_breach_webhook_receiver.py.
    """
    body_payload = {
        "workflow_id": workflow_id,
        "symbol": symbol,
        "exchange": exchange,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "target1": target1,
        "breach_price": "{{webhook.trigger_price}}",
        "triggered_at": "{{webhook.triggered_at}}",
    }

    nodes = [
        {
            "id": "trigger",
            "type": "priceAlert",
            "position": {"x": 0, "y": 0},
            "data": {
                "symbol": symbol,
                "exchange": exchange,
                "condition": "outside_channel",
                "priceLower": stop_loss,
                "priceUpper": target1,
                "trigger": "once",
                "expiration": "none",
            },
        },
        {
            "id": "notify",
            "type": "httpRequest",
            "position": {"x": 0, "y": 150},
            "data": {
                "method": "POST",
                "url": webhook_url,
                "headers": json.dumps({"Content-Type": "application/json"}),
                "body": json.dumps(body_payload),
                "timeout": 30,
                "outputVariable": "notifyResp",
            },
        },
    ]
    edges = [{"id": "edge-trigger-notify", "source": "trigger", "target": "notify"}]

    return {
        "name": name,
        "description": f"Price breach watch: {symbol} outside [{stop_loss}, {target1}]",
        "nodes": nodes,
        "edges": edges,
    }


def create_workflow(session: requests.Session, base_url: str, payload: dict) -> int:
    resp = session.post(f"{base_url}/flow/api/workflows", json=payload, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Create workflow failed ({resp.status_code}): {resp.text}")
    return resp.json()["id"]


def update_workflow_graph(session: requests.Session, base_url: str, workflow_id: int, nodes: list, edges: list) -> None:
    resp = session.put(
        f"{base_url}/flow/api/workflows/{workflow_id}",
        json={"nodes": nodes, "edges": edges},
        timeout=15,
    )
    if not resp.ok:
        raise RuntimeError(f"Update workflow {workflow_id} failed ({resp.status_code}): {resp.text}")


def activate_workflow(session: requests.Session, base_url: str, workflow_id: int) -> dict:
    resp = session.post(f"{base_url}/flow/api/workflows/{workflow_id}/activate", timeout=15)
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if not resp.ok or body.get("error"):
        raise RuntimeError(f"Activate failed: {body.get('error', resp.text)}")
    return body


def deactivate_workflow(session: requests.Session, base_url: str, workflow_id: int) -> dict:
    """Call this from your webhook receiver after it has handled the breach
    notification (see price_breach_webhook_receiver.py) — a workflow cannot
    deactivate itself from an httpRequest node, since this route needs a
    session cookie a flow has no way to carry."""
    resp = session.post(f"{base_url}/flow/api/workflows/{workflow_id}/deactivate", timeout=15)
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if not resp.ok or body.get("error"):
        raise RuntimeError(f"Deactivate failed: {body.get('error', resp.text)}")
    return body


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", required=True, help="e.g. http://127.0.0.1:5000")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--totp", help="6-digit code, only if this account requires TOTP for login")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="create the breach workflow and activate it")
    _add_common_args(p_create)
    p_create.add_argument("--name", default=None, help="workflow name (default: derived from symbol)")
    p_create.add_argument("--symbol", required=True)
    p_create.add_argument("--exchange", default="NSE")
    p_create.add_argument("--entry", type=float, default=None, help="EntryPrice, informational only")
    p_create.add_argument("--stop-loss", type=float, required=True)
    p_create.add_argument("--target1", type=float, required=True)
    p_create.add_argument("--webhook-url", required=True, help="your receiver's URL")
    p_create.add_argument(
        "--no-activate", action="store_true", help="create only; do not activate the watch"
    )

    p_deact = sub.add_parser("deactivate", help="deactivate an existing workflow by id")
    _add_common_args(p_deact)
    p_deact.add_argument("--workflow-id", type=int, required=True)

    args = parser.parse_args()

    session = requests.Session()
    try:
        login(session, args.base_url, args.username, args.password, args.totp)
    except LoginError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1

    if args.command == "create":
        name = args.name or f"{args.symbol} breach watch"
        payload = build_breach_workflow(
            name=name,
            symbol=args.symbol,
            exchange=args.exchange,
            stop_loss=args.stop_loss,
            target1=args.target1,
            webhook_url=args.webhook_url,
            entry_price=args.entry,
        )
        try:
            workflow_id = create_workflow(session, args.base_url, payload)
            print(f"Created workflow {workflow_id}: {name}")

            # The notify node's body was built without a workflow_id (it did
            # not exist yet). Rebuild it now that it does, so your receiver
            # can tell OpenAlgo which workflow to deactivate.
            final_payload = build_breach_workflow(
                name=name,
                symbol=args.symbol,
                exchange=args.exchange,
                stop_loss=args.stop_loss,
                target1=args.target1,
                webhook_url=args.webhook_url,
                entry_price=args.entry,
                workflow_id=workflow_id,
            )
            update_workflow_graph(
                session, args.base_url, workflow_id, final_payload["nodes"], final_payload["edges"]
            )

            if not args.no_activate:
                activate_workflow(session, args.base_url, workflow_id)
                print(
                    f"Activated. Watching {args.symbol}@{args.exchange} for price outside "
                    f"[{args.stop_loss}, {args.target1}]. Your webhook receives one POST on breach; "
                    f"call `deactivate --workflow-id {workflow_id}` (or see "
                    "price_breach_webhook_receiver.py) from your receiver afterwards."
                )
            else:
                print(f"Not activated (--no-activate). Activate later with workflow id {workflow_id}.")
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    elif args.command == "deactivate":
        try:
            deactivate_workflow(session, args.base_url, args.workflow_id)
            print(f"Deactivated workflow {args.workflow_id}.")
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
