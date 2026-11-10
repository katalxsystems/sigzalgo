"""The 1-minute candle backup for a price-breach call.

/api/v1/pricebreach/create now also arms a scheduled workflow that checks the
last closed 1-minute candle's high/low against stop_loss and target1, and
deactivating the call tears it down with the LTP watches.

The graph is run through the real Flow executor against a fake broker client
and a fake HTTP client, so these check what the workflow actually sends.
"""

import os
import sys
import time
import types
from datetime import datetime
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database.flow_db as flow_db  # noqa: E402
import services.flow_executor_service as fes  # noqa: E402
import services.indicator_service as indicator_service  # noqa: E402
import services.price_breach_service as pbs  # noqa: E402
from services.flow_workflow_validator import validate_workflow  # noqa: E402

WEBHOOK = "https://receiver.example/hook"
KEY = "KEY"
ARMED = 1_794_000_000  # a minute boundary
BUY = {"stop_loss": 780.0, "target1": 840.0}
SELL = {"stop_loss": 820.0, "target1": 760.0}


def graph_for(levels, armed=ARMED, exchange="NSE"):
    return pbs._candle_check_graph(
        "C1",
        "M1",
        "SBIN",
        exchange,
        800.0,
        levels["stop_loss"],
        levels["target1"],
        WEBHOOK,
        KEY,
        own_id=9,
        name="C1_M1_SBIN (1m Candle)",
        armed_from_epoch=armed,
    )


def candle(high, low, start=ARMED + 60):
    return {"timestamp": start, "open": 800.0, "high": high, "low": low, "close": 800.0}


class FakeClient:
    def __init__(self, records=None, fail=False):
        self.records, self.fail = records or [], fail

    def get_history(self, **kwargs):
        if self.fail:
            return {"status": "error", "message": "broker down"}
        return {"status": "success", "data": self.records}


@pytest.fixture(autouse=True)
def fresh_history_cache():
    indicator_service._history_cache.clear()
    yield
    indicator_service._history_cache.clear()


def run(graph, records=None, fail=False, now=ARMED + 180):
    """Execute once; return (webhook bodies, deactivate urls). `now` is late
    enough that every candle in `records` counts as closed."""
    posts = []
    http = mock.Mock()
    http.post.side_effect = lambda url, json=None, content=None, **kw: (
        posts.append((url, json if json is not None else ("RAW", content)))
        or types.SimpleNamespace(status_code=200, is_success=True, json=lambda: {})
    )
    ctx = fes.WorkflowContext()
    executor = fes.NodeExecutor(FakeClient(records, fail), ctx, [])
    edge_map, incoming = {}, {}
    for e in graph["edges"]:
        edge_map.setdefault(e["source"], []).append(e)
        incoming.setdefault(e["target"], []).append(e)
    with (
        mock.patch("utils.httpx_client.get_httpx_client", return_value=http),
        mock.patch.object(
            indicator_service, "datetime", wraps=datetime, now=lambda: datetime.fromtimestamp(now)
        ),
    ):
        fes.execute_node_chain(
            "start", graph["nodes"], edge_map, incoming, executor, ctx, {}, depth=0
        )
    for _url, body in posts:
        assert not isinstance(body, tuple), f"body was posted as raw text: {body}"
    webhooks = [b for u, b in posts if u == WEBHOOK]
    deactivations = [u for u, _ in posts if u.endswith("/api/v1/pricebreach/C1/deactivate")]
    return webhooks, deactivations


# --- the graph ------------------------------------------------------------------


@pytest.mark.parametrize("levels", [BUY, SELL])
def test_the_graph_is_a_valid_scheduled_workflow(levels):
    graph = graph_for(levels)
    assert validate_workflow({"name": "c", **graph}, strict=True) == []
    start = graph["nodes"][0]["data"]
    assert (start["scheduleType"], start["intervalValue"], start["intervalUnit"]) == (
        "interval",
        1,
        "minutes",
    )
    assert start["marketHoursOnly"] is True


def test_an_index_is_gated_by_its_exchanges_calendar():
    start = graph_for(BUY, exchange="NSE_INDEX")["nodes"][0]["data"]
    assert start["marketHoursExchange"] == "NSE"


def test_armed_from_is_the_next_whole_minute():
    assert pbs._armed_from_epoch(ARMED + 0.5) == ARMED + 60
    assert pbs._armed_from_epoch(ARMED + 59.9) == ARMED + 60


# --- what one run does ----------------------------------------------------------


