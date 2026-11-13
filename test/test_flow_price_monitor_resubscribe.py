"""A live-tick subscribe the feed refuses is retried, not forgotten.

The Flow price monitor recorded a symbol as subscribed whatever the proxy
answered. After a re-login tore the broker feed down, every watch armed in the
meantime was refused ("Broker adapter not found"), recorded as subscribed
anyway, and never asked for again: those calls ran on the 30-second quote poll
for the rest of the day even once the feed was back.
"""

import types

import pytest

import services.flow_price_monitor_service as fpm

KEY = "k"
SYMBOL, EXCHANGE = "SUNPHARMA23NOV261860PE", "NFO"
SYMKEY = f"{EXCHANGE}:{SYMBOL}"
REFUSED = {"status": "error", "message": "Broker adapter not found"}
ACCEPTED = {"status": "success", "subscriptions": [{"symbol": SYMBOL, "status": "success"}]}


class FakeClient:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def subscribe(self, symbols, mode):
        self.calls += 1
        answer = self.answers.pop(0) if self.answers else ACCEPTED
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture
def monitor(monkeypatch):
    m = fpm.get_flow_price_monitor()
    m._alerts.clear()
    with m._ws_lock:
        m._ws_subscribed.clear()
        m._ws_refused.clear()
    client = types.SimpleNamespace(value=None)
    monkeypatch.setattr(m, "_ensure_ws", lambda api_key: client.value)
    monkeypatch.setattr(m, "_start_monitoring", lambda: None)
    monkeypatch.setattr(m, "_stop_monitoring", lambda: None)
    monkeypatch.setattr(m, "_check_alert", lambda alert: None)

    def arm(answers, workflow_id=950):
        client.value = FakeClient(*answers)
        m.add_alert(
            workflow_id=workflow_id,
            symbol=SYMBOL,
            exchange=EXCHANGE,
            condition="outside_channel",
            target_price=29.0,
            price_lower=11.5,
            price_upper=29.0,
            api_key=KEY,
        )
        return client.value

    yield types.SimpleNamespace(m=m, arm=arm, client=client)
    m._alerts.clear()
    with m._ws_lock:
        m._ws_subscribed.clear()
        m._ws_refused.clear()


def _subscribed(m):
    return SYMKEY in m._ws_subscribed.get(KEY, set())


def test_a_refused_subscribe_is_not_recorded_as_subscribed(monitor):
    monitor.arm([REFUSED])
    assert not _subscribed(monitor.m)


def test_the_poll_retries_it_until_the_feed_accepts(monitor):
    client = monitor.arm([REFUSED, REFUSED, ACCEPTED])
    monitor.m._check_all_alerts()
    assert not _subscribed(monitor.m)
    monitor.m._check_all_alerts()
    assert _subscribed(monitor.m)
    assert client.calls == 3

    # Accepted once: no more subscribe traffic on later polls.
    monitor.m._check_all_alerts()
    assert client.calls == 3


def test_a_subscribe_that_raises_is_retried_too(monitor):
    monitor.arm([TimeoutError("ack timed out")])
    assert not _subscribed(monitor.m)
    monitor.m._check_all_alerts()
    assert _subscribed(monitor.m)


def test_a_watch_armed_before_the_feed_was_up_is_subscribed_once_it_is(monitor):
    monitor.client.value = None
    monitor.m.add_alert(
        workflow_id=951,
        symbol=SYMBOL,
        exchange=EXCHANGE,
        condition="above",
        target_price=20.0,
        api_key=KEY,
    )
    assert not _subscribed(monitor.m)

    monitor.client.value = FakeClient(ACCEPTED)
    monitor.m._check_all_alerts()
    assert _subscribed(monitor.m)


def test_the_refusal_is_warned_once_and_the_recovery_logged(monitor, caplog):
    caplog.set_level("DEBUG", logger=fpm.logger.name)
    monitor.arm([REFUSED, REFUSED, ACCEPTED])
    monitor.m._check_all_alerts()
    monitor.m._check_all_alerts()

    warnings = [r for r in caplog.records if r.levelname == "WARNING" and SYMKEY in r.message]
    restored = [r for r in caplog.records if "restored" in r.message]
    assert len(warnings) == 1
    assert len(restored) == 1
    assert SYMKEY not in monitor.m._ws_refused


def test_a_removed_watch_is_not_retried_and_leaves_nothing_behind(monitor):
    client = monitor.arm([REFUSED])
    monitor.m.remove_alert(950)
    monitor.m._check_all_alerts()
    assert client.calls == 1
    assert SYMKEY not in monitor.m._ws_refused
