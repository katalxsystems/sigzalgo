"""Single-condition price alerts (Flow), exposed over the apikey-authenticated
/api/v1/ surface -- the same relationship price_breach_service.py has to
/api/v1/pricebreach/*, but for one plain "notify me when price does X"
watch instead of the paired stop-loss/target + entry-recross model that
module builds. See docs/api/market-data/pricealert.md.

Unlike pricebreach's two-workflow, apikey-loopback deactivate dance (needed
because pricebreach's sl_target and entry_recross workflows must tear each
other down together), a single alert workflow needs no auto-cleanup node at
all: Flow already deactivates a `trigger: "once"` alert's whole workflow the
instant it fires (see docs/prompt/flow-import-format.md, "One-shot triggers
deactivate the workflow when they fire"). An `every_time` alert is meant to
keep watching indefinitely (until its `expiration` or a manual deactivate),
which also needs no extra node. So the graph here is just
`priceAlert -> httpRequest`, and workflow.deactivate_one() (this module's own
sibling in price_breach_service.py, already generic over any workflow_id) is
reused as-is for manual/operator cancellation rather than duplicated.
"""

import json
from typing import Any

from database.flow_db import activate_workflow as db_activate_workflow
from database.flow_db import create_workflow as db_create_workflow
from database.flow_db import owner_for_api_key, update_workflow
from services.flow_price_monitor_service import get_flow_price_monitor
from utils.logging import get_logger

logger = get_logger(__name__)


def create_and_activate(
    api_key: str,
    symbol: str,
    exchange: str,
    condition: str,
    price: float,
    webhook_url: str,
    trigger: str = "once",
    expiration: str = "none",
    message: str | None = None,
    name: str | None = None,
) -> tuple[bool, dict[str, Any], int]:
    """Create, wire up and arm one price-alert Flow workflow.

    Returns (success, response_data, status_code), the convention every
    other service function in this codebase follows (see
    price_breach_service.create_and_activate()).
    """
    display_name = name or f"{symbol} {condition} {price}"
    display_message = message or f"{symbol} {exchange} {condition} {price}"

    account_id, owner_username = owner_for_api_key(api_key)

    # Pass 1: create an empty row to get a real id -- the notify node's own
    # body has nothing that depends on the workflow's id, so unlike
    # price_breach_service (whose deactivate nodes loop back with the id),
    # this could technically be built in one pass. Kept as two passes anyway
    # to match that module's shape and because a workflow row always needs
    # to exist before update_workflow() can target it.
    workflow = db_create_workflow(
        name=display_name,
        description=f"Price alert: {symbol}@{exchange} {condition} {price}",
        nodes=[],
        edges=[],
        account_id=account_id,
        owner_username=owner_username,
    )
    if not workflow:
        return False, {"status": "error", "message": "Failed to create workflow"}, 500

    trigger_data = {
        "symbol": symbol,
        "exchange": exchange,
        "condition": condition,
        "price": price,
        "trigger": trigger,
        "expiration": expiration,
        "message": display_message,
    }

    notify_body = {
        "symbol": symbol,
        "exchange": exchange,
        "condition": condition,
        "price": price,
        "message": display_message,
        "breach_price": "{{webhook.trigger_price}}",
        "triggered_at": "{{webhook.triggered_at}}",
    }

    nodes = [
        {"id": "trigger", "type": "priceAlert", "position": {"x": 0, "y": 0}, "data": trigger_data},
        {
            "id": "notify",
            "type": "httpRequest",
            "position": {"x": 0, "y": 150},
            "data": {
                "method": "POST",
                "url": webhook_url,
                "headers": json.dumps({"Content-Type": "application/json"}),
                "body": json.dumps(notify_body),
                "timeout": 10,
                "outputVariable": "notifyResp",
            },
        },
    ]
    edges = [{"id": "edge-trigger-notify", "source": "trigger", "target": "notify"}]

    update_workflow(workflow.id, nodes=nodes, edges=edges)

    # Pass 2: register the live watch and flip is_active -- same order
    # price_breach_service.create_and_activate() uses (add_alert() before
    # db_activate_workflow(), since the watch is what actually matters and
    # the DB flag is bookkeeping for the UI).
    price_monitor = get_flow_price_monitor()
    price_monitor.add_alert(
        workflow_id=workflow.id,
        symbol=symbol,
        exchange=exchange,
        condition=condition,
        target_price=float(price),
        api_key=api_key,
        trigger=trigger,
        expiration=expiration,
    )
    db_activate_workflow(workflow.id, api_key=api_key)

    logger.info(
        f"Price alert {workflow.id}: watching {symbol}@{exchange} {condition} {price} "
        f"(trigger: {trigger}, expires: {expiration})"
    )

    return (
        True,
        {
            "status": "success",
            "workflow_id": workflow.id,
            "name": display_name,
            "watching": f"{symbol}@{exchange} {condition} {price}",
            "message": (
                "Alert created and armed. It will POST to your webhook_url when the "
                "condition is met."
            ),
        },
        201,
    )
