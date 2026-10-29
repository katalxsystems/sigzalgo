"""Strategy monitor: an apikey-managed Flow workflow that watches one strategy.

Signalz (or any external platform) owns the levels and pushes them here with
``upsert``; this instance keeps that copy in the workflow graph and checks it
on a fixed schedule:

- combined strategy P&L (``strategyPnl``) against ``target`` and ``stoploss``
- the underlying's spot price against ``upper_spot_stoploss`` and
  ``lower_spot_stoploss``

On a breach it POSTs one notification to the caller's ``webhook_url`` and
places no orders itself - exiting is the caller's job. Once the webhook answers
2xx the monitor deactivates itself through this instance's own apikey route
(the same loopback the price-breach watches use), so a breach is reported once.
A failed delivery leaves it armed, and the next run retries.

A monitor is identified by the account behind the API key plus the strategy
tag, so upserting the same strategy again replaces the stored levels in place
(same workflow id) and re-arms it.
"""

import json
import threading
from typing import Any

from database.flow_db import activate_workflow as db_activate_workflow
from database.flow_db import create_workflow as db_create_workflow
from database.flow_db import deactivate_workflow as db_deactivate_workflow
from database.flow_db import (
    get_workflow_api_key,
    get_workflows_for_account,
    owner_for_api_key,
    set_schedule_job_id,
    update_workflow,
)
from utils.logging import get_logger

logger = get_logger(__name__)

NAME_PREFIX = "Strategy Monitor - "

# Serializes upserts so two concurrent calls for one strategy cannot both miss
# the lookup and create two monitors.
_upsert_lock = threading.Lock()

# The session whose market hours gate the schedule, by underlying exchange.
_MARKET_EXCHANGE = {"NSE_INDEX": "NSE", "BSE_INDEX": "BSE", "MCX_INDEX": "MCX"}


def monitor_name(strategy: str) -> str:
    return f"{NAME_PREFIX}{strategy}"


def find_monitor(account_id: str, strategy: str):
    name = monitor_name(strategy)
    return next((w for w in get_workflows_for_account(account_id) if w.name == name), None)


def _node(node_id: str, node_type: str, y: int, **data) -> dict:
    return {"id": node_id, "type": node_type, "position": {"x": 0, "y": y}, "data": data}


def _notify_body(workflow_id: int, strategy: str, levels: dict[str, Any]) -> str:
    """The webhook JSON, with Flow references spliced in unquoted where the
    value is a number, list or null. Every reference here resolves on every
    path that reaches the notify node: an unresolved one would leave `{{...}}`
    in the body, which then fails to parse and is posted as plain text."""
    raw = {
        "event": "strategy_breach",
        "workflow_id": workflow_id,
        "strategy": strategy,
        "reason": "{{exitReason}}",
        "idempotency_key": f"{strategy}-{{{{session_date}}}}-{{{{exitReason}}}}",
        "levels": levels,
        "pnl": {
            "total": "@@{{pnl.total}}@@",
            "realized": "@@{{pnl.realized}}@@",
            "unrealized": "@@{{pnl.unrealized}}@@",
            "today_total": "@@{{pnl.today_total}}@@",
            "unpriced_legs": "@@{{pnl.unpriced_legs}}@@",
        },
        "spot": {
            "symbol": levels.get("underlying"),
            "exchange": levels.get("underlying_exchange"),
            "ltp": "@@{{spotLtp}}@@",
        },
        "open_legs": "@@{{pnl.open_legs}}@@",
        "legs": "@@{{pnl.legs}}@@",
        "triggered_at": "{{iso_timestamp}}",
        "session_date": "{{session_date}}",
    }
    return json.dumps(raw).replace('"@@', "").replace('@@"', "")


