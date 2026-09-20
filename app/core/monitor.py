"""
app/core/monitor.py
===================
Heartbeat daemon thread for SafeIM Lead Monitor V2 (Extension-First).

The MonitorThread no longer touches a browser or runs Playwright.
All DOM interaction is owned by the Chrome Extension (content.js).

Responsibilities
----------------
  1. Start the LocalServer HTTP Command Center on localhost:9223.
  2. Run a lightweight heartbeat loop:
       - Push purchase_count updates to the UI every tick.
       - Check daily/session caps and emit limit-hit events.
  3. Expose pause() / resume() / stop() for the UI Start/Pause/Stop buttons.

Communication with the UI
--------------------------
  All UI updates go through the thread-safe queue.Queue (ui_queue).
  The AppWindow drains this queue every 100 ms on the main thread.

Event schema pushed to ui_queue
--------------------------------
  {"type": "log",            "msg": str, "level": str}
  {"type": "purchase_count", "count": int, "limit": int}
  {"type": "daily_limit_hit","limit": int}
  {"type": "ext_connected"}          <- client made first contact
  {"type": "conn_state", "state": str, "detail": str}
  {"type": "monitor_paused"}
  {"type": "monitor_resumed"}
  {"type": "monitor_stopped"}

Note: "lead" events and "ext_connected" are pushed directly by LocalServer,
not by MonitorThread, since they arrive asynchronously from the client.

Resource ownership
------------------
  MonitorThread OWNS the LocalServer object: it constructs it in run(),
  publishes it as ``self.server`` so the UI thread can verify shutdown, and
  destroys it in the finally block of run().  The UI thread never calls
  LocalServer.start(); it only reads the published verification API.

  Ownership chain:
      AppWindow  --owns-->  MonitorThread
      MonitorThread  --owns-->  LocalServer
      LocalServer  --owns-->  HTTPServer + serving thread

  Each level is responsible for explicitly stopping the level below it and
  for proving that it did so.  Daemon flags exist only so a hard crash cannot
  wedge the process; they are never the cleanup mechanism.
"""

import logging
import queue
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Heartbeat interval: how often the loop checks caps and refreshes UI counters.
_HEARTBEAT_SECONDS = 5.0

# Bounded wait used when the UI asks "did the server finish binding yet?"
_SERVER_READY_TIMEOUT = 10.0


