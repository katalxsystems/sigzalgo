"""Render a Plotly figure to PNG bytes, safely under gunicorn+eventlet.

Kaleido 1.x's ``fig.to_image()`` internally calls ``asyncio.run()``, which
raises ``RuntimeError`` when invoked from a thread that already has a running
event loop, and eventlet's monkey-patched ``threading.Thread`` does not
escape that: its ``ThreadPoolExecutor`` workers are greenlets, not real OS
threads, so they still see whatever loop the current greenlet is on.

The reliable escape hatch is a real, unpatched OS thread (``utils.real_threading.Thread``)
-- a brand-new OS thread has no event loop of its own, so ``asyncio.run()``
inside Kaleido gets a clean slate. Unlike a fire-and-forget background job,
callers here need the PNG bytes back before they can proceed (embedding it in
a Telegram message or a PDF page), so this waits for the render -- via
``real_threading.join()``'s poll loop, not a blocking ``Thread.join()``, so an
eventlet worker's hub stays free to serve other requests while waiting.
"""

import queue as _queue

from utils import real_threading
from utils.logging import get_logger

logger = get_logger(__name__)


def render_plotly_png(fig, *, timeout: float = 30.0) -> bytes:
    """Render a Plotly figure to PNG bytes on a real OS thread.

    Args:
        fig: A plotly.graph_objects.Figure.
        timeout: Seconds to wait for the render before raising TimeoutError.

    Returns:
        PNG image bytes.

    Raises:
        TimeoutError: the render did not finish within `timeout`.
        Exception: whatever Kaleido/Chromium raised, re-raised as-is.
    """
    result_q: _queue.Queue[tuple[str, object]] = real_threading.Queue()

    def _worker() -> None:
        try:
            png = fig.to_image(format="png", engine="kaleido")
            result_q.put(("ok", png))
        except BaseException as exc:  # noqa: BLE001 - propagate across thread
            result_q.put(("err", exc))

    t = real_threading.Thread(target=_worker, daemon=True, name="openalgo-kaleido-render")
    t.start()
    if not real_threading.join(t, timeout=timeout):
        raise TimeoutError(f"Plotly PNG render did not finish within {timeout}s")

    status, payload = result_q.get_nowait()
    if status == "err":
        raise payload  # type: ignore[misc]
    return payload  # type: ignore[return-value]
