"""Tests for the apikey-managed strategy monitor (/api/v1/strategymonitor).

The graph is run through the real Flow executor against a fake broker client
and a fake HTTP client, so these check what the monitor actually does on each
path: which reason is sent, that nothing is sent on missing data, and that a
delivered notification - and only a delivered one - deactivates the monitor.
"""

import atexit
import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = Path(__file__).resolve().parents[1] / "tmp" / "test_strategy_monitor.db"
TEST_DB.parent.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{TEST_DB.as_posix()}")
os.environ.setdefault("API_KEY_PEPPER", "a" * 64)
atexit.register(lambda: TEST_DB.unlink(missing_ok=True))

from marshmallow import ValidationError  # noqa: E402

import database.flow_db as flow_db  # noqa: E402
import services.flow_executor_service as fes  # noqa: E402
import services.strategy_monitor_service as sms  # noqa: E402
from restx_api.data_schemas import StrategyMonitorUpsertSchema  # noqa: E402
from services.flow_workflow_validator import validate_workflow  # noqa: E402

TAG = "NIFTY_STRADDLE_27OCT"
KEY = "key-1"
WEBHOOK = "https://signalz.example/hooks/breach"

ALL_LEVELS = {
    "webhook_url": WEBHOOK,
    "webhook_secret": "s3cret",
    "target": 3000.0,
    "stoploss": 2000.0,
    "underlying": "NIFTY",
    "underlying_exchange": "NSE_INDEX",
    "upper_spot_stoploss": 23000.0,
    "lower_spot_stoploss": 22400.0,
    "interval_seconds": 60,
}


def _leg(symbol, quantity, average_price):
    return {
        "strategy": TAG,
        "symbol": symbol,
        "exchange": "NFO",
        "product": "NRML",
        "quantity": quantity,
        "average_price": average_price,
        "realized_pnl": 0.0,
        "today_realized_pnl": 0.0,
    }


# Long straddle, 90 net debit (50 + 40), 65 qty per leg:
# P&L = 65 * ((ce - 50) + (pe - 40)).
LEGS = [_leg("NIFTY27OCT2622700CE", 65.0, 50.0), _leg("NIFTY27OCT2622700PE", 65.0, 40.0)]


class FakeClient:
    def __init__(self, ce, pe, spot):
        self.ce, self.pe, self.spot = ce, pe, spot
        self.quotes = 0

    def positionbook(self):
        prices = (self.ce, self.pe)
        return {
            "status": "success",
            "data": [
                {"symbol": leg["symbol"], "exchange": "NFO", "product": "NRML", "ltp": p}
                for leg, p in zip(LEGS, prices, strict=True)
                if p is not None
            ],
        }

    def get_quotes(self, symbol, exchange):
        self.quotes += 1
        if self.spot is None:
            return {"status": "error", "message": "quote failed"}
        return {"status": "success", "data": {"ltp": self.spot}}


class FakeResponse:
    def __init__(self, status):
        self.status_code = status
        self.is_success = 200 <= status < 300

    def json(self):
        return {}


def run(graph, ce, pe, spot, legs=LEGS, webhook_status=200):
    """Execute the graph once. Returns (webhook bodies, deactivate bodies, client)."""
    posts = []
    http = mock.Mock()
    http.post.side_effect = lambda url, json=None, content=None, **kw: (
        posts.append((url, json if json is not None else ("RAW", content)))
        or FakeResponse(200 if "/strategymonitor/deactivate" in url else webhook_status)
    )
    client = FakeClient(ce, pe, spot)
    with (
        mock.patch("database.strategy_book_db.get_strategy_legs", return_value=legs),
        mock.patch("utils.httpx_client.get_httpx_client", return_value=http),
    ):
        ctx = fes.WorkflowContext()
        executor = fes.NodeExecutor(client, ctx, [])
        edge_map, incoming = {}, {}
        for e in graph["edges"]:
            edge_map.setdefault(e["source"], []).append(e)
            incoming.setdefault(e["target"], []).append(e)
        fes.execute_node_chain(
            "start", graph["nodes"], edge_map, incoming, executor, ctx, {}, depth=0
        )
    for _url, body in posts:
        assert not isinstance(body, tuple), f"body was posted as raw text: {body}"
    webhooks = [b for u, b in posts if u == WEBHOOK]
    deactivations = [b for u, b in posts if u.endswith("/api/v1/strategymonitor/deactivate")]
    return webhooks, deactivations, client


def graph_for(**overrides):
    settings = {**ALL_LEVELS, **overrides}
    return sms.build_graph(7, TAG, KEY, **settings)


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"underlying": None, "upper_spot_stoploss": None, "lower_spot_stoploss": None},
        {"target": None, "stoploss": None},
        {"target": None, "lower_spot_stoploss": None},
        {"interval_seconds": 30},
    ],
)
def test_every_level_combination_is_a_valid_workflow(overrides):
    graph = graph_for(**overrides)
    assert validate_workflow({"name": "m", **graph}, strict=True) == []


@pytest.mark.parametrize(
    "ce,pe,spot,reason,spot_ltp",
    [
        (100, 40, 22700, "TARGET", None),  # P&L +3250
        (20, 30, 22700, "STOPLOSS", None),  # P&L -2600
        (55, 40, 23010, "UPPER_SPOT_SL", 23010),
        (55, 40, 22390, "LOWER_SPOT_SL", 22390),
    ],
)
def test_each_breach_notifies_once_with_its_reason_then_deactivates(ce, pe, spot, reason, spot_ltp):
    webhooks, deactivations, _ = run(graph_for(), ce, pe, spot)

    assert len(webhooks) == 1
    body = webhooks[0]
    assert body["reason"] == reason
    assert body["strategy"] == TAG
    assert body["spot"]["ltp"] == spot_ltp
    assert body["levels"]["upper_spot_stoploss"] == 23000.0
    assert [leg["symbol"] for leg in body["legs"]] == [leg["symbol"] for leg in LEGS]
    assert deactivations == [{"apikey": KEY, "strategy": TAG}]


