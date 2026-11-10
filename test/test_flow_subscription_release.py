"""Flow release failures must leave enough ownership to retry safely.

Service-level tests ported from upstream test/test_gthread_memory_flow.py
(marketcalls/openalgo 38dbdcb0f); route tests rewritten for this branch.
"""

from types import SimpleNamespace

import pytest
from flask import Flask


@pytest.fixture(autouse=True)
def isolated_flow_subscription_registry(monkeypatch):
    from services import flow_executor_service as flow

    monkeypatch.setattr(flow, "_workflow_subscriptions", {})
    monkeypatch.setattr(flow, "_workflow_subscription_versions", {})
    monkeypatch.setattr(flow, "_workflow_subscription_owners", {})
    monkeypatch.setattr(flow, "_pending_release_ids", set())


def test_failed_flow_unsubscribe_keeps_each_owner_until_retry(monkeypatch):
    from services import flow_executor_service as flow
    from services import websocket_service

    monkeypatch.setattr(flow, "_subscription_owner", lambda _id: ("trader", "broker"))
    available = {"value": False}
    calls = []

    def unsubscribe(_username, _broker, symbols, _mode):
        calls.append(symbols[0]["symbol"])
        return available["value"], {}, 200 if available["value"] else 503

    monkeypatch.setattr(websocket_service, "unsubscribe_from_symbols", unsubscribe)
    for workflow_id in range(100_000, 100_100):
        flow.record_workflow_subscription(workflow_id, f"SYM{workflow_id}", "NSE", "LTP")
        assert flow.release_workflow_subscriptions(workflow_id) == 0
        assert flow._workflow_subscriptions[workflow_id] == {(f"SYM{workflow_id}", "NSE", "LTP")}
        available["value"] = True
        assert flow.release_workflow_subscriptions(workflow_id) == 1
        assert workflow_id not in flow._workflow_subscriptions
        available["value"] = False

    assert len(calls) == 200
    assert not any(100_000 <= key < 100_100 for key in flow._workflow_subscriptions)
    assert not any(100_000 <= key < 100_100 for key in flow._workflow_subscription_versions)
    assert not any(100_000 <= key < 100_100 for key in flow._workflow_subscription_owners)


def test_partial_release_keeps_only_failed_entries_and_captured_owner(monkeypatch):
    from services import flow_executor_service as flow
    from services import websocket_service

    workflow_id = 100_150
    monkeypatch.setattr(flow, "_subscription_owner", lambda _id: (None, "unknown"))
    failed = {"FAIL"}
    owners = []

    def unsubscribe(username, broker, symbols, _mode):
        symbol = symbols[0]["symbol"]
        owners.append((username, broker))
        return symbol not in failed, {}, 200 if symbol not in failed else 503

    monkeypatch.setattr(websocket_service, "unsubscribe_from_symbols", unsubscribe)
    flow.record_workflow_subscription(workflow_id, "PASS", "NSE", "LTP", "trader", "broker")
    flow.record_workflow_subscription(workflow_id, "FAIL", "NSE", "LTP", "trader", "broker")

    assert flow.release_workflow_subscriptions(workflow_id) == 1
    assert flow._workflow_subscriptions[workflow_id] == {("FAIL", "NSE", "LTP")}
    failed.clear()
    assert flow.release_workflow_subscriptions(workflow_id) == 1
    assert workflow_id not in flow._workflow_subscriptions
    assert workflow_id not in flow._workflow_subscription_owners
    assert owners == [("trader", "broker"), ("trader", "broker"), ("trader", "broker")]


def test_release_does_not_forget_a_new_subscription_with_the_same_key(monkeypatch):
    from services import flow_executor_service as flow
    from services import websocket_service

    workflow_id = 100_151
    calls = []

    def unsubscribe(*_args):
        calls.append(1)
        if len(calls) == 1:
            flow.record_workflow_subscription(workflow_id, "SYM", "NSE", "LTP", "trader", "broker")
        return True, {}, 200

    monkeypatch.setattr(websocket_service, "unsubscribe_from_symbols", unsubscribe)
    flow.record_workflow_subscription(workflow_id, "SYM", "NSE", "LTP", "trader", "broker")

    assert flow.release_workflow_subscriptions(workflow_id) == 1
    assert flow._workflow_subscriptions[workflow_id] == {("SYM", "NSE", "LTP")}
    assert flow.release_workflow_subscriptions(workflow_id) == 1
    assert workflow_id not in flow._workflow_subscriptions


def test_one_flow_owner_does_not_unsubscribe_another_flows_feed(monkeypatch):
    from services import flow_executor_service as flow
    from services import websocket_service

    calls = []
    monkeypatch.setattr(
        websocket_service,
        "unsubscribe_from_symbols",
        lambda *args: (calls.append(args) or True, {}, 200),
    )
    flow.record_workflow_subscription(100_160, "SYM", "NSE", "LTP", "trader", "broker")
    flow.record_workflow_subscription(100_161, "SYM", "NSE", "LTP", "trader", "broker")

    assert flow.release_workflow_subscriptions(100_160) == 1
    assert 100_160 not in flow._workflow_subscriptions
    assert 100_161 in flow._workflow_subscriptions
    assert calls == []
    assert flow.release_workflow_subscriptions(100_161) == 1
    assert len(calls) == 1