@pytest.mark.parametrize(
    "levels,high,low,breach_type,breach_price",
    [
        (BUY, 805.0, 779.5, "stop_loss", "779.5"),  # low under the buy stop-loss
        (BUY, 840.5, 795.0, "target1", "840.5"),  # high over the buy target
        (SELL, 820.5, 795.0, "stop_loss", "820.5"),  # high over the sell stop-loss
        (SELL, 805.0, 759.0, "target1", "759.0"),  # low under the sell target
    ],
)
def test_a_candle_through_a_level_notifies_once_then_ends_the_call(
    levels, high, low, breach_type, breach_price
):
    webhooks, deactivations = run(graph_for(levels), [candle(high, low)])

    assert len(webhooks) == 1
    body = webhooks[0]
    assert body["breach_type"] == breach_type
    assert body["breach_price"] == breach_price
    assert body["detected_by"] == "candle_1m"
    assert body["alert_type"] == "sl_target_breach"
    assert (body["call_id"], body["mentor_id"]) == ("C1", "M1")
    assert body["candle"]["high"] == str(high) and body["candle"]["low"] == str(low)
    assert len(deactivations) == 1


def test_a_candle_inside_the_channel_sends_nothing():
    assert run(graph_for(BUY), [candle(839.0, 781.0)]) == ([], [])


def test_touching_a_level_exactly_is_not_a_breach():
    # Same strict test as the LTP watch's outside_channel.
    assert run(graph_for(BUY), [candle(840.0, 780.0)]) == ([], [])


def test_a_candle_spanning_both_levels_reports_the_stop_loss():
    webhooks, _ = run(graph_for(BUY), [candle(845.0, 775.0)])
    assert [b["breach_type"] for b in webhooks] == ["stop_loss"]


def test_the_candle_the_call_was_made_in_is_not_checked():
    # Started before ARMED, so part of its range predates the call.
    webhooks, _ = run(graph_for(BUY), [candle(805.0, 700.0, start=ARMED - 60)])
    assert webhooks == []


def test_the_forming_candle_is_not_checked():
    # The last record started 20 s ago: still forming, so the closed one before
    # it (inside the channel) is the one tested.
    records = [candle(810.0, 790.0, start=ARMED + 60), candle(900.0, 700.0, start=ARMED + 120)]
    webhooks, _ = run(graph_for(BUY), records, now=ARMED + 140)
    assert webhooks == []


def test_a_failed_candle_fetch_never_reports_a_breach():
    assert run(graph_for(BUY), fail=True) == ([], [])
    assert run(graph_for(BUY), records=[]) == ([], [])


def test_iso_string_timestamps_work_too():
    iso = datetime.fromtimestamp(ARMED + 60).isoformat()
    webhooks, _ = run(graph_for(BUY), [{**candle(805.0, 770.0), "timestamp": iso}])
    assert [b["breach_type"] for b in webhooks] == ["stop_loss"]


# --- create and deactivate ------------------------------------------------------


@pytest.fixture
def created(monkeypatch):
    """create_and_activate with persistence, the price monitor and the
    scheduler stubbed."""
    ids = iter(range(700, 800))
    recorded, updated, activated = {}, {}, []
    scheduler = mock.Mock()
    scheduler.add_workflow_job.side_effect = lambda workflow_id, **kw: (
        f"flow_workflow_{workflow_id}"
    )

    monkeypatch.setattr(flow_db, "owner_for_api_key", lambda key: ("acct", "user"))
    monkeypatch.setattr(
        pbs, "db_create_workflow", lambda name, **kw: types.SimpleNamespace(id=next(ids), name=name)
    )
    monkeypatch.setattr(pbs, "record_price_breach_call", lambda *a, **k: recorded.update(k))
    monkeypatch.setattr(pbs, "update_workflow", lambda wid, **k: updated.setdefault(wid, k))
    monkeypatch.setattr(pbs, "db_activate_workflow", lambda wid, **k: activated.append(wid))
    monkeypatch.setattr(flow_db, "set_schedule_job_id", lambda *a: None)
    monkeypatch.setattr(
        pbs, "get_flow_price_monitor", lambda: types.SimpleNamespace(add_alert=lambda **kw: None)
    )
    monkeypatch.setattr("services.flow_scheduler_service.get_flow_scheduler", lambda: scheduler)

    def call(**overrides):
        args = dict(
            api_key=KEY,
            call_id="C1",
            mentor_id="M1",
            symbol="SBIN",
            exchange="NSE",
            active_price=800.0,
            entry_price=790.0,
            **BUY,
            webhook_url=WEBHOOK,
        )
        args.update(overrides)
        return pbs.create_and_activate(**args)

    return types.SimpleNamespace(
        call=call, recorded=recorded, updated=updated, activated=activated, scheduler=scheduler
    )


