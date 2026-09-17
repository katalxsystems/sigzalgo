"""
Standalone 5Paisa WebSocket connection tester -- isolates whether a feed
failure is caused by OpenAlgo's adapter/pool code or by 5paisa itself
(dead token, wrong client code, account-side rejection).

Talks to 5paisa's feed WebSocket directly using the exact same protocol as
broker/fivepaisa/streaming/fivepaisa_websocket.py (same URL format, same
redirect-server decode), but with NO OpenAlgo app code in the path: no
Flask, no adapter, no connection pool, no ZMQ bus. Just `websocket-client`
talking to 5paisa.

Two ways to get credentials:

  1) --from-db (recommended): pulls the CURRENT decrypted token + client
     code straight out of this deployment's db/openalgo.db, the same way
     scripts/extract_broker_token.py does. Since a revoked account has its
     token wiped to an empty string, this only works right after a fresh
     login (before anything else revokes the row again) -- log in via the
     OpenAlgo UI, then immediately run this script.

  2) --token / --client-code: paste in credentials obtained some other way
     (e.g. copied from scripts/extract_broker_token.py's output, or from a
     direct 5paisa login).

Usage:
  uv run python scripts/fivepaisa_ws_isolate.py --from-db
  uv run python scripts/fivepaisa_ws_isolate.py --token "<jwt>" --client-code "<code>"

  # Also try subscribing to one symbol to confirm data actually flows
  # (default scrip is RELIANCE on NSE cash: Exch=N, ExchType=C, ScripCode=2885):
  uv run python scripts/fivepaisa_ws_isolate.py --from-db --subscribe

SECURITY: the token this script uses grants full account access. Never
paste it into chat, never commit it, never log it to disk. This script
only prints a redacted prefix/suffix, matching extract_broker_token.py's
own banner.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, REPO_ROOT)

WEBSOCKET_URLS = {
    "A": "wss://aopenfeed.5paisa.com/feeds/api/chat",
    "B": "wss://bopenfeed.5paisa.com/feeds/api/chat",
    "C": "wss://openfeed.5paisa.com/feeds/api/chat",
    "default": "wss://openfeed.5paisa.com/Feeds/api/chat",
}

# RELIANCE on NSE cash -- a liquid, always-listed symbol good enough to
# confirm ticks are flowing. Override with --scrip if you want another one.
DEFAULT_SCRIP = {"Exch": "N", "ExchType": "C", "ScripCode": 2885}


def _redact(token: str) -> str:
    if len(token) <= 24:
        return "*" * len(token)
    return f"{token[:12]}...{token[-12:]}"


def _decode_redirect_server(token: str) -> str:
    """Same decode fivepaisa_websocket.py._decode_token does."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            print("  [warn] token is not a 3-part JWT; using default server")
            return "default"
        payload = parts[1]
        padding = len(payload) % 4
        if padding:
            payload += "=" * (4 - padding)
        decoded = base64.urlsafe_b64decode(payload)
        payload_data = json.loads(decoded)
        redirect_server = payload_data.get("RedirectServer", "default")
        print(f"  [ok] decoded RedirectServer = {redirect_server!r}")
        return redirect_server
    except Exception as e:
        print(f"  [warn] could not decode JWT payload ({e}); using default server")
        return "default"


def _credentials_from_db() -> tuple[str, str]:
    """Pull the current decrypted fivepaisa token + client code from
    db/openalgo.db, the same way scripts/extract_broker_token.py does."""
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(REPO_ROOT, ".env"))
    except ImportError:
        pass

    if not os.getenv("API_KEY_PEPPER"):
        sys.stderr.write(
            "ERROR: API_KEY_PEPPER not set. Source the .env that holds it first.\n"
        )
        sys.exit(2)

    from database.auth_db import Auth, db_session, decrypt_token

    session = db_session()
    try:
        row = (
            session.query(Auth)
            .filter_by(broker="fivepaisa")
            .filter(Auth.is_revoked.is_(False))
            .first()
        )
    finally:
        session.close()

    if row is None:
        sys.stderr.write(
            "No non-revoked fivepaisa auth row found in db/openalgo.db.\n"
            "Log in to fivepaisa via the OpenAlgo UI first, then re-run this\n"
            "script immediately -- the row is revoked (token wiped) again the\n"
            "moment you log out or the session expires.\n"
        )
        sys.exit(1)

    token = decrypt_token(row.auth)
    if not token:
        sys.stderr.write(
            f"Auth row '{row.name}' has an empty token even though it is not "
            "marked revoked -- log in again.\n"
        )
        sys.exit(1)

    client_code = _resolve_client_code(row.name)
    print(f"[from-db] account_id={row.name!r} broker={row.broker!r} client_code={client_code!r}")
    print(f"[from-db] token = {_redact(token)}")
    return token, client_code