# --- Route behaviour on this branch -----------------------------------------
#
# Upstream (38dbdcb0f) refuses deactivate/delete with 503 while a release is
# failing and leaves the triggers running. This branch always stops the
# workflow and queues the release for retry, so a user can stop a live
# trading workflow while the market-data feed is disconnected.


@pytest.fixture
def stub_triggers(monkeypatch):
    """Replace the scheduler and both monitors; record what was torn down."""
    from services import (
        flow_order_update_monitor_service,
        flow_price_monitor_service,
        flow_scheduler_service,
    )

    removed = []
    monkeypatch.setattr(
        flow_scheduler_service,
        "get_flow_scheduler",
        lambda: SimpleNamespace(
            remove_workflow_job=lambda *_a, **_k: removed.append("schedule") or True
        ),
    )
    monkeypatch.setattr(
        flow_price_monitor_service,
        "get_flow_price_monitor",
        lambda: SimpleNamespace(remove_alert=lambda _id: removed.append("price")),
    )
    monkeypatch.setattr(
        flow_order_update_monitor_service,
        "get_flow_order_update_monitor",
        lambda: SimpleNamespace(remove_watch=lambda _id: removed.append("order")),
    )
    return removed


def _use_workflow(monkeypatch, workflow):
    from blueprints import flow as routes

    monkeypatch.setattr(routes, "load_workflow", lambda _id: (workflow, None))


def _feed(monkeypatch, available):
    """Make every unsubscribe succeed or fail depending on available["value"]."""
    calls = []

    def unsubscribe(_username, _broker, symbols, _mode):
        calls.append(symbols[0]["symbol"])
        return available["value"], {}, 200 if available["value"] else 503

    monkeypatch.setattr("services.websocket_service.unsubscribe_from_symbols", unsubscribe)
    return calls


def _status(result):
    return result[1] if isinstance(result, tuple) else result.status_code


def _body(result):
    response = result[0] if isinstance(result, tuple) else result
    return response.get_json()


def test_deactivate_stops_triggers_even_when_the_feed_is_down(monkeypatch, stub_triggers):
    from blueprints import flow as routes
    from database import flow_db
    from services import flow_executor_service as flow

    workflow_id = 100_201
    _use_workflow(monkeypatch, SimpleNamespace(is_active=True, schedule_job_id=None))
    deactivated = []
    monkeypatch.setattr(flow_db, "deactivate_workflow", lambda _id: deactivated.append(_id) or True)
    _feed(monkeypatch, {"value": False})
    flow.record_workflow_subscription(workflow_id, "SYM", "NSE", "LTP", "trader", "broker")

    with Flask(__name__).test_request_context():
        result = routes.deactivate_workflow.__wrapped__(workflow_id)

    assert _status(result) == 200
    assert _body(result)["feed_release_pending"] is True
    assert deactivated == [workflow_id]
    assert stub_triggers == ["schedule", "price", "order"]
    assert workflow_id in flow._workflow_subscriptions
    assert workflow_id in flow._pending_release_ids


def test_delete_proceeds_and_the_release_is_retried_after_the_row_is_gone(
    monkeypatch, stub_triggers
):
    from blueprints import flow as routes
    from database import flow_db
    from services import flow_executor_service as flow

    workflow_id = 100_200
    _use_workflow(monkeypatch, SimpleNamespace(is_active=False))
    deleted = []
    monkeypatch.setattr(flow_db, "delete_workflow", lambda _id: deleted.append(_id) or True)
    available = {"value": False}
    calls = _feed(monkeypatch, available)
    # Owner captured at subscribe time: the retry must not need the deleted row.
    monkeypatch.setattr(
        flow, "_subscription_owner", lambda _id: pytest.fail("owner lookup after delete")
    )
    flow.record_workflow_subscription(workflow_id, "SYM", "NSE", "LTP", "trader", "broker")

    with Flask(__name__).test_request_context():
        result = routes.delete_workflow.__wrapped__(workflow_id)

    assert _status(result) == 200
    assert _body(result)["feed_release_pending"] is True
    assert deleted == [workflow_id]
    assert workflow_id in flow._pending_release_ids

    available["value"] = True
    assert flow.retry_pending_releases() == 1
    assert workflow_id not in flow._workflow_subscriptions
    assert workflow_id not in flow._pending_release_ids
    assert calls == ["SYM", "SYM"]


