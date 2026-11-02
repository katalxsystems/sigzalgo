"""/api/v1/pricebreach/create handles sell calls, told apart by the levels.

A sell call's stop-loss sits above its target, so every sell call was refused
with "stop_loss must be less than target1". The request has no side field and
callers cannot add one yet, so the side is read from the levels: stop_loss
below target1 is a buy, above it a sell.

Removing the refusal alone was not enough. The SL/Target workflow decides which
level was hit with a comparison against stop_loss, and the buy-only `<` would
have reported a sell call's stop-loss hit to the webhook as target1.
"""

import types

import pytest

import database.flow_db as flow_db
import services.price_breach_service as pbs
from services.flow_executor_service import NodeExecutor, WorkflowContext


def _sl_target(stop_loss, target1):
    return pbs._sl_target_graph(
        "C1",
        "M1",
        "SBIN",
        "NSE",
        800.0,
        stop_loss,
        target1,
        "https://receiver.example/hook",
        "KEY",
        own_id=1,
        name="t",
    )


def _node(graph, node_id):
    return next(n for n in graph["nodes"] if n["id"] == node_id)


def _branch_taken(graph, breach_price):
    """Which notify node the real executor routes a breach at this price to."""
    context = WorkflowContext(workflow_id=1)
    context.set_variable("webhook", {"trigger_price": breach_price})
    result = NodeExecutor(None, context, []).execute_var_condition(_node(graph, "branch")["data"])
    assert result["status"] == "success"
    handle = "true" if result["condition"] else "false"
    edge = next(
        e for e in graph["edges"] if e["source"] == "branch" and e.get("sourceHandle") == handle
    )
    breach_type = '"breach_type": "stop_loss"' if edge["target"] == "notify_sl" else None
    if edge["target"] == "notify_target":
        breach_type = '"breach_type": "target1"'
    assert breach_type in _node(graph, edge["target"])["data"]["body"]
    return edge["target"]


# --- which level a breach is reported as ----------------------------------------


@pytest.mark.parametrize(
    ("stop_loss", "target1", "breach_price", "reported"),
    [
        # buy: stop-loss below, target above
        (780.0, 840.0, 775.0, "notify_sl"),
        (780.0, 840.0, 845.0, "notify_target"),
        # sell: stop-loss above, target below
        (820.0, 760.0, 825.0, "notify_sl"),
        (820.0, 760.0, 755.0, "notify_target"),
    ],
)
def test_a_breach_is_reported_as_the_level_actually_crossed(
    stop_loss, target1, breach_price, reported
):
    assert _branch_taken(_sl_target(stop_loss, target1), breach_price) == reported


@pytest.mark.parametrize(("stop_loss", "target1"), [(780.0, 840.0), (820.0, 760.0)])
def test_the_watched_channel_is_lower_to_upper_for_either_side(stop_loss, target1):
    trigger = _node(_sl_target(stop_loss, target1), "trigger")["data"]

    assert (trigger["priceLower"], trigger["priceUpper"]) == (
        min(stop_loss, target1),
        max(stop_loss, target1),
    )


def test_the_webhook_still_carries_the_callers_own_levels():
    """The receiver keys on stop_loss/target1 as sent, not on the sorted bounds."""
    body = _node(_sl_target(820.0, 760.0), "notify_sl")["data"]["body"]

    assert '"stop_loss": 820.0' in body
    assert '"target1": 760.0' in body


# --- the create call ------------------------------------------------------------


@pytest.fixture
def created(monkeypatch):
    """create_and_activate with persistence and the live monitor stubbed."""
    alerts = []
    ids = iter(range(500, 600))

    def create_workflow(name, **kwargs):
        return types.SimpleNamespace(id=next(ids), name=name)

    monkeypatch.setattr(flow_db, "owner_for_api_key", lambda key: ("acct", "user"))
    monkeypatch.setattr(pbs, "db_create_workflow", create_workflow)
    monkeypatch.setattr(pbs, "record_price_breach_call", lambda *a, **k: None)
    monkeypatch.setattr(pbs, "update_workflow", lambda *a, **k: None)
    monkeypatch.setattr(pbs, "db_activate_workflow", lambda *a, **k: None)
    monkeypatch.setattr(
        pbs,
        "get_flow_price_monitor",
        lambda: types.SimpleNamespace(add_alert=lambda **kw: alerts.append(kw)),
    )

    def call(stop_loss, target1, active_price=800.0, entry_price=790.0):
        result = pbs.create_and_activate(
            api_key="KEY",
            call_id="C1",
            mentor_id="M1",
            symbol="SBIN",
            exchange="NSE",
            active_price=active_price,
            entry_price=entry_price,
            stop_loss=stop_loss,
            target1=target1,
            webhook_url="https://receiver.example/hook",
        )
        return result, alerts

    return call


def test_a_sell_call_is_accepted_and_watches_the_sorted_channel(created):
    (ok, body, status), alerts = created(stop_loss=820.0, target1=760.0)

    assert (ok, status) == (True, 201)
    channel = next(a for a in alerts if a["condition"] == "outside_channel")
    assert (channel["price_lower"], channel["price_upper"]) == (760.0, 820.0)
    assert body["workflows"]["sl_target"]["watching"] == "price outside [760.0, 820.0]"


def test_a_buy_call_is_unchanged(created):
    (ok, _, status), alerts = created(stop_loss=780.0, target1=840.0)

    assert (ok, status) == (True, 201)
    channel = next(a for a in alerts if a["condition"] == "outside_channel")
    assert (channel["price_lower"], channel["price_upper"]) == (780.0, 840.0)


def test_equal_levels_are_refused(created):
    (ok, body, status), alerts = created(stop_loss=800.0, target1=800.0)

    assert (ok, status) == (False, 400)
    assert body["message"] == "stop_loss and target1 must differ"
    assert alerts == []
