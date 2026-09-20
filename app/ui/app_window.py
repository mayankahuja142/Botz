"""
app/ui/app_window.py
====================
Root CustomTkinter window for SafeIM Lead Monitor V2.

Responsibilities:
  ① Display the prominent full-width Mode Banner (the centrepiece safety feature):
       • 🟡 SIMULATION — bold amber banner, static.
       • 🔴 LIVE MODE  — bold red banner, blinking at 700 ms intervals.
       • 🟠 SESSION EXPIRED — orange warning banner, auto-restores after 10 s.

  ② Host the three-tab CTkTabview:
       📡 Monitor  |  ⚙️ Settings  |  📋 History

  ③ Manage the MonitorThread lifecycle (▶ Start / ⏸️ Pause / ⏹ Stop buttons).

  ④ Drain the thread-safe ui_queue every 100 ms and dispatch events to tabs.
     All UI state changes happen on the main thread via this drain loop,
     which is the only thread-safe way to update Tkinter widgets.

  ⑤ Own the shutdown sequence (WM_DELETE_WINDOW): cancel every scheduled Tk
     callback, stop the monitor thread, verify the HTTP server released its
     port, close the database, then destroy the window — in that order.

Event types consumed from ui_queue:
  "log"                → MonitorTab.append_log()
  "lead"               → MonitorTab.add_lead_card() + HistoryTab.refresh()
  "purchase_count"     → MonitorTab.update_counter()
  "daily_limit_hit"    → MonitorTab.append_log()
  "session_expired"    → banner switch to SESSION EXPIRED + MonitorTab.append_log()
  "ext_connected"      → green status dot (emitted once per server run)
  "conn_state"         → MonitorTab.set_conn_state() + status bar text
  "browser_context_lost" → reset all buttons, log message
  "monitor_paused"     → switch pause_btn to ▶️ Resume style (amber)
  "monitor_resumed"    → switch pause_btn back to ⏸️ Pause style
  "monitor_stopped"    → reset all buttons, update status bar

Resource ownership
------------------
  AppWindow owns: the Tk window, the ui_queue, the after() job registry,
                  the MonitorThread object, and the LeadDatabase handle.
  AppWindow does NOT own the HTTP server — MonitorThread does.  The UI only
  reads the server's public verification API through the monitor thread.
"""

import _tkinter
import logging
import queue
from pathlib import Path
from typing import Any, Dict

import customtkinter as ctk

from app.ui.monitor_tab  import MonitorTab
from app.ui.settings_tab import SettingsTab
from app.ui.history_tab  import HistoryTab

logger = logging.getLogger(__name__)

# =============================================================================
# Banner Configuration Constants
# =============================================================================

_BANNER_SIM = {
    "text": "🟡  SIMULATION MODE  —  Safe testing active. No IndiaMART credits will be spent.",
    "bg":   "#6b4c00",
    "fg":   "#ffe066",
}
_BANNER_LIVE_TEXT = (
    "🔴  ⚠  LIVE PURCHASE MODE  ⚠  —  "
    "Real IndiaMART credits ARE being spent on every matched lead."
)
_BANNER_LIVE_BG_A = "#bb0000"    # Bright red (blink state A)
_BANNER_LIVE_BG_B = "#770000"    # Dark red   (blink state B)
_BANNER_LIVE_FG   = "#ffffff"

_BANNER_SESSION = {
    "text": "⚠️  SESSION EXPIRED  —  IndiaMART login expired. Open the browser to log back in.",
    "bg":   "#7c3200",
    "fg":   "#ffcc88",
}

_BLINK_INTERVAL_MS = 700         # Banner blink period for Live mode


