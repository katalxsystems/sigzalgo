"""Outage tracking for scheduled candle checks (the candleScan Flow node).

A candle check that gets no usable data looks exactly like one that sees no
breach: the run completes and reports success. That is what happened when a
5paisa session expired - every price-breach candle check went blind for the
rest of the session with nothing louder than one WARNING.

This module tracks, per workflow, how many consecutive runs had no usable
candle data, and says when to raise one alert (after FAILURE_RUNS_BEFORE_ALERT
runs) and when to announce recovery. One alert per outage, not one per minute.

State is in memory, keyed by workflow id, in a TTLCache: bounded in size, and
an entry not updated for STATE_TTL_SECONDS (overnight, or a deactivated
workflow) is forgotten, so a check that is still failing the next session is
reported again.
"""

import os
import threading

from cachetools import TTLCache

from utils.logging import get_logger

logger = get_logger(__name__)

# Consecutive no-data runs before alerting. Runs are a minute apart, so a
# single slow or failed fetch does not page anyone.
FAILURE_RUNS_BEFORE_ALERT = max(int(os.getenv("CANDLE_CHECK_ALERT_AFTER_RUNS", "3")), 1)
STATE_TTL_SECONDS = 6 * 3600

_state: TTLCache = TTLCache(maxsize=4096, ttl=STATE_TTL_SECONDS)
_lock = threading.Lock()

OUTAGE = "outage"
RECOVERED = "recovered"


def record_run(workflow_id: int | None, has_data: bool) -> str | None:
    """Record one run's outcome.

    Returns OUTAGE exactly once when the failure streak reaches the threshold,
    RECOVERED once on the first good run after an alerted outage, and None
    otherwise. Without a workflow id (a manual test run in the editor) nothing
    is tracked.
    """
    if workflow_id is None:
        return None
    with _lock:
        entry = _state.get(workflow_id) or {"failures": 0, "alerted": False}
        if has_data:
            recovered = entry["alerted"]
            # Dropped rather than reset, so a healthy check holds no state.
            _state.pop(workflow_id, None)
            return RECOVERED if recovered else None
        entry = {**entry, "failures": entry["failures"] + 1}
        should_alert = not entry["alerted"] and entry["failures"] >= FAILURE_RUNS_BEFORE_ALERT
        if should_alert:
            entry["alerted"] = True
        _state[workflow_id] = entry
        return OUTAGE if should_alert else None


def forget(workflow_id: int) -> None:
    """Drop a workflow's state, e.g. when it is deactivated."""
    with _lock:
        _state.pop(workflow_id, None)


def failure_count(workflow_id: int) -> int:
    with _lock:
        entry = _state.get(workflow_id)
        return entry["failures"] if entry else 0
