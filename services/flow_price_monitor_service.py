# services/flow_price_monitor_service.py
"""
Flow Price Monitor Service
Real-time price monitoring for Price Alert triggers.

Primary trigger path is WebSocket ticks from the broker's live feed (the
same unified proxy scalping_risk_monitor_service.py subscribes to), so a
breach fires within one tick instead of waiting out a poll interval. REST
quote polling (services/flow_openalgo_client.py's get_quotes) remains as a
slower backstop -- it catches an alert whose WS subscribe silently failed
(feed not connected yet, broker down) or whose feed drops without the
client noticing, at the cost of the old 5s-latency ceiling for that alert
only. Both paths converge on the same _apply_price() so a breach is
evaluated and a workflow triggered identically regardless of which path
supplied the price.
"""

import atexit
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from services.flow_openalgo_client import FlowOpenAlgoClient, get_flow_client
from utils.env_config import env_int
from utils.logging import get_logger

logger = get_logger(__name__)


def _fmt_price(value: float | None) -> str:
    """Price for logs: 2 decimals with thousands separators; more precision
    for sub-1 prices (crypto)."""
    if value is None:
        return "n/a"
    return f"{value:,.2f}" if abs(value) >= 1 else f"{value:.6g}"


def _signed(value: float, base: float | None = None) -> str:
    """'+12.35 (+0.05%)' -- a move, optionally with its % of ``base``."""
    text = f"{value:+,.2f}" if abs(value) >= 1 or value == 0 else f"{value:+.4g}"
    if base:
        text += f" ({value / base * 100:+.2f}%)"
    return text


def describe_watch(alert: "PriceAlert") -> str:
    """What an alert is watching for, in plain words."""
    condition = FlowPriceMonitor.normalize_condition(alert.condition)
    level = _fmt_price(alert.target_price)
    lower = _fmt_price(alert.price_lower or alert.target_price)
    upper = _fmt_price(alert.price_upper or alert.target_price)
    pct = alert.percentage or 0
    return {
        "greater_than": f"price above {level}",
        "less_than": f"price below {level}",
        "crossing": f"price at {level} (within 0.1%)",
        "crossing_up": f"price crossing above {level}",
        "crossing_down": f"price crossing below {level}",
        "entering_channel": f"price inside channel {lower} - {upper}",
        "inside_channel": f"price inside channel {lower} - {upper}",
        "exiting_channel": f"price outside channel {lower} - {upper}",
        "outside_channel": f"price outside channel {lower} - {upper}",
        "moving_up": "price moving up from the previous tick",
        "moving_down": "price moving down from the previous tick",
        "moving_up_percent": f"price moving up {pct}% or more from the previous price",
        "moving_down_percent": f"price moving down {pct}% or more from the previous price",
    }.get(condition, f"condition {alert.condition!r} at {level}")


def describe_breach(
    alert: "PriceAlert", price: float, previous: float | None
) -> tuple[str, float | None]:
    """How ``price`` met the alert: the level that was breached and by how
    much. Returns (description, breached level)."""
    condition = FlowPriceMonitor.normalize_condition(alert.condition)
    target = alert.target_price
    lower = alert.price_lower or target
    upper = alert.price_upper or target
    now = _fmt_price(price)
    was = _fmt_price(previous)

    if condition == "greater_than":
        return f"LTP {now} is above level {_fmt_price(target)} by {_signed(price - target, target)}", target
    if condition == "less_than":
        return f"LTP {now} is below level {_fmt_price(target)} by {_signed(price - target, target)}", target
    if condition == "crossing":
        return (
            f"LTP {now} reached level {_fmt_price(target)} "
            f"(off by {_signed(price - target)}, tolerance 0.1% = {_fmt_price(price * 0.001)})"
        ), target
    if condition == "crossing_up":
        path = f"{was} -> {now}" if previous is not None else f"first price {now} already above"
        return (
            f"crossed ABOVE level {_fmt_price(target)} ({path}), "
            f"now {_signed(price - target, target)} beyond the level"
        ), target
    if condition == "crossing_down":
        path = f"{was} -> {now}" if previous is not None else f"first price {now} already below"
        return (
            f"crossed BELOW level {_fmt_price(target)} ({path}), "
            f"now {_signed(price - target, target)} beyond the level"
        ), target
    if condition in ("entering_channel", "inside_channel"):
        return (
            f"LTP {now} is inside channel {_fmt_price(lower)} - {_fmt_price(upper)} "
            f"({_signed(price - lower)} from lower, {_signed(price - upper)} from upper)"
        ), None
    if condition in ("exiting_channel", "outside_channel"):
        if price > upper:
            return (
                f"LTP {now} broke ABOVE channel upper {_fmt_price(upper)} "
                f"by {_signed(price - upper, upper)} (channel {_fmt_price(lower)} - {_fmt_price(upper)})"
            ), upper
        return (
            f"LTP {now} broke BELOW channel lower {_fmt_price(lower)} "
            f"by {_signed(price - lower, lower)} (channel {_fmt_price(lower)} - {_fmt_price(upper)})"
        ), lower
    if condition in ("moving_up", "moving_down", "moving_up_percent", "moving_down_percent"):
        move = price - (previous or 0)
        threshold = (
            f", threshold {alert.percentage or 0}%" if condition.endswith("_percent") else ""
        )
        return f"moved {was} -> {now} ({_signed(move, previous)}{threshold})", previous
    return f"LTP {now} met condition {alert.condition!r} at {_fmt_price(target)}", target

