"""A regenerated API key must not strand an account's Flow workflows.

A workflow copied its account's key at activation and kept sending that copy.
Once the account's key was regenerated, every older workflow of the account
failed with "Invalid API key" (price alerts, webhooks, schedules) until each
was deactivated and activated again. Workflows now run with the account's
current key; the stored copy is only a fallback.
"""

import types
from unittest import mock

import pytest

import services.flow_order_update_monitor_service as oum
import services.flow_price_monitor_service as pm
import services.websocket_client as wsc
from database import auth_db
from database.flow_db import (
    FlowWorkflow,
    create_workflow,
    db_session,
    get_workflow_api_key,
    init_db,
)

ACCT = "rot_t_fivepaisa_1"
OTHER = "rot_t_angel_2"


@pytest.fixture
def workflow():
    """A snapshot of a stored workflow of ACCT, activated with KEY-OLD.

    The flow and auth sessions share one SQLite file, so the flow session is
    released before the test writes a key; an open read there locks it out.
    """
    auth_db.init_db()
    init_db()
    auth_db.upsert_api_key(ACCT, "KEY-OLD")
    stored = FlowWorkflow.query.get(create_workflow("rot-test-wf").id)
    stored.account_id = ACCT
    stored.api_key = auth_db.encrypt_token("KEY-OLD")
    db_session.commit()
    wf = types.SimpleNamespace(id=stored.id, account_id=ACCT, api_key=stored.api_key)
    db_session.remove()
    yield wf
    FlowWorkflow.query.filter(FlowWorkflow.name.like("rot-test-%")).delete(
        synchronize_session=False
    )
    db_session.commit()
    db_session.remove()
    auth_db.ApiKeys.query.filter(auth_db.ApiKeys.account_id.in_([ACCT, OTHER])).delete(
        synchronize_session=False
    )
    auth_db.db_session.commit()
    auth_db.db_session.remove()


def test_a_workflow_runs_with_its_accounts_regenerated_key(workflow):
    auth_db.upsert_api_key(ACCT, "KEY-NEW")

    assert get_workflow_api_key(workflow) == "KEY-NEW"


def test_the_stored_copy_is_the_fallback_when_the_account_has_no_key(workflow):
    workflow.account_id = OTHER  # an account with no key on file

    assert get_workflow_api_key(workflow) == "KEY-OLD"


def test_a_workflow_without_an_account_uses_its_stored_copy(workflow):
    workflow.account_id = None

    assert get_workflow_api_key(workflow) == "KEY-OLD"


def test_regenerating_a_key_moves_the_accounts_running_price_alerts(workflow):
    monitor = pm.get_flow_price_monitor()
    mine = pm.PriceAlert(workflow.id, "LT", "NSE", "greater_than", 1.0, api_key="KEY-OLD")
    theirs = pm.PriceAlert(-1, "SBIN", "NSE", "greater_than", 1.0, api_key="KEY-THEIRS")
    with mock.patch.object(monitor, "_ws_subscribe") as subscribe:
        monitor._alerts.update({mine.workflow_id: mine, theirs.workflow_id: theirs})
        try:
            auth_db.upsert_api_key(ACCT, "KEY-NEW")
        finally:
            monitor._alerts.pop(mine.workflow_id, None)
            monitor._alerts.pop(theirs.workflow_id, None)

    assert mine.api_key == "KEY-NEW"
    assert theirs.api_key == "KEY-THEIRS"
    subscribe.assert_called_once_with("KEY-NEW", "LT", "NSE")


def test_an_order_update_run_uses_the_accounts_current_key(workflow):
    auth_db.upsert_api_key(ACCT, "KEY-NEW")
    monitor = oum.get_flow_order_update_monitor()
    watch = oum.OrderUpdateWatch(workflow_id=workflow.id, api_key="KEY-OLD", trigger="every_time")
    event = types.SimpleNamespace(
        orderid="1",
        symbol="LT",
        exchange="NSE",
        order_status="complete",
        filled_quantity=1,
        average_price=1.0,
    )
    inline_pool = types.SimpleNamespace(submit=lambda fn: fn())

    with (
        mock.patch.object(oum, "_WORKFLOW_POOL", inline_pool),
        mock.patch.object(monitor, "get_watch", return_value=watch),
        mock.patch.object(oum.FlowOrderUpdateMonitor, "_workflow_is_active", return_value=True),
        mock.patch(
            "services.flow_executor_service.execute_workflow", return_value={"status": "success"}
        ) as execute,
    ):
        monitor._fire(watch, event)

    assert execute.call_args.kwargs["api_key"] == "KEY-NEW"


def test_a_client_refused_at_connect_is_shut_down():
    refused = mock.Mock()
    refused.connect.return_value = False

    with mock.patch.object(wsc, "WebSocketClient", return_value=refused):
        with pytest.raises(ConnectionError):
            wsc.get_websocket_client("KEY-REFUSED")

    refused.disconnect.assert_called_once()
    assert "KEY-REFUSED" not in wsc._client_instances
