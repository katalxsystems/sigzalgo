"""The Flow list page searches workflows by call_id and mentor_id. Neither
monitor type stores them in a column, so the list endpoint reads them out of
the graph. These run the extraction against graphs built by the real
builders, so a change to either body format breaks a test here rather than
silently emptying the search."""

from blueprints.flow import _external_ids
from services import price_breach_service as pbs
from services import strategy_monitor_service as sms


def test_price_breach_watch_exposes_its_call_and_mentor():
    graph = pbs._sl_target_graph(
        "call-77",
        "mentor-9",
        "SBIN",
        "NSE",
        entry_price=800.0,
        stop_loss=790.0,
        target1=820.0,
        webhook_url="https://signalz.example/hooks/breach",
        api_key="key-1",
        own_id=11,
        name="call-77_mentor-9_SBIN",
    )
    assert _external_ids(graph["nodes"]) == (["call-77"], ["mentor-9"])


def test_strategy_monitor_exposes_every_leg_call_and_the_mentor():
    legs = [
        {
            "call_id": 1001,
            "symbol": "NIFTY27OCT2622700CE",
            "exchange": "NFO",
            "product": "NRML",
            "quantity": 65.0,
            "entry_price": 50.0,
            "realized_pnl": 0.0,
        },
        {
            "call_id": 1002,
            "symbol": "NIFTY27OCT2622700PE",
            "exchange": "NFO",
            "product": "NRML",
            "quantity": 65.0,
            "entry_price": 40.0,
            "realized_pnl": 0.0,
        },
    ]
    graph = sms.build_graph(
        7,
        "NIFTY_STRADDLE",
        "key-1",
        strategy_id=42,
        mentor_id=7,
        webhook_url="https://signalz.example/hooks/strategy",
        webhook_secret=None,
        target=3000.0,
        stoploss=None,
        underlying=None,
        underlying_exchange="NSE_INDEX",
        upper_spot_stoploss=None,
        lower_spot_stoploss=None,
        interval_seconds=60,
        legs=legs,
    )
    assert _external_ids(graph["nodes"]) == (["1001", "1002"], ["7"])


def test_an_ordinary_workflow_has_no_ids():
    nodes = [
        {"id": "start", "type": "start", "data": {}},
        {"id": "n", "type": "httpRequest", "data": {"body": '{"symbol": "SBIN"}'}},
        {"id": "bad", "type": "variable", "data": None},
    ]
    assert _external_ids(nodes) == ([], [])
    assert _external_ids(None) == ([], [])
