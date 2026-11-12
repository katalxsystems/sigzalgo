"""Price-breach Flow workflows: build, create+activate, and deactivate.

Wraps Flow's `priceAlert` trigger (a real background service polling LTP
every second -- not a one-shot check) into two independent watches per call.
Every notify body carries the caller-supplied `call_id` and `mentor_id`, and
-- unless an explicit name override is given -- both workflows are named
from `call_id`, `mentor_id`, and `symbol` (the "script"), in that order:

- sl_target: fires once price leaves the channel between stop_loss and
  target1. Its notify branches on which bound was actually crossed (see
  _sl_target_graph), so the webhook always carries an explicit breach_type
  ("stop_loss" / "target1"), not just a price the receiver would have to
  compare itself. Buy and sell calls both work, told apart by the levels
  alone (the request has no side field): stop_loss below target1 is a buy,
  above it a sell (see _call_side).
- entry_recross: fires once price crosses back through entry_price -- and
  only once while the workflow is active (`trigger: "once"`; the live watch
  is removed the instant it fires, same as sl_target) -- in whichever
  direction is a "re-cross" given where price was when the call was made
  (active_price). See _entry_cross_condition() below.
  - active_price == entry_price: there is no cross direction to watch for,
    because price is already there -- the entry breach is true at call time.
    The entry_recross workflow is still created (same graph: notify, then
    deactivate itself) but, instead of a price watch, it is executed once
    immediately in the background with trigger_price = active_price. Its
    notify therefore comes from its own workflow_id and shows in its
    Execution Log, and its own deactivate step retires it; the sl_target
    watch stays live.

Each LTP watch has a 1-minute candle backup, a scheduled workflow that runs
every minute and scans the last CANDLE_LOOKBACK_MINUTES of closed 1-minute
candles (only those after the call was armed) with a candleScan node:

- candle_sl_target: a candle whose range left the stop_loss/target1 channel.
  Ends the call, same as sl_target.
- candle_entry: a candle that crossed back through entry_price in the watched
  direction. Retires both entry watches; sl_target stays live. Not created
  when active_price == entry_price.

Scanning the whole window, not just the last candle, means a breach that one
run missed (a skipped or failed run, a restart) is still found by the next.

Each live watch's action chain ends in one [deactivate] node: a loopback HTTP
request to this same instance's own apikey-authenticated /api/v1/pricebreach/*
-- possible (unlike the session-cookie authenticated
/flow/api/workflows/<id>/deactivate) specifically because those routes take
an apikey, not a session cookie, so a Flow httpRequest node can call them.
The two watches deactivate at different scopes, because they mean different
things for the call:

- sl_target firing is a real stop-loss/target breach -- the call is over, so
  its deactivate node hits /api/v1/pricebreach/<call_id>/deactivate
  (deactivate_by_call_id()), tearing down both sibling workflows together.
- entry_recross firing just means price came back to entry -- the call is
  NOT over, since price can still go on to hit stop_loss or target1 after
  that. Its deactivate node instead hits
  /api/v1/pricebreach/workflow/<workflow_id>/deactivate (deactivate_one()),
  retiring only itself and leaving the sl_target watch live.

Callable directly with an already-resolved api_key -- no session cookie
needed. This is what lets restx_api/price_breach_monitor.py offer the
feature over the ordinary apikey-authenticated /api/v1/ surface.

Flow workflows have no owner column (see database/flow_db.py's FlowWorkflow)
-- like the rest of Flow, this is instance-wide, not scoped per account. That
matches the existing session-based /flow/api/workflows/* routes; this module
does not add isolation those routes do not already have, beyond the
apikey-match check in deactivate() below.
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from database.flow_db import activate_workflow as db_activate_workflow
from database.flow_db import create_workflow as db_create_workflow
from database.flow_db import deactivate_workflow as db_deactivate_workflow
from database.flow_db import delete_workflow as db_delete_workflow
from database.flow_db import (
    get_workflow,
    get_workflow_api_key,
    get_workflow_ids_by_call_id,
    record_price_breach_call,
    update_workflow,
)
from services.flow_price_monitor_service import get_flow_price_monitor
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)

# Shared and bounded, never one thread per call: the immediate
# active_price == entry_price entry_recross run (see create_and_activate)
# executes in the background so a slow/unreachable webhook_url does not delay
# the API response. Mirrors flow_price_monitor_service.py's _WORKFLOW_POOL.
_NOTIFY_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pricebreach-notify")


def _loopback_base_url() -> str:
    """Where a Flow httpRequest node reaches back into this instance's own
    /api/v1/*. Same resolution order as blueprints/mcp_http.py's loopback
    (kept in sync manually; there is no shared helper for it yet):
    MCP_LOOPBACK_URL override > HOST_SERVER (set by every install script;
    native installs bind gunicorn to a Unix socket, so the public HTTPS URL
    via nginx is the only loopback that actually answers) > dev-server
    fallback on FLASK_PORT.
    """
    loopback = (os.getenv("MCP_LOOPBACK_URL") or "").strip()
    if not loopback:
        loopback = (os.getenv("HOST_SERVER") or "").strip()
    if not loopback:
        flask_port = os.getenv("FLASK_PORT") or os.getenv("PORT") or "5000"
        loopback = f"http://127.0.0.1:{flask_port}"
    return loopback.rstrip("/")


def _call_side(stop_loss: float, target1: float) -> str:
    """BUY or SELL, read from the levels themselves.

    The create request carries no side field, and callers cannot add one yet.
    The levels settle it: a buy call's stop-loss sits below its target, a sell
    call's above. Equal levels are rejected before this is asked.
    """
    return "SELL" if stop_loss > target1 else "BUY"


def _channel(stop_loss: float, target1: float) -> tuple[float, float]:
    """(lower, upper) bounds of the sl_target channel, for either side."""
    return min(stop_loss, target1), max(stop_loss, target1)


def _entry_cross_condition(active_price: float, entry_price: float) -> str | None:
    """Which direction counts as "price came back to entry", given where
    price was when the call was made.

    - active_price > entry_price: price started above entry, so a breach is
      it falling back down through entry -> watch for "crosses_below".
    - active_price < entry_price: price started below entry, so a breach is
      it rising back up through entry -> watch for "crosses_above".
    - active_price == entry_price: no cross direction to watch for.
    """
    if active_price > entry_price:
        return "crosses_below"
    if active_price < entry_price:
        return "crosses_above"
    return None


def _execute_entry_now(workflow_id: int, api_key: str, price: float) -> None:
    """Run an entry_recross workflow once, now, as if its price alert had just
    fired at ``price`` (the active_price == entry_price case)."""
    from services.flow_executor_service import execute_workflow
    from utils.db_sessions import remove_all_scoped_sessions

    try:
        result = execute_workflow(
            workflow_id,
            webhook_data={
                "trigger_type": "price_alert",
                "trigger_price": price,
                "triggered_at": datetime.now().isoformat(),
                "immediate": True,
            },
            api_key=api_key,
        )
        logger.info(
            f"Immediate entry_recross run for workflow {workflow_id}: {result.get('status')}"
        )
    except Exception:
        logger.exception(f"Immediate entry_recross run failed for workflow {workflow_id}")
    finally:
        # Pool thread: no app context, so teardown never removes its sessions.
        remove_all_scoped_sessions()


def _send_notification(webhook_url: str, payload: dict) -> None:
    """POST payload to webhook_url. Used both for the immediate
    active_price == entry_price case and could be reused for any other
    out-of-band notification; live-watch notifications go through Flow's own
    httpRequest node instead (see _notify_node), not this function."""
    try:
        get_httpx_client().post(webhook_url, json=payload, timeout=30)
    except Exception as e:
        logger.warning(f"Price-breach notification to {webhook_url} failed: {e}")


def _base_notify_body(
    call_id: str,
    mentor_id: str,
    workflow_id: int | None,
    alert_type: str,
    symbol: str,
    exchange: str,
    entry_price: float,
    stop_loss: float,
    target1: float,
) -> dict:
    return {
        "call_id": call_id,
        "mentor_id": mentor_id,
        "workflow_id": workflow_id,
        "alert_type": alert_type,
        "symbol": symbol,
        "exchange": exchange,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "target1": target1,
    }


def _notify_node(node_id: str, position_y: int, webhook_url: str, body_payload: dict) -> dict:
    return {
        "id": node_id,
        "type": "httpRequest",
        "position": {"x": 0, "y": position_y},
        "data": {
            "method": "POST",
            "url": webhook_url,
            "headers": json.dumps({"Content-Type": "application/json"}),
            "body": json.dumps(body_payload),
            "timeout": 30000,
            "outputVariable": "notifyResp",
        },
    }


def _deactivate_node(node_id: str, position_y: int, call_id: str, api_key: str) -> dict:
    """An httpRequest node that calls this instance's own deactivate route by
    call_id -- which tears down both sibling workflows for that call
    together (see deactivate_by_call_id()), so a single node here retires
    the whole pair. Fire-and-forget: outputVariable is intentionally unset,
    and a failure here does not roll back the workflow's own row -- the
    operator/receiver can still deactivate manually
    (POST /api/v1/pricebreach/<call_id>/deactivate) if this self-call fails
    for some reason (network hiccup, instance restarting mid-run).
    """
    url = f"{_loopback_base_url()}/api/v1/pricebreach/{call_id}/deactivate"
    return {
        "id": node_id,
        "type": "httpRequest",
        "position": {"x": 0, "y": position_y},
        "data": {
            "method": "POST",
            "url": url,
            "headers": json.dumps({"Content-Type": "application/json"}),
            # Only ever reached after a breach was notified, so it marks the
            # workflows it stops as breached (red in the Flow list).
            "body": json.dumps({"apikey": api_key, "breach": True}),
            "timeout": 10000,
        },
    }


def _deactivate_chain(
    call_id: str, api_key: str, start_y: int
) -> tuple[list[dict], list[dict], str]:
    """The [deactivate_call] tail used by the sl_target watch. Returns
    (nodes, edges, last_node_id) so callers can wire their own notify
    node(s) into "deactivate_call" without duplicating this part. Tears down
    the whole call_id pair -- see the module docstring for why sl_target
    (unlike entry_recross) deactivates at that scope."""
    nodes = [_deactivate_node("deactivate_call", start_y, call_id, api_key)]
    return nodes, [], "deactivate_call"


def _deactivate_self_node(node_id: str, position_y: int, workflow_id: int, api_key: str) -> dict:
    """An httpRequest node that calls this instance's own deactivate route for
    ONE workflow_id -- unlike _deactivate_node, it never touches a sibling
    workflow. Used by the entry_recross watch: coming back to entry is not the
    end of the call, so only this watch is retired (see module docstring).
    Fire-and-forget, same as _deactivate_node.
    """
    url = f"{_loopback_base_url()}/api/v1/pricebreach/workflow/{workflow_id}/deactivate"
    return {
        "id": node_id,
        "type": "httpRequest",
        "position": {"x": 0, "y": position_y},
        "data": {
            "method": "POST",
            "url": url,
            "headers": json.dumps({"Content-Type": "application/json"}),
            # Only ever reached after a breach was notified, so it marks the
            # workflows it stops as breached (red in the Flow list).
            "body": json.dumps({"apikey": api_key, "breach": True}),
            "timeout": 10000,
        },
    }


def _deactivate_self_chain(
    workflow_id: int, api_key: str, start_y: int
) -> tuple[list[dict], list[dict], str]:
    """The [deactivate_self] tail used by the entry_recross watch. Returns
    (nodes, edges, last_node_id), matching _deactivate_chain's shape."""
    nodes = [_deactivate_self_node("deactivate_self", start_y, workflow_id, api_key)]
    return nodes, [], "deactivate_self"


def _sl_target_graph(
    call_id: str,
    mentor_id: str,
    symbol: str,
    exchange: str,
    entry_price: float,
    stop_loss: float,
    target1: float,
    webhook_url: str,
    api_key: str,
    own_id: int,
    name: str,
) -> dict[str, Any]:
    """Trigger -> branch on which bound was crossed -> one of two notify
    bodies (breach_type "stop_loss" or "target1") -> deactivate chain.

    outside_channel already guarantees the breach price is past exactly one
    bound, so a single comparison with stop_loss distinguishes the two: below
    it for a buy call (stop-loss is the lower bound), above it for a sell call
    (stop-loss is the upper bound). Flow has no way to compute a conditional
    string inline in an httpRequest body, so the branch has to happen as an
    actual node.
    """
    lower, upper = _channel(stop_loss, target1)
    stop_loss_hit = "<" if _call_side(stop_loss, target1) == "BUY" else ">"
    trigger_data = {
        "symbol": symbol,
        "exchange": exchange,
        "condition": "outside_channel",
        "priceLower": lower,
        "priceUpper": upper,
        "trigger": "once",
        "expiration": "none",
    }
    common = _base_notify_body(
        call_id,
        mentor_id,
        own_id,
        "sl_target_breach",
        symbol,
        exchange,
        entry_price,
        stop_loss,
        target1,
    )
    common["detected_by"] = "ltp"
    common["breach_price"] = "{{webhook.trigger_price}}"
    common["triggered_at"] = "{{webhook.triggered_at}}"
    sl_body = {**common, "breach_type": "stop_loss"}
    target_body = {**common, "breach_type": "target1"}

    nodes = [
        {"id": "trigger", "type": "priceAlert", "position": {"x": 0, "y": 0}, "data": trigger_data},
        {
            "id": "branch",
            "type": "varCondition",
            "position": {"x": 0, "y": 100},
            "data": {
                "leftValue": "{{webhook.trigger_price}}",
                "operator": stop_loss_hit,
                "rightValue": str(stop_loss),
            },
        },
        _notify_node("notify_sl", 200, webhook_url, sl_body),
        _notify_node("notify_target", 200, webhook_url, target_body),
    ]
    edges = [
        {"id": "edge-trigger-branch", "source": "trigger", "target": "branch"},
        {
            "id": "edge-branch-notify_sl",
            "source": "branch",
            "sourceHandle": "true",
            "target": "notify_sl",
        },
        {
            "id": "edge-branch-notify_target",
            "source": "branch",
            "sourceHandle": "false",
            "target": "notify_target",
        },
    ]

    deact_nodes, deact_edges, _ = _deactivate_chain(call_id, api_key, start_y=300)
    nodes += deact_nodes
    edges += deact_edges
    edges.append(
        {"id": "edge-notify_sl-deactivate_call", "source": "notify_sl", "target": "deactivate_call"}
    )
    edges.append(
        {
            "id": "edge-notify_target-deactivate_call",
            "source": "notify_target",
            "target": "deactivate_call",
        }
    )

    return {
        "name": name,
        "description": f"Price breach watch: {symbol} outside [{lower}, {upper}]",
        "nodes": nodes,
        "edges": edges,
    }


def _entry_recross_graph(
    call_id: str,
    mentor_id: str,
    symbol: str,
    exchange: str,
    entry_price: float,
    stop_loss: float,
    target1: float,
    condition: str,
    webhook_url: str,
    api_key: str,
    own_id: int,
    name: str,
    candle_entry_id: int | None = None,
) -> dict[str, Any]:
    """`candle_entry_id`, when set, is the 1-minute candle entry check for the
    same call. It is retired along with this watch, so one re-cross is
    reported once whichever of the two sees it first."""
    trigger_data = {
        "symbol": symbol,
        "exchange": exchange,
        "condition": condition,
        "price": entry_price,
        "trigger": "once",
        "expiration": "none",
    }
    notify_body = _base_notify_body(
        call_id,
        mentor_id,
        own_id,
        "entry_recross",
        symbol,
        exchange,
        entry_price,
        stop_loss,
        target1,
    )
    notify_body["breach_type"] = "entry_price"
    notify_body["detected_by"] = "ltp"
    notify_body["breach_price"] = "{{webhook.trigger_price}}"
    notify_body["triggered_at"] = "{{webhook.triggered_at}}"

    nodes = [
        {"id": "trigger", "type": "priceAlert", "position": {"x": 0, "y": 0}, "data": trigger_data},
        _notify_node("notify", 150, webhook_url, notify_body),
    ]
    edges = [{"id": "edge-trigger-notify", "source": "trigger", "target": "notify"}]

    # The entry watches only, not the call_id: coming back to entry does not
    # end the call, so the sl_target watches must stay live. See module
    # docstring.
    previous = "notify"
    if candle_entry_id is not None:
        nodes.append(
            _deactivate_self_node("deactivate_candle_entry", 250, candle_entry_id, api_key)
        )
        edges.append(
            {"id": "edge-notify-candle", "source": "notify", "target": "deactivate_candle_entry"}
        )
        previous = "deactivate_candle_entry"
    deact_nodes, deact_edges, last_id = _deactivate_self_chain(own_id, api_key, start_y=300)
    nodes += deact_nodes
    edges += deact_edges
    edges.append({"id": f"edge-{previous}-{last_id}", "source": previous, "target": last_id})

    return {
        "name": name,
        "description": f"Entry re-cross watch: {symbol} {condition} {entry_price}",
        "nodes": nodes,
        "edges": edges,
    }


# Market-hours calendar for a symbol's exchange. The calendar knows the trading
# exchanges only, so an index is gated by the exchange it is computed on.
_CALENDAR_EXCHANGE = {"NSE_INDEX": "NSE", "BSE_INDEX": "BSE", "MCX_INDEX": "MCX"}

SCAN_VARIABLE = "scan"

# How far back each candle-check run looks. Every closed 1-minute candle in
# this window (and after the call was armed) is scanned, so a breach that one
# run missed -- a skipped or failed run, a broker history hiccup, a restart --
# is still found by a later one.
CANDLE_LOOKBACK_MINUTES = 120


def _armed_from_epoch(now: float | None = None) -> int:
    """Start of the first 1-minute candle that lies wholly after `now`.

    The candle a call is created in began before the call existed, so its
    high/low may include prices from before the levels applied; checking it
    could report a breach that never happened while the call was live. That
    minute is covered by the LTP watches alone.
    """
    now = time.time() if now is None else now
    return (int(now) // 60 + 1) * 60


def _scan_ref(path: str) -> str:
    return "{{" + f"{SCAN_VARIABLE}.{path}" + "}}"


def _candle_schedule_node(exchange: str) -> dict:
    """Every minute, during the symbol's market hours."""
    return {
        "id": "start",
        "type": "start",
        "position": {"x": 0, "y": 0},
        "data": {
            "label": "Every minute",
            "scheduleType": "interval",
            "intervalValue": 1,
            "intervalUnit": "minutes",
            "marketHoursOnly": True,
            "marketHoursExchange": _CALENDAR_EXCHANGE.get(exchange, exchange),
        },
    }


def _candle_scan_node(
    symbol: str,
    exchange: str,
    armed_from_epoch: int,
    above: float | None = None,
    below: float | None = None,
    tie_prefer: str = "below",
) -> dict:
    """candleScan over the last CANDLE_LOOKBACK_MINUTES of closed 1-minute
    candles, ignoring any that began before the call was armed. A failed
    fetch stores no output, so the breached check after it errors and takes
    neither branch: missing data never reports a breach."""
    data: dict[str, Any] = {
        "symbol": symbol,
        "exchange": exchange,
        "interval": "1m",
        "source": "api",
        "lookbackMinutes": CANDLE_LOOKBACK_MINUTES,
        "sinceEpoch": armed_from_epoch,
        "tiePrefer": tie_prefer,
        # Log an error and send a Telegram alert once if the check goes
        # several runs without candle data (an expired broker session looks
        # like a quiet market otherwise), and once more when it recovers.
        "alertOnNoData": True,
        "outputVariable": SCAN_VARIABLE,
    }
    if above is not None:
        data["above"] = above
    if below is not None:
        data["below"] = below
    return {"id": "scan", "type": "candleScan", "position": {"x": 0, "y": 100}, "data": data}


def _cond_node(node_id: str, y: int, left: str, op: str, right: Any) -> dict:
    return {
        "id": node_id,
        "type": "varCondition",
        "position": {"x": 0, "y": y},
        "data": {"leftValue": left, "operator": op, "rightValue": str(right)},
    }


def _candle_notify_fields() -> dict[str, Any]:
    """What every candle-detected breach adds to the notify body."""
    return {
        "detected_by": "candle_1m",
        "breach_price": _scan_ref("breach_price"),
        "triggered_at": "{{iso_timestamp}}",
        "candles_scanned": _scan_ref("candles_scanned"),
        "candle": {
            "interval": "1m",
            **{f: _scan_ref(f"candle.{f}") for f in ("timestamp", "open", "high", "low", "close")},
        },
    }


def _candle_sl_target_graph(
    call_id: str,
    mentor_id: str,
    symbol: str,
    exchange: str,
    entry_price: float,
    stop_loss: float,
    target1: float,
    webhook_url: str,
    api_key: str,
    own_id: int,
    name: str,
    armed_from_epoch: int,
) -> dict[str, Any]:
    """The 1-minute candle backup for the sl_target watch.

    Every minute: scan the closed 1-minute candles of the last
    CANDLE_LOOKBACK_MINUTES for the first whose range left the channel
    between stop_loss and target1 -- a buy call's stop-loss is broken by a low
    below it and its target by a high above it, a sell call the other way
    round. Comparisons are strict, matching the LTP watch's outside_channel.

    When one candle breaks both levels its range cannot say which came first;
    it is reported as the stop-loss, the outcome a receiver must not miss. A
    breach ends the call exactly as an sl_target breach does: notify, then
    deactivate every watch for the call_id.
    """
    is_buy = _call_side(stop_loss, target1) == "BUY"
    scan = _candle_scan_node(
        symbol,
        exchange,
        armed_from_epoch,
        above=target1 if is_buy else stop_loss,
        below=stop_loss if is_buy else target1,
        tie_prefer="below" if is_buy else "above",
    )
    common = _base_notify_body(
        call_id,
        mentor_id,
        own_id,
        "sl_target_breach",
        symbol,
        exchange,
        entry_price,
        stop_loss,
        target1,
    )
    common.update(_candle_notify_fields())
    sl_body = {**common, "breach_type": "stop_loss"}
    target_body = {**common, "breach_type": "target1"}
    # A breach on the low side is the stop-loss for a buy, the target for a sell.
    low_side, high_side = (
        ("notify_sl", "notify_target") if is_buy else ("notify_target", "notify_sl")
    )

    nodes = [
        _candle_schedule_node(exchange),
        scan,
        _cond_node("breached", 200, _scan_ref("breached"), "==", 1),
        _cond_node("low_side", 300, _scan_ref("is_below"), "==", 1),
        _notify_node("notify_sl", 400, webhook_url, sl_body),
        _notify_node("notify_target", 400, webhook_url, target_body),
    ]
    edges = [
        {"id": "edge-start-scan", "source": "start", "target": "scan"},
        {"id": "edge-scan-breached", "source": "scan", "target": "breached"},
        {
            "id": "edge-breached-low_side",
            "source": "breached",
            "sourceHandle": "true",
            "target": "low_side",
        },
        {
            "id": "edge-low_side-true",
            "source": "low_side",
            "sourceHandle": "true",
            "target": low_side,
        },
        {
            "id": "edge-low_side-false",
            "source": "low_side",
            "sourceHandle": "false",
            "target": high_side,
        },
    ]

    # The call is over on a breach, same scope as sl_target: tear down every
    # watch for this call_id, this one included.
    deact_nodes, deact_edges, last_id = _deactivate_chain(call_id, api_key, start_y=500)
    nodes += deact_nodes
    edges += deact_edges
    edges.append({"id": "edge-notify_sl-deactivate", "source": "notify_sl", "target": last_id})
    edges.append(
        {"id": "edge-notify_target-deactivate", "source": "notify_target", "target": last_id}
    )

    lower, upper = _channel(stop_loss, target1)
    return {
        "name": name,
        "description": (
            f"1-minute candle check: {symbol} candle range outside [{lower}, {upper}] in the "
            f"last {CANDLE_LOOKBACK_MINUTES} min (backup for the SL/Target watch)"
        ),
        "nodes": nodes,
        "edges": edges,
    }


def _candle_entry_graph(
    call_id: str,
    mentor_id: str,
    symbol: str,
    exchange: str,
    entry_price: float,
    stop_loss: float,
    target1: float,
    condition: str,
    webhook_url: str,
    api_key: str,
    own_id: int,
    name: str,
    armed_from_epoch: int,
    ltp_entry_id: int,
) -> dict[str, Any]:
    """The 1-minute candle backup for the entry_recross watch.

    Every minute: scan the closed 1-minute candles of the last
    CANDLE_LOOKBACK_MINUTES for the first that crossed back through
    entry_price in the watched direction -- a low below it for
    "crosses_below", a high above it for "crosses_above". Strict, matching the
    LTP watch's cross test.

    Coming back to entry does not end the call, so on a breach this retires
    only the entry watches: the LTP entry_recross watch (so the same re-cross
    is not reported twice) and then itself. The sl_target watches stay live.
    """
    below = entry_price if condition == "crosses_below" else None
    above = entry_price if condition == "crosses_above" else None
    body = _base_notify_body(
        call_id,
        mentor_id,
        own_id,
        "entry_recross",
        symbol,
        exchange,
        entry_price,
        stop_loss,
        target1,
    )
    body["breach_type"] = "entry_price"
    body.update(_candle_notify_fields())

    nodes = [
        _candle_schedule_node(exchange),
        _candle_scan_node(symbol, exchange, armed_from_epoch, above=above, below=below),
        _cond_node("breached", 200, _scan_ref("breached"), "==", 1),
        _notify_node("notify", 300, webhook_url, body),
        _deactivate_self_node("deactivate_ltp_entry", 400, ltp_entry_id, api_key),
        _deactivate_self_node("deactivate_self", 500, own_id, api_key),
    ]
    edges = [
        {"id": "edge-start-scan", "source": "start", "target": "scan"},
        {"id": "edge-scan-breached", "source": "scan", "target": "breached"},
        {
            "id": "edge-breached-notify",
            "source": "breached",
            "sourceHandle": "true",
            "target": "notify",
        },
        {"id": "edge-notify-ltp", "source": "notify", "target": "deactivate_ltp_entry"},
        {"id": "edge-ltp-self", "source": "deactivate_ltp_entry", "target": "deactivate_self"},
    ]
    return {
        "name": name,
        "description": (
            f"1-minute candle check: {symbol} {condition} {entry_price} in the last "
            f"{CANDLE_LOOKBACK_MINUTES} min (backup for the Entry re-cross watch)"
        ),
        "nodes": nodes,
        "edges": edges,
    }


def _arm_candle_check(workflow_id: int, api_key: str, start_data: dict) -> None:
    """Register the candle check's every-minute schedule, then mark it active.
    Same order the Flow activate route and the strategy monitor use.

    If marking it active fails, the job just registered is removed before the
    error propagates. Left behind, it would fire every minute for the life of
    the process against a workflow that stays inactive.
    """
    from database.flow_db import set_schedule_job_id
    from services.flow_scheduler_service import get_flow_scheduler

    scheduler = get_flow_scheduler()
    job_id = scheduler.add_workflow_job(
        workflow_id=workflow_id,
        schedule_type="interval",
        interval_value=start_data["intervalValue"],
        interval_unit=start_data["intervalUnit"],
        market_hours_only=True,
    )
    try:
        set_schedule_job_id(workflow_id, job_id)
        db_activate_workflow(workflow_id, api_key=api_key)
    except Exception:
        scheduler.remove_workflow_job(workflow_id)
        raise


def create_and_activate(
    api_key: str,
    call_id: str,
    mentor_id: str,
    symbol: str,
    exchange: str,
    active_price: float,
    entry_price: float,
    stop_loss: float,
    target1: float,
    webhook_url: str,
    name: str | None = None,
) -> tuple[bool, dict[str, Any], int]:
    """Create (and activate) the sl_target watch, and the entry_recross
    watch when active_price != entry_price -- or, when they are equal, send
    one immediate webhook notification instead (see module docstring).
    mentor_id is echoed back in every webhook payload alongside call_id, and
    -- unless an explicit name override is given -- both workflows are named
    from call_id, mentor_id, and symbol (the "script").
    Returns (success, response_data, status_code) -- the convention every
    other service function in this codebase follows.
    """
    # Either order is a valid call: stop_loss < target1 is a buy, stop_loss >
    # target1 a sell (see _call_side). Only equal levels leave no channel.
    if stop_loss == target1:
        return False, {"status": "error", "message": "stop_loss and target1 must differ"}, 400
    lower, upper = _channel(stop_loss, target1)

    base_name = name or f"{call_id}_{mentor_id}_{symbol}"
    entry_condition = _entry_cross_condition(active_price, entry_price)

    # Pass 1: create empty rows to get real ids, nodes/edges start blank.
    # Owned by the account behind the caller's API key (per-account Flow).
    from database.flow_db import owner_for_api_key

    account_id, owner_username = owner_for_api_key(api_key)
    sl_target_wf = db_create_workflow(
        name=f"{base_name} (SL/Target)",
        description="",
        nodes=[],
        edges=[],
        account_id=account_id,
        owner_username=owner_username,
    )
    if not sl_target_wf:
        return False, {"status": "error", "message": "Failed to create sl_target workflow"}, 500

    # Always created: when active_price == entry_price it runs once immediately
    # instead of watching (see module docstring).
    entry_wf = db_create_workflow(
        name=f"{base_name} (Entry re-cross)",
        description="",
        nodes=[],
        edges=[],
        account_id=account_id,
        owner_username=owner_username,
    )
    if not entry_wf:
        # Roll back the half-created pair rather than leave an orphaned,
        # never-activated sl_target workflow behind.
        db_delete_workflow(sl_target_wf.id)
        return (
            False,
            {"status": "error", "message": "Failed to create entry_recross workflow"},
            500,
        )

    # The 1-minute candle backups: one for SL/Target, always, and one for the
    # entry re-cross when there is a direction to watch.
    created = [sl_target_wf, entry_wf]

    def create_or_roll_back(suffix):
        workflow = db_create_workflow(
            name=f"{base_name} ({suffix})",
            description="",
            nodes=[],
            edges=[],
            account_id=account_id,
            owner_username=owner_username,
        )
        if workflow:
            created.append(workflow)
        else:
            for earlier in reversed(created):
                db_delete_workflow(earlier.id)
        return workflow

    candle_sl_wf = create_or_roll_back("1m Candle SL/Target")
    if not candle_sl_wf:
        return (
            False,
            {
                "status": "error",
                "message": "Failed to create the 1-minute candle SL/Target workflow",
            },
            500,
        )
    candle_entry_wf = None
    if entry_condition is not None:
        candle_entry_wf = create_or_roll_back("1m Candle Entry")
        if not candle_entry_wf:
            return (
                False,
                {
                    "status": "error",
                    "message": "Failed to create the 1-minute candle entry workflow",
                },
                500,
            )

    # Record the call_id -> workflow_id(s) mapping so
    # /api/v1/pricebreach/<call_id>/deactivate can resolve all of them later.
    record_price_breach_call(
        call_id,
        sl_target_workflow_id=sl_target_wf.id,
        entry_recross_workflow_id=(entry_wf.id if entry_wf else None),
        candle_workflow_id=candle_sl_wf.id,
        candle_entry_workflow_id=(candle_entry_wf.id if candle_entry_wf else None),
    )

    # Pass 2: build the real graphs -- each watch's own deactivate chain
    # tears down the whole call_id pair (see _deactivate_chain) -- and write
    # them in.
    sl_target_graph = _sl_target_graph(
        call_id,
        mentor_id,
        symbol,
        exchange,
        entry_price,
        stop_loss,
        target1,
        webhook_url,
        api_key,
        own_id=sl_target_wf.id,
        name=sl_target_wf.name,
    )
    update_workflow(sl_target_wf.id, nodes=sl_target_graph["nodes"], edges=sl_target_graph["edges"])

    if entry_wf:
        entry_graph = _entry_recross_graph(
            call_id,
            mentor_id,
            symbol,
            exchange,
            entry_price,
            stop_loss,
            target1,
            # No cross direction when price is already at entry; "crossing"
            # (at the level) describes it, and is never registered as a watch.
            entry_condition or "crossing",
            webhook_url,
            api_key,
            own_id=entry_wf.id,
            name=entry_wf.name,
            candle_entry_id=candle_entry_wf.id if candle_entry_wf else None,
        )
        update_workflow(entry_wf.id, nodes=entry_graph["nodes"], edges=entry_graph["edges"])

    armed_from = _armed_from_epoch()
    candle_graphs = [
        (
            "candle_sl_target",
            candle_sl_wf,
            _candle_sl_target_graph(
                call_id,
                mentor_id,
                symbol,
                exchange,
                entry_price,
                stop_loss,
                target1,
                webhook_url,
                api_key,
                own_id=candle_sl_wf.id,
                name=candle_sl_wf.name,
                armed_from_epoch=armed_from,
            ),
            f"1-minute candle high/low outside [{lower}, {upper}]",
        )
    ]
    if candle_entry_wf:
        candle_graphs.append(
            (
                "candle_entry",
                candle_entry_wf,
                _candle_entry_graph(
                    call_id,
                    mentor_id,
                    symbol,
                    exchange,
                    entry_price,
                    stop_loss,
                    target1,
                    entry_condition,
                    webhook_url,
                    api_key,
                    own_id=candle_entry_wf.id,
                    name=candle_entry_wf.name,
                    armed_from_epoch=armed_from,
                    ltp_entry_id=entry_wf.id,
                ),
                f"1-minute candle {entry_condition} {entry_price}",
            )
        )
    for _key, workflow, graph, _watching in candle_graphs:
        update_workflow(
            workflow.id,
            nodes=graph["nodes"],
            edges=graph["edges"],
            description=graph["description"],
        )

    # Pass 3: register the live watches and flip is_active.
    price_monitor = get_flow_price_monitor()
    price_monitor.add_alert(
        workflow_id=sl_target_wf.id,
        symbol=symbol,
        exchange=exchange,
        condition="outside_channel",
        target_price=float(target1),
        price_lower=float(lower),
        price_upper=float(upper),
        api_key=api_key,
        trigger="once",
        expiration="none",
    )
    db_activate_workflow(sl_target_wf.id, api_key=api_key)

    if entry_condition is not None:
        price_monitor.add_alert(
            workflow_id=entry_wf.id,
            symbol=symbol,
            exchange=exchange,
            condition=entry_condition,
            target_price=float(entry_price),
            api_key=api_key,
            trigger="once",
            expiration="none",
        )
        db_activate_workflow(entry_wf.id, api_key=api_key)
    else:
        # active_price == entry_price: the entry breach is already true, so
        # run the entry_recross workflow once now rather than waiting for a
        # cross that has no direction. Activated first so its own deactivate
        # step (and the call_id teardown) see a live workflow.
        db_activate_workflow(entry_wf.id, api_key=api_key)

    # Backups, so failing to arm one does not undo the live watches the call
    # already has; it is reported in the response instead.
    candle_infos: dict[str, dict[str, Any]] = {}
    for key, workflow, graph, watching in candle_graphs:
        info: dict[str, Any] = {
            "workflow_id": workflow.id,
            "watching": watching,
            "lookback_minutes": CANDLE_LOOKBACK_MINUTES,
            "checks_candles_from": datetime.fromtimestamp(armed_from).isoformat(),
            "active": True,
        }
        try:
            _arm_candle_check(workflow.id, api_key, graph["nodes"][0]["data"])
        except Exception:
            logger.exception(
                f"Price-breach call {call_id}: could not arm {key} check {workflow.id}"
            )
            info["active"] = False
            info["error"] = "Could not schedule the candle check; the LTP watches are live"
        candle_infos[key] = info

    logger.info(
        f"Price-breach call {call_id} ({_call_side(stop_loss, target1)}): "
        f"sl_target={sl_target_wf.id} outside [{lower}, {upper}]"
        + (
            f", entry_recross={entry_wf.id} {entry_condition} {entry_price}"
            if entry_condition is not None
            else f", entry_recross={entry_wf.id} executed immediately (active_price == entry_price)"
        )
        + "".join(
            f", {key}={info['workflow_id']} ({'armed' if info['active'] else 'NOT armed'})"
            for key, info in candle_infos.items()
        )
    )

    workflows: dict[str, Any] = {
        "sl_target": {
            "workflow_id": sl_target_wf.id,
            "watching": f"price outside [{lower}, {upper}]",
        }
    }
    if entry_condition is not None:
        workflows["entry_recross"] = {
            "workflow_id": entry_wf.id,
            "watching": f"price {entry_condition} {entry_price}",
        }
        message = (
            "Each watch fires once and notifies your webhook. sl_target firing ends the call and "
            "deactivates both watches; entry_recross firing deactivates only itself, since price "
            "can still go on to hit stop_loss or target1 afterwards -- no separate deactivate call "
            "needed either way."
        )
    else:
        workflows["entry_recross"] = {
            "workflow_id": entry_wf.id,
            "watching": None,
            "executed_immediately": True,
        }
        message = (
            "active_price equals entry_price: the entry_recross workflow was executed immediately "
            "(its notify reaches your webhook and it then deactivates itself). The sl_target watch "
            "stays live."
        )
    workflows["candle_sl_target"] = candle_infos["candle_sl_target"]
    # No candle entry check when active_price == entry_price: there is no
    # direction to watch, and the entry notification has already been sent.
    workflows["candle_entry"] = candle_infos.get("candle_entry")
    message += (
        f" 1-minute candle checks also run every minute as backups, each scanning the last "
        f"{CANDLE_LOOKBACK_MINUTES} minutes of candles so a breach between runs is not missed: "
        "one for stop_loss/target1 (a breach ends the call and deactivates every watch) and, "
        "when there is an entry direction, one for the entry re-cross (a breach retires both "
        "entry watches)."
    )

    if entry_condition is None:
        # Submitted last, once everything above has used this request's ORM
        # objects: the run executes on a pool thread and then removes that
        # thread's scoped sessions.
        _NOTIFY_POOL.submit(_execute_entry_now, entry_wf.id, api_key, float(active_price))

    return (
        True,
        {
            "status": "success",
            "call_id": call_id,
            "mentor_id": mentor_id,
            "workflows": workflows,
            "message": message,
        },
        201,
    )


def deactivate_one(workflow_id: int, api_key: str, breach: bool = False) -> tuple[bool, str, int]:
    """Stop one live watch (if still registered) and flip is_active off.

    `breach` is set by a watch's own deactivate node after it notified a
    breach. A workflow that was still active is then stamped `breached_at`,
    which the Flow list shows in red; a manual deactivation (no flag) leaves
    it unstamped (amber). One already inactive is not stamped, so a watch the
    operator switched off earlier stays amber when its call later breaches.

    Flow workflows have no owner column, so this is the one place this
    module adds a check the underlying /flow/api/workflows/* routes do not
    have: the caller's apikey must match the one the workflow was activated
    with, so one account's apikey cannot deactivate another account's watch
    just by guessing a workflow id. Returns (success, message, status_code).

    Two callers: deactivate_by_call_id() aggregates this across a call_id's
    workflows (the sl_target watch's deactivate node), and
    restx_api/price_breach_monitor.py's per-workflow deactivate route calls
    it directly for a single id (the entry_recross watch's deactivate node
    -- see _deactivate_self_node -- and manual/operator use).
    """
    workflow = get_workflow(workflow_id)
    if not workflow:
        return False, f"Workflow {workflow_id} not found", 404

    stored_key = get_workflow_api_key(workflow)
    if stored_key and stored_key != api_key:
        return False, "This apikey did not create this workflow", 403

    was_active = bool(workflow.is_active)
    get_flow_price_monitor().remove_alert(workflow_id)
    # The 1-minute candle check runs on the scheduler, not the price monitor.
    # A no-op for the LTP watches, which have no job. Strict, so a jobstore
    # failure leaves the workflow visibly active rather than marked inactive
    # with its job still running.
    from database.flow_db import set_schedule_job_id
    from services.flow_scheduler_service import get_flow_scheduler

    try:
        get_flow_scheduler().remove_workflow_job(workflow_id, strict=True)
    except Exception:
        logger.exception(f"Could not remove the schedule of price-breach workflow {workflow_id}")
        return False, f"Failed to stop workflow {workflow_id}'s schedule", 500
    if workflow.schedule_job_id:
        set_schedule_job_id(workflow_id, None)
    db_deactivate_workflow(workflow_id)
    if breach and was_active:
        from database.flow_db import mark_breached

        mark_breached(workflow_id)
    # A candle check stopped mid-outage must not report a stale streak if
    # it is ever re-armed.
    from services.candle_check_health import forget

    forget(workflow_id)

    logger.info(
        f"Price-breach workflow {workflow_id} deactivated"
        + (" after a breach" if breach and was_active else "")
    )
    return True, f"Workflow {workflow_id} deactivated", 200


def deactivate_by_call_id(
    call_id: str, api_key: str, breach: bool = False
) -> tuple[bool, dict[str, Any], int]:
    """Deactivate every workflow create_and_activate() created for call_id
    (sl_target and, if present, entry_recross) as one unit.

    Not usually needed -- each watch deactivates itself and its sibling
    automatically once either fires. Exposed for manual/operator use (e.g.
    canceling a call before it fires). Requires the same apikey the call was
    created with. Ownership is verified against every workflow found for
    call_id before any of them are deactivated, so a mismatched apikey
    leaves all of them untouched rather than partially tearing down the
    pair.
    """
    workflow_ids = get_workflow_ids_by_call_id(call_id)
    workflows = [w for w in (get_workflow(wid) for wid in workflow_ids) if w]

    if not workflows:
        return (
            False,
            {"status": "error", "message": f"No price-breach watch found for call_id {call_id}"},
            404,
        )

    for workflow in workflows:
        stored_key = get_workflow_api_key(workflow)
        if stored_key and stored_key != api_key:
            return (
                False,
                {"status": "error", "message": "This apikey did not create this call"},
                403,
            )

    deactivated_ids = []
    for workflow in workflows:
        success, _message, _status = deactivate_one(workflow.id, api_key, breach=breach)
        if success:
            deactivated_ids.append(workflow.id)

    logger.info(f"Price-breach call {call_id} deactivated (workflows={deactivated_ids})")
    return (
        True,
        {
            "status": "success",
            "message": f"Call {call_id} deactivated",
            "workflow_ids": deactivated_ids,
        },
        200,
    )