def _resolve_client_code(account_id: str) -> str:
    """Same resolution as the FIXED fivepaisa_adapter.py._resolve_client_code:
    per-account DB-stored broker_api_key first (utils.config.get_broker_api_key),
    falling back to instance Settings, then legacy .env BROKER_API_KEY. Format
    is api_key:::user_id:::client_id. (The old version of both this script and
    the adapter read os.getenv("BROKER_API_KEY") directly, which misses the
    per-account credential entirely on a multi-account install and ends up
    sending account_id itself as ClientCode -- that was the actual cause of
    the 401s this script was built to chase down.)"""
    from utils.config import get_broker_api_key

    broker_api_key = get_broker_api_key(account_id)
    if broker_api_key:
        parts = broker_api_key.split(":::")
        if len(parts) >= 3:
            return parts[2]
        print("  [warn] broker_api_key format incorrect, using account_id as client_code")
    else:
        print("  [warn] broker_api_key not found (checked per-account, instance Settings, .env)")
    return account_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from-db", action="store_true", help="Pull token/client_code from db/openalgo.db")
    parser.add_argument("--token", help="5paisa JWT access token (paste directly)")
    parser.add_argument("--client-code", help="5paisa demat client code (paste directly)")
    parser.add_argument("--subscribe", action="store_true", help="Also subscribe to a test scrip and print incoming ticks")
    parser.add_argument("--duration", type=int, default=20, help="Seconds to stay connected before exiting (default 20)")
    args = parser.parse_args()

    if args.from_db:
        token, client_code = _credentials_from_db()
    elif args.token and args.client_code:
        token, client_code = args.token, args.client_code
        print(f"[manual] client_code={client_code!r} token={_redact(token)}")
    else:
        parser.error("Provide either --from-db, or both --token and --client-code")
        return 2

    print("\n" + "=" * 72)
    print("5PAISA WEBSOCKET ISOLATION TEST")
    print("Connecting directly to 5paisa -- no OpenAlgo app code in the path.")
    print("=" * 72)

    import websocket

    redirect_server = _decode_redirect_server(token)
    base_url = WEBSOCKET_URLS.get(redirect_server, WEBSOCKET_URLS["default"])
    connection_url = f"{base_url}?Value1={token}|{client_code}"
    print(f"\nConnecting to: {base_url}")

    result = {"opened": False, "closed_code": None, "closed_msg": None, "error": None, "ticks": 0}

    def on_open(ws):
        result["opened"] = True
        print("\n*** CONNECTION OPENED *** -- token + client_code were accepted by 5paisa.\n")
        if args.subscribe:
            scrip = DEFAULT_SCRIP
            request = {
                "Method": "MarketFeedV3",
                "Operation": "Subscribe",
                "ClientCode": client_code,
                "MarketFeedData": [scrip],
            }
            ws.send(json.dumps(request))
            print(f"Subscribed to {scrip}; waiting for ticks...")

    def on_message(ws, message):
        result["ticks"] += 1
        print(f"[tick #{result['ticks']}] {message[:300]}")

    def on_error(ws, error):
        result["error"] = str(error)
        print(f"\n*** ERROR *** {error}\n")

    def on_close(ws, close_status_code, close_msg):
        result["closed_code"] = close_status_code
        result["closed_msg"] = close_msg
        print(f"\n*** CLOSED *** code={close_status_code} msg={close_msg}\n")

    ws = websocket.WebSocketApp(
        connection_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    import threading

    t = threading.Thread(
        target=lambda: ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE}, ping_interval=10, ping_timeout=5),
        daemon=True,
    )
    t.start()
    t.join(timeout=args.duration)
    ws.close()

    print("\n" + "=" * 72)
    print("RESULT")
    print("=" * 72)
    if result["opened"]:
        print("5paisa ACCEPTED this token/client_code and opened the connection.")
        if args.subscribe:
            print(f"Received {result['ticks']} tick(s) during the test window.")
            if result["ticks"] == 0:
                print("No ticks arrived -- check market hours / scrip validity, not auth.")
        print("\n=> The credentials and 5paisa's side are fine. If OpenAlgo's own")
        print("   adapter still fails with the SAME fresh token, the bug is in")
        print("   OpenAlgo's code (adapter/pool), not the broker session.")
        return 0
    else:
        print("5paisa REJECTED the connection (never opened).")
        if result["error"]:
            print(f"Error detail: {result['error']}")
        if result["closed_code"] == 401 or (result["error"] and "401" in result["error"]):
            print("\n=> 401 Unauthorized directly from 5paisa: this token is genuinely")
            print("   dead/invalid on the broker side. Re-login to fivepaisa (fresh")
            print("   token) and retry this script before touching OpenAlgo's code.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