def test_create_arms_a_candle_check_alongside_the_ltp_watches(created):
    before = time.time()
    ok, body, status = created.call()

    assert (ok, status) == (True, 201)
    candle_info = body["workflows"]["candle_1m"]
    candle_id = candle_info["workflow_id"]
    assert candle_info["active"] is True
    assert created.recorded["candle_workflow_id"] == candle_id
    assert candle_id in created.activated
    created.scheduler.add_workflow_job.assert_called_once_with(
        workflow_id=candle_id,
        schedule_type="interval",
        interval_value=1,
        interval_unit="minutes",
        market_hours_only=True,
    )
    # The stored guard starts at the next whole minute after the call.
    fresh = next(n for n in created.updated[candle_id]["nodes"] if n["id"] == "fresh")
    assert int(fresh["data"]["rightValue"]) == pbs._armed_from_epoch(before)


def test_create_still_succeeds_when_the_candle_check_cannot_be_scheduled(created):
    created.scheduler.add_workflow_job.side_effect = RuntimeError("scheduler down")
    ok, body, status = created.call()

    assert (ok, status) == (True, 201)
    candle_info = body["workflows"]["candle_1m"]
    assert candle_info["active"] is False and "error" in candle_info
    assert candle_info["workflow_id"] not in created.activated
    # The LTP watches are still armed.
    assert body["workflows"]["sl_target"]["workflow_id"] in created.activated


def test_a_failed_activation_removes_the_job_it_just_registered(created, monkeypatch):
    def fail_for_the_candle_check(wid, **kw):
        # Only the candle check is activated after its job is registered.
        if created.scheduler.add_workflow_job.called:
            raise RuntimeError("database is locked")
        created.activated.append(wid)

    monkeypatch.setattr(pbs, "db_activate_workflow", fail_for_the_candle_check)
    ok, body, status = created.call()

    assert (ok, status) == (True, 201) and body["workflows"]["candle_1m"]["active"] is False
    candle_id = body["workflows"]["candle_1m"]["workflow_id"]
    created.scheduler.remove_workflow_job.assert_called_once_with(candle_id)


@pytest.mark.parametrize(
    "workflow",
    [None, types.SimpleNamespace(is_active=False, nodes=[])],
    ids=["deleted", "inactive"],
)
def test_a_skipped_scheduled_run_still_releases_its_sessions(monkeypatch, workflow):
    # The skip returns read the workflow row first, so they hold a session too.
    from services import flow_scheduler_service as fss

    released = []
    monkeypatch.setattr(flow_db, "get_workflow", lambda wid: workflow)
    monkeypatch.setattr(
        "utils.db_sessions.remove_all_scoped_sessions", lambda: released.append(True)
    )
    executed = mock.Mock()
    monkeypatch.setattr("services.flow_executor_service.execute_workflow", executed)

    fss.execute_workflow_scheduled(9)

    assert released == [True]
    executed.assert_not_called()


@pytest.fixture
def stored(monkeypatch):
    workflow = types.SimpleNamespace(id=42, schedule_job_id="flow_workflow_42")
    monkeypatch.setattr(pbs, "get_workflow", lambda wid: workflow if wid == 42 else None)
    monkeypatch.setattr(pbs, "get_workflow_api_key", lambda wf: KEY)
    monitor = mock.Mock()
    monkeypatch.setattr(pbs, "get_flow_price_monitor", lambda: monitor)
    scheduler = mock.Mock()
    monkeypatch.setattr("services.flow_scheduler_service.get_flow_scheduler", lambda: scheduler)
    cleared, deactivated = [], []
    monkeypatch.setattr(flow_db, "set_schedule_job_id", lambda wid, job: cleared.append((wid, job)))
    monkeypatch.setattr(pbs, "db_deactivate_workflow", lambda wid: deactivated.append(wid))
    return types.SimpleNamespace(scheduler=scheduler, cleared=cleared, deactivated=deactivated)


def test_deactivate_stops_the_candle_checks_schedule(stored):
    ok, _, status = pbs.deactivate_one(42, KEY)

    assert (ok, status) == (True, 200)
    stored.scheduler.remove_workflow_job.assert_called_once_with(42, strict=True)
    assert stored.cleared == [(42, None)]
    assert stored.deactivated == [42]


def test_a_failed_schedule_removal_leaves_the_workflow_active(stored):
    stored.scheduler.remove_workflow_job.side_effect = RuntimeError("jobstore")
    ok, _, status = pbs.deactivate_one(42, KEY)

    assert (ok, status) == (False, 500)
    assert stored.deactivated == []


def test_a_call_id_resolves_to_its_candle_check_too(monkeypatch):
    rows = [
        types.SimpleNamespace(
            sl_target_workflow_id=1, entry_recross_workflow_id=2, candle_workflow_id=3
        ),
        # A call created before the candle check existed.
        types.SimpleNamespace(
            sl_target_workflow_id=4, entry_recross_workflow_id=None, candle_workflow_id=None
        ),
    ]
    query = mock.Mock()
    query.filter_by.return_value.all.return_value = rows
    monkeypatch.setattr(flow_db.PriceBreachCall, "query", query, raising=False)

    assert flow_db.get_workflow_ids_by_call_id("C1") == [1, 2, 3, 4]
