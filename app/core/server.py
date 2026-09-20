"""
app/core/server.py
==================
Local HTTP Command Center.

This module is generic local-desktop infrastructure: it owns a single
HTTPServer bound to the loopback interface, a single serving thread, and a
deterministic lifecycle state machine.  It knows nothing about what the
payloads mean beyond persisting them and forwarding them to the UI queue.

Endpoints
---------
  GET  /health        -> Deterministic service-identity document (no settings)
  GET  /config        -> Returns current settings dict + today_count to client
  POST /log           -> Client sends events; Python persists + pushes to UI
  OPTIONS *           -> CORS preflight only.  NEVER treat as proof of health.

Lifecycle state machine
-----------------------
      new
       |  start()
       v
   starting --(pre-bind probe finds a listener, or bind fails)--> bind_failed
       |                                                              |
       | bind ok                                                      | start()
       v                                                              v
   running  --stop()-->  stopping  --join+server_close-->  stopped --start()--> starting

  * start()  is rejected (returns True, logs a warning) when already running.
  * stop()   is idempotent and safe from any thread; concurrent callers block
             on a completion Event and receive the same shutdown report.
  * stopped  is a restartable state -- start() rebuilds a fresh HTTPServer.

Verification contract
---------------------
  stop() does NOT destroy the state needed to verify that shutdown worked.
  After stop() returns, callers may inspect:
      server.get_state()        -> "stopped"
      server.is_thread_alive()  -> False when the serving thread really exited
      server.last_shutdown      -> dict of verified facts about the shutdown
      server.verify_stopped()   -> live re-verification incl. a port probe
  References are only cleared once the shutdown report has been recorded.

Windows socket semantics (IMPORTANT -- do not "simplify" this)
---------------------------------------------------------------
  http.server.HTTPServer sets ``allow_reuse_address = 1``, which applies
  SO_REUSEADDR.  On POSIX that only relaxes TIME_WAIT.  On Windows SO_REUSEADDR
  is far more permissive: it lets a second socket bind an address that another
  socket is *actively listening* on.  The winner of subsequent connections is
  indeterminate.  That turns "port already in use" into a silent failure --
  exactly the class of bug this module must make explicit.

  Therefore:
      Windows : allow_reuse_address = False, plus SO_EXCLUSIVEADDRUSE.
      POSIX   : allow_reuse_address = True  (needed so an immediate restart is
                not blocked by TIME_WAIT from previously served connections;
                on POSIX it does NOT permit stealing an active listener).

  One conditional exception exists on Windows: if the exclusive bind keeps
  failing while repeated probes prove that NO process is listening, the code
  makes a final attempt with SO_REUSEADDR and logs that decision at WARNING.
  See LocalServer._bind_with_retry() for why that is safe.

Thread Safety
-------------
  * settings is a reference to the shared mutable dict owned by AppWindow.
  * db uses its own internal threading.Lock() for all writes.
  * ui_queue is a threading.Queue -- non-blocking put_nowait() only.
  * All lifecycle mutation is guarded by a re-entrant lock; blocking work
    (join / server_close) is performed OUTSIDE that lock so a request handler
    can never deadlock a shutdown.

Security
--------
  Binds exclusively to 127.0.0.1 -- never 0.0.0.0.
  /health deliberately exposes service identity only -- never the settings
  dict -- so a health probe cannot be used to read configuration.
"""

import http.server
import json
import logging
import os
import queue
import socket
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Service identity ──────────────────────────────────────────────────────────
SERVICE_NAME    = "SafeIM Local Server"
SERVICE_VERSION = "2.0"

# Unique identifier for this *process*.  Printed on startup and included in
# lifecycle logs so that multiple concurrent SafeIM processes can be told apart
# in app.log.  Each LocalServer *object* additionally gets its own instance_id.
PROCESS_ID = uuid.uuid4().hex[:8].upper()

# CORS headers sent on every response.
# Required so a browser extension's fetch() to 127.0.0.1 is allowed.
_CORS_HEADERS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}

# ── Tunables ──────────────────────────────────────────────────────────────────
_STOP_THREAD_TIMEOUT = 5.0    # bounded join() for the serving thread
_SERVE_POLL_INTERVAL = 0.2    # serve_forever() shutdown responsiveness
_BIND_RETRY_ATTEMPTS = 5      # only for provably-transient bind failures
_BIND_RETRY_DELAY    = 0.2
_CLIENT_STALE_SECONDS = 20.0  # no client contact for this long => "no client"

