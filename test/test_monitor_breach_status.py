"""Breached / monitoring / deactivated status for monitor workflows.

A watch's own deactivate node, which only runs after it notified a breach,
sends `"breach": true`; the deactivate service then stamps `breached_at` on
the workflows it switches off (red in the Flow list). A manual deactivation
sends no flag and leaves them unstamped (amber).
"""

import json
import os
import sys
import types
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database.flow_db as flow_db  # noqa: E402
import services.price_breach_service as pbs  # noqa: E402
from restx_api.data_schemas import (  # noqa: E402
    PriceBreachDeactivateSchema,
    StrategyMonitorDeactivateSchema,
)

KEY = "KEY"
ARMED = 1_794_000_000


@pytest.fixture
def stored(monkeypatch):
    """deactivate_one against fake workflows; records who got marked."""
    workflows = {
        1: types.SimpleNamespace(id=1, schedule_job_id=None, is_active=True),
        2: types.SimpleNamespace(id=2, schedule_job_id=None, is_active=False),
    }
    marked, deactivated = [], []
    monkeypatch.setattr(pbs, "get_workflow", workflows.get)
    monkeypatch.setattr(pbs, "get_workflow_api_key", lambda wf: KEY)
    monkeypatch.setattr(pbs, "get_flow_price_monitor", lambda: mock.Mock())
    monkeypatch.setattr("services.flow_scheduler_service.get_flow_scheduler", lambda: mock.Mock())
    monkeypatch.setattr(pbs, "db_deactivate_workflow", deactivated.append)
    monkeypatch.setattr(flow_db, "mark_breached", marked.append)
    monkeypatch.setattr(pbs, "get_workflow_ids_by_call_id", lambda call_id: [1, 2])
    return types.SimpleNamespace(marked=marked, deactivated=deactivated)


def test_a_breach_deactivation_marks_the_watch_breached(stored):
    assert pbs.deactivate_one(1, KEY, breach=True)[0]
    assert stored.marked == [1]


def test_a_manual_deactivation_does_not(stored):
    assert pbs.deactivate_one(1, KEY)[0]
    assert stored.marked == [] and stored.deactivated == [1]


def test_a_watch_already_switched_off_stays_deactivated_when_its_call_breaches(stored):
    ok, body, _ = pbs.deactivate_by_call_id("C1", KEY, breach=True)
    assert ok and body["workflow_ids"] == [1, 2]
    assert stored.marked == [1]  # 2 was inactive already: amber, not red


@pytest.mark.parametrize(
    "graph,node_id",
    [
        (
            pbs._sl_target_graph(
                "C1",
                "M1",
                "SBIN",
                "NSE",
                800.0,
                780.0,
                840.0,
                "https://x/h",
                KEY,
                own_id=1,
                name="n",
            ),
            "deactivate_call",
        ),
        (
            pbs._entry_recross_graph(
                "C1",
                "M1",
                "SBIN",
                "NSE",
                790.0,
                780.0,
                840.0,
                "crosses_below",
                "https://x/h",
                KEY,
                own_id=2,
                name="n",
                candle_entry_id=4,
            ),
            "deactivate_self",
        ),
        (
            pbs._candle_sl_target_graph(
                "C1",
                "M1",
                "SBIN",
                "NSE",
                800.0,
                780.0,
                840.0,
                "https://x/h",
                KEY,
                own_id=3,
                name="n",
                armed_from_epoch=ARMED,
            ),
            "deactivate_call",
        ),
        (
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
                KEY,
                own_id=4,
                name="n",
                armed_from_epoch=ARMED,
                ltp_entry_id=2,
            ),
            "deactivate_ltp_entry",
        ),
    ],
    ids=["sl_target", "entry_recross", "candle_sl_target", "candle_entry"],
)
def test_every_breach_deactivate_node_sends_the_flag(graph, node_id):
    node = next(n for n in graph["nodes"] if n["id"] == node_id)
    assert json.loads(node["data"]["body"]) == {"apikey": KEY, "breach": True}


@pytest.mark.parametrize(
    "schema,payload",
    [
        (PriceBreachDeactivateSchema, {"apikey": KEY}),
        (StrategyMonitorDeactivateSchema, {"apikey": KEY, "strategy": "S1"}),
    ],
)
def test_a_deactivate_request_without_the_flag_is_a_manual_one(schema, payload):
    assert schema().load(payload)["breach"] is False
    assert schema().load({**payload, "breach": True})["breach"] is True
