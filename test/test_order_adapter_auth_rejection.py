"""Order-update adapters must not spew errors when the broker token is dead.

The reconnect loop treated a 401/403 handshake like a network blip and retried
it forever, one WARNING plus one library-level ERROR (with the broker's whole
HTML error page) per attempt. An expired Angel session on staging logged that
pair every minute for over 300 attempts.

Upstream also skips stale sessions in the boot scan by app-login time. Auth
rows here are per broker account and carry no token timestamp, so that guard
does not apply; a stale token at boot instead costs one refused handshake,
after which the adapter stands down like any other rejection.
"""

import os

import pytest

os.environ.setdefault("HOST_SERVER", "https://legit.example.com")

from websocket_proxy.order_adapter import (  # noqa: E402
    BaseOrderUpdateAdapter,
    _is_auth_rejection,
)


class _FakeBadStatus(Exception):
    """Stands in for websocket.WebSocketBadStatusException, which carries the
    rejected handshake's status on ``status_code``."""

    def __init__(self, status_code):
        super().__init__(f"Handshake status {status_code}")
        self.status_code = status_code


class TestIsAuthRejection:
    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_statuses_are_rejections(self, status):
        assert _is_auth_rejection(_FakeBadStatus(status))

    @pytest.mark.parametrize("status", [500, 502, 503, 429, 400, 404])
    def test_other_statuses_are_not(self, status):
        """Transient/server-side failures must keep retrying."""
        assert not _is_auth_rejection(_FakeBadStatus(status))

    def test_plain_exception_is_not_a_rejection(self):
        """A socket error has no status_code and must not stop the loop."""
        assert not _is_auth_rejection(ConnectionResetError("connection reset"))

    def test_none_is_not_a_rejection(self):
        assert not _is_auth_rejection(None)


class _ScriptedAdapter(BaseOrderUpdateAdapter):
    """Each connect attempt raises the next scripted error; None means the
    socket closed cleanly. Runs the real reconnect loop without a network."""

    def __init__(self, errors):
        super().__init__(broker_name="angel", user_id="acct_angel_1")
        self.errors = list(errors)
        self.attempts = 0

    def get_ws_url(self):
        return "wss://example.invalid"

    def get_headers(self):
        return None

    def normalize(self, raw_message):
        return None

    def _connect_once(self):
        self.attempts += 1
        error = self.errors.pop(0) if self.errors else None
        if not self.errors:
            self._shutting_down = True  # end the loop after the script runs out
        if error is not None:
            raise error


class TestRetryLoopBehaviour:
    @pytest.mark.parametrize("status", [401, 403])
    def test_a_rejected_session_is_tried_once(self, status):
        adapter = _ScriptedAdapter([_FakeBadStatus(status), None, None])
        adapter._running = True

        adapter._run_forever()

        assert adapter.attempts == 1
        assert adapter._running is False

    def test_a_network_failure_keeps_retrying(self, monkeypatch):
        monkeypatch.setattr("websocket_proxy.order_adapter.time.sleep", lambda s: None)
        adapter = _ScriptedAdapter([ConnectionResetError("reset"), _FakeBadStatus(502), None])
        adapter._running = True

        adapter._run_forever()

        assert adapter.attempts == 3


class TestRetryLoopStandsDown:
    def test_run_forever_breaks_on_auth_rejection(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(repo_root, "websocket_proxy", "order_adapter.py"),
            encoding="utf-8",
        ) as f:
            source = f.read()
        assert "_auth_rejected" in source
        # The flag must be cleared on connect(), or a restarted adapter would
        # refuse to run.
        assert source.count("self._auth_rejected = False") >= 2, (
            "_auth_rejected must be initialised and reset in connect()"
        )