class AppWindow(ctk.CTk):
    """
    Main application window.

    Constructor parameters:
        db            : LeadDatabase  — shared database instance
        settings      : dict          — shared mutable settings dict
        settings_path : Path          — path to config/settings.json on disk
    """

    def __init__(self, db, settings: Dict[str, Any], settings_path: Path):
        super().__init__()

        self.db            = db
        self.settings      = settings
        self.settings_path = settings_path

        # Thread communication
        self.ui_queue: queue.Queue = queue.Queue(maxsize=600)
        self.ctrl_queue: queue.Queue = queue.Queue()

        # Monitor thread state
        self._monitor_thread  = None
        self._monitor_running = False
        self._monitor_paused  = False   # Mirrors the thread's pause_event

        # Banner blink state
        self._blink_state    = False
        self._blink_job      = None
        self._session_banner = False   # True while session-expired banner is showing

        # ── Tk callback lifecycle ─────────────────────────────────────────────
        # EVERY after() call in this window is registered here by key so that
        # (a) re-scheduling a timer replaces the old one instead of stacking a
        #     new one on top of it, and
        # (b) _on_close() can cancel all of them before destroy(), so no
        #     callback can ever fire against a destroyed widget.
        self._after_jobs: Dict[str, str] = {}

        # Shutdown guards — prevent re-entrant close calls.
        # _closing  : set the moment shutdown begins (blocks new work)
        # _closed   : set once _finish_close() has actually run (idempotency)
        self._closing = False
        self._closed  = False

        self._setup_window()
        self._build_ui()
        self._update_banner()

        # Intercept the OS close button (✕) so we can shut down cleanly.
        # Without this, Tk calls destroy() immediately, killing daemon threads
        # (MonitorThread + LocalServer) without releasing the TCP socket,
        # which causes a port-bind failure on the next restart.
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Start the 100 ms queue drain loop
        self._schedule("drain", 100, self._drain_queue)

        # Auto-start the monitor as soon as the window has fully rendered.
        # 500 ms gives Tkinter one full event-loop pass to paint all widgets
        # before _start_monitor mutates button states — identical to the user
        # clicking ▶ Start manually.
        self._schedule("autostart", 500, self._start_monitor)


    # =========================================================================
    # Tk callback lifecycle
    # =========================================================================

    def _schedule(self, key: str, delay_ms: int, callback) -> None:
        """
        Schedule `callback` after `delay_ms`, replacing any pending job that
        was registered under the same `key`.

        Using a keyed registry instead of bare self.after() fixes two concrete
        defects:
          * Timer accumulation — a repeating event (e.g. a client polling the
            server) previously queued one new 5-second status-reset timer per
            event, so hundreds of identical callbacks could be pending at once.
          * Callbacks surviving shutdown — an after() job with no stored id
            cannot be cancelled, so it may fire after destroy() and raise
            TclError ("invalid command name").
        """
        if self._closing:
            return
        self._cancel(key)
        try:
            self._after_jobs[key] = self.after(
                delay_ms, lambda: self._run_job(key, callback)
            )
        except _tkinter.TclError as exc:
            # Interpreter already torn down — nothing left to schedule against.
            logger.debug("[AppWindow] schedule(%s) skipped: %s", key, exc)

    def _run_job(self, key: str, callback) -> None:
        """Wrapper that de-registers a job before running it."""
        self._after_jobs.pop(key, None)
        if self._closing and key != "shutdown_poll":
            # Shutdown started between scheduling and firing — drop the work.
            return
        callback()

    def _cancel(self, key: str) -> None:
        """Cancel one pending job by key, if present."""
        job_id = self._after_jobs.pop(key, None)
        if job_id is None:
            return
        try:
            self.after_cancel(job_id)
        except (_tkinter.TclError, ValueError) as exc:
            logger.debug("[AppWindow] after_cancel(%s) ignored: %s", key, exc)

    def _cancel_all_jobs(self) -> int:
        """
        Cancel every pending after() job.  Returns how many were cancelled.
        Called during shutdown so no Tk callback can outlive the widgets.
        """
        keys = list(self._after_jobs.keys())
        for key in keys:
            self._cancel(key)

        # The blink loop predates the registry and stores its own id.
        if self._blink_job is not None:
            try:
                self.after_cancel(self._blink_job)
            except (_tkinter.TclError, ValueError):
                pass
            self._blink_job = None

        if keys:
            logger.info("[AppWindow] Cancelled %d pending Tk callback(s): %s",
                        len(keys), ", ".join(keys))
        return len(keys)


    # =========================================================================
    # Window Setup
    # =========================================================================

    def _setup_window(self):
        self.title("SafeIM Lead Monitor — V2")
        self.geometry("1120x740")
        self.minsize(920, 600)
        ctk.set_appearance_mode("dark")

    # =========================================================================
    # UI Construction
    # =========================================================================

    def _build_ui(self):
        # ── Header bar ────────────────────────────────────────────────────────
        header = ctk.CTkFrame(
            self, fg_color=("#0a0a1c", "#0a0a1c"), height=56, corner_radius=0
        )
        header.pack(fill="x")
        header.pack_propagate(False)

        # App title
        ctk.CTkLabel(
            header,
            text="🛡️  SafeIM Lead Monitor",
            font=ctk.CTkFont(size=19, weight="bold"),
            text_color="#4facfe",
        ).pack(side="left", padx=16, pady=12)

        ctk.CTkLabel(
            header,
            text="V2",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#333355",
        ).pack(side="left", pady=14)

        # ⏹ Stop button (disabled until monitor is running)
        self.stop_btn = ctk.CTkButton(
            header,
            text="⏹  Stop",
            width=108, height=36,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#4a0000",
            hover_color="#7f1d1d",
            state="disabled",
            corner_radius=8,
            command=self._stop_monitor,
        )
        self.stop_btn.pack(side="right", padx=(4, 16), pady=10)

        # ⏸️ Pause / ▶️ Resume toggle button (disabled until monitor is running)
        self.pause_btn = ctk.CTkButton(
            header,
            text="⏸️  Pause",
            width=120, height=36,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#4a3800",          # Muted amber when disabled/idle
            hover_color="#7c6200",
            state="disabled",
            corner_radius=8,
            command=self._toggle_pause,
        )
        self.pause_btn.pack(side="right", padx=4, pady=10)

        # ▶ Start button
        self.start_btn = ctk.CTkButton(
            header,
            text="▶  Start",
            width=108, height=36,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#14532d",
            hover_color="#15803d",
            corner_radius=8,
            command=self._start_monitor,
        )
        self.start_btn.pack(side="right", padx=4, pady=10)

        # ── Mode Banner ────────────────────────────────────────────────────────
        self.banner_frame = ctk.CTkFrame(
            self, height=38, corner_radius=0, fg_color=_BANNER_SIM["bg"]
        )
        self.banner_frame.pack(fill="x")
        self.banner_frame.pack_propagate(False)

        self.banner_label = ctk.CTkLabel(
            self.banner_frame,
            text="",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=_BANNER_SIM["fg"],
        )
        self.banner_label.pack(fill="both", expand=True)

        # ── Tab View ──────────────────────────────────────────────────────────
        self.tabs = ctk.CTkTabview(
            self,
            fg_color=("#0a0a18", "#0a0a18"),
            corner_radius=0,
            segmented_button_fg_color="#0f0f24",
            segmented_button_selected_color="#4facfe",
            segmented_button_selected_hover_color="#007bff",
            segmented_button_unselected_color="#0f0f24",
            segmented_button_unselected_hover_color="#1a1a3a",
        )
        self.tabs.pack(fill="both", expand=True)

        for tab in ("📡  Monitor", "⚙️  Settings", "📋  History"):
            self.tabs.add(tab)

        # Instantiate each tab frame
        self.monitor_tab = MonitorTab(self.tabs.tab("📡  Monitor"))
        self.monitor_tab.pack(fill="both", expand=True)

        self.settings_tab = SettingsTab(
            self.tabs.tab("⚙️  Settings"),
            settings=self.settings,
            settings_path=self.settings_path,
            on_settings_saved=self._on_settings_saved,
        )
        self.settings_tab.pack(fill="both", expand=True)

        self.history_tab = HistoryTab(
            self.tabs.tab("📋  History"),
            db=self.db,
        )
        self.history_tab.pack(fill="both", expand=True)
        self.history_tab.refresh()

        # ── Status bar ─────────────────────────────────────────────────────────
        status_bar = ctk.CTkFrame(
            self, fg_color=("#060612", "#060612"), height=22, corner_radius=0
        )
        status_bar.pack(fill="x", side="bottom")
        status_bar.pack_propagate(False)

        self.status_bar = ctk.CTkLabel(
            status_bar,
            text="Ready  —  Press ▶ Start to launch the local server. Then open IndiaMART in Chrome.",
            font=ctk.CTkFont(size=10),
            text_color="#333355",
        )
        self.status_bar.pack(side="left", padx=10)

    # =========================================================================
    # Mode Banner
    # =========================================================================

    def _update_banner(self, session_expired: bool = False):
        """
        Refresh the mode banner to reflect the current state.

        Priority order:
          1. session_expired=True  → orange SESSION EXPIRED banner (10 s auto-restore)
          2. simulation_mode=True  → static amber SIMULATION banner
          3. simulation_mode=False → blinking red LIVE MODE banner
        """
        # Cancel any existing blink job
        if self._blink_job:
            self.after_cancel(self._blink_job)
            self._blink_job = None

        if session_expired:
            self._session_banner = True
            cfg = _BANNER_SESSION
            self.banner_frame.configure(fg_color=cfg["bg"])
            self.banner_label.configure(text=cfg["text"], text_color=cfg["fg"])
            # Auto-restore after 10 seconds (keyed: repeated session_expired
            # events reset the same timer instead of stacking new ones)
            self._schedule(
                "banner_restore", 10_000,
                lambda: self._update_banner(session_expired=False),
            )

        elif self.settings.get("simulation_mode", True):
            self._session_banner = False
            cfg = _BANNER_SIM
            self.banner_frame.configure(fg_color=cfg["bg"])
            self.banner_label.configure(text=cfg["text"], text_color=cfg["fg"])

        else:
            # Live mode — start blinking
            self._session_banner = False
            self.banner_label.configure(
                text=_BANNER_LIVE_TEXT, text_color=_BANNER_LIVE_FG
            )
            self._blink()

    def _blink(self):
        """
        Alternate the banner background between two shades of red to
        create a blinking effect that signals "Live Mode is active".
        """
        if self.settings.get("simulation_mode", True) or self._session_banner:
            return   # Stop blinking when mode is no longer Live
        if self._closing:
            return   # Never re-arm the blink loop during shutdown

        self._blink_state = not self._blink_state
        color = _BANNER_LIVE_BG_A if self._blink_state else _BANNER_LIVE_BG_B
        self.banner_frame.configure(fg_color=color)
        self._blink_job = self.after(_BLINK_INTERVAL_MS, self._blink)

    def _on_settings_saved(self):
        """
        Callback invoked by SettingsTab after saving.
        Refreshes the banner to reflect any mode change.
        """
        self._update_banner()
        self._set_status("✅ Settings saved and applied.")
        self._schedule("status_reset", 3_500, lambda: self._set_status(
            "Ready  —  Configure settings, then press ▶ Start."
        ))

    # =========================================================================
    # Monitor Thread Control
    # =========================================================================

    def _start_monitor(self):
        """Start the background monitor thread."""
        if self._monitor_running:
            return

        from app.core.monitor import MonitorThread

        # Drain any stale events from a previous run
        while not self.ui_queue.empty():
            try:
                self.ui_queue.get_nowait()
            except queue.Empty:
                break

        self._monitor_thread = MonitorThread(
            db=self.db,
            settings=self.settings,
            ui_queue=self.ui_queue,
            ctrl_queue=self.ctrl_queue,
        )
        self._monitor_thread.start()
        self._monitor_running = True
        self._monitor_paused  = False

        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.pause_btn.configure(
            state="normal",
            text="⏸️  Pause",
            fg_color="#6b4c00",
            hover_color="#a37000",
        )
        self._set_status(
            f"Listening for Chrome Extension on port "
            f"{self.settings.get('server_port', 9223)}…"
        )

        self.tabs.set("📡  Monitor")
        logger.info("Monitor thread started.")

    def _stop_monitor(self):
        """Send a stop signal to the monitor thread."""
        if self._monitor_thread and self._monitor_running:
            self._monitor_thread.stop()
        # Pause button always resets on stop
        self._monitor_paused = False
        self.pause_btn.configure(
            state="disabled",
            text="⏸️  Pause",
            fg_color="#4a3800",
        )
        self.stop_btn.configure(state="disabled")
        self._set_status("Stop signal sent — finishing current cycle…")
        logger.info("Monitor stop requested.")

    def _toggle_pause(self):
        """
        Toggle between Pause and Resume on the same button.
        Calls the thread's pause()/resume() directly — both are thread-safe
        (they only set/clear a threading.Event, which is atomic).
        The button visuals are updated here immediately for responsiveness;
        the thread confirms via monitor_paused / monitor_resumed queue events.
        """
        if not self._monitor_thread or not self._monitor_running:
            return

        if self._monitor_paused:
            # Currently paused — resume
            self._monitor_thread.resume()
            # Optimistic UI update (confirmed by monitor_resumed event)
            self._monitor_paused = False
            self.pause_btn.configure(
                text="⏸️  Pause",
                fg_color="#6b4c00",
                hover_color="#a37000",
            )
            self._set_status("Monitor resumed — scanning active.")
        else:
            # Currently running — pause
            self._monitor_thread.pause()
            # Optimistic UI update (confirmed by monitor_paused event)
            self._monitor_paused = True
            self.pause_btn.configure(
                text="▶️  Resume",
                fg_color="#92400e",      # Deep amber — clear pause-state indicator
                hover_color="#b45309",
            )
            self._set_status("⏸️ Monitor paused — click Resume to continue.")

    # =========================================================================
    # Queue Drain Loop
    # =========================================================================

    def _drain_queue(self):
        """
        Called every 100 ms on the main thread via self.after().
        Processes up to 25 queued events per call so the UI never freezes even
        under heavy event loads.

        Shutdown behaviour
        ------------------
        The loop re-arms itself ONLY while _closing is False.  Previously the
        re-arm lived in an unconditional `finally`, so the drain loop kept
        scheduling itself right through shutdown and could dispatch an event
        into widgets that were about to be destroyed.
        """
        if self._closing:
            return

        # Drain all control events first (they are rare but critical)
        while not self._closing:
            try:
                event = self.ctrl_queue.get_nowait()
                self._dispatch(event)
            except queue.Empty:
                break
            except _tkinter.TclError as exc:
                logger.debug("Control queue drain TclError: %s", exc)
            except Exception as exc:
                logger.error("Control queue drain error: %s", exc, exc_info=True)

        try:
            for _ in range(25):
                event = self.ui_queue.get_nowait()
                self._dispatch(event)
        except queue.Empty:
            pass
        except _tkinter.TclError as exc:
            # Widget torn down mid-dispatch — expected during shutdown.
            logger.debug("Queue drain TclError: %s", exc)
        except Exception as exc:
            # A real bug in a handler.  Log it properly rather than hiding it
            # at DEBUG level, but keep the drain loop alive.
            logger.error("Queue drain error: %s", exc, exc_info=True)

        if not self._closing:
            self._schedule("drain", 100, self._drain_queue)

    def _guarded(self, label: str, fn, *args):
        """
        Run a widget-touching callback, absorbing teardown errors.

        TclError is expected while a tab is unpacked or the window is being
        destroyed, so it is logged at DEBUG.  Anything else is a genuine bug
        and is logged at ERROR with a traceback — the previous
        `except (_tkinter.TclError, Exception)` clause caught every exception
        and logged all of them at DEBUG, which hid real failures.
        """
        try:
            fn(*args)
        except _tkinter.TclError as exc:
            logger.debug("Suppressed TclError on '%s': %s", label, exc)
        except Exception as exc:
            logger.error("Handler error on '%s': %s", label, exc, exc_info=True)

    def _dispatch(self, event: Dict[str, Any]):
        """
        Route a single queue event to the correct UI handler.

        Every branch that touches a widget goes through _guarded() so that an
        error on one event does not silently drop subsequent events in the
        same drain cycle.
        """
        t = event.get("type")

        if t == "log":
            self._guarded(
                "log", self.monitor_tab.append_log,
                event.get("msg", ""), event.get("level", "info"),
            )

        elif t == "lead":
            self._guarded(
                "lead", self.monitor_tab.add_lead_card,
                event.get("data", {}), event.get("status", "filtered"),
            )
            self._guarded("history refresh", self.history_tab.refresh)

        elif t == "purchase_count":
            self._guarded(
                "purchase_count", self.monitor_tab.update_counter,
                event.get("count", 0), event.get("limit", 0),
            )

        elif t == "daily_limit_hit":
            self._guarded(
                "daily_limit_hit", self.monitor_tab.append_log,
                f"⛔ DAILY LIMIT HIT  ({event.get('limit')} purchases). "
                "Monitoring paused. Adjust in ⚙️ Settings.",
                "error",
            )

        elif t == "session_expired":
            self._guarded("session_expired banner", self._update_banner, True)
            self._guarded(
                "session_expired log", self.monitor_tab.append_log,
                "⚠️ IndiaMART session expired. "
                "Please open the browser window to log in.",
                "error",
            )

        elif t == "ext_connected":
            # Emitted once per server run on first real client contact.
            self._guarded("ext_connected", self._handle_ext_connected)

        elif t == "conn_state":
            self._guarded(
                "conn_state", self._handle_conn_state,
                event.get("state", ""), event.get("detail", ""),
            )

        elif t == "browser_context_lost":
            # Legacy event kept for schema compatibility — no action needed
            # in Extension-First mode (no browser process to lose)
            logger.debug("browser_context_lost event received (ignored in Extension-First mode).")

        elif t == "monitor_paused":
            # Confirmation from the thread that it entered the pause gate.
            # The button was already updated optimistically in _toggle_pause().
            self._monitor_paused = True

        elif t == "monitor_resumed":
            # Confirmation from the thread that it exited the pause gate.
            self._monitor_paused = False

        elif t == "monitor_stopped":
            self._monitor_running = False
            self._monitor_paused  = False
            self._guarded("monitor_stopped", self._handle_monitor_stopped)

        else:
            logger.debug("Unhandled ui_queue event type: %r", t)

    # ── Event handlers ───────────────────────────────────────────────────────

    def _handle_ext_connected(self):
        """Green flash + a status line that reverts after 5 s."""
        self._set_status("✅ Chrome Extension connected — leads are being processed.")
        # Keyed so repeated connect events reset ONE timer instead of stacking
        # a new 5-second callback per event.
        self._schedule("status_reset", 5_000, lambda: self._set_status(
            f"Listening for Chrome Extension on port "
            f"{self.settings.get('server_port', 9223)}…"
        ))

    def _handle_conn_state(self, state: str, detail: str):
        """
        Apply a connection-state transition published by MonitorThread/LocalServer.

        These events are edge-triggered (emitted only on change), so this
        handler never runs in a loop and never re-arms a timer per poll.
        """
        logger.debug("[AppWindow] conn_state=%s (%s)", state, detail)

        if hasattr(self.monitor_tab, "set_conn_state"):
            self.monitor_tab.set_conn_state(state, detail)

        port = self.settings.get("server_port", 9223)
        messages = {
            "server_starting":          f"Starting local server on port {port}…",
            "bind_failed":              f"⛔ Port {port} unavailable — {detail}",
            "server_running_no_client": f"Listening on port {port} — extension not connected yet.",
            "client_connected":         "✅ Chrome Extension connected — leads are being processed.",
            "server_stopping":          "Stopping local server…",
            "server_stopped":           "Local server stopped.",
        }
        if state in messages:
            self._set_status(messages[state])
            # A conn_state message is authoritative; drop any pending revert.
            self._cancel("status_reset")

    def _handle_monitor_stopped(self):
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.pause_btn.configure(state="disabled", text="⏸️  Pause", fg_color="#4a3800")
        self._set_status("Monitor stopped.")
        logger.info("Monitor thread confirmed stopped.")

    # =========================================================================
    # Window Close / Shutdown
    # =========================================================================

    def _on_close(self):
        """
        Called when the user clicks the OS ✕ button (WM_DELETE_WINDOW).

        Shutdown order
        --------------
        A. Set _closing guard immediately to prevent re-entrant calls and to
           stop the drain loop / blink loop from re-arming themselves.
        B. Cancel every pending after() job so nothing can fire after destroy().
        C. Signal MonitorThread to stop (non-blocking — just sets an Event).
           MonitorThread is the owner of LocalServer and stops it in its own
           finally block; the UI never reaches into the server directly.
        D. Delegate the wait to _await_shutdown() via after(), which polls
           every 100 ms without blocking the Tk event loop.
        E. _finish_close() verifies the server released its port, closes the
           DB, and destroys the window — exactly once.

        We do NOT call thread.join() here because that would freeze the UI
        window and prevent Tk from processing the destroy() call.

        Safe to call more than once: the _closing guard makes repeat calls a
        no-op, and _finish_close() is separately guarded by _closed.
        """
        if self._closing:
            logger.debug("[AppWindow] _on_close() re-entered — ignored.")
            return
        self._closing = True

        logger.info("[AppWindow] ── Shutdown initiated ──")

        # Cancel every scheduled Tk callback (drain loop, blink loop, status
        # reverts, banner restore, auto-start) BEFORE anything is destroyed.
        self._cancel_all_jobs()

        # Signal the monitor thread to stop (sets a threading.Event — instant)
        if self._monitor_thread and self._monitor_thread.is_alive():
            logger.info("[AppWindow] Signalling MonitorThread to stop.")
            try:
                self._monitor_thread.stop()
            except Exception as exc:
                logger.error("[AppWindow] Error signalling monitor stop: %s", exc)
                self._finish_close()
                return
        else:
            # No live thread. The server may still exist from a previous run,
            # so _finish_close() still performs the release verification.
            logger.info("[AppWindow] No live monitor thread — closing directly.")
            self._finish_close()
            return

        # Start the non-blocking shutdown poller (max 50 ticks × 100 ms = 5 s)
        self._await_shutdown(ticks_remaining=50)

    def _await_shutdown(self, ticks_remaining: int):
        """
        Called every 100 ms via after() while waiting for MonitorThread to exit.

        Once the thread is dead (or the tick budget is exhausted) it calls
        _finish_close() to release resources and destroy the window.
        This approach keeps the Tk event loop alive and responsive throughout,
        and the tick budget makes the wait strictly bounded.
        """
        thread_done = (
            self._monitor_thread is None
            or not self._monitor_thread.is_alive()
        )

        if thread_done or ticks_remaining <= 0:
            if not thread_done:
                logger.warning(
                    "[AppWindow] MonitorThread did not stop within 5 s — "
                    "proceeding with close. The OS will reclaim the port when "
                    "the process exits."
                )
            self._finish_close()
        else:
            # Scheduled through the registry under a reserved key so that the
            # _closing guard in _run_job() does not cancel the shutdown itself.
            job_id = self.after(
                100,
                lambda: self._run_job(
                    "shutdown_poll",
                    lambda: self._await_shutdown(ticks_remaining - 1),
                ),
            )
            self._after_jobs["shutdown_poll"] = job_id

    def _finish_close(self):
        """
        Final cleanup step — verify server release, close the DB, destroy Tk.

        Idempotent: guarded by _closed so a second invocation (e.g. the poller
        firing once more after a direct call) does nothing.
        """
        if self._closed:
            logger.debug("[AppWindow] _finish_close() already ran — ignored.")
            return
        self._closed  = True
        self._closing = True

        # 1. Cancel anything scheduled after _on_close ran (e.g. the poller).
        self._cancel_all_jobs()

        # 2. Verify the LocalServer actually released its socket and thread.
        #    This asks the owner (MonitorThread) rather than poking at private
        #    attributes, and the answer is backed by a live TCP probe.
        try:
            if self._monitor_thread is not None and hasattr(
                self._monitor_thread, "verify_server_released"
            ):
                verdict = self._monitor_thread.verify_server_released()
                if verdict.get("clean"):
                    logger.info(
                        "[AppWindow] Server release VERIFIED (state=%s, "
                        "thread_alive=%s, listener=%s).",
                        verdict.get("state"), verdict.get("thread_alive"),
                        verdict.get("port_has_listener"),
                    )
                else:
                    logger.warning(
                        "[AppWindow] Server release NOT verified: %s", verdict
                    )
        except Exception as exc:
            logger.warning("[AppWindow] Server verification failed: %s", exc)

        # 3. Close the database.  close_all() releases every thread-local
        #    connection (monitor thread + serving thread), not just this one.
        try:
            if hasattr(self.db, "close_all"):
                closed = self.db.close_all()
                logger.info("[AppWindow] Database closed (%s connection(s)).", closed)
            elif hasattr(self.db, "close"):
                self.db.close()
                logger.info("[AppWindow] Database closed.")
        except Exception as exc:
            logger.warning("[AppWindow] Error closing database: %s", exc)

        # 4. Destroy the window last, once nothing can call back into it.
        logger.info("[AppWindow] Destroying window. ── Shutdown complete ──")
        try:
            self.destroy()
        except Exception as exc:
            logger.debug("[AppWindow] destroy() raised (already gone?): %s", exc)

    # =========================================================================
    # Helpers
    # =========================================================================

    def _set_status(self, text: str):
        """Update the bottom status bar label (no-op once the window is gone)."""
        if self._closed:
            return
        try:
            self.status_bar.configure(text=text)
        except _tkinter.TclError as exc:
            logger.debug("[AppWindow] _set_status skipped: %s", exc)
