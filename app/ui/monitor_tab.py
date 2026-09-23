"""
app/ui/monitor_tab.py
=====================
Live monitoring tab for SafeIM Lead Monitor V2.

Layout:
  ┌────────────────────────────────────────────────────────────────┐
  │  🔴 Leads Today: X / Y            ⬤ Running                   │  ← top bar
  ├──────────────────────────────┬─────────────────────────────────┤
  │  📋 Activity Log             │  📇 Lead Feed                   │
  │  (scrollable color-coded     │  (scrollable lead cards with     │
  │   timestamped log)           │   status chips, badges, contact) │
  └──────────────────────────────┴─────────────────────────────────┘

Colors:
  Log level info    → #c9d1d9  (gray)
  Log level success → #4ade80  (green)
  Log level warning → #fbbf24  (amber)
  Log level error   → #f87171  (red)

Lead card status chips:
  ✅ Purchased → green bg
  🔵 Simulated → blue bg
  🟡 Filtered  → amber bg
"""

import logging
from datetime import datetime
from typing import Any, Dict

import customtkinter as ctk

logger = logging.getLogger(__name__)

# ── Colour maps ────────────────────────────────────────────────────────────────
_LOG_TAGS = {
    "info":    "#c9d1d9",
    "success": "#4ade80",
    "warning": "#fbbf24",
    "error":   "#f87171",
}

_STATUS_CFG = {
    "purchased": {"bg": "#14532d", "fg": "#4ade80",  "icon": "✅", "label": "PURCHASED"},
    "simulated": {"bg": "#1e3a5f", "fg": "#60a5fa",  "icon": "🔵", "label": "SIMULATED"},
    "filtered":  {"bg": "#3b1f00", "fg": "#fbbf24",  "icon": "🟡", "label": "FILTERED"},
}


