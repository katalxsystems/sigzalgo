"""Price-breach Flow workflow: build, create+activate, and deactivate.

Wraps the same Flow primitives scripts/price_breach_monitor.py drives over
HTTP (one `priceAlert` trigger with `condition="outside_channel"` watching
[stop_loss, target1], into one `httpRequest` action notifying a webhook), but
callable directly with an already-resolved account_id/api_key -- no session
cookie needed. This is what lets restx_api/price_breach_monitor.py offer the
same feature over the ordinary apikey-authenticated /api/v1/ surface.

Flow workflows have no owner column (see database/flow_db.py's FlowWorkflow)
-- like the rest of Flow, this is instance-wide, not scoped per account. That
matches the existing session-based /flow/api/workflows/* routes; this module
does not add isolation those routes do not already have.
"""

import json
from typing import Any

from database.flow_db import activate_workflow as db_activate_workflow
from database.flow_db import create_workflow as db_create_workflow
from database.flow_db import deactivate_workflow as db_deactivate_workflow
from database.flow_db import get_workflow, get_workflow_api_key, update_workflow
from services.flow_price_monitor_service import get_flow_price_monitor
from utils.logging import get_logger

logger = get_logger(__name__)


def build_breach_workflow_graph(
    symbol: str,
    exchange: str,
    stop_loss: float,
    target1: float,
    webhook_url: str,
    entry_price: float | None = None,
    workflow_id: int | None = None,
) -> dict[str, Any]:
    """Build the {name, description, nodes, edges} graph for a price-breach
    watch. `workflow_id` is None until the workflow has been created once --
    see create_and_activate() below, which creates, then rewrites the notify
    node's body with the real id before activating."""
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
        "name": f"{symbol} breach watch",
        "description": f"Price breach watch: {symbol} outside [{stop_loss}, {target1}]",
        "nodes": nodes,
        "edges": edges,
    }


def create_and_activate(
    api_key: str,
    symbol: str,
    exchange: str,
    stop_loss: float,
    target1: float,
    webhook_url: str,
    entry_price: float | None = None,
    name: str | None = None,
) -> tuple[bool, dict[str, Any], int]:
    """Create the breach workflow, wire in its own id, and activate the
    price-alert watch. Returns (success, response_data, status_code) -- the
    convention every other service function in this codebase follows.
    """
    if stop_loss >= target1:
        return (
            False,
            {"status": "error", "message": "stop_loss must be less than target1"},
            400,
        )

    graph = build_breach_workflow_graph(symbol, exchange, stop_loss, target1, webhook_url, entry_price)
    if name:
        graph["name"] = name

    workflow = db_create_workflow(
        name=graph["name"], description=graph["description"], nodes=graph["nodes"], edges=graph["edges"]
    )
    if not workflow:
        return False, {"status": "error", "message": "Failed to create workflow"}, 500

    # Rebuild the notify node's body now that the workflow (and therefore its
    # id) exists, so the receiver's webhook payload can name it.
    final_graph = build_breach_workflow_graph(
        symbol, exchange, stop_loss, target1, webhook_url, entry_price, workflow_id=workflow.id
    )
    updated = update_workflow(workflow.id, nodes=final_graph["nodes"], edges=final_graph["edges"])
    if not updated:
        return (
            False,
            {"status": "error", "message": "Workflow created but failed to finalize", "workflow_id": workflow.id},
            500,
        )

    price_monitor = get_flow_price_monitor()
    price_monitor.add_alert(
        workflow_id=workflow.id,
        symbol=symbol,
        exchange=exchange,
        condition="outside_channel",
        target_price=float(target1),
        price_lower=float(stop_loss),
        price_upper=float(target1),
        api_key=api_key,
        trigger="once",
        expiration="none",
    )
    db_activate_workflow(workflow.id, api_key=api_key)

    logger.info(
        f"Price-breach workflow {workflow.id} activated for {symbol}@{exchange}: "
        f"outside [{stop_loss}, {target1}]"
    )

    return (
        True,
        {
            "status": "success",
            "workflow_id": workflow.id,
            "message": f"Watching {symbol}@{exchange} for price outside [{stop_loss}, {target1}]",
        },
        201,
    )


def deactivate(workflow_id: int, api_key: str) -> tuple[bool, dict[str, Any], int]:
    """Stop the live watch (if still registered) and flip is_active off.

    Flow workflows have no owner column, so this is the one place this
    module adds a check the underlying /flow/api/workflows/* routes do not
    have: the caller's apikey must match the one the workflow was activated
    with, so one account's apikey cannot deactivate another account's watch
    just by guessing a workflow id.
    """
    workflow = get_workflow(workflow_id)
    if not workflow:
        return False, {"status": "error", "message": "Workflow not found"}, 404

    stored_key = get_workflow_api_key(workflow)
    if stored_key and stored_key != api_key:
        return False, {"status": "error", "message": "This apikey did not create this workflow"}, 403

    get_flow_price_monitor().remove_alert(workflow_id)
    db_deactivate_workflow(workflow_id)

    logger.info(f"Price-breach workflow {workflow_id} deactivated")
    return True, {"status": "success", "message": f"Workflow {workflow_id} deactivated"}, 200
