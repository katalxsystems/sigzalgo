"""The 1-minute candle backups for a price-breach call.

/api/v1/pricebreach/create arms two scheduled workflows next to the LTP
watches: one for stop_loss/target1 and one for the entry re-cross. Each run
scans every closed 1-minute candle of the last two hours (after the call was
armed), so a breach a previous run missed is still found.

The graphs are run through the real Flow executor against a fake broker client
and a fake HTTP client, so these check what the workflows actually send.
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
MIN = 60
BUY = {"stop_loss": 780.0, "target1": 840.0}
SELL = {"stop_loss": 820.0, "target1": 760.0}
CANDLE_ID, LTP_ENTRY_ID = 9, 8


def sl_target_graph(levels, armed=ARMED, exchange="NSE"):
    return pbs._candle_sl_target_graph(
        "C1",
        "M1",
        "SBIN",
        exchange,
        800.0,
        levels["stop_loss"],
        levels["target1"],
        WEBHOOK,
        KEY,
        own_id=CANDLE_ID,
        name="C1_M1_SBIN (1m Candle SL/Target)",
        armed_from_epoch=armed,
    )


def entry_graph(condition, entry=790.0, armed=ARMED):
    return pbs._candle_entry_graph(
        "C1",
        "M1",
        "SBIN",
        "NSE",
        entry,
        780.0,
        840.0,
        condition,
        WEBHOOK,
        KEY,
        own_id=CANDLE_ID,
        name="C1_M1_SBIN (1m Candle Entry)",
        armed_from_epoch=armed,
        ltp_entry_id=LTP_ENTRY_ID,
    )


def candle(high, low, start=ARMED + MIN):
    return {"timestamp": start, "open": 800.0, "high": high, "low": low, "close": 800.0}


def quiet(start, end, step=MIN):
    """Candles inside every level used here, one per minute in [start, end)."""
    return [candle(800.5, 799.5, start=t) for t in range(start, end, step)]


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


def run(graph, records=None, fail=False, now=ARMED + 3 * MIN):
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
            graph["nodes"][0]["id"], graph["nodes"], edge_map, incoming, executor, ctx, {}, depth=0
        )
    for _url, body in posts:
        assert not isinstance(body, tuple), f"body was posted as raw text: {body}"
    webhooks = [b for u, b in posts if u == WEBHOOK]
    deactivations = [u.split("/api/v1/pricebreach/")[1] for u, _ in posts if u != WEBHOOK]
    return webhooks, deactivations


# --- the graphs -----------------------------------------------------------------


@pytest.mark.parametrize(
    "graph",
    [
        sl_target_graph(BUY),
        sl_target_graph(SELL),
        entry_graph("crosses_below"),
        entry_graph("crosses_above"),
    ],
    ids=["sl-buy", "sl-sell", "entry-below", "entry-above"],
)
def test_each_graph_is_a_valid_every_minute_workflow_scanning_two_hours(graph):
    assert validate_workflow({"name": "c", **graph}, strict=True) == []
    start = graph["nodes"][0]["data"]
    assert (start["scheduleType"], start["intervalValue"], start["intervalUnit"]) == (
        "interval",
        1,
        "minutes",
    )
    assert start["marketHoursOnly"] is True
    scan = next(n for n in graph["nodes"] if n["type"] == "candleScan")["data"]
    assert (scan["interval"], scan["lookbackMinutes"], scan["sinceEpoch"]) == ("1m", 120, ARMED)


def test_an_index_is_gated_by_its_exchanges_calendar():
    start = sl_target_graph(BUY, exchange="NSE_INDEX")["nodes"][0]["data"]
    assert start["marketHoursExchange"] == "NSE"


def test_armed_from_is_the_next_whole_minute():
    assert pbs._armed_from_epoch(ARMED + 0.5) == ARMED + MIN
    assert pbs._armed_from_epoch(ARMED + 59.9) == ARMED + MIN


# --- SL/Target: what one run does -----------------------------------------------


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
    webhooks, deactivations = run(sl_target_graph(levels), [candle(high, low)])

    assert len(webhooks) == 1
    body = webhooks[0]
    assert body["breach_type"] == breach_type
    assert body["breach_price"] == breach_price
    assert body["detected_by"] == "candle_1m"
    assert body["alert_type"] == "sl_target_breach"
    assert (body["call_id"], body["mentor_id"]) == ("C1", "M1")
    assert body["candle"]["high"] == str(high) and body["candle"]["low"] == str(low)
    assert deactivations == ["C1/deactivate"]


def test_a_candle_inside_the_channel_sends_nothing():
    assert run(sl_target_graph(BUY), [candle(839.0, 781.0)]) == ([], [])


def test_touching_a_level_exactly_is_not_a_breach():
    # Same strict test as the LTP watch's outside_channel.
    assert run(sl_target_graph(BUY), [candle(840.0, 780.0)]) == ([], [])


@pytest.mark.parametrize("levels", [BUY, SELL], ids=["buy", "sell"])
def test_a_candle_spanning_both_levels_reports_the_stop_loss(levels):
    webhooks, _ = run(sl_target_graph(levels), [candle(845.0, 755.0)])
    assert [b["breach_type"] for b in webhooks] == ["stop_loss"]


def test_a_breach_an_earlier_run_missed_is_still_found():
    # The breach was 90 minutes ago; every candle since is quiet. Checking only
    # the last candle would never see it.
    now = ARMED + 100 * MIN
    breach_at = now - 90 * MIN
    records = [
        *quiet(ARMED, breach_at),
        candle(805.0, 778.0, start=breach_at),
        *quiet(breach_at + MIN, now - MIN),
    ]
    webhooks, _ = run(sl_target_graph(BUY), records, now=now)

    assert [b["breach_type"] for b in webhooks] == ["stop_loss"]
    assert webhooks[0]["candle"]["timestamp"] == str(breach_at)


def test_the_earliest_breach_in_the_window_is_the_one_reported():
    # Target first, stop-loss later: the call ended at the target.
    records = [
        candle(841.0, 800.0, start=ARMED + MIN),
        candle(800.0, 770.0, start=ARMED + 2 * MIN),
    ]
    webhooks, _ = run(sl_target_graph(BUY), records, now=ARMED + 4 * MIN)
    assert [b["breach_type"] for b in webhooks] == ["target1"]


def test_a_breach_older_than_two_hours_is_outside_the_window():
    now = ARMED + 200 * MIN
    old = now - 130 * MIN
    records = [candle(805.0, 700.0, start=old), *quiet(old + MIN, now - MIN)]
    assert run(sl_target_graph(BUY), records, now=now) == ([], [])


def test_candles_from_before_the_call_are_not_checked():
    # Inside the two-hour window, but started before the call was armed.
    records = [candle(805.0, 700.0, start=ARMED - MIN), *quiet(ARMED, ARMED + 2 * MIN)]
    assert run(sl_target_graph(BUY), records) == ([], [])


def test_the_forming_candle_is_not_checked():
    # The last record started 20 s ago: still forming.
    records = [candle(810.0, 790.0, start=ARMED + MIN), candle(900.0, 700.0, start=ARMED + 2 * MIN)]
    assert run(sl_target_graph(BUY), records, now=ARMED + 2 * MIN + 20) == ([], [])


def test_a_failed_candle_fetch_never_reports_a_breach():
    assert run(sl_target_graph(BUY), fail=True) == ([], [])
    assert run(sl_target_graph(BUY), records=[]) == ([], [])


def test_iso_string_timestamps_work_too():
    iso = datetime.fromtimestamp(ARMED + MIN).isoformat()
    webhooks, _ = run(sl_target_graph(BUY), [{**candle(805.0, 770.0), "timestamp": iso}])
    assert [b["breach_type"] for b in webhooks] == ["stop_loss"]


# --- Entry: what one run does ---------------------------------------------------


@pytest.mark.parametrize(
    "condition,high,low,breach_price",
    [
        ("crosses_below", 800.0, 789.5, "789.5"),  # came back down through 790
        ("crosses_above", 790.5, 785.0, "790.5"),  # came back up through 790
    ],
)
def test_an_entry_recross_notifies_then_retires_both_entry_watches(
    condition, high, low, breach_price
):
    webhooks, deactivations = run(entry_graph(condition), [candle(high, low)])

    assert len(webhooks) == 1
    body = webhooks[0]
    assert (body["alert_type"], body["breach_type"]) == ("entry_recross", "entry_price")
    assert body["detected_by"] == "candle_1m"
    assert body["breach_price"] == breach_price
    # The LTP entry watch first, then itself -- never the whole call.
    assert deactivations == [
        f"workflow/{LTP_ENTRY_ID}/deactivate",
        f"workflow/{CANDLE_ID}/deactivate",
    ]


@pytest.mark.parametrize(
    "condition,high,low",
    [
        ("crosses_below", 795.0, 790.0),  # touched, not through
        ("crosses_above", 790.0, 785.0),
        ("crosses_below", 850.0, 791.0),  # moved the other way
    ],
)
def test_no_entry_recross_sends_nothing(condition, high, low):
    assert run(entry_graph(condition), [candle(high, low)]) == ([], [])


def test_an_entry_recross_an_earlier_run_missed_is_still_found():
    now = ARMED + 60 * MIN
    breach_at = now - 45 * MIN
    records = [
        *quiet(ARMED, breach_at),
        candle(800.0, 788.0, start=breach_at),
        *quiet(breach_at + MIN, now - MIN),
    ]
    webhooks, _ = run(entry_graph("crosses_below"), records, now=now)
    assert len(webhooks) == 1 and webhooks[0]["candle"]["timestamp"] == str(breach_at)


def test_the_ltp_entry_watch_also_retires_its_candle_sibling():
    graph = pbs._entry_recross_graph(
        "C1",
        "M1",
        "SBIN",
        "NSE",
        790.0,
        780.0,
        840.0,
        "crosses_below",
        WEBHOOK,
        KEY,
        own_id=LTP_ENTRY_ID,
        name="e",
        candle_entry_id=CANDLE_ID,
    )
    assert validate_workflow({"name": "e", **graph}, strict=True) == []
    ctx_webhook = {"trigger_price": 789.0, "triggered_at": "t"}
    ctx = fes.WorkflowContext()
    ctx.set_variable("webhook", ctx_webhook)
    posts = []
    http = mock.Mock()
    http.post.side_effect = lambda url, **kw: (
        posts.append(url) or types.SimpleNamespace(status_code=200, is_success=True, json=dict)
    )
    edge_map, incoming = {}, {}
    for e in graph["edges"]:
        edge_map.setdefault(e["source"], []).append(e)
        incoming.setdefault(e["target"], []).append(e)
    with mock.patch("utils.httpx_client.get_httpx_client", return_value=http):
        fes.execute_node_chain(
            "notify", graph["nodes"], edge_map, incoming, fes.NodeExecutor(None, ctx, []), ctx, {}
        )
    assert [u.split("/api/v1/pricebreach/")[-1] for u in posts if u != WEBHOOK] == [
        f"workflow/{CANDLE_ID}/deactivate",
        f"workflow/{LTP_ENTRY_ID}/deactivate",
    ]


# --- the scan itself ------------------------------------------------------------


def test_scan_reports_the_preferred_side_for_a_candle_breaking_both():
    records = [candle(850.0, 750.0)]
    now = datetime.fromtimestamp(ARMED + 3 * MIN)
    for prefer in ("above", "below"):
        result = indicator_service.first_candle_breach(
            records, above=840.0, below=780.0, tie_prefer=prefer, now=now
        )
        assert result["side"] == prefer and result["breached"] == 1


def test_scan_counts_only_candles_in_its_window():
    now = ARMED + 10 * MIN
    records = quiet(ARMED - 5 * MIN, now - MIN)
    result = indicator_service.first_candle_breach(
        records,
        above=900.0,
        lookback_minutes=120,
        since_epoch=ARMED,
        now=datetime.fromtimestamp(now),
    )
    assert result["breached"] == 0
    assert result["candles_scanned"] == 9  # ARMED .. now-2min; the forming one is dropped
    assert result["window_from_epoch"] == ARMED


# --- create and deactivate ------------------------------------------------------


@pytest.fixture
def created(monkeypatch):
    """create_and_activate with persistence, the price monitor and the
    scheduler stubbed."""
    ids = iter(range(700, 800))
    workflows, recorded, updated, activated, deleted = {}, {}, {}, [], []
    scheduler = mock.Mock()
    scheduler.add_workflow_job.side_effect = lambda workflow_id, **kw: (
        f"flow_workflow_{workflow_id}"
    )

    def create_workflow(name, **kw):
        workflow = types.SimpleNamespace(id=next(ids), name=name)
        workflows[name.split("(")[-1].rstrip(")")] = workflow.id
        return workflow

    monkeypatch.setattr(flow_db, "owner_for_api_key", lambda key: ("acct", "user"))
    monkeypatch.setattr(pbs, "db_create_workflow", create_workflow)
    monkeypatch.setattr(pbs, "db_delete_workflow", deleted.append)
    monkeypatch.setattr(pbs, "record_price_breach_call", lambda *a, **k: recorded.update(k))
    monkeypatch.setattr(pbs, "update_workflow", lambda wid, **k: updated.setdefault(wid, k))
    monkeypatch.setattr(pbs, "db_activate_workflow", lambda wid, **k: activated.append(wid))
    monkeypatch.setattr(flow_db, "set_schedule_job_id", lambda *a: None)
    monkeypatch.setattr(
        pbs, "get_flow_price_monitor", lambda: types.SimpleNamespace(add_alert=lambda **kw: None)
    )
    monkeypatch.setattr(pbs._NOTIFY_POOL, "submit", lambda *a, **k: None)
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
        call=call,
        workflows=workflows,
        recorded=recorded,
        updated=updated,
        activated=activated,
        deleted=deleted,
        scheduler=scheduler,
    )


def _node(graph_update, node_id):
    return next(n for n in graph_update["nodes"] if n["id"] == node_id)


def test_create_arms_both_candle_checks_alongside_the_ltp_watches(created):
    before = time.time()
    ok, body, status = created.call()

    assert (ok, status) == (True, 201)
    sl_info, entry_info = body["workflows"]["candle_sl_target"], body["workflows"]["candle_entry"]
    sl_id, entry_id = sl_info["workflow_id"], entry_info["workflow_id"]
    assert sl_info["active"] is True and entry_info["active"] is True
    assert sl_info["lookback_minutes"] == entry_info["lookback_minutes"] == 120
    assert (created.recorded["candle_workflow_id"], created.recorded["candle_entry_workflow_id"]) == (
        sl_id,
        entry_id,
    )
    assert {sl_id, entry_id} <= set(created.activated)
    scheduled = [c.kwargs["workflow_id"] for c in created.scheduler.add_workflow_job.call_args_list]
    assert scheduled == [sl_id, entry_id]

    # Both scans start at the next whole minute after the call.
    armed = pbs._armed_from_epoch(before)
    for wid in (sl_id, entry_id):
        assert _node(created.updated[wid], "scan")["data"]["sinceEpoch"] == armed

    # The two entry watches retire each other.
    ltp_entry_id = created.workflows["Entry re-cross"]
    ltp_entry = created.updated[ltp_entry_id]
    assert f"/workflow/{entry_id}/deactivate" in _node(ltp_entry, "deactivate_candle_entry")[
        "data"
    ]["url"]
    candle_entry = created.updated[entry_id]
    assert f"/workflow/{ltp_entry_id}/deactivate" in _node(candle_entry, "deactivate_ltp_entry")[
        "data"
    ]["url"]


def test_no_candle_entry_check_when_price_is_already_at_entry(created):
    ok, body, status = created.call(active_price=790.0, entry_price=790.0)

    assert (ok, status) == (True, 201)
    assert body["workflows"]["candle_entry"] is None
    assert created.recorded["candle_entry_workflow_id"] is None
    assert created.scheduler.add_workflow_job.call_count == 1
    ltp_entry = created.updated[created.workflows["Entry re-cross"]]
    assert all(n["id"] != "deactivate_candle_entry" for n in ltp_entry["nodes"])


def test_create_still_succeeds_when_the_candle_checks_cannot_be_scheduled(created):
    created.scheduler.add_workflow_job.side_effect = RuntimeError("scheduler down")
    ok, body, status = created.call()

    assert (ok, status) == (True, 201)
    for key in ("candle_sl_target", "candle_entry"):
        info = body["workflows"][key]
        assert info["active"] is False and "error" in info
        assert info["workflow_id"] not in created.activated
    # The LTP watches are still armed.
    assert body["workflows"]["sl_target"]["workflow_id"] in created.activated


def test_a_failed_activation_removes_the_job_it_just_registered(created, monkeypatch):
    def fail_after_scheduling(wid, **kw):
        # Only the candle checks are activated after their job is registered.
        if created.scheduler.add_workflow_job.called:
            raise RuntimeError("database is locked")
        created.activated.append(wid)

    monkeypatch.setattr(pbs, "db_activate_workflow", fail_after_scheduling)
    ok, body, status = created.call()

    assert (ok, status) == (True, 201)
    ids = [body["workflows"][k]["workflow_id"] for k in ("candle_sl_target", "candle_entry")]
    removed = [c.args[0] for c in created.scheduler.remove_workflow_job.call_args_list]
    assert removed == ids


def test_a_failed_create_rolls_back_every_workflow_made_so_far(created, monkeypatch):
    real = pbs.db_create_workflow

    def fail_on_candle_entry(name, **kw):
        return None if "1m Candle Entry" in name else real(name, **kw)

    monkeypatch.setattr(pbs, "db_create_workflow", fail_on_candle_entry)
    ok, _, status = created.call()

    assert (ok, status) == (False, 500)
    assert sorted(created.deleted) == sorted(created.workflows.values())
    assert created.recorded == {}


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
    workflow = types.SimpleNamespace(id=42, schedule_job_id="flow_workflow_42", is_active=True)
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


def test_deactivate_stops_a_candle_checks_schedule(stored):
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


def test_a_call_id_resolves_to_both_candle_checks_too(monkeypatch):
    rows = [
        types.SimpleNamespace(
            sl_target_workflow_id=1,
            entry_recross_workflow_id=2,
            candle_workflow_id=3,
            candle_entry_workflow_id=4,
        ),
        # A call created before the candle checks existed.
        types.SimpleNamespace(
            sl_target_workflow_id=5,
            entry_recross_workflow_id=None,
            candle_workflow_id=None,
            candle_entry_workflow_id=None,
        ),
    ]
    query = mock.Mock()
    query.filter_by.return_value.all.return_value = rows
    monkeypatch.setattr(flow_db.PriceBreachCall, "query", query, raising=False)

    assert flow_db.get_workflow_ids_by_call_id("C1") == [1, 2, 3, 4, 5]