class MonitorTab(ctk.CTkFrame):
    """Dual-panel live monitoring tab."""

    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self._build_ui()

    # =========================================================================
    # UI Construction
    # =========================================================================

    def _build_ui(self):
        # ── Top bar ────────────────────────────────────────────────────────────
        top = ctk.CTkFrame(
            self, fg_color=("#131326", "#131326"),
            height=46, corner_radius=8,
        )
        top.pack(fill="x", padx=8, pady=(8, 0))
        top.pack_propagate(False)

        self.counter_badge = ctk.CTkLabel(
            top,
            text="🔴  Leads Today: 0 / 20",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#f87171",
        )
        self.counter_badge.pack(side="left", padx=16)

        self.status_dot = ctk.CTkLabel(
            top,
            text="⬤  Server Stopped",
            font=ctk.CTkFont(size=12),
            text_color="#444455",
        )
        self.status_dot.pack(side="right", padx=16)

        # ── Dual-panel grid ───────────────────────────────────────────────────
        panels = ctk.CTkFrame(self, fg_color="transparent")
        panels.pack(fill="both", expand=True, padx=8, pady=8)
        panels.columnconfigure(0, weight=55)
        panels.columnconfigure(1, weight=45)
        panels.rowconfigure(0, weight=1)

        # ── LEFT: Activity Log ────────────────────────────────────────────────
        log_wrap = ctk.CTkFrame(
            panels, fg_color=("#0c0c1c", "#0c0c1c"), corner_radius=10
        )
        log_wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 4))

        log_hdr = ctk.CTkFrame(log_wrap, fg_color="transparent")
        log_hdr.pack(fill="x", padx=12, pady=(10, 2))
        ctk.CTkLabel(
            log_hdr,
            text="📋  Activity Log",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#4facfe",
        ).pack(side="left")

        self._clear_log_btn = ctk.CTkButton(
            log_hdr,
            text="Clear",
            width=54, height=24,
            font=ctk.CTkFont(size=10),
            fg_color="#22223a",
            hover_color="#2a2a50",
            command=self.clear_log,
        )
        self._clear_log_btn.pack(side="right")

        ctk.CTkFrame(log_wrap, height=1, fg_color="#1e1e3a").pack(fill="x", padx=8)

        self.log_box = ctk.CTkTextbox(
            log_wrap,
            font=ctk.CTkFont(family="Consolas", size=11),
            wrap="word",
            state="disabled",
            fg_color="#080816",
            text_color="#c9d1d9",
            scrollbar_button_color="#1e1e3a",
        )
        self.log_box.pack(fill="both", expand=True, padx=6, pady=6)

        # Register colour tags on the underlying tk.Text widget
        for tag, colour in _LOG_TAGS.items():
            self.log_box._textbox.tag_configure(tag, foreground=colour)
        self.log_box._textbox.tag_configure("ts", foreground="#333344")

        # ── RIGHT: Lead Card Feed ─────────────────────────────────────────────
        cards_wrap = ctk.CTkFrame(
            panels, fg_color=("#0c0c1c", "#0c0c1c"), corner_radius=10
        )
        cards_wrap.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

        ctk.CTkLabel(
            cards_wrap,
            text="📇  Lead Feed",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#4facfe",
        ).pack(anchor="w", padx=12, pady=(10, 4))

        ctk.CTkFrame(cards_wrap, height=1, fg_color="#1e1e3a").pack(fill="x", padx=8)

        self.cards_frame = ctk.CTkScrollableFrame(
            cards_wrap,
            fg_color="#080816",
            scrollbar_button_color="#1e1e3a",
        )
        self.cards_frame.pack(fill="both", expand=True, padx=6, pady=6)

    # =========================================================================
    # Public API — called from AppWindow._dispatch_event()
    # =========================================================================

    def append_log(self, msg: str, level: str = "info"):
        """Append a timestamped, colour-coded line to the activity log."""
        ts = datetime.now().strftime("%H:%M:%S")
        valid_level = level if level in _LOG_TAGS else "info"

        self.log_box.configure(state="normal")
        self.log_box._textbox.insert("end", f"[{ts}]  ", "ts")
        self.log_box._textbox.insert("end", f"{msg}\n", valid_level)
        self.log_box.configure(state="disabled")
        self.log_box._textbox.see("end")

    def add_lead_card(self, lead_data: Dict[str, Any], status: str):
        """Prepend a lead card to the top of the right-side feed panel."""
        cfg = _STATUS_CFG.get(status, _STATUS_CFG["filtered"])

        # ── Card container ─────────────────────────────────────────────────────
        card = ctk.CTkFrame(
            self.cards_frame,
            fg_color=("#141430", "#141430"),
            corner_radius=10,
            border_width=1,
            border_color="#2a2a50",
        )

        # Pack at top (newest first).
        before_widget = self._first_card_widget()
        if before_widget is not None:
            card.pack(fill="x", pady=(0, 8), padx=2, before=before_widget)
        else:
            card.pack(fill="x", pady=(0, 8), padx=2)

        # ── Status chip + country ─────────────────────────────────────────────
        row1 = ctk.CTkFrame(card, fg_color="transparent")
        row1.pack(fill="x", padx=10, pady=(8, 2))

        ctk.CTkLabel(
            row1,
            text=f"  {cfg['icon']} {cfg['label']}  ",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=cfg["fg"],
            fg_color=cfg["bg"],
            corner_radius=5,
        ).pack(side="left")

        country = lead_data.get("country", "")
        if country:
            ctk.CTkLabel(
                row1,
                text=f"🌍 {country}",
                font=ctk.CTkFont(size=11),
                text_color="#666677",
            ).pack(side="right")

        # ── Title ──────────────────────────────────────────────────────────────
        title = lead_data.get("title", "Unknown Lead")
        ctk.CTkLabel(
            card,
            text=(title[:85] + "…") if len(title) > 85 else title,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#dde0f0",
            anchor="w",
            justify="left",
            wraplength=230,
        ).pack(fill="x", padx=10, pady=(2, 0))

        # ── Quantity + location ────────────────────────────────────────────────
        qty      = lead_data.get("quantity", "")
        location = lead_data.get("location", "")
        if qty or location:
            row2 = ctk.CTkFrame(card, fg_color="transparent")
            row2.pack(fill="x", padx=10, pady=(3, 0))
            if qty:
                ctk.CTkLabel(
                    row2,
                    text=f"📦 {qty}",
                    font=ctk.CTkFont(size=11),
                    text_color="#a78bfa",
                ).pack(side="left")
            if location:
                ctk.CTkLabel(
                    row2,
                    text=f"📍 {location}",
                    font=ctk.CTkFont(size=11),
                    text_color="#555566",
                ).pack(side="right")

        # ── Contact info (only visible when unlocked) ──────────────────────────
        contact  = lead_data.get("contact_info", "")
        display_name = lead_data.get("display_name", "")
        if contact or display_name:
            ctk.CTkFrame(card, height=1, fg_color="#2a2a50").pack(
                fill="x", padx=8, pady=(6, 2)
            )
            if display_name:
                ctk.CTkLabel(
                    card,
                    text=f"👤 {display_name}",
                    font=ctk.CTkFont(size=11, weight="bold"),
                    text_color="#60a5fa",
                    anchor="w",
                ).pack(fill="x", padx=10, pady=(0, 1))
            if contact:
                ctk.CTkLabel(
                    card,
                    text=f"📞 {contact}",
                    font=ctk.CTkFont(size=11),
                    text_color="#4ade80",
                    anchor="w",
                ).pack(fill="x", padx=10, pady=(0, 6))
        else:
            ctk.CTkLabel(
                card,
                text="Contact hidden (filtered or simulated).",
                font=ctk.CTkFont(size=10),
                text_color="#333344",
                anchor="w",
            ).pack(fill="x", padx=10, pady=(4, 8))

    def update_counter(self, count: int, limit: int):
        """Refresh the daily purchase counter badge with colour-coded thresholds."""
        pct   = count / limit if limit > 0 else 0
        emoji = "🔴" if pct >= 0.8 else ("🟡" if pct >= 0.5 else "🟢")
        color = "#f87171" if pct >= 0.8 else ("#fbbf24" if pct >= 0.5 else "#4ade80")
        self.counter_badge.configure(
            text=f"{emoji}  Leads Today: {count} / {limit}",
            text_color=color,
        )

    # =========================================================================
    # Connection State API
    # =========================================================================

    # ── Connection state indicator ───────────────────────────────────────────
    # Maps a LocalServer connection state to dot text + colour.
    # Kept as a plain dict so adding a state never requires touching layout.
    _CONN_STATE_VISUALS = {
        "server_stopped":           ("⬤  Server Stopped",                       "#444455"),
        "server_starting":          ("⬤  Server Starting…",                      "#60a5fa"),
        "bind_failed":              ("⬤  Port Unavailable — Server Not Started", "#f87171"),
        "server_running_no_client": ("⬤  Server Running — Extension Not Connected", "#fbbf24"),
        "client_connected":         ("⬤  Extension Active",                      "#4ade80"),
        "server_stopping":          ("⬤  Server Stopping…",                      "#a78bfa"),
    }

    def set_conn_state(self, state: str, detail: str = ""):
        """
        Render granular network and lifecycle state exactly as dictated by the server.

        `detail` is shown as a tooltip-style suffix only when present and short.
        """
        text, color = self._CONN_STATE_VISUALS.get(
            state, ("⬤  Unknown State", "#888899")
        )
        if detail and state == "bind_failed":
            text = f"{text}"    # keep the dot line short; detail goes to the log
        try:
            self.status_dot.configure(text=text, text_color=color)
        except Exception:
            # Widget may be mid-teardown; the caller logs at a higher level.
            pass

    def clear_log(self):
        """Wipe the activity log."""
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    # =========================================================================
    # Internal Helpers
    # =========================================================================

    def _first_card_widget(self):
        """
        Return the first *user* card inside the scrollable feed so new cards can
        be inserted before it (newest-on-top ordering).

        CTkScrollableFrame renders user content into an inner _scrollable_frame
        canvas viewport, but that inner frame may also contain internal CTk
        structural children that are NOT managed by pack (e.g. a bare CTkFrame
        used as a layout helper).  Passing an unpacked widget to pack(before=)
        raises a TclError that _guarded() absorbs silently, leaving the feed empty.

        We filter for children that are already pack-managed (i.e. have a non-empty
        pack info dict).  The first such child is the topmost user card.
        Returns None if no pack-managed card exists yet, causing pack() to append.
        """
        try:
            inner = getattr(self.cards_frame, "_scrollable_frame", None)
            candidates = inner.winfo_children() if inner is not None else self.cards_frame.winfo_children()
            for child in candidates:
                try:
                    if child.pack_info():  # non-empty dict means it is pack-managed
                        return child
                except Exception:
                    pass  # widget not managed by pack — skip it
            return None
        except Exception:
            return None