# Shared and bounded, never one thread per fire: an every_time alert on a fast
# poll interval would otherwise spawn a thread per tick, each running a whole
# workflow. Mirrors the order-update monitor's pool.
_WORKFLOW_POOL = ThreadPoolExecutor(
    max_workers=env_int("FLOW_PRICE_ALERT_WORKERS", 4, minimum=1),
    thread_name_prefix="flow-price-alert",
)

# WS subscribe mode: only LTP is needed to evaluate any of the supported
# conditions (all of them compare against last-traded price).
_WS_SUBSCRIBE_MODE = "LTP"


@dataclass
class PriceAlert:
    """Represents an active price alert"""

    workflow_id: int
    symbol: str
    exchange: str
    condition: str
    target_price: float
    price_lower: float | None = None
    price_upper: float | None = None
    percentage: float | None = None
    last_price: float | None = None
    triggered: bool = False
    created_at: datetime = field(default_factory=datetime.now)
    api_key: str | None = None
    # The editor offers these; they were previously dropped at activation, so
    # "Every Time" silently behaved as "Only Once" and expiry never applied.
    trigger: str = "once"
    expiration: str = "none"


class FlowPriceMonitor:
    """
    Singleton service that monitors prices (WS-primary, poll backstop)
    and triggers workflows when price conditions are met.
    """

    _instance: Optional["FlowPriceMonitor"] = None
    _lock = threading.Lock()

    # Class-level defaults. _trigger_workflow relies on these, and an instance
    # can exist without __init__ having run (the singleton is created in
    # __new__, and test harnesses build one directly), so they must never be
    # merely instance attributes.
    _pending: set[int] = set()
    _pending_lock = threading.Lock()
    # Guards _alerts together with _running/_stop_event/_monitor_thread. The
    # emptiness check that stops the loop and the registration that starts it
    # are check-then-act pairs run from both request threads and the monitor
    # thread itself, so they must not interleave. RLock because _check_alert
    # re-enters through remove_alert from inside the loop.
    _alerts_lock = threading.RLock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._initialized = True
        self._alerts: dict[int, PriceAlert] = {}
        self._running = False
        self._monitor_thread: threading.Thread | None = None
        # Backstop only -- the WS tick path (_on_tick) is what normally fires
        # an alert. See env_int() below for the override var.
        self._poll_interval = env_int("FLOW_PRICE_ALERT_POLL_INTERVAL", 30, minimum=5)
        self._stop_event = threading.Event()
        # Workflows with a queued or running price-alert execution, so a tick
        # cannot stack another one behind it. Class-level defaults above cover
        # an instance built without __init__.
        self._pending = set()
        self._pending_lock = threading.Lock()
        self._alerts_lock = threading.RLock()

        # WS plumbing. One WebSocketClient per api_key (get_websocket_client is
        # itself a keyed singleton, so this dict just tracks which ones this
        # monitor has already wired a callback into and what it has subscribed
        # on each -- mirrors scalping_risk_monitor_service.py's _ws/_subscribed.
        self._ws_clients: dict[str, Any] = {}
        self._ws_subscribed: dict[str, set[str]] = {}
        self._ws_lock = threading.Lock()

        self._shutdown_done = False

        logger.debug(
            f"FlowPriceMonitor initialized (WS-primary, "
            f"{self._poll_interval}s poll backstop)"
        )

    # The editor and this monitor grew separate vocabularies for the same four
    # conditions, so every alert the UI could produce fell through to the final
    # `return False` and the trigger never fired. Both spellings are accepted;
    # the UI's are the ones users actually have saved.
    _CONDITION_ALIASES = {
        "above": "greater_than",
        "price_above": "greater_than",
        "below": "less_than",
        "price_below": "less_than",
        "crosses_above": "crossing_up",
        "cross_above": "crossing_up",
        "crosses_below": "crossing_down",
        "cross_below": "crossing_down",
        "crosses": "crossing",
    }

    @classmethod
    def normalize_condition(cls, value: str | None) -> str:
        """Canonical condition name, accepting either vocabulary."""
        raw = str(value or "").strip().lower().replace("-", "_")
        return cls._CONDITION_ALIASES.get(raw, raw)

    def add_alert(
        self,
        workflow_id: int,
        symbol: str,
        exchange: str,
        condition: str,
        target_price: float,
        price_lower: float | None = None,
        price_upper: float | None = None,
        percentage: float | None = None,
        api_key: str | None = None,
        trigger: str = "once",
        expiration: str = "none",
    ) -> bool:
        """Add a price alert for a workflow"""
        condition = self.normalize_condition(condition)
        alert = PriceAlert(
            workflow_id=workflow_id,
            symbol=symbol,
            exchange=exchange,
            condition=condition,
            target_price=target_price,
            price_lower=price_lower,
            price_upper=price_upper,
            percentage=percentage,
            api_key=api_key,
            trigger=str(trigger or "once").strip().lower(),
            expiration=str(expiration or "none").strip().lower(),
        )

        with self._alerts_lock:
            self._alerts[workflow_id] = alert
            logger.info(
                f"Added price alert for workflow {workflow_id}: watching "
                f"{symbol}@{exchange} for {describe_watch(alert)} "
                f"(trigger: {alert.trigger}, expires: {alert.expiration})"
            )

            if not self._running:
                self._start_monitoring()

        # Blocking WS call: kept outside _alerts_lock so a slow subscribe
        # (broker capacity, proxy round trip) never blocks other alert
        # registrations/removals.
        if api_key:
            self._ws_subscribe(api_key, symbol, exchange)
        else:
            logger.warning(
                f"Price alert for workflow {workflow_id} has no api_key; cannot "
                "subscribe to live ticks, relying entirely on the poll backstop."
            )

        return True

    def remove_alert(self, workflow_id: int) -> bool:
        """Remove a price alert for a workflow"""
        with self._alerts_lock:
            if workflow_id not in self._alerts:
                return False

            alert = self._alerts[workflow_id]
            del self._alerts[workflow_id]
            logger.info(f"Removed price alert for workflow {workflow_id}")

            if not self._alerts and self._running:
                self._stop_monitoring()

        if alert.api_key:
            self._ws_unsubscribe_if_unused(alert.api_key, alert.symbol, alert.exchange)

        return True

    def get_alert(self, workflow_id: int) -> PriceAlert | None:
        """Get alert for a workflow"""
        return self._alerts.get(workflow_id)

    def get_active_alerts_count(self) -> int:
        """Get count of active alerts"""
        return len(self._alerts)

    # ------------------------------------------------------------------ ws plumbing
    @staticmethod
    def _symkey(symbol: str, exchange: str) -> str:
        return f"{exchange}:{symbol}"

    def _ensure_ws(self, api_key: str):
        """Get (or create) the shared WebSocketClient for this api_key and make
        sure this monitor's callbacks are wired into it. Returns None if the
        feed isn't available yet (broker not connected, proxy down) -- the
        caller falls back to the poll backstop, exactly like
        scalping_risk_monitor_service.py's _ensure_ws()."""
        with self._ws_lock:
            client = self._ws_clients.get(api_key)
        if client is not None and getattr(client, "connected", False):
            return client

        try:
            from services.websocket_client import get_websocket_client

            client = get_websocket_client(api_key)
        except Exception as e:
            logger.debug(f"Flow price monitor: feed not available yet for this account: {e}")
            return None

        with self._ws_lock:
            is_new = api_key not in self._ws_clients
            self._ws_clients[api_key] = client
            self._ws_subscribed.setdefault(api_key, set())
        if is_new:
            client.register_callback("market_data", lambda data: self._on_tick(api_key, data))
            client.register_callback("auth", lambda data: self._on_ws_auth(api_key, data))
        return client

    def _ws_subscribe(self, api_key: str, symbol: str, exchange: str) -> None:
        """Subscribe to live LTP ticks for one symbol on this account's feed.
        Safe to call redundantly -- e.g. a call's sl_target and entry_recross
        watches share the same symbol; the underlying WS client de-dupes, and
        _ws_subscribed just needs to know at least one alert wants it."""
        key = self._symkey(symbol, exchange)
        with self._ws_lock:
            already = key in self._ws_subscribed.get(api_key, set())
        if already:
            return

        client = self._ensure_ws(api_key)
        if client is None:
            return  # poll backstop covers this alert until the feed comes up

        try:
            client.subscribe([{"exchange": exchange, "symbol": symbol}], mode=_WS_SUBSCRIBE_MODE)
            with self._ws_lock:
                self._ws_subscribed.setdefault(api_key, set()).add(key)
            logger.debug(f"Flow price monitor subscribed to {key} for live ticks")
        except Exception as e:
            logger.warning(f"Flow price monitor: WS subscribe failed for {key}: {e}")

    def _ws_unsubscribe_if_unused(self, api_key: str, symbol: str, exchange: str) -> None:
        """Unsubscribe only if no other live (non-triggered) alert on this
        account still wants this symbol -- a call's two sibling watches share
        one subscription, and removing the first must not blind the second."""
        key = self._symkey(symbol, exchange)
        with self._alerts_lock:
            still_wanted = any(
                a.api_key == api_key and self._symkey(a.symbol, a.exchange) == key
                for a in self._alerts.values()
            )
        if still_wanted:
            return

        with self._ws_lock:
            client = self._ws_clients.get(api_key)
            subscribed = self._ws_subscribed.get(api_key, set())
            if key not in subscribed:
                return
            subscribed.discard(key)
        if client is None:
            return
        try:
            client.unsubscribe([{"exchange": exchange, "symbol": symbol}], mode=_WS_SUBSCRIBE_MODE)
        except Exception as e:
            logger.debug(f"Flow price monitor: WS unsubscribe failed for {key}: {e}")

    def _on_ws_auth(self, api_key: str, data: dict) -> None:
        """Re-subscribe this account's symbols after a (re)connect -- the
        client re-auths automatically but does not restore subscriptions."""
        if data.get("status") != "success":
            return
        with self._ws_lock:
            keys = set(self._ws_subscribed.get(api_key, set()))
            self._ws_subscribed[api_key] = set()
        for key in keys:
            exchange, symbol = key.split(":", 1)
            self._ws_subscribe(api_key, symbol, exchange)

    def _on_tick(self, api_key: str, data: dict) -> None:
        """Primary trigger path: evaluate every matching alert against a live
        tick the instant it arrives, instead of waiting for the poll loop."""
        try:
            symbol = data.get("symbol")
            exchange = data.get("exchange")
            inner = data.get("data") or {}
            ltp = inner.get("ltp")
            if ltp is None:
                ltp = inner.get("last_price")
            if symbol is None or exchange is None or ltp is None:
                return
            ltp = float(ltp)
            if ltp <= 0:
                return
        except (TypeError, ValueError):
            return

        key = self._symkey(symbol, exchange)
        with self._alerts_lock:
            matches = [
                a
                for a in self._alerts.values()
                if a.api_key == api_key
                and self._symkey(a.symbol, a.exchange) == key
                and not a.triggered
            ]
        for alert in matches:
            try:
                self._apply_price(alert, ltp, source="websocket tick")
            except Exception as e:
                logger.exception(f"Error applying tick to workflow {alert.workflow_id}: {e}")

    def _start_monitoring(self):
        """Start the price monitoring thread"""
        if self._running:
            return

        # A fresh event per generation. Sharing one and clearing it on restart
        # revived a previous loop that had been told to stop but had not yet
        # exited, leaving two loops polling and double-submitting every_time
        # executions.
        self._stop_event = threading.Event()
        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._monitoring_loop, args=(self._stop_event,), daemon=True
        )
        self._monitor_thread.start()
        logger.info(f"Price monitoring started with {len(self._alerts)} alerts")

    def _stop_monitoring(self):
        """Stop the price monitoring thread"""
        if not self._running:
            return

        # Signals only this generation. A later start creates its own event, so
        # this loop can never be un-stopped by a restart.
        self._stop_event.set()
        self._running = False

        # Clear only the generation this call is stopping. A concurrent
        # _start_monitoring may already have installed a newer thread here, and
        # nulling that one would leave the live loop unreferenced and unjoinable.
        thread = self._monitor_thread
        if thread is not None:
            self._monitor_thread = None

        # remove_alert() is called from inside the monitoring loop when a
        # one-shot alert expires, and joining the current thread raises
        # RuntimeError. The loop exits on the stop event anyway.
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

        logger.info("Price monitoring stopped")

    def _monitoring_loop(self, stop_event: threading.Event | None = None):
        """Poll-backstop loop. _on_tick handles the normal case; this thread
        only matters for an alert whose WS subscribe hasn't delivered a tick
        yet, which is why its interval is much coarser than a tick stream.

        Watches the event it was started with, not whatever the instance
        currently holds, so a restart cannot resurrect it.
        """
        from utils.db_sessions import remove_all_scoped_sessions

        stop_event = stop_event or self._stop_event
        while not stop_event.is_set():
            try:
                self._check_all_alerts()
            except Exception as e:
                logger.exception(f"Error in monitoring loop: {e}")
            finally:
                # Quote lookups bind scoped sessions to this thread, which has
                # no Flask app context, so teardown_appcontext never fires. Left
                # alone they accumulate a connection per generation of this loop.
                remove_all_scoped_sessions()

            # Wait for next poll interval
            stop_event.wait(timeout=self._poll_interval)

    def _check_all_alerts(self):
        """Check all active alerts against current prices"""
        with self._alerts_lock:
            workflow_ids = list(self._alerts.keys())

        for workflow_id in workflow_ids:
            alert = self._alerts.get(workflow_id)
            if alert and not alert.triggered:
                try:
                    self._check_alert(alert)
                except Exception as e:
                    logger.exception(f"Error checking alert for workflow {workflow_id}: {e}")

    # Windows the editor offers for "expiration". A watch past its window is
    # removed rather than left running for the life of the process.
    _EXPIRATION_WINDOWS = {
        "1h": timedelta(hours=1),
        "4h": timedelta(hours=4),
        "1d": timedelta(days=1),
        "1w": timedelta(weeks=1),
    }

    def _is_expired(self, alert: PriceAlert) -> bool:
        window = self._EXPIRATION_WINDOWS.get(alert.expiration)
        if window is None:
            return False
        return datetime.now() - alert.created_at >= window

    def _check_alert(self, alert: PriceAlert):
        """Poll backstop: fetch one REST quote and apply it. Only reached for
        an alert whose WS subscribe hasn't (yet) delivered a tick -- normally
        _on_tick beats this to the trigger every time."""
        if not alert.api_key:
            logger.warning(f"No API key for alert workflow {alert.workflow_id}")
            return

        if self._is_expired(alert):
            logger.info(
                f"Price alert for workflow {alert.workflow_id} expired after "
                f"{alert.expiration}; no longer watching."
            )
            self.remove_alert(alert.workflow_id)
            return

        try:
            client = get_flow_client(alert.api_key)
            result = client.get_quotes(symbol=alert.symbol, exchange=alert.exchange)

            if result.get("status") != "success":
                logger.debug(f"Failed to get quote for {alert.symbol}: {result}")
                return

            data = result.get("data", {})
            current_price = float(data.get("ltp", 0) if data else 0)

            if current_price <= 0:
                return

            self._apply_price(alert, current_price, source="REST quote poll")

        except Exception as e:
            logger.exception(f"Error checking price for {alert.symbol}: {e}")

    def _apply_price(
        self, alert: PriceAlert, current_price: float, source: str = "price update"
    ) -> None:
        """Evaluate one alert against one price and trigger its workflow if the
        condition is met. Shared by the WS tick path (_on_tick, the normal
        case) and the REST poll backstop (_check_alert) so a breach is handled
        identically no matter which path supplied the price.

        De-registration of a one-shot alert belongs to the run it launches
        (_trigger_workflow / _retire_one_shot), not to this method: removing
        it here would race the pool worker (submit() usually keeps the GIL,
        so the alert would be gone before the worker thread ever read it, and
        the run dropped as "no longer registered" -- the webhook would then
        never fire even though the breach was correctly detected).
        """
        if alert.triggered:
            return

        previous_price = alert.last_price
        condition_met = self._evaluate_condition(alert, current_price)

        if condition_met:
            # `triggered` is the one-shot latch, and both _check_all_alerts and
            # _on_tick skip any alert carrying it. Setting it for an
            # every_time alert left the watch registered but permanently
            # ignored.
            if alert.trigger != "every_time":
                alert.triggered = True
            breach, breach_level = describe_breach(alert, current_price, previous_price)
            logger.info(
                f"PRICE BREACH workflow {alert.workflow_id} {alert.symbol}@{alert.exchange}: "
                f"{breach} | watching for {describe_watch(alert)} | LTP {_fmt_price(current_price)}, "
                f"previous {_fmt_price(previous_price)} | via {source} | trigger {alert.trigger}"
            )

            if alert.trigger == "every_time":
                # Keep watching; record the price so an edge-triggered
                # crossing needs a fresh cross rather than re-firing.
                alert.last_price = current_price

            self._trigger_workflow(
                alert,
                current_price,
                breach={
                    "description": breach,
                    "breach_level": breach_level,
                    "previous_price": previous_price,
                    "condition": alert.condition,
                    "target_price": alert.target_price,
                    "price_lower": alert.price_lower,
                    "price_upper": alert.price_upper,
                    "symbol": alert.symbol,
                    "exchange": alert.exchange,
                    "source": source,
                },
            )
        else:
            alert.last_price = current_price

    def _evaluate_condition(self, alert: PriceAlert, current_price: float) -> bool:
        """Evaluate if the price condition is met"""
        condition = self.normalize_condition(alert.condition)
        target = alert.target_price
        last_price = alert.last_price

        tolerance = current_price * 0.001

        if condition == "greater_than":
            return current_price > target

        elif condition == "less_than":
            return current_price < target

        elif condition == "crossing":
            return abs(current_price - target) <= tolerance

        elif condition == "crossing_up":
            if last_price is None:
                return current_price > target
            return last_price <= target and current_price > target

        elif condition == "crossing_down":
            if last_price is None:
                return current_price < target
            return last_price >= target and current_price < target

        elif condition in ["entering_channel", "inside_channel"]:
            lower = alert.price_lower or target
            upper = alert.price_upper or target
            return lower <= current_price <= upper

        elif condition in ["exiting_channel", "outside_channel"]:
            lower = alert.price_lower or target
            upper = alert.price_upper or target
            return current_price < lower or current_price > upper

        elif condition == "moving_up":
            if last_price is None:
                return False
            return current_price > last_price

        elif condition == "moving_down":
            if last_price is None:
                return False
            return current_price < last_price

        elif condition == "moving_up_percent":
            if last_price is None or last_price == 0:
                return False
            pct_change = ((current_price - last_price) / last_price) * 100
            return pct_change >= (alert.percentage or 0)

        elif condition == "moving_down_percent":
            if last_price is None or last_price == 0:
                return False
            pct_change = ((last_price - current_price) / last_price) * 100
            return pct_change >= (alert.percentage or 0)

        # Never silently false: an unknown condition means the alert can never
        # fire, which is indistinguishable from "the level was not reached".
        logger.error(
            f"Price alert for workflow {alert.workflow_id} has an unrecognized "
            f"condition {alert.condition!r}; it can never trigger."
        )
        return False

    def _trigger_workflow(
        self, alert: PriceAlert, trigger_price: float, breach: dict | None = None
    ):
        """Queue one execution for this alert, at most one at a time.

        Coalesced deliberately. The pool's queue is unbounded, so an every_time
        alert whose workflow runs slower than the poll interval would stack a
        task per tick and execute long after the price that caused it. One
        pending run per workflow keeps the alert responsive without a backlog.

        The whole alert object is passed, not its id, so the worker can prove
        the registration it is about to consume is still the one that produced
        it. A deactivate/reactivate cycle installs a new PriceAlert under the
        same id, and an id-keyed removal would delete that new one.
        """
        workflow_id = alert.workflow_id
        api_key = alert.api_key
        one_shot = alert.trigger != "every_time"

        with self._pending_lock:
            if workflow_id in self._pending:
                logger.debug(
                    f"Price alert for workflow {workflow_id} already has a run queued; "
                    "skipping this tick."
                )
                return
            self._pending.add(workflow_id)

        # Whether the workflow actually ran. A one-shot alert is only spent by
        # a run that reached the graph.
        executed = False

        def run_workflow():
            nonlocal executed
            try:
                # Re-checked here, not only at submit time: a queued run can sit
                # behind a slow execution while the alert is removed, the watch
                # expires, or the workflow is deactivated. execute_workflow does
                # not require the workflow to be active, so without this a stale
                # price event could still place orders.
                if self._alerts.get(workflow_id) is not alert:
                    logger.info(
                        f"Dropping queued price-alert run for workflow {workflow_id}: "
                        "the alert was removed or replaced before this run claimed it."
                    )
                    return
                if not self._workflow_is_active(workflow_id):
                    logger.info(
                        f"Dropping queued price-alert run for workflow {workflow_id}: "
                        "the workflow is no longer active."
                    )
                    return

                from services.flow_executor_service import execute_workflow

                webhook_data = {
                    "trigger_type": "price_alert",
                    "trigger_price": trigger_price,
                    "triggered_at": datetime.now().isoformat(),
                    # The breached level, previous price and plain-text
                    # description; the executor writes the description into
                    # the run's Execution Log.
                    **({"breach": breach} if breach else {}),
                }

                result = execute_workflow(workflow_id, webhook_data=webhook_data, api_key=api_key)
                logger.info(f"Workflow {workflow_id} execution result: {result.get('status')}")
                # `already_running` means this event never reached the graph.
                # Anything else did run, whatever the broker made of it.
                executed = not result.get("already_running")

            except Exception as e:
                logger.exception(f"Failed to execute workflow {workflow_id}: {e}")
            finally:
                if one_shot:
                    if executed:
                        self._retire_one_shot(alert)
                    else:
                        # Nothing ran, so the alert must not be consumed. It was
                        # retired unconditionally here, which meant a one-shot
                        # colliding with an in-flight run -- or dropped by a
                        # guard above -- was silently spent and its workflow
                        # deactivated, with the event lost for good.
                        alert.triggered = False
                        logger.info(
                            f"Re-arming one-shot price alert for workflow {workflow_id}: "
                            "the run did not execute."
                        )
                with self._pending_lock:
                    self._pending.discard(workflow_id)
                # No Flask app context on a pool thread, so teardown_appcontext
                # never fires and every session the run touched would stay bound
                # to the thread holding its connection.
                from utils.db_sessions import remove_all_scoped_sessions

                remove_all_scoped_sessions()

        try:
            _WORKFLOW_POOL.submit(run_workflow)
        except Exception:
            with self._pending_lock:
                self._pending.discard(workflow_id)
            # The one-shot latch is set before submitting, so that a second tick
            # cannot queue the same alert twice. If the submit itself fails the
            # latch has to come back off, or _check_all_alerts skips this alert
            # for the rest of the process and the trigger is dead.
            if one_shot:
                alert.triggered = False
                logger.error(
                    f"Could not queue the price-alert run for workflow {workflow_id}; "
                    "the alert stays armed."
                )
            raise

    def _retire_one_shot(self, alert: PriceAlert) -> None:
        """Consume a one-shot alert: drop the watch and clear the active flag.

        Both halves matter. Dropping only the in-memory watch left the row
        `is_active`, so the UI kept showing the workflow as armed, the activate
        endpoint answered `already_active` and refused to re-arm it, and
        `restore_price_alerts` would re-arm the spent alert on the next restart
        and fire an order the user believed was one-time.

        Removal is by identity, so a run that finishes after the user has
        deactivated and reactivated cannot delete the newer registration.
        remove_alert() also unsubscribes the WS feed if no other alert still
        wants this symbol.
        """
        workflow_id = alert.workflow_id
        with self._alerts_lock:
            if self._alerts.get(workflow_id) is not alert:
                logger.debug(
                    f"One-shot price alert for workflow {workflow_id} was already "
                    "replaced; leaving the current registration alone."
                )
                return
            self.remove_alert(workflow_id)

        try:
            from database.flow_db import deactivate_workflow

            deactivate_workflow(workflow_id)
            logger.info(
                f"One-shot price alert for workflow {workflow_id} consumed; "
                "workflow deactivated."
            )
        except Exception:
            logger.exception(
                f"Could not deactivate workflow {workflow_id} after its one-shot "
                "price alert fired"
            )

    @staticmethod
    def _workflow_is_active(workflow_id: int) -> bool:
        """Whether the workflow is still active. Fails closed on error."""
        try:
            from database.flow_db import get_workflow

            workflow = get_workflow(workflow_id)
            return bool(workflow and workflow.is_active)
        except Exception:
            logger.exception(f"Could not confirm workflow {workflow_id} is active")
            return False

    def is_running(self) -> bool:
        """Check if monitoring is active"""
        return self._running

    def get_status(self) -> dict[str, Any]:
        """Get current monitor status"""
        with self._ws_lock:
            ws_subscribed_count = sum(len(s) for s in self._ws_subscribed.values())
        return {
            "running": self._running,
            "alerts_count": len(self._alerts),
            "poll_interval": self._poll_interval,
            "poll_interval_is_backstop": True,
            "ws_subscribed_count": ws_subscribed_count,
            "alerts": [
                {
                    "workflow_id": alert.workflow_id,
                    "symbol": alert.symbol,
                    "exchange": alert.exchange,
                    "condition": alert.condition,
                    "target_price": alert.target_price,
                    "last_price": alert.last_price,
                    "triggered": alert.triggered,
                    "ws_subscribed": (
                        self._symkey(alert.symbol, alert.exchange)
                        in self._ws_subscribed.get(alert.api_key or "", set())
                    ),
                }
                for alert in self._alerts.values()
            ],
        }

    def shutdown(self):
        """Stop polling, unsubscribe every WS symbol and release the worker pool.

        Registered with atexit below, mirroring the scalping risk monitor. It
        previously had no caller anywhere, so the poll thread and the executor
        pool were simply abandoned at process exit -- and unlike the sibling
        order-update monitor it never released the pool at all, leaving its
        threads and their file descriptors held.

        Idempotent: atexit may fire after an explicit call.
        """
        if getattr(self, "_shutdown_done", False):
            return
        self._shutdown_done = True

        self._stop_monitoring()

        with self._ws_lock:
            subs_by_key = {k: set(v) for k, v in self._ws_subscribed.items()}
            clients = dict(self._ws_clients)
            self._ws_subscribed.clear()
        # Unsubscribe this monitor's symbols only -- the WebSocketClient itself
        # is a shared per-account singleton (other services may hold the same
        # instance), so it is never disconnected here.
        for api_key, keys in subs_by_key.items():
            client = clients.get(api_key)
            if client is None:
                continue
            for key in keys:
                exchange, symbol = key.split(":", 1)
                try:
                    client.unsubscribe(
                        [{"exchange": exchange, "symbol": symbol}], mode=_WS_SUBSCRIBE_MODE
                    )
                except Exception as e:
                    logger.debug(f"Flow price monitor shutdown: unsubscribe failed for {key}: {e}")

        with self._alerts_lock:
            self._alerts.clear()
        # Let an in-flight workflow finish rather than killing it mid-order; the
        # pool's threads are released with it instead of outliving the process.
        _WORKFLOW_POOL.shutdown(wait=False)
        logger.info("FlowPriceMonitor shutdown")