class MonitorThread(threading.Thread):
    """
    Daemon thread that manages the LocalServer and runs the heartbeat loop.

    Parameters
    ----------
    db       : LeadDatabase -- thread-safe database instance.
    settings : dict         -- shared mutable settings dict (read on every tick).
    ui_queue : queue.Queue  -- event queue drained by the main UI thread.
    """

    def __init__(self, db, settings: Dict[str, Any], ui_queue: queue.Queue, ctrl_queue: queue.Queue = None):
        super().__init__(daemon=True, name="MonitorThread")
        self.db       = db
        self.settings = settings
        self.ui_queue = ui_queue
        self.ctrl_queue = ctrl_queue or queue.Queue()

        self._stop_event        = threading.Event()
        self._pause_event       = threading.Event()
        self._session_purchases = 0   # Resets every time the thread is restarted

        # ── Owned resource, published for verification by the UI thread ───────
        # Assigned in run() BEFORE start() is called so that a bind failure is
        # still inspectable from outside.  Guarded by _server_lock because the
        # UI thread reads it concurrently.
        self._server_lock: threading.Lock = threading.Lock()
        self._server = None                       # type: Optional[Any]
        self._server_ready = threading.Event()    # set once start() returned
        self._server_started_ok: bool = False

        # Last connection state published to the UI; used so that conn_state
        # events are emitted ON TRANSITION ONLY (no duplicate event flood).
        self._last_conn_state: Optional[str] = None

    # =========================================================================
    # Ownership / verification API  (safe to call from the UI thread)
    # =========================================================================

    @property
    def server(self):
        """The LocalServer this thread owns, or None before run() starts it."""
        with self._server_lock:
            return self._server

    def wait_for_server(self, timeout: float = _SERVER_READY_TIMEOUT) -> bool:
        """
        Block (bounded) until run() has attempted to start the server.

        Returns True if the server bound successfully, False on timeout or on
        an explicit bind failure.  Never call this from the Tk main thread with
        a long timeout -- it blocks.
        """
        if not self._server_ready.wait(timeout=timeout):
            return False
        return self._server_started_ok

    def verify_server_released(self) -> Dict[str, Any]:
        """
        Ask the owned LocalServer to prove it released its socket and thread.

        Returns the LocalServer.verify_stopped() report, or a synthetic report
        when no server was ever created.  This replaces the previous check,
        which read ``server._thread`` after stop() had already set that
        attribute to None -- a test that could never fail and therefore proved
        nothing.
        """
        srv = self.server
        if srv is None:
            return {
                "state":             "never_created",
                "thread_alive":      False,
                "serve_loop_exited": True,
                "port_has_listener": None,
                "clean":             True,
                "last_shutdown":     None,
            }
        return srv.verify_stopped()

    # =========================================================================
    # Control API  (called from the UI thread -- all operations are thread-safe)
    # =========================================================================

    def stop(self):
        """Signal the thread to exit cleanly after the current tick."""
        self._stop_event.set()
        self._pause_event.clear()   # Unblock any _wait_while_paused() spin

    def pause(self):
        """Pause the heartbeat loop after the current tick completes."""
        if not self._pause_event.is_set():
            self._pause_event.set()
            logger.info("[Monitor] Pause requested.")

    def resume(self):
        """Resume the heartbeat loop from a paused state."""
        if self._pause_event.is_set():
            self._pause_event.clear()
            logger.info("[Monitor] Resume requested.")

    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    def _stopped(self) -> bool:
        return self._stop_event.is_set()

    # =========================================================================
    # Thread Entry Point
    # =========================================================================

    def run(self):
        """Entry point executed in the background thread."""
        from app.core.server import LocalServer
        from app.core import notifier

        port = int(self.settings.get("server_port", 9223))

        self._log(
            f"Starting local HTTP server on 127.0.0.1:{port}...",
            "info",
        )

        server = LocalServer(
            db=self.db,
            settings=self.settings,
            ui_queue=self.ui_queue,
            ctrl_queue=self.ctrl_queue,
            host="127.0.0.1",
            port=port,
        )

        # Publish BEFORE start() so a bind failure is still inspectable from
        # the UI thread, and so shutdown always has something to verify.
        with self._server_lock:
            self._server = server

        self._publish_conn_state(server)

        ok = server.start()
        self._server_started_ok = bool(ok)
        self._server_ready.set()

        if not ok:
            self._publish_conn_state(server, force=True)
            self._log(
                f"Failed to bind to port {port}. "
                f"Reason: {server.get_bind_error() or 'unknown'}. "
                "Is another instance already running? "
                "Change 'server_port' in Settings and restart.",
                "error",
            )
            self._push_ctrl({"type": "monitor_stopped"})
            return

        self._log(
            f"Listening for Chrome Extension on http://127.0.0.1:{port}\n"
            f"  Server instance: {server.instance_id}\n"
            "  Install the extension in Chrome and open IndiaMART to begin.",
            "success",
        )
        self._publish_conn_state(server, force=True)

        try:
            self._heartbeat_loop(notifier, server)
        except Exception as exc:
            logger.error("[Monitor] Critical error: %s", exc, exc_info=True)
            self._log(f"Critical error: {exc}", "error")
        finally:
            # Always stop the server before exiting, even on error.
            # server.stop() is idempotent and bounded -- it will not block forever.
            logger.info("[Monitor] Stopping LocalServer (instance %s)...",
                        server.instance_id)
            self._publish_conn_state(server, force=True)

            report = server.stop()

            # Verify shutdown against real evidence: the join() result recorded
            # inside stop(), the serve-loop exit Event, and a live TCP probe of
            # the port.  None of these can be faked by clearing a reference.
            verdict = server.verify_stopped()

            if verdict["clean"]:
                logger.info(
                    "[Monitor] LocalServer %s shutdown VERIFIED: thread exited, "
                    "serve loop exited, socket closed, no listener on port %d "
                    "(%.3fs).",
                    server.instance_id, port, report.get("duration_seconds", 0.0),
                )
            else:
                logger.error(
                    "[Monitor] LocalServer %s shutdown NOT clean -- "
                    "state=%s thread_alive=%s serve_loop_exited=%s "
                    "port_has_listener=%s errors=%s",
                    server.instance_id,
                    verdict["state"], verdict["thread_alive"],
                    verdict["serve_loop_exited"], verdict["port_has_listener"],
                    report.get("errors"),
                )
                self._log(
                    f"Warning: port {port} may not have been released cleanly. "
                    "See app.log for details.",
                    "warning",
                )

            self._publish_conn_state(server, force=True)
            self._push_ctrl({"type": "monitor_stopped"})
            self._log("Monitor stopped.", "warning")

    # =========================================================================
    # Heartbeat Loop
    # =========================================================================

    def _heartbeat_loop(self, notifier, server):
        """
        Lightweight loop -- no browser, no scraping.

        Each tick:
          1. Checks daily / session caps.
          2. Pushes a purchase_count update to the UI counter badge.
          3. Publishes a conn_state event IF (and only if) the connection state
             changed since the last tick.
          4. Sleeps _HEARTBEAT_SECONDS while remaining responsive to stop/pause.
        """
        while not self._stopped():

            # ── Pause gate ────────────────────────────────────────────────────
            self._wait_while_paused()
            if self._stopped():
                break

            # ── Read live settings ────────────────────────────────────────────
            sim_mode   = bool(self.settings.get("simulation_mode", True))
            max_daily  = int(self.settings.get("max_daily_purchases", 20))
            max_session = int(self.settings.get("max_session_purchases", 10))

            # ── Refresh purchase counter in the UI badge ──────────────────────
            today_count = self.db.get_today_purchase_count()
            self._push({
                "type":  "purchase_count",
                "count": today_count,
                "limit": max_daily,
            })

            # ── Connection state (edge-triggered, never level-triggered) ──────
            self._publish_conn_state(server)

            # ── Daily cap notification ────────────────────────────────────────
            if not sim_mode and today_count >= max_daily:
                self._log(
                    f"Daily limit reached ({today_count}/{max_daily}). "
                    "Extension will not purchase more leads until tomorrow.",
                    "error",
                )
                notifier.notify_limit_reached(max_daily)
                self._push({"type": "daily_limit_hit", "limit": max_daily})

            # ── Sleep, responsive to stop/pause ──────────────────────────────
            self._sleep(_HEARTBEAT_SECONDS)

    # =========================================================================
    # Helpers
    # =========================================================================

    def _publish_conn_state(self, server, force: bool = False):
        """
        Emit a conn_state event only when the state actually changed.

        Edge-triggered delivery is deliberate: the previous design pushed an
        "ext_connected" event on every single client poll, which both flooded
        the queue and caused the UI to re-arm a timer per event.
        """
        try:
            state, detail = server.compute_conn_state()
        except Exception as exc:               # pragma: no cover - defensive
            logger.debug("[Monitor] compute_conn_state failed: %s", exc)
            return

        if not force and state == self._last_conn_state:
            return

        self._last_conn_state = state
        self._push_ctrl({"type": "conn_state", "state": state, "detail": detail})
        logger.debug("[Monitor] conn_state -> %s (%s)", state, detail)

    def _log(self, msg: str, level: str = "info"):
        """Push a log event and mirror to the Python logger."""
        self._push({"type": "log", "msg": msg, "level": level})
        getattr(
            logger,
            level if level in ("info", "warning", "error", "debug") else "info"
        )(f"[Monitor] {msg}")

    def _push(self, event: Dict[str, Any]):
        """Non-blocking push to the UI queue; drops if full."""
        try:
            self.ui_queue.put_nowait(event)
        except queue.Full:
            logger.warning("[Monitor] UI queue full -- event dropped.")

    def _push_ctrl(self, event: Dict[str, Any]):
        """Push a critical state event to the unbounded control queue."""
        self.ctrl_queue.put_nowait(event)

    def _sleep(self, seconds: float):
        """
        Sleep for `seconds` while remaining responsive to stop() and pause().
        Wakes every 0.5 s to check both events.
        """
        end_time = time.monotonic() + seconds
        while time.monotonic() < end_time and not self._stopped():
            if self._pause_event.is_set():
                self._wait_while_paused()
            time.sleep(max(0.0, min(0.5, end_time - time.monotonic())))

    def _wait_while_paused(self):
        """
        Block in 1-second ticks while _pause_event is set.
        Exits when resume() clears _pause_event OR stop() sets _stop_event.
        """
        if not self._pause_event.is_set():
            return   # Fast path

        first_tick = True
        while self._pause_event.is_set() and not self._stopped():
            if first_tick:
                self._log(
                    "Monitor paused. Click Resume to continue.",
                    "warning",
                )
                self._push_ctrl({"type": "monitor_paused"})
                first_tick = False
            
            # Maintain connection state updates while paused
            if self.server:
                self._publish_conn_state(self.server)
                
            time.sleep(1.0)

        if not self._stopped() and not first_tick:
            self._log("Monitor resumed.", "success")
            self._push_ctrl({"type": "monitor_resumed"})