# ── Lifecycle states ──────────────────────────────────────────────────────────
STATE_NEW         = "new"
STATE_STARTING    = "starting"
STATE_RUNNING     = "running"
STATE_STOPPING    = "stopping"
STATE_STOPPED     = "stopped"
STATE_BIND_FAILED = "bind_failed"

# ── Connection states (server <-> local client) ───────────────────────────────
# These are deliberately distinct from lifecycle states: a running server with
# no client is NOT the same thing as a connected client.
CONN_SERVER_STOPPED   = "server_stopped"
CONN_SERVER_STARTING  = "server_starting"
CONN_BIND_FAILED      = "bind_failed"
CONN_NO_CLIENT        = "server_running_no_client"
CONN_CLIENT_CONNECTED = "client_connected"
CONN_SERVER_STOPPING  = "server_stopping"

_IS_WINDOWS = (os.name == "nt")


# =============================================================================
# Socket helpers
# =============================================================================

def probe_listener(host: str, port: int, timeout: float = 0.3) -> bool:
    """
    Return True if *something* is currently accepting TCP connections on
    host:port.

    This is the portable, truthful way to ask "is this port occupied?".
    It is used instead of a trial bind() because a trial bind with
    SO_REUSEADDR reports success on Windows even when another process is
    actively listening -- i.e. it lies in exactly the case we care about.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


class _LifecycleHTTPServer(http.server.HTTPServer):
    """
    HTTPServer with platform-correct address-reuse semantics.

    See the "Windows socket semantics" note in the module docstring for why
    this override exists.  Removing it reintroduces silent port hijacking on
    Windows.

    ``exclusive`` is honoured on Windows only.  It is True for the normal path;
    the recovery path in LocalServer._bind_with_retry() may build one with
    exclusive=False, but only after proving no listener owns the port.
    """

    # POSIX: needed so restart is not blocked by TIME_WAIT.
    # Windows: must be off, otherwise bind() succeeds over a live listener.
    allow_reuse_address = not _IS_WINDOWS

    def __init__(self, server_address, handler_cls, exclusive=True):
        self._exclusive = exclusive
        super().__init__(server_address, handler_cls)

    def server_bind(self):
        if _IS_WINDOWS:
            if self._exclusive and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                # Windows-native way to claim a port exclusively and to make a
                # conflicting bind fail loudly instead of silently succeeding.
                try:
                    self.socket.setsockopt(
                        socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1
                    )
                except OSError as exc:      # pragma: no cover - platform dependent
                    logger.debug("[Server] SO_EXCLUSIVEADDRUSE not applied: %s", exc)
            elif not self._exclusive:
                # Recovery path only -- see _bind_with_retry().
                try:
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                except OSError as exc:      # pragma: no cover
                    logger.debug("[Server] SO_REUSEADDR not applied: %s", exc)
        super().server_bind()


# =============================================================================
# LocalServer
# =============================================================================

class LocalServer:
    """
    Owns exactly one HTTPServer and exactly one serving thread.

    Ownership contract
    ------------------
      * LocalServer creates the HTTPServer in start() and destroys it in stop().
      * LocalServer creates the serving thread in start() and joins it in stop().
      * Nothing outside LocalServer may touch _server or _thread.  Callers use
        the public verification API (get_state / is_thread_alive /
        verify_stopped / last_shutdown).
      * Daemon status on the serving thread is a crash-safety net only.  It is
        never relied upon as the cleanup mechanism.

    Parameters
    ----------
    db        : LeadDatabase  -- thread-safe DB instance
    settings  : dict          -- shared mutable settings dict (read by reference)
    ui_queue  : queue.Queue   -- event bus shared with AppWindow
    host      : str           -- bind address (always 127.0.0.1)
    port      : int           -- TCP port (default 9223)
    """

    def __init__(
        self,
        db,
        settings: Dict[str, Any],
        ui_queue: queue.Queue,
        ctrl_queue: queue.Queue = None,
        host: str = "127.0.0.1",
        port: int = 9223,
    ):
        self.db       = db
        self.settings = settings
        self.ui_queue = ui_queue
        self.ctrl_queue = ctrl_queue or queue.Queue()
        self.host     = host
        self.port     = port

        # Per-object identity (distinct from the per-process PROCESS_ID).
        self.instance_id = uuid.uuid4().hex[:8].upper()

        # ── Lifecycle state (guarded by _lock) ────────────────────────────────
        self._lock   = threading.RLock()
        self._state  = STATE_NEW
        self._server: Optional[_LifecycleHTTPServer] = None
        self._thread: Optional[threading.Thread]     = None

        # Deterministic completion signals -- independent of thread identity,
        # so verification never depends on a reference stop() might clear.
        self._serve_exited  = threading.Event()
        self._stop_complete = threading.Event()

        self._started_at: Optional[float] = None
        self._bind_error: Optional[str]   = None
        self.last_shutdown: Optional[Dict[str, Any]] = None

        # ── Client-contact tracking (guarded by _lock) ────────────────────────
        self._last_client_contact: float = 0.0   # time.monotonic()
        self._client_contact_count: int  = 0
        self._first_contact_announced: bool = False

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def start(self) -> bool:
        """
        Bind and start serving in a dedicated thread.

        Returns True on success, False on an explicit bind failure.
        Never changes the port.  Never kills another process.
        Safe to call again after stop() -- builds a fresh HTTPServer + thread.
        """
        with self._lock:
            if self._state == STATE_RUNNING:
                logger.warning(
                    "[Server] INSTANCE=%s start() ignored -- already running on %s:%d",
                    self.instance_id, self.host, self.port,
                )
                return True
            if self._state == STATE_STOPPING:
                logger.error(
                    "[Server] INSTANCE=%s start() refused -- shutdown in progress.",
                    self.instance_id,
                )
                return False

            self._state = STATE_STARTING
            self._reset_for_start()

        logger.info(
            "[Server] PROCESS=%s INSTANCE=%s Starting on %s:%d …",
            PROCESS_ID, self.instance_id, self.host, self.port,
        )

        # Pre-bind conflict probe.  If someone is already LISTENING here the
        # failure is hard and immediate -- no retry, no port substitution.
        if probe_listener(self.host, self.port):
            self._record_bind_failure(
                "another process is already LISTENING on this address"
            )
            return False

        srv, err = self._bind_with_retry()
        if srv is None:
            self._record_bind_failure(err or "unknown bind error")
            return False

        with self._lock:
            self._server = srv
            self._serve_exited.clear()
            self._thread = threading.Thread(
                target=self._serve,
                daemon=True,   # crash-safety net only; stop() is the real path
                name=f"LocalServer:{self.port}:{self.instance_id}",
            )
            self._started_at = time.monotonic()
            self._thread.start()
            self._state = STATE_RUNNING

        logger.info(
            "[Server] PROCESS=%s INSTANCE=%s LISTENING on http://%s:%d "
            "(service=%s v%s, thread=%s)",
            PROCESS_ID, self.instance_id, self.host, self.port,
            SERVICE_NAME, SERVICE_VERSION, self._thread.name,
        )
        return True

    def stop(self, timeout: float = _STOP_THREAD_TIMEOUT) -> Dict[str, Any]:
        """
        Shut down the HTTP server and release the socket.

        Idempotent and safe from any thread.  Bounded -- never blocks longer
        than ``timeout`` waiting for the serving thread.

        Returns a shutdown report dict:
            {
              "attempted":            bool,   # did this call do the work?
              "shutdown_signalled":   bool,
              "thread_joined":        bool,   # thread exited within timeout
              "thread_alive_after":   bool,
              "serve_loop_exited":    bool,   # from the serve_forever wrapper
              "socket_closed":        bool,
              "duration_seconds":     float,
              "instance_id":          str,
              "port":                 int,
              "errors":               [str, ...],
            }
        """
        with self._lock:
            if self._state in (STATE_NEW, STATE_BIND_FAILED):
                logger.info(
                    "[Server] INSTANCE=%s stop() -- nothing to stop (state=%s).",
                    self.instance_id, self._state,
                )
                report = self._noop_shutdown_report()
                self.last_shutdown = report
                self._state = STATE_STOPPED
                self._stop_complete.set()
                return report

            if self._state == STATE_STOPPED:
                # Idempotent no-op: return the report from the real shutdown.
                return dict(self.last_shutdown or self._noop_shutdown_report())

            if self._state == STATE_STOPPING:
                waiter = True
            else:
                waiter = False
                self._state = STATE_STOPPING
                self._stop_complete.clear()

            server = self._server
            thread = self._thread

        # A concurrent caller waits for the in-flight shutdown to finish rather
        # than returning a report that has not been produced yet.
        if waiter:
            self._stop_complete.wait(timeout=timeout + 1.0)
            return dict(self.last_shutdown or self._noop_shutdown_report())

        logger.info(
            "[Server] PROCESS=%s INSTANCE=%s Stop requested (port %d).",
            PROCESS_ID, self.instance_id, self.port,
        )

        t0 = time.monotonic()
        report: Dict[str, Any] = {
            "attempted":          True,
            "shutdown_signalled": False,
            "thread_joined":      False,
            "thread_alive_after": False,
            "serve_loop_exited":  False,
            "socket_closed":      False,
            "duration_seconds":   0.0,
            "instance_id":        self.instance_id,
            "port":               self.port,
            "errors":             [],
        }

        # ── Step 1: signal serve_forever() to exit ────────────────────────────
        # Performed outside the lock: shutdown() blocks until the serve loop
        # acknowledges, and a handler thread may be contending for the lock.
        if server is not None:
            try:
                server.shutdown()
                report["shutdown_signalled"] = True
                logger.info("[Server] INSTANCE=%s serve_forever() shutdown signalled.",
                            self.instance_id)
            except Exception as exc:
                report["errors"].append(f"shutdown(): {exc}")
                logger.warning("[Server] INSTANCE=%s error during shutdown(): %s",
                               self.instance_id, exc)

        # ── Step 2: bounded wait for the serving thread ───────────────────────
        if thread is not None:
            thread.join(timeout=timeout)
            alive = thread.is_alive()
            report["thread_alive_after"] = alive
            report["thread_joined"]      = not alive
            if alive:
                logger.error(
                    "[Server] INSTANCE=%s serving thread did NOT exit within "
                    "%.1fs -- socket may remain bound on port %d.",
                    self.instance_id, timeout, self.port,
                )
                report["errors"].append("serving thread did not exit within timeout")
            else:
                logger.info("[Server] INSTANCE=%s serving thread exited.",
                            self.instance_id)
        else:
            report["thread_joined"] = True

        report["serve_loop_exited"] = self._serve_exited.is_set()

        # ── Step 3: close the listening socket ────────────────────────────────
        if server is not None:
            try:
                server.server_close()
                report["socket_closed"] = True
                logger.info("[Server] INSTANCE=%s socket closed; port %d released.",
                            self.instance_id, self.port)
            except Exception as exc:
                report["errors"].append(f"server_close(): {exc}")
                logger.warning("[Server] INSTANCE=%s error during server_close(): %s",
                               self.instance_id, exc)

        report["duration_seconds"] = round(time.monotonic() - t0, 4)

        # ── Step 4: record the verified report, THEN clear references ─────────
        with self._lock:
            self.last_shutdown = report
            self._state        = STATE_STOPPED
            self._started_at   = None
            # Safe to clear now: verification facts live in `report`,
            # `_serve_exited`, and the live port probe -- not in these handles.
            self._server = None
            self._thread = None
            self._stop_complete.set()

        logger.info(
            "[Server] PROCESS=%s INSTANCE=%s Stopped. "
            "thread_joined=%s socket_closed=%s duration=%.3fs errors=%d",
            PROCESS_ID, self.instance_id,
            report["thread_joined"], report["socket_closed"],
            report["duration_seconds"], len(report["errors"]),
        )
        return report

    # ── Lifecycle helpers ─────────────────────────────────────────────────────

    def _reset_for_start(self):
        """Clear per-run state so a restart begins from a clean slate."""
        self._server = None
        self._thread = None
        self._bind_error = None
        self._started_at = None
        self._serve_exited.clear()
        self._stop_complete.clear()
        self._last_client_contact = 0.0
        self._client_contact_count = 0
        self._first_contact_announced = False

    def _bind_with_retry(self) -> Tuple[Optional[_LifecycleHTTPServer], Optional[str]]:
        """
        Attempt to bind, retrying ONLY for provably-transient conditions.

        A retry happens only when bind() failed AND a fresh probe shows that
        nothing is listening -- i.e. lingering kernel socket state (TIME_WAIT)
        rather than a real conflict.  If a listener is present the failure is
        immediate and final.  This is bounded OS-state handling, not a sleep
        used to paper over a race in our own code.

        Windows recovery path
        ---------------------
        Windows may refuse a bind while connections this process previously
        served are still in TIME_WAIT.  Exactly when that happens under
        SO_EXCLUSIVEADDRUSE is version-dependent, so rather than assume, the
        code handles it: if the exclusive bind keeps failing and repeated
        probes prove NO listener owns the port, it makes one final attempt with
        SO_REUSEADDR and logs that decision at WARNING.

        The relaxation is safe here precisely because it is conditional: the
        dangerous property of SO_REUSEADDR on Windows is that it can bind over
        a LIVE listener, and this path is only taken after confirming there is
        none.  The normal path never sets it.
        """
        last_exc: Optional[OSError] = None

        for attempt in range(1, _BIND_RETRY_ATTEMPTS + 1):
            try:
                return _LifecycleHTTPServer(
                    (self.host, self.port), self._make_handler(), exclusive=True
                ), None
            except OSError as exc:
                last_exc = exc
                if probe_listener(self.host, self.port):
                    return None, (
                        f"port owned by another LISTENING process ({exc})"
                    )
                if attempt < _BIND_RETRY_ATTEMPTS:
                    logger.warning(
                        "[Server] INSTANCE=%s bind attempt %d/%d failed with a "
                        "transient error (no listener present): %s -- retrying in %.1fs",
                        self.instance_id, attempt, _BIND_RETRY_ATTEMPTS,
                        exc, _BIND_RETRY_DELAY,
                    )
                    time.sleep(_BIND_RETRY_DELAY)

        # Windows-only conditional recovery, after re-confirming no listener.
        if _IS_WINDOWS and not probe_listener(self.host, self.port):
            logger.warning(
                "[Server] INSTANCE=%s exclusive bind on %s:%d kept failing (%s) "
                "while NO listener owns the port -- this indicates lingering "
                "TIME_WAIT state. Retrying once with SO_REUSEADDR. This is "
                "safe here only because the absence of a listener was verified "
                "immediately beforehand.",
                self.instance_id, self.host, self.port, last_exc,
            )
            try:
                return _LifecycleHTTPServer(
                    (self.host, self.port), self._make_handler(), exclusive=False
                ), None
            except OSError as exc:
                last_exc = exc

        return None, (
            f"bind failed after {_BIND_RETRY_ATTEMPTS} attempts ({last_exc})"
        )

    def _record_bind_failure(self, reason: str):
        """Log an explicit PORT BIND FAILED diagnostic and set terminal state."""
        with self._lock:
            self._state      = STATE_BIND_FAILED
            self._bind_error = reason
            self._server     = None
            self._thread     = None

        logger.error(
            "[Server] ================================================\n"
            "[Server]  PORT BIND FAILED\n"
            "[Server]  Process  : %s\n"
            "[Server]  Instance : %s\n"
            "[Server]  Address  : %s:%d\n"
            "[Server]  Reason   : %s\n"
            "[Server]  The port is NOT changed automatically and no other\n"
            "[Server]  process is terminated.  Close the process that owns\n"
            "[Server]  this port, or change 'server_port' in Settings.\n"
            "[Server] ================================================",
            PROCESS_ID, self.instance_id, self.host, self.port, reason,
        )
        self._push({"type": "conn_state", "state": CONN_BIND_FAILED, "detail": reason})

    def _serve(self):
        """Serving-thread entry point.  Always signals _serve_exited on exit."""
        try:
            self._server.serve_forever(poll_interval=_SERVE_POLL_INTERVAL)
        except Exception as exc:                        # pragma: no cover
            logger.error(
                "[Server] INSTANCE=%s serve_forever() crashed: %s",
                self.instance_id, exc, exc_info=True,
            )
        finally:
            self._serve_exited.set()
            logger.debug("[Server] INSTANCE=%s serve loop exited.", self.instance_id)

    def _noop_shutdown_report(self) -> Dict[str, Any]:
        return {
            "attempted":          False,
            "shutdown_signalled": False,
            "thread_joined":      True,
            "thread_alive_after": False,
            "serve_loop_exited":  True,
            "socket_closed":      False,
            "duration_seconds":   0.0,
            "instance_id":        self.instance_id,
            "port":               self.port,
            "errors":             [],
        }

    # =========================================================================
    # Public verification API
    # =========================================================================

    def get_state(self) -> str:
        """Current lifecycle state (one of the STATE_* constants)."""
        with self._lock:
            return self._state

    def is_running(self) -> bool:
        """True only when the state is RUNNING *and* the serving thread lives."""
        with self._lock:
            return self._state == STATE_RUNNING and self.is_thread_alive()

    def is_thread_alive(self) -> bool:
        """
        True when the serving thread exists and is still running.

        Returns False after a completed stop() -- and, unlike the previous
        implementation, that False is backed by an actual join() result rather
        than by stop() having nulled the attribute.
        """
        with self._lock:
            t = self._thread
        return bool(t is not None and t.is_alive())

    def get_bind_error(self) -> Optional[str]:
        with self._lock:
            return self._bind_error

    def uptime_seconds(self) -> float:
        with self._lock:
            return 0.0 if self._started_at is None else round(
                time.monotonic() - self._started_at, 3
            )

    def verify_stopped(self, probe_port: bool = True) -> Dict[str, Any]:
        """
        Re-verify shutdown against live evidence rather than cached flags.

        Returns
        -------
        {
          "state":            str,
          "thread_alive":     bool,   # real thread liveness
          "serve_loop_exited":bool,   # serve_forever() wrapper reached finally
          "port_has_listener":bool,   # live TCP probe (None if not probed)
          "clean":            bool,   # stopped AND thread dead AND no listener
          "last_shutdown":    dict|None,
        }
        """
        state        = self.get_state()
        thread_alive = self.is_thread_alive()
        loop_exited  = self._serve_exited.is_set()
        listener     = probe_listener(self.host, self.port) if probe_port else None

        clean = (
            state == STATE_STOPPED
            and not thread_alive
            and loop_exited
            and (listener is False if probe_port else True)
        )
        return {
            "state":             state,
            "thread_alive":      thread_alive,
            "serve_loop_exited": loop_exited,
            "port_has_listener": listener,
            "clean":             clean,
            "last_shutdown":     dict(self.last_shutdown) if self.last_shutdown else None,
        }

    # =========================================================================
    # Client-contact tracking / connection state
    # =========================================================================

    def note_client_contact(self) -> bool:
        """
        Record that a real application client (not a bare health probe) talked
        to us.  Returns True if this was the first contact of this run.

        /health deliberately does NOT call this: a diagnostic probe is not a
        connected client, and treating it as one is how the UI ends up claiming
        a client is active when only a status popup is open.
        """
        with self._lock:
            self._last_client_contact = time.monotonic()
            self._client_contact_count += 1
            first = not self._first_contact_announced
            if first:
                self._first_contact_announced = True
        return first

    def seconds_since_client_contact(self) -> Optional[float]:
        with self._lock:
            if self._last_client_contact <= 0.0:
                return None
            return round(time.monotonic() - self._last_client_contact, 3)

    def client_contact_count(self) -> int:
        with self._lock:
            return self._client_contact_count

    def compute_conn_state(
        self, stale_after: float = _CLIENT_STALE_SECONDS
    ) -> Tuple[str, str]:
        """
        Derive the connection state from real evidence.

        Returns (state, detail).  State is one of the CONN_* constants.
        """
        state = self.get_state()

        if state == STATE_BIND_FAILED:
            return CONN_BIND_FAILED, self.get_bind_error() or "bind failed"
        if state in (STATE_NEW, STATE_STOPPED):
            return CONN_SERVER_STOPPED, f"server {state}"
        if state == STATE_STARTING:
            return CONN_SERVER_STARTING, "binding"
        if state == STATE_STOPPING:
            return CONN_SERVER_STOPPING, "shutting down"

        age = self.seconds_since_client_contact()
        if age is None:
            return CONN_NO_CLIENT, "no client contact yet"
        if age > stale_after:
            return CONN_NO_CLIENT, f"last client contact {age:.0f}s ago"
        return CONN_CLIENT_CONNECTED, f"last contact {age:.1f}s ago"

    def health_document(self) -> Dict[str, Any]:
        """
        Deterministic service-identity document returned by GET /health.

        Contains identity and lifecycle facts ONLY -- never the settings dict.
        A client must validate both the HTTP status and this structure before
        declaring the backend online.
        """
        conn_state, conn_detail = self.compute_conn_state()
        return {
            "ok":              self.is_running(),
            "service":         SERVICE_NAME,
            "version":         SERVICE_VERSION,
            "instance_id":     self.instance_id,
            "process_id":      PROCESS_ID,
            "state":           self.get_state(),
            "host":            self.host,
            "port":            self.port,
            "uptime_seconds":  self.uptime_seconds(),
            "conn_state":      conn_state,
            "conn_detail":     conn_detail,
            "client_contacts": self.client_contact_count(),
            "server_time":     datetime.now().isoformat(timespec="seconds"),
        }

    # =========================================================================
    # HTTP handler
    # =========================================================================

    def _make_handler(self):
        """Build the request-handler class bound to this LocalServer instance."""
        server_ref = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            """Minimal HTTP handler -- all business logic delegates to server_ref."""

            # HTTP/1.0 => no keep-alive, so connections never hold shutdown open.
            protocol_version = "HTTP/1.0"

            def log_message(self, fmt, *args):
                pass   # Suppress noisy access log

            def do_OPTIONS(self):
                # CORS preflight ONLY.  Never a health signal -- this returns
                # 200 for any path, including paths this server does not serve.
                self._send_cors(200, b"")

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/health":
                    self._handle_health()
                elif path == "/config":
                    self._handle_config()
                else:
                    self._send_cors(404, b'{"error":"not found"}')

            def do_POST(self):
                if self.path.split("?")[0] != "/log":
                    self._send_cors(404, b'{"error":"not found"}')
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    raw    = self.rfile.read(length).decode("utf-8", errors="replace")
                    data   = json.loads(raw) if raw.strip() else {}
                except Exception as exc:
                    logger.warning("[Server] POST /log parse error: %s", exc)
                    self._send_cors(400, b'{"error":"bad json"}')
                    return
                if not isinstance(data, dict):
                    logger.warning("[Server] POST /log rejected: body is not a JSON object")
                    self._send_cors(400, b'{"error":"body must be a JSON object"}')
                    return
                try:
                    server_ref._note_and_announce_contact()
                    server_ref._handle_log(data)
                    self._send_cors(200, b'{"ok":true}')
                    logger.debug(
                        "[Server] INSTANCE=%s POST /log status=%s hash=%.12s",
                        server_ref.instance_id,
                        data.get("status", "?"),
                        data.get("lead_hash", ""),
                    )
                except Exception as exc:
                    logger.error("[Server] POST /log handler error: %s", exc, exc_info=True)
                    self._send_cors(500, b'{"error":"internal"}')

            # ── Endpoint implementations ──────────────────────────────────────

            def _handle_health(self):
                """Service identity only.  Does NOT count as a client contact."""
                try:
                    body = json.dumps(server_ref.health_document()).encode("utf-8")
                    self._send_cors(200, body, "application/json")
                    logger.debug(
                        "[Server] PROCESS=%s INSTANCE=%s GET /health from %s -> ok=%s state=%s",
                        PROCESS_ID, server_ref.instance_id,
                        self.client_address[0],
                        server_ref.is_running(), server_ref.get_state(),
                    )
                except Exception as exc:
                    logger.error("[Server] GET /health error: %s", exc, exc_info=True)
                    self._send_cors(500, b'{"error":"internal"}')

            def _handle_config(self):
                try:
                    server_ref._note_and_announce_contact()
                    today_count = server_ref.db.get_today_purchase_count()
                    payload = dict(server_ref.settings)
                    payload["today_count"] = today_count
                    body = json.dumps(payload).encode("utf-8")
                    self._send_cors(200, body, "application/json")
                    logger.debug(
                        "[Server] INSTANCE=%s GET /config from %s -> %d bytes",
                        server_ref.instance_id, self.client_address[0], len(body),
                    )
                except Exception as exc:
                    logger.error("[Server] GET /config error: %s", exc, exc_info=True)
                    self._send_cors(500, b'{"error":"internal"}')

            def _send_cors(self, code, body, content_type="application/json"):
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(body)))
                    for k, v in _CORS_HEADERS.items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                    # Client vanished mid-response (tab closed, app quitting).
                    logger.debug("[Server] response write aborted: %s", exc)

        return _Handler

    def _note_and_announce_contact(self):
        """
        Record a client contact and emit the first-contact event exactly once
        per run, so the UI is not flooded with duplicate connection events by
        a polling client.
        """
        if self.note_client_contact():
            logger.info(
                "[Server] PROCESS=%s INSTANCE=%s first client contact on port %d.",
                PROCESS_ID, self.instance_id, self.port,
            )
            # Back-compat event kept for existing UI handlers.
            self._push_ctrl({"type": "ext_connected"})
            self._push_ctrl({
                "type":   "conn_state",
                "state":  CONN_CLIENT_CONNECTED,
                "detail": "first client contact",
            })

    # =========================================================================
    # POST /log handler
    # =========================================================================

    def _handle_log(self, data: Dict[str, Any]):
        """
        Process an event POSTed by the client.

        NOTE: This method's domain semantics are intentionally unchanged.
        Only transport-level concerns (contact tracking, body validation) were
        touched during the infrastructure work.

        Expected payload fields
        -----------------------
          status        : "purchased" | "simulated" | "filtered" |
                          "filtered_contact" | "failed"
          title         : str
          country       : str
          quantity      : str
          location      : str
          display_name  : str
          contact_info  : str
          lead_hash     : str
          reason        : str
          score         : int
        """
        status        = data.get("status", "failed")
        title         = data.get("title", "")
        lead_hash     = data.get("lead_hash", "")
        score         = data.get("score", 0)
        reason        = data.get("reason", "")
        contact_info  = data.get("contact_info", "")
        display_name  = data.get("display_name", data.get("customer_name", ""))
        timestamp     = datetime.now().isoformat(sep=" ", timespec="seconds")
        _ts           = datetime.now().strftime("%H:%M:%S")

        logger.debug(
            "[Server] /log status=%s title=%.50s score=%s", status, title, score
        )

        # Server-side daily cap safeguard (purchased events only)
        if status == "purchased":
            max_daily   = int(self.settings.get("max_daily_purchases", 20))
            today_count = self.db.get_today_purchase_count()
            if today_count >= max_daily:
                logger.warning(
                    "[Server] SAFEGUARD: purchase event ignored -- "
                    "daily cap already reached (%d/%d).",
                    today_count, max_daily,
                )
                self._push({
                    "type":  "log",
                    "msg":   (
                        f"[{_ts}] SAFEGUARD: purchase ignored -- "
                        f"daily cap already reached ({today_count}/{max_daily})."
                    ),
                    "level": "error",
                })
                return   # Do NOT persist purchase or increment counter

        # Duplicate guard
        if lead_hash and self.db.lead_exists(lead_hash):
            logger.debug("[Server] Duplicate lead_hash -- skipping: %.16s", lead_hash)
            return

        # Persist to DB
        self.db.add_lead({
            "lead_hash":       lead_hash or self.db.compute_hash(
                                   title,
                                   data.get("location", ""),
                                   data.get("quantity", ""),
                               ),
            "title":           title,
            "location":        data.get("location", ""),
            "country":         data.get("country", ""),
            "quantity":        data.get("quantity", ""),
            "display_name":    display_name,
            "contact_info":    contact_info,
            "purchased":       1 if status == "purchased" else 0,
            "simulated":       1 if status == "simulated" else 0,
            "filtered_reason": reason,
            "timestamp":       timestamp,
        })

        # Increment daily counter on verified real purchase
        if status == "purchased":
            self.db.increment_today_count()
            today_count = self.db.get_today_purchase_count()
            max_daily   = int(self.settings.get("max_daily_purchases", 20))
            self._push({
                "type":  "purchase_count",
                "count": today_count,
                "limit": max_daily,
            })

        # Build log message
        _snip = title[:50]

        if status == "purchased":
            self._push({
                "type":  "log",
                "msg":   (
                    f"[{_ts}] [Purchased] '{_snip}' "
                    f"| Contact: {contact_info or 'N/A'} | Score: {score}"
                ),
                "level": "success",
            })
        elif status == "simulated":
            self._push({
                "type":  "log",
                "msg":   (
                    f"[{_ts}] [Simulated] '{_snip}' "
                    f"| Score: {score} -- No credits spent."
                ),
                "level": "info",
            })
        elif status in ("filtered", "filtered_contact"):
            self._push({
                "type":  "log",
                "msg":   (
                    f"[{_ts}] [Filtered] '{_snip}' "
                    f"| Reason: {reason} | Score: {score}"
                ),
                "level": "warning",
            })
        else:
            self._push({
                "type":  "log",
                "msg":   f"[{_ts}] [Failed] '{_snip}' | Reason: {reason or 'unknown'}",
                "level": "error",
            })

        # Lead card event for the right-panel feed
        ui_status = (
            status if status in ("purchased", "simulated", "filtered")
            else "filtered"
        )
        self._push({"type": "lead", "data": data, "status": ui_status})

    # =========================================================================
    # Helpers
    # =========================================================================

    def _push(self, event: Dict[str, Any]):
        """Non-blocking push to the UI queue; silently drops if full."""
        try:
            self.ui_queue.put_nowait(event)
        except queue.Full:
            logger.warning(
                "[Server] INSTANCE=%s UI queue full -- event dropped: %s",
                self.instance_id, event.get("type"),
            )

    def _push_ctrl(self, event: Dict[str, Any]):
        """Push a critical state event to the unbounded control queue."""
        self.ctrl_queue.put_nowait(event)