def test_a_later_deactivate_retries_another_workflows_pending_release(monkeypatch, stub_triggers):
    from blueprints import flow as routes
    from services import flow_executor_service as flow

    pending_id, other_id = 100_202, 100_212
    available = {"value": False}
    _feed(monkeypatch, available)
    flow.record_workflow_subscription(pending_id, "SYM", "NSE", "LTP", "trader", "broker")
    assert flow.release_workflow_subscriptions_or_defer(pending_id) is False

    _use_workflow(monkeypatch, SimpleNamespace(is_active=False))
    available["value"] = True
    with Flask(__name__).test_request_context():
        result = routes.deactivate_workflow.__wrapped__(other_id)

    assert _status(result) == 200
    assert "feed_release_pending" not in _body(result)
    assert pending_id not in flow._workflow_subscriptions
    assert pending_id not in flow._pending_release_ids


def test_retry_skips_a_workflow_that_is_running(monkeypatch):
    from services import flow_executor_service as flow

    workflow_id = 100_204
    available = {"value": False}
    calls = _feed(monkeypatch, available)
    flow.record_workflow_subscription(workflow_id, "SYM", "NSE", "LTP", "trader", "broker")
    flow.release_workflow_subscriptions_or_defer(workflow_id)
    available["value"] = True

    lock = flow.get_workflow_lock(workflow_id)
    assert lock.acquire(blocking=False)
    try:
        assert flow.retry_pending_releases() == 0
    finally:
        lock.release()

    assert calls == ["SYM"], "a running workflow's feed must not be released under it"
    assert workflow_id in flow._pending_release_ids


def test_activation_cancels_the_pending_release(monkeypatch):
    from services import flow_executor_service as flow

    workflow_id = 100_205
    available = {"value": False}
    calls = _feed(monkeypatch, available)
    flow.record_workflow_subscription(workflow_id, "SYM", "NSE", "LTP", "trader", "broker")
    flow.release_workflow_subscriptions_or_defer(workflow_id)

    flow.cancel_pending_release(workflow_id)
    available["value"] = True
    assert flow.retry_pending_releases() == 0
    assert calls == ["SYM"]
    assert workflow_id in flow._workflow_subscriptions


def test_delete_refuses_a_workflow_still_executing(monkeypatch, stub_triggers):
    from blueprints import flow as routes
    from database import flow_db
    from services import flow_executor_service as flow

    workflow_id = 100_203
    changed = []
    _use_workflow(monkeypatch, SimpleNamespace(is_active=True, schedule_job_id=None))
    monkeypatch.setattr(flow_db, "delete_workflow", lambda _id: changed.append("delete") or True)
    lock = flow.get_workflow_lock(workflow_id)
    assert lock.acquire(blocking=False)
    try:
        with Flask(__name__).test_request_context():
            result = routes.delete_workflow.__wrapped__(workflow_id)
    finally:
        lock.release()

    assert _status(result) == 409
    assert changed == []
    assert stub_triggers == []


def test_deactivate_during_a_run_stops_triggers_and_releases_after_the_run(
    monkeypatch, stub_triggers
):
    from blueprints import flow as routes
    from database import flow_db
    from services import flow_executor_service as flow

    workflow_id = 100_206
    deactivated = []
    _use_workflow(monkeypatch, SimpleNamespace(is_active=True, schedule_job_id=None))
    monkeypatch.setattr(flow_db, "deactivate_workflow", lambda _id: deactivated.append(_id) or True)
    calls = _feed(monkeypatch, {"value": True})

    lock = flow.get_workflow_lock(workflow_id)
    assert lock.acquire(blocking=False)
    try:
        with Flask(__name__).test_request_context():
            result = routes.deactivate_workflow.__wrapped__(workflow_id)
        # The run subscribes after the deactivate, before it finishes.
        flow.record_workflow_subscription(workflow_id, "SYM", "NSE", "LTP", "trader", "broker")
        assert flow.retry_pending_releases() == 0, "must not release under the running workflow"
    finally:
        lock.release()

    assert _status(result) == 200
    assert _body(result)["feed_release_pending"] is True
    assert deactivated == [workflow_id]
    assert stub_triggers == ["schedule", "price", "order"]
    assert calls == []

    assert flow.retry_pending_releases() == 1
    assert calls == ["SYM"]
    assert workflow_id not in flow._workflow_subscriptions
    assert workflow_id not in flow._pending_release_ids


@pytest.mark.parametrize("action", ["delete", "deactivate"])
def test_an_unknown_workflow_id_creates_no_lock_entry(monkeypatch, action):
    from flask import jsonify

    from blueprints import flow as routes
    from services import flow_executor_service as flow

    workflow_id = 100_299
    monkeypatch.setattr(flow, "_workflow_locks", {})
    with Flask(__name__).test_request_context():
        monkeypatch.setattr(
            routes,
            "load_workflow",
            lambda _id: (None, (jsonify({"error": "Workflow not found"}), 404)),
        )
        route = routes.delete_workflow if action == "delete" else routes.deactivate_workflow
        result = route.__wrapped__(workflow_id)

    assert _status(result) == 404
    assert workflow_id not in flow._workflow_locks
