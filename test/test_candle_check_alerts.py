"""Alerting when a candle check goes blind.

An expired broker session made every price-breach candle check get no data
while each run still reported success. A candleScan node with alertOnNoData
now logs an error and sends one Telegram alert after a few consecutive runs
without candle data, and one more when data returns.
"""

import os
import sys
from datetime import datetime
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services.candle_check_health as health  # noqa: E402
import services.flow_executor_service as fes  # noqa: E402
import services.indicator_service as indicator_service  # noqa: E402
import services.price_breach_service as pbs  # noqa: E402

ARMED = 1_794_000_000
WID = 501


class FakeClient:
    """History as configured; live quotes that work or fail; Telegram recorded."""

    def __init__(self):
        self.records, self.history_error, self.quote_ok = [], False, False
        self.messages = []

    def get_history(self, **kwargs):
        if self.history_error:
            return {"status": "error", "message": "broker down"}
        return {"status": "success", "data": self.records}

    def get_quotes(self, symbol, exchange):
        if self.quote_ok:
            return {"status": "success", "data": {"ltp": 100.0}}
        return {"status": "error", "message": "Failed to fetch quotes"}

    def telegram(self, message):
        self.messages.append(message)
        return {"status": "success"}


@pytest.fixture(autouse=True)
def clean_state():
    indicator_service._history_cache.clear()
    health._state.clear()
    yield
    indicator_service._history_cache.clear()
    health._state.clear()


def scan_node(alert=True):
    data = pbs._candle_scan_node("GOLD27NOV26150000CE", "MCX", ARMED, above=200.0, below=100.0)[
        "data"
    ]
    return {**data, "alertOnNoData": alert}


def run_once(client, node=None, workflow_id=WID):
    executor = fes.NodeExecutor(client, fes.WorkflowContext(workflow_id=workflow_id), [])
    with (
        mock.patch.object(
            indicator_service,
            "datetime",
            wraps=datetime,
            now=lambda: datetime.fromtimestamp(ARMED + 600),
        ),
        mock.patch("database.flow_db.get_workflow", return_value=None),
    ):
        return executor.execute_candle_scan(node or scan_node())


def good_candle():
    return {"timestamp": ARMED + 60, "open": 150, "high": 150, "low": 150, "close": 150}


# --- the tracker ----------------------------------------------------------------


def test_one_alert_per_outage_then_one_recovery():
    events = [health.record_run(WID, has_data=False) for _ in range(6)]
    assert events == [None, None, health.OUTAGE, None, None, None]
    assert health.record_run(WID, has_data=True) == health.RECOVERED
    assert health.record_run(WID, has_data=True) is None
    # A new outage is reported again.
    assert [health.record_run(WID, has_data=False) for _ in range(3)][-1] == health.OUTAGE


def test_a_short_blip_is_not_reported():
    health.record_run(WID, has_data=False)
    health.record_run(WID, has_data=False)
    assert health.record_run(WID, has_data=True) is None
    assert health.failure_count(WID) == 0


def test_a_run_without_a_workflow_is_not_tracked():
    assert [health.record_run(None, has_data=False) for _ in range(5)] == [None] * 5


# --- the node -------------------------------------------------------------------


def test_an_empty_history_alerts_once_and_names_a_dead_session():
    # An expired 5paisa session: history comes back empty and successful, and
    # the live quote fails too.
    client = FakeClient()
    with mock.patch.object(fes.logger, "error") as error_log:
        results = [run_once(client) for _ in range(5)]

    assert all(r["status"] == "error" for r in results)  # never "no breach"
    assert len(client.messages) == 1
    alert = client.messages[0]
    assert "Candle check NOT WORKING" in alert and "GOLD27NOV26150000CE on MCX" in alert
    assert "3 consecutive runs" in alert
    assert "broker session is likely down" in alert
    assert any("Candle check NOT WORKING" in c.args[0] for c in error_log.call_args_list)


def test_a_failing_history_with_a_live_quote_says_the_session_is_up():
    client = FakeClient()
    client.history_error, client.quote_ok = True, True
    for _ in range(3):
        run_once(client)
    assert len(client.messages) == 1 and "broker session is up" in client.messages[0]


def test_recovery_is_announced_once():
    client = FakeClient()
    for _ in range(3):
        run_once(client)
    client.records = [good_candle()]
    run_once(client)
    run_once(client)

    assert len(client.messages) == 2
    assert client.messages[1].startswith("Candle check recovered")


def test_without_the_option_nothing_is_tracked_or_sent():
    client = FakeClient()
    for _ in range(5):
        run_once(client, node=scan_node(alert=False))
    assert client.messages == [] and health.failure_count(WID) == 0


def test_a_telegram_failure_does_not_break_the_run():
    client = FakeClient()
    client.telegram = mock.Mock(side_effect=RuntimeError("bot down"))
    for _ in range(3):
        result = run_once(client)
    assert result["status"] == "error"  # the run's own outcome is unchanged


def test_price_breach_candle_checks_have_alerting_on():
    for graph in (
        pbs._candle_sl_target_graph(
            "C1",
            "M1",
            "SBIN",
            "NSE",
            800.0,
            780.0,
            840.0,
            "https://x/h",
            "K",
            own_id=1,
            name="n",
            armed_from_epoch=ARMED,
        ),
        pbs._candle_entry_graph(
            "C1",
            "M1",
            "SBIN",
            "NSE",
            790.0,
            780.0,
            840.0,
            "crosses_below",
            "https://x/h",
            "K",
            own_id=2,
            name="n",
            armed_from_epoch=ARMED,
            ltp_entry_id=3,
        ),
    ):
        scan = next(n for n in graph["nodes"] if n["type"] == "candleScan")
        assert scan["data"]["alertOnNoData"] is True


def test_deactivating_a_candle_check_forgets_its_streak(monkeypatch):
    import types

    import database.flow_db as flow_db

    for _ in range(2):
        health.record_run(42, has_data=False)
    monkeypatch.setattr(
        pbs,
        "get_workflow",
        lambda wid: types.SimpleNamespace(id=42, schedule_job_id=None, is_active=True),
    )
    monkeypatch.setattr(pbs, "get_workflow_api_key", lambda wf: "K")
    monkeypatch.setattr(pbs, "get_flow_price_monitor", lambda: mock.Mock())
    monkeypatch.setattr("services.flow_scheduler_service.get_flow_scheduler", lambda: mock.Mock())
    monkeypatch.setattr(flow_db, "set_schedule_job_id", lambda *a: None)
    monkeypatch.setattr(pbs, "db_deactivate_workflow", lambda wid: True)

    assert pbs.deactivate_one(42, "K")[0] is True
    assert health.failure_count(42) == 0