def test_inside_every_level_sends_nothing():
    webhooks, deactivations, _ = run(graph_for(), 55, 40, 22700)
    assert webhooks == [] and deactivations == []


def test_a_failed_spot_quote_never_reports_a_breach():
    # A quote read as 0 would trip the lower spot stoploss.
    webhooks, _, _ = run(graph_for(), 55, 40, None)
    assert webhooks == []


def test_an_unpriced_leg_skips_pnl_levels_but_spot_still_applies():
    webhooks, _, _ = run(graph_for(), None, 40, 23050)
    assert [b["reason"] for b in webhooks] == ["UPPER_SPOT_SL"]


def test_a_flat_strategy_is_not_checked():
    flat = [dict(leg, quantity=0.0) for leg in LEGS]
    webhooks, _, client = run(graph_for(), 100, 40, 23050, legs=flat)
    assert webhooks == [] and client.quotes == 0


def test_a_failed_delivery_stays_armed():
    webhooks, deactivations, _ = run(graph_for(), 100, 40, 22700, webhook_status=503)
    assert len(webhooks) == 1 and deactivations == []


def test_the_secret_header_is_sent_only_when_given():
    with_secret = next(n for n in graph_for()["nodes"] if n["id"] == "notify")
    without = next(n for n in graph_for(webhook_secret=None)["nodes"] if n["id"] == "notify")
    assert with_secret["data"]["headers"]["X-Webhook-Secret"] == "s3cret"
    assert "X-Webhook-Secret" not in without["data"]["headers"]


# --- upsert / deactivate against a real (temporary) flow database ---


@pytest.fixture()
def flow(monkeypatch):
    flow_db.init_db()
    flow_db.FlowWorkflow.query.delete()
    flow_db.db_session.commit()
    owners = {"key-1": ("acct_1", "alice"), "key-2": ("acct_2", "bob")}
    monkeypatch.setattr(sms, "owner_for_api_key", lambda key: owners.get(key, (None, None)))
    scheduler = mock.Mock()
    scheduler.add_workflow_job.side_effect = lambda workflow_id, **kw: (
        f"flow_workflow_{workflow_id}"
    )
    monkeypatch.setattr("services.flow_scheduler_service.get_flow_scheduler", lambda: scheduler)
    monkeypatch.setattr(fes, "release_workflow_subscriptions", lambda workflow_id: 0)
    yield scheduler
    flow_db.FlowWorkflow.query.delete()
    flow_db.db_session.commit()


def _levels(workflow):
    notify = next(n for n in workflow.nodes if n["id"] == "notify")
    return json.loads(notify["data"]["body"].split('"levels": ')[1].split(', "pnl"')[0])


def test_upsert_creates_then_updates_the_same_monitor(flow):
    ok, created, status = sms.upsert(KEY, TAG, **ALL_LEVELS)
    assert ok and status == 201 and created["created"] is True

    ok, updated, status = sms.upsert(KEY, TAG, **{**ALL_LEVELS, "target": 4500.0})
    assert ok and status == 200 and updated["created"] is False
    assert updated["workflow_id"] == created["workflow_id"]

    workflow = flow_db.get_workflow(created["workflow_id"])
    assert workflow.is_active
    assert workflow.account_id == "acct_1"
    assert _levels(workflow)["target"] == 4500.0
    assert flow.add_workflow_job.call_count == 2


def test_the_same_tag_on_another_account_is_a_separate_monitor(flow):
    _, first, _ = sms.upsert(KEY, TAG, **ALL_LEVELS)
    _, second, status = sms.upsert("key-2", TAG, **ALL_LEVELS)
    assert status == 201 and second["workflow_id"] != first["workflow_id"]


def test_deactivate_disarms_and_is_idempotent(flow):
    _, created, _ = sms.upsert(KEY, TAG, **ALL_LEVELS)

    ok, _, status = sms.deactivate(KEY, TAG)
    assert ok and status == 200
    assert not flow_db.get_workflow(created["workflow_id"]).is_active
    flow.remove_workflow_job.assert_called_once_with(created["workflow_id"], strict=True)

    ok, again, status = sms.deactivate(KEY, TAG)
    assert ok and status == 200 and again["message"] == "Monitor already inactive"


def test_another_accounts_key_cannot_reach_the_monitor(flow):
    sms.upsert(KEY, TAG, **ALL_LEVELS)
    ok, _, status = sms.deactivate("key-2", TAG)
    assert not ok and status == 404


def test_an_invalid_key_is_refused(flow):
    assert sms.upsert("nope", TAG, **ALL_LEVELS)[2] == 403
    assert sms.deactivate("nope", TAG)[2] == 403


# --- request validation ---


@pytest.mark.parametrize(
    "body,field",
    [
        ({}, "_schema"),
        ({"upper_spot_stoploss": 23000}, "underlying"),
        (
            {"underlying": "NIFTY", "upper_spot_stoploss": 22000, "lower_spot_stoploss": 22500},
            "lower_spot_stoploss",
        ),
        ({"target": -5}, "target"),
        ({"target": 100, "strategy": "bad tag!"}, "strategy"),
    ],
)
def test_schema_rejects_bad_requests(body, field):
    payload = {"apikey": KEY, "strategy": TAG, "webhook_url": WEBHOOK, **body}
    with pytest.raises(ValidationError) as err:
        StrategyMonitorUpsertSchema().load(payload)
    assert field in err.value.messages
