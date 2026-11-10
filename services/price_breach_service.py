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
            "timeout": 30,
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
            "body": json.dumps({"apikey": api_key}),
            "timeout": 10,
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
            "body": json.dumps({"apikey": api_key}),
            "timeout": 10,
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
) -> dict[str, Any]:
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

    # Self only, not the call_id pair: coming back to entry does not end the
    # call, so the sl_target watch must stay live. See module docstring.
    deact_nodes, deact_edges, last_id = _deactivate_self_chain(own_id, api_key, start_y=300)
    nodes += deact_nodes
    edges += deact_edges
    edges.append({"id": f"edge-notify-{last_id}", "source": "notify", "target": last_id})

    return {
        "name": name,
        "description": f"Entry re-cross watch: {symbol} {condition} {entry_price}",
        "nodes": nodes,
        "edges": edges,
    }


# Market-hours calendar for a symbol's exchange. The calendar knows the trading
# exchanges only, so an index is gated by the exchange it is computed on.
_CALENDAR_EXCHANGE = {"NSE_INDEX": "NSE", "BSE_INDEX": "BSE", "MCX_INDEX": "MCX"}

CANDLE_VARIABLE = "candle1m"


def _armed_from_epoch(now: float | None = None) -> int:
    """Start of the first 1-minute candle that lies wholly after `now`.

    The candle a call is created in began before the call existed, so its
    high/low may include prices from before the levels applied; checking it
    could report a breach that never happened while the call was live. That
    minute is covered by the LTP watch alone.
    """
    now = time.time() if now is None else now
    return (int(now) // 60 + 1) * 60


def _candle_check_graph(
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

    Every minute during market hours: fetch the last CLOSED 1-minute candle
    (barOffset, offset 0) and test its high/low against the same channel the
    LTP watch uses. A candle range catches a level touched between two LTP
    samples, which the LTP watch can miss when it is running on its REST poll
    backstop (one sample every FLOW_PRICE_ALERT_POLL_INTERVAL seconds).

    The stop-loss side is checked first: when one candle spans both levels its
    range cannot say which was hit first, and the stop-loss is the outcome a
    receiver must not miss. Comparisons are strict, matching the LTP watch's
    outside_channel test. A breach notifies, then deactivates the whole call
    (all its watches) exactly as an sl_target breach does.

    A failed or empty candle fetch leaves `candle1m.*` unresolved, so the
    varCondition below errors and takes neither branch: missing data never
    reports a breach.
    """
    side = _call_side(stop_loss, target1)
    if side == "BUY":
        sl_check = (f"{{{{{CANDLE_VARIABLE}.low}}}}", "<", stop_loss)
        target_check = (f"{{{{{CANDLE_VARIABLE}.high}}}}", ">", target1)
    else:
        sl_check = (f"{{{{{CANDLE_VARIABLE}.high}}}}", ">", stop_loss)
        target_check = (f"{{{{{CANDLE_VARIABLE}.low}}}}", "<", target1)

    candle_ref = {
        field: f"{{{{{CANDLE_VARIABLE}.{field}}}}}"
        for field in ("timestamp", "open", "high", "low", "close")
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
    common["detected_by"] = "candle_1m"
    common["triggered_at"] = "{{iso_timestamp}}"
    common["candle"] = {"interval": "1m", **candle_ref}
    # The extreme of the candle on the side that crossed the level.
    sl_body = {**common, "breach_type": "stop_loss", "breach_price": sl_check[0]}
    target_body = {**common, "breach_type": "target1", "breach_price": target_check[0]}

    def cond(node_id, y, left, op, right):
        return {
            "id": node_id,
            "type": "varCondition",
            "position": {"x": 0, "y": y},
            "data": {"leftValue": left, "operator": op, "rightValue": str(right)},
        }

    nodes = [
        {
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
        },
        {
            "id": "candle",
            "type": "barOffset",
            "position": {"x": 0, "y": 100},
            "data": {
                "symbol": symbol,
                "exchange": exchange,
                "interval": "1m",
                "offsetBars": 0,
                "source": "api",
                "outputVariable": CANDLE_VARIABLE,
            },
        },
        cond(
            "fresh",
            200,
            f"{{{{{CANDLE_VARIABLE}.timestamp_epoch}}}}",
            ">=",
            armed_from_epoch,
        ),
        cond("check_sl", 300, *sl_check),
        cond("check_target", 400, *target_check),
        _notify_node("notify_sl", 500, webhook_url, sl_body),
        _notify_node("notify_target", 500, webhook_url, target_body),
    ]
    edges = [
        {"id": "edge-start-candle", "source": "start", "target": "candle"},
        {"id": "edge-candle-fresh", "source": "candle", "target": "fresh"},
        {
            "id": "edge-fresh-check_sl",
            "source": "fresh",
            "sourceHandle": "true",
            "target": "check_sl",
        },
        {
            "id": "edge-check_sl-notify_sl",
            "source": "check_sl",
            "sourceHandle": "true",
            "target": "notify_sl",
        },
        {
            "id": "edge-check_sl-check_target",
            "source": "check_sl",
            "sourceHandle": "false",
            "target": "check_target",
        },
        {
            "id": "edge-check_target-notify_target",
            "source": "check_target",
            "sourceHandle": "true",
            "target": "notify_target",
        },
    ]

    # The call is over on a breach, same scope as sl_target: tear down every
    # watch for this call_id, this one included.
    deact_nodes, deact_edges, last_id = _deactivate_chain(call_id, api_key, start_y=600)
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
            f"1-minute candle check: {symbol} candle range outside [{lower}, {upper}] "
            "(backup for the SL/Target watch)"
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

    candle_wf = db_create_workflow(
        name=f"{base_name} (1m Candle)",
        description="",
        nodes=[],
        edges=[],
        account_id=account_id,
        owner_username=owner_username,
    )
    if not candle_wf:
        db_delete_workflow(entry_wf.id)
        db_delete_workflow(sl_target_wf.id)
        return (
            False,
            {"status": "error", "message": "Failed to create the 1-minute candle workflow"},
            500,
        )

    # Record the call_id -> workflow_id(s) mapping so
    # /api/v1/pricebreach/<call_id>/deactivate can resolve all of them later.
    record_price_breach_call(
        call_id,
        sl_target_workflow_id=sl_target_wf.id,
        entry_recross_workflow_id=(entry_wf.id if entry_wf else None),
        candle_workflow_id=candle_wf.id,
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
        )
        update_workflow(entry_wf.id, nodes=entry_graph["nodes"], edges=entry_graph["edges"])

    armed_from = _armed_from_epoch()
    candle_graph = _candle_check_graph(
        call_id,
        mentor_id,
        symbol,
        exchange,
        entry_price,
        stop_loss,
        target1,
        webhook_url,
        api_key,
        own_id=candle_wf.id,
        name=candle_wf.name,
        armed_from_epoch=armed_from,
    )
    update_workflow(
        candle_wf.id,
        nodes=candle_graph["nodes"],
        edges=candle_graph["edges"],
        description=candle_graph["description"],
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

    # A backup, so failing to arm it does not undo the live watches the call
    # already has; it is reported in the response instead.
    candle_info: dict[str, Any] = {
        "workflow_id": candle_wf.id,
        "watching": f"1-minute candle high/low outside [{lower}, {upper}]",
        "checks_candles_from": datetime.fromtimestamp(armed_from).isoformat(),
        "active": True,
    }
    try:
        _arm_candle_check(candle_wf.id, api_key, candle_graph["nodes"][0]["data"])
    except Exception:
        logger.exception(
            f"Price-breach call {call_id}: could not arm 1-minute candle check {candle_wf.id}"
        )
        candle_info["active"] = False
        candle_info["error"] = "Could not schedule the candle check; the LTP watches are live"

    logger.info(
        f"Price-breach call {call_id} ({_call_side(stop_loss, target1)}): "
        f"sl_target={sl_target_wf.id} outside [{lower}, {upper}]"
        + (
            f", entry_recross={entry_wf.id} {entry_condition} {entry_price}"
            if entry_condition is not None
            else f", entry_recross={entry_wf.id} executed immediately (active_price == entry_price)"
        )
        + f", candle_1m={candle_wf.id} ({'armed' if candle_info['active'] else 'NOT armed'})"
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
    workflows["candle_1m"] = candle_info
    message += (
        " A 1-minute candle check also runs as a backup for stop_loss/target1; whichever check "
        "sees the breach first notifies and deactivates every watch for the call."
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


def deactivate_one(workflow_id: int, api_key: str) -> tuple[bool, str, int]:
    """Stop one live watch (if still registered) and flip is_active off.

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

    logger.info(f"Price-breach workflow {workflow_id} deactivated")
    return True, f"Workflow {workflow_id} deactivated", 200


def deactivate_by_call_id(call_id: str, api_key: str) -> tuple[bool, dict[str, Any], int]:
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
        success, _message, _status = deactivate_one(workflow.id, api_key)
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
