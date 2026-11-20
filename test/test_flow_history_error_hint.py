"""A history failure names its real cause.

An unknown symbol used to come back with "if your broker does not support the
'1m' interval ...", sending the trader to change an interval that was fine.
"""

from services.flow_executor_service import NodeExecutor

NOT_FOUND = {
    "status": "error",
    "message": "Symbol 'SENSEX26N1983000CE' not found for exchange 'BFO'. Please verify the "
    "symbol name and ensure master contracts are downloaded.",
}


def test_an_unknown_symbol_gets_no_interval_hint():
    msg = NodeExecutor._history_error(NOT_FOUND, "SENSEX26N1983000CE", "1m", "api")
    assert "not found" in msg
    assert "Intervals node" not in msg


def test_a_broker_refusal_still_gets_the_interval_hint():
    refused = {"status": "error", "message": "Interval 1m is not supported"}
    msg = NodeExecutor._history_error(refused, "SBIN", "1m", "api")
    assert "Intervals node" in msg