def build_graph(
    workflow_id: int,
    strategy: str,
    api_key: str,
    webhook_url: str,
    webhook_secret: str | None,
    target: float | None,
    stoploss: float | None,
    underlying: str | None,
    underlying_exchange: str,
    upper_spot_stoploss: float | None,
    lower_spot_stoploss: float | None,
    interval_seconds: int,
) -> dict:
    """The monitor's nodes and edges. Only the checks whose level is set are
    included. Order per run: P&L target, P&L stoploss, upper spot, lower spot;
    the first that is hit names the reason, so one run sends one notification.
    """
    from services.price_breach_service import _loopback_base_url

    nodes: list[dict] = []
    edges: list[dict] = []
    y = 0

    def add(node_id, node_type, **data):
        nonlocal y
        nodes.append(_node(node_id, node_type, y, **data))
        y += 100
        return node_id

    def link(source, target_id, handle=None):
        edge = {"id": f"e{len(edges) + 1}", "source": source, "target": target_id}
        if handle:
            edge["sourceHandle"] = handle
        edges.append(edge)

    def cond(node_id, label, left, op, right):
        return add(
            node_id, "varCondition", label=label, leftValue=left, operator=op, rightValue=str(right)
        )

    def reason(node_id, value):
        return add(
            node_id,
            "variable",
            label=f"Reason = {value}",
            variableName="exitReason",
            operation="set",
            value=value,
        )

    if interval_seconds % 60 == 0:
        every = {"intervalValue": interval_seconds // 60, "intervalUnit": "minutes"}
    else:
        every = {"intervalValue": interval_seconds, "intervalUnit": "seconds"}
    start = add(
        "start",
        "start",
        label="Schedule",
        scheduleType="interval",
        **every,
        marketHoursOnly=True,
        marketHoursExchange=_MARKET_EXCHANGE.get(underlying_exchange, underlying_exchange)
        if underlying
        else "NSE",
    )
    pnl = add("pnl", "strategyPnl", label="Strategy P&L", strategy=strategy, outputVariable="pnl")
    link(start, pnl)
    is_open = cond("open", "Any leg open?", "{{pnl.open_legs}}", ">", 0)
    link(pnl, is_open)
    # Initialised on every path so the notify body always resolves; set to the
    # quote only when the spot is actually fetched.
    spot_init = add(
        "spot_init",
        "variable",
        label="spotLtp = null",
        variableName="spotLtp",
        operation="set",
        value="null",
    )
    link(is_open, spot_init, "true")

    breaches: list[str] = []  # reason nodes, all wired to the notify node

    # Spot block first, so the P&L block knows where its FALSE branches go.
    spot_entry = None
    spot_steps = [
        ("upper", ">=", upper_spot_stoploss, "UPPER_SPOT_SL"),
        ("lower", "<=", lower_spot_stoploss, "LOWER_SPOT_SL"),
    ]
    spot_steps = [s for s in spot_steps if s[2] is not None]
    if spot_steps:
        spot_entry = add(
            "spot_quote",
            "getQuote",
            label=f"{underlying} quote",
            symbol=underlying,
            exchange=underlying_exchange,
            outputVariable="spot",
        )
        # A failed quote leaves {{spot.data.ltp}} unresolved, so this errors
        # and takes neither branch: no exit on missing data. (priceCondition
        # would read a failed quote as 0 and fire a lower stoploss.)
        ok = cond("spot_ok", "Spot price valid?", "{{spot.data.ltp}}", ">", 0)
        link(spot_entry, ok)
        prev = add(
            "spot_set",
            "variable",
            label="spotLtp = quote",
            variableName="spotLtp",
            operation="set",
            value="{{spot.data.ltp}}",
        )
        link(ok, prev, "true")
        prev_handle = None
        for key, op, level, why in spot_steps:
            check = cond(f"spot_{key}", f"Spot {op} {level}?", "{{spot.data.ltp}}", op, level)
            link(prev, check, prev_handle)
            hit = reason(f"reason_{key}", why)
            link(check, hit, "true")
            breaches.append(hit)
            prev, prev_handle = check, "false"

    pnl_steps = [
        ("target", ">=", target, "TARGET"),
        ("stoploss", "<=", -stoploss if stoploss is not None else None, "STOPLOSS"),
    ]
    pnl_steps = [s for s in pnl_steps if s[2] is not None]
    if pnl_steps:
        # An unpriced leg is left out of the total, so P&L levels are skipped
        # that run - but spot levels still apply.
        priced = cond("priced", "All legs priced?", "{{pnl.unpriced_legs}}", "==", 0)
        link(spot_init, priced)
        if spot_entry:
            link(priced, spot_entry, "false")
        prev, prev_handle = priced, "true"
        for key, op, level, why in pnl_steps:
            check = cond(f"pnl_{key}", f"P&L {op} {level}?", "{{pnl.total}}", op, level)
            link(prev, check, prev_handle)
            hit = reason(f"reason_{key}", why)
            link(check, hit, "true")
            breaches.append(hit)
            prev, prev_handle = check, "false"
        if spot_entry:
            link(prev, spot_entry, "false")
    else:
        link(spot_init, spot_entry)

    levels = {
        "target": target,
        "stoploss": stoploss,
        "underlying": underlying,
        "underlying_exchange": underlying_exchange if underlying else None,
        "upper_spot_stoploss": upper_spot_stoploss,
        "lower_spot_stoploss": lower_spot_stoploss,
    }
    headers = {"Content-Type": "application/json"}
    if webhook_secret:
        headers["X-Webhook-Secret"] = webhook_secret
    notify = add(
        "notify",
        "httpRequest",
        label="Notify webhook",
        method="POST",
        url=webhook_url,
        headers=headers,
        body=_notify_body(workflow_id, strategy, levels),
        timeout=15,
        outputVariable="notifyResp",
    )
    for hit in breaches:
        link(hit, notify)

    # Delivered (2xx) -> stop monitoring. Anything else stays armed and retries.
    ok_low = cond("delivered_low", "Webhook status >= 200?", "{{notifyResp.statusCode}}", ">=", 200)
    link(notify, ok_low)
    ok_high = cond("delivered_high", "Webhook status < 300?", "{{notifyResp.statusCode}}", "<", 300)
    link(ok_low, ok_high, "true")
    stop = add(
        "deactivate",
        "httpRequest",
        label="Deactivate this monitor",
        method="POST",
        url=f"{_loopback_base_url()}/api/v1/strategymonitor/deactivate",
        headers={"Content-Type": "application/json"},
        body=json.dumps({"apikey": api_key, "strategy": strategy}),
        timeout=10,
    )
    link(ok_high, stop, "true")

    return {"nodes": nodes, "edges": edges}


def _arm(workflow_id: int, api_key: str, trigger: dict) -> None:
    """Register (or replace) the schedule job, then mark the workflow active."""
    from services.flow_scheduler_service import get_flow_scheduler

    job_id = get_flow_scheduler().add_workflow_job(
        workflow_id=workflow_id,
        schedule_type="interval",
        interval_value=trigger["intervalValue"],
        interval_unit=trigger["intervalUnit"],
        market_hours_only=True,
    )
    set_schedule_job_id(workflow_id, job_id)
    db_activate_workflow(workflow_id, api_key=api_key)


def upsert(api_key: str, strategy: str, **settings) -> tuple[bool, dict[str, Any], int]:
    """Create the strategy's monitor, or replace its levels, and arm it.
    Returns (success, response_data, status_code)."""
    from services.flow_workflow_validator import validate_workflow

    account_id, owner_username = owner_for_api_key(api_key)
    if not account_id:
        return False, {"status": "error", "message": "Invalid openalgo apikey"}, 403

    with _upsert_lock:
        workflow = find_monitor(account_id, strategy)
        created = workflow is None
        if created:
            workflow = db_create_workflow(
                name=monitor_name(strategy),
                description="",
                nodes=[],
                edges=[],
                account_id=account_id,
                owner_username=owner_username,
            )
            if not workflow:
                return False, {"status": "error", "message": "Failed to create monitor"}, 500

        graph = build_graph(workflow.id, strategy, api_key, **settings)
        errors = validate_workflow({"name": monitor_name(strategy), **graph}, strict=True)
        if errors:
            # A bug in build_graph, not bad input: the schema already checked it.
            logger.error(f"Strategy monitor graph for {strategy} is invalid: {errors}")
            if created:
                from database.flow_db import delete_workflow

                delete_workflow(workflow.id)
            return False, {"status": "error", "message": "Could not build the monitor"}, 500

        update_workflow(
            workflow.id,
            nodes=graph["nodes"],
            edges=graph["edges"],
            description=f"Monitors strategy {strategy}; notifies {settings['webhook_url']} on a breach.",
        )
        trigger = graph["nodes"][0]["data"]
        try:
            _arm(workflow.id, api_key, trigger)
        except Exception as e:
            logger.exception(f"Failed to arm strategy monitor {workflow.id} for {strategy}: {e}")
            return False, {"status": "error", "message": "Failed to activate monitor"}, 500

    logger.info(
        f"Strategy monitor {workflow.id} for {strategy} on account {account_id} "
        f"{'created' if created else 'updated'} and armed"
    )
    return (
        True,
        {
            "status": "success",
            "workflow_id": workflow.id,
            "strategy": strategy,
            "created": created,
            "active": True,
            "message": "Monitor created and armed"
            if created
            else "Levels updated and monitor armed",
        },
        201 if created else 200,
    )


def deactivate(api_key: str, strategy: str) -> tuple[bool, dict[str, Any], int]:
    """Disarm the strategy's monitor. Idempotent: an already inactive monitor
    is a success. Returns (success, response_data, status_code)."""
    from services.flow_executor_service import release_workflow_subscriptions
    from services.flow_scheduler_service import get_flow_scheduler

    account_id, _owner = owner_for_api_key(api_key)
    if not account_id:
        return False, {"status": "error", "message": "Invalid openalgo apikey"}, 403

    workflow = find_monitor(account_id, strategy)
    if workflow is None:
        return False, {"status": "error", "message": f"No monitor for strategy {strategy}"}, 404

    stored_key = get_workflow_api_key(workflow)
    if stored_key and stored_key != api_key:
        return False, {"status": "error", "message": "This apikey did not create this monitor"}, 403

    if not workflow.is_active:
        return (
            True,
            {
                "status": "success",
                "workflow_id": workflow.id,
                "message": "Monitor already inactive",
            },
            200,
        )

    # strict: a jobstore failure raises rather than leaving a live job behind
    # a workflow marked inactive.
    get_flow_scheduler().remove_workflow_job(workflow.id, strict=True)
    set_schedule_job_id(workflow.id, None)
    release_workflow_subscriptions(workflow.id)
    if not db_deactivate_workflow(workflow.id):
        return False, {"status": "error", "message": "Failed to deactivate monitor"}, 500

    logger.info(f"Strategy monitor {workflow.id} for {strategy} deactivated")
    return (
        True,
        {"status": "success", "workflow_id": workflow.id, "message": "Monitor deactivated"},
        200,
    )