def restore_price_alerts() -> int:
    """Re-register alerts for already-active priceAlert workflows.

    Alerts live only in memory, but `is_active` is persisted. Without this, a
    server restart leaves such workflows marked active while watching nothing,
    and the activate endpoint rejects them as `already_active` -- so they stay
    silently dead until manually deactivated and reactivated. This mirrors
    restore_order_update_watches; call once at startup, after the DB is ready.

    Safe to call repeatedly: add_alert replaces the entry for a workflow id
    rather than stacking a second one. A one-shot alert that has already fired
    is not restored, because _retire_one_shot clears is_active when it is
    consumed -- otherwise every restart would re-arm a spent alert.
    """
    from database.flow_db import get_active_workflows, get_workflow_api_key

    monitor = get_flow_price_monitor()
    restored = 0
    for workflow in get_active_workflows():
        trigger_node = next(
            (n for n in (workflow.nodes or []) if n.get("type") == "priceAlert"), None
        )
        if not trigger_node:
            continue
        api_key = get_workflow_api_key(workflow)
        if not api_key:
            logger.warning(
                f"Cannot restore price alert for workflow {workflow.id}: no stored API key"
            )
            continue
        data = trigger_node.get("data", {}) or {}
        symbol = data.get("symbol", "")
        if not symbol:
            logger.warning(
                f"Cannot restore price alert for workflow {workflow.id}: no symbol configured"
            )
            continue
        try:
            monitor.add_alert(
                workflow_id=workflow.id,
                symbol=symbol,
                exchange=data.get("exchange", "NSE"),
                condition=data.get("condition", "greater_than"),
                target_price=float(data.get("price", 0) or 0),
                price_lower=data.get("priceLower"),
                price_upper=data.get("priceUpper"),
                percentage=data.get("percentage"),
                api_key=api_key,
                trigger=data.get("trigger", "once"),
                expiration=data.get("expiration", "none"),
            )
            restored += 1
        except (TypeError, ValueError) as e:
            logger.warning(f"Skipping invalid price alert for workflow {workflow.id}: {e}")
        except Exception:
            logger.exception(f"Failed to restore price alert for workflow {workflow.id}")
    if restored:
        logger.info(f"Restored {restored} price alert(s) from active workflows")
    return restored


# Singleton instance
flow_price_monitor = FlowPriceMonitor()


# Release the poll thread, WS subscriptions and the worker pool on the way
# out. Without this the monitor's file descriptors were held until the
# process was killed.
def _shutdown_at_exit() -> None:
    """atexit entry point.

    Logging handlers are often already closed by the time interpreter shutdown
    reaches us -- pytest closes its capture streams first -- and logging into a
    closed stream prints a "Logging error" traceback that looks like a fault
    but is only ordering. Nothing after this point needs a log line, so quiet
    the loggers before releasing the pool.
    """
    logging.disable(logging.CRITICAL)
    try:
        flow_price_monitor.shutdown()
    except Exception:
        pass


atexit.register(_shutdown_at_exit)


def get_flow_price_monitor() -> FlowPriceMonitor:
    """Get the global price monitor instance"""
    return flow_price_monitor
