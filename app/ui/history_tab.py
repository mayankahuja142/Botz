"""
app/ui/history_tab.py
=====================
Lead history browser tab for SafeIM Lead Monitor V2.

Features:
  - Dark-themed ttk.Treeview listing all lead records from SQLite.
  - Live search bar that filters the table as the user types.
  - Sortable column headers (click to sort ascending).
  - Bottom detail panel showing the full record for the selected row.
  - "📥 Export to TXT" button — opens a Windows file-save dialog and writes
    a formatted text report (title, contact, timestamp, status, etc.).
  - "🔄 Refresh" button to re-load records from the database on demand.
    (Also called automatically whenever a new lead is purchased/simulated.)
"""

import logging
import tkinter as tk
from tkinter import filedialog, ttk
from datetime import datetime
from typing import Any, Dict, List, Optional

import customtkinter as ctk

logger = logging.getLogger(__name__)

# Column definitions: (id, heading, pixel_width, stretch)
_COLUMNS = [
    ("timestamp", "Timestamp",   145, False),
    ("title",     "Lead Title",  270, True),
    ("country",   "Country",     90,  False),
    ("quantity",  "Quantity",    90,  False),
    ("contact",   "Contact",     180, False),
    ("status",    "Status",      105, False),
]


class HistoryTab(ctk.CTkFrame):
    """Full lead history browser with search, detail panel, and TXT export."""

    def __init__(self, master, db, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.db             = db
        self._all_records:  List[Dict[str, Any]] = []
        self._sort_col:     str  = "timestamp"
        self._sort_reverse: bool = True
        self._flash_job:    Optional[str] = None

        self._build_ui()
        self._apply_dark_treeview_style()

    # =========================================================================
    # UI Construction
    # =========================================================================

    def _build_ui(self):
        # ── Toolbar ────────────────────────────────────────────────────────────
        toolbar = ctk.CTkFrame(
            self, fg_color=("#131326", "#131326"), corner_radius=8
        )
        toolbar.pack(fill="x", padx=8, pady=(8, 0))

        ctk.CTkLabel(
            toolbar,
            text="📋  Lead History",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#4facfe",
        ).pack(side="left", padx=14, pady=8)

        self.export_btn = ctk.CTkButton(
            toolbar,
            text="📥  Export to TXT",
            font=ctk.CTkFont(size=12, weight="bold"),
            width=150, height=32,
            fg_color="#7c3aed",
            hover_color="#6d28d9",
            corner_radius=7,
            command=self._export_txt,
        )
        self.export_btn.pack(side="right", padx=8, pady=8)

        self.refresh_btn = ctk.CTkButton(
            toolbar,
            text="🔄  Refresh",
            font=ctk.CTkFont(size=12),
            width=100, height=32,
            fg_color="#22223a",
            hover_color="#2a2a50",
            corner_radius=7,
            command=self.refresh,
        )
        self.refresh_btn.pack(side="right", padx=(0, 4), pady=8)

        self.count_label = ctk.CTkLabel(
            toolbar, text="0 records",
            font=ctk.CTkFont(size=11),
            text_color="#444455",
        )
        self.count_label.pack(side="right", padx=10, pady=8)

        # ── Search bar ─────────────────────────────────────────────────────────
        search_row = ctk.CTkFrame(self, fg_color="transparent")
        search_row.pack(fill="x", padx=8, pady=(6, 0))

        ctk.CTkLabel(
            search_row, text="🔍",
            font=ctk.CTkFont(size=14),
        ).pack(side="left", padx=(0, 6))

        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", self._on_search)

        self.search_entry = ctk.CTkEntry(
            search_row,
            textvariable=self._search_var,
            placeholder_text="Search by title, contact, country, customer name…",
            font=ctk.CTkFont(size=12),
            height=34,
        )
        self.search_entry.pack(fill="x", expand=True)

        # ── Treeview wrapper ───────────────────────────────────────────────────
        tree_wrap = ctk.CTkFrame(
            self, fg_color=("#0c0c1c", "#0c0c1c"), corner_radius=8
        )
        tree_wrap.pack(fill="both", expand=True, padx=8, pady=(6, 0))

        vsb = ttk.Scrollbar(tree_wrap, orient="vertical")
        hsb = ttk.Scrollbar(tree_wrap, orient="horizontal")

        self.tree = ttk.Treeview(
            tree_wrap,
            columns=[c[0] for c in _COLUMNS],
            show="headings",
            yscrollcommand=vsb.set,
            xscrollcommand=hsb.set,
            selectmode="browse",
        )
        vsb.config(command=self.tree.yview)
        hsb.config(command=self.tree.xview)

        for col_id, heading, width, stretch in _COLUMNS:
            self.tree.heading(
                col_id, text=heading,
                command=lambda c=col_id: self._sort_by(c),
            )
            self.tree.column(
                col_id, width=width, stretch=stretch, minwidth=50
            )

        vsb.pack(side="right", fill="y")
        hsb.pack(side="bottom", fill="x")
        self.tree.pack(fill="both", expand=True, padx=4, pady=4)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # ── Detail panel ───────────────────────────────────────────────────────
        detail_wrap = ctk.CTkFrame(
            self, fg_color=("#0c0c1c", "#0c0c1c"), corner_radius=8, height=130
        )
        detail_wrap.pack(fill="x", padx=8, pady=(6, 8))
        detail_wrap.pack_propagate(False)

        detail_hdr = ctk.CTkFrame(detail_wrap, fg_color="transparent")
        detail_hdr.pack(fill="x", padx=10, pady=(6, 0))
        ctk.CTkLabel(
            detail_hdr,
            text="📄  Record Details",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#4facfe",
        ).pack(side="left")

        self.detail_box = ctk.CTkTextbox(
            detail_wrap,
            font=ctk.CTkFont(family="Consolas", size=11),
            state="disabled",
            fg_color="#080816",
            text_color="#c9d1d9",
            height=90,
        )
        self.detail_box.pack(fill="both", expand=True, padx=6, pady=(2, 6))

    # =========================================================================
    # Dark Treeview Styling
    # =========================================================================

    def _apply_dark_treeview_style(self):
        """Override ttk default styles so the Treeview matches the dark theme."""
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass   # "clam" not available on all systems; fall back gracefully

        style.configure(
            "Treeview",
            background="#0c0c1c",
            foreground="#c9d1d9",
            fieldbackground="#0c0c1c",
            rowheight=28,
            borderwidth=0,
            font=("Segoe UI", 10),
        )
        style.configure(
            "Treeview.Heading",
            background="#18182e",
            foreground="#4facfe",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            borderwidth=0,
        )
        style.map(
            "Treeview",
            background=[("selected", "#1e3a5f")],
            foreground=[("selected", "#ffffff")],
        )
        style.map(
            "Treeview.Heading",
            background=[("active", "#22223a")],
        )
        style.configure(
            "Vertical.TScrollbar",
            background="#18182e", troughcolor="#0c0c1c",
            arrowcolor="#333355", borderwidth=0,
        )
        style.configure(
            "Horizontal.TScrollbar",
            background="#18182e", troughcolor="#0c0c1c",
            arrowcolor="#333355", borderwidth=0,
        )

    # =========================================================================
    # Data Operations
    # =========================================================================

    def refresh(self):
        """Reload all records from the database and repopulate the table."""
        self._all_records = self.db.get_all_leads()
        self._apply_search_filter()

    def _apply_search_filter(self):
        """Filter self._all_records by the active search query and populate."""
        q = self._search_var.get().strip().lower()
        if not q:
            self._populate(self._all_records)
            return

        filtered = [
            r for r in self._all_records
            if (q in r.get("title", "").lower()
                or q in r.get("contact_info", "").lower()
                or q in r.get("country", "").lower()
                or q in r.get("display_name", "").lower()
                or q in r.get("location", "").lower())
        ]
        self._populate(filtered)

    def _populate(self, records: List[Dict[str, Any]]):
        """Clear and fill the Treeview with the given list of records."""
        self.tree.delete(*self.tree.get_children())

        for rec in records:
            status_txt, tag = self._status_display(rec)
            self.tree.insert(
                "", "end",
                iid=str(rec["id"]),
                values=(
                    rec.get("timestamp", "")[:16],
                    rec.get("title", "")[:80],
                    rec.get("country", ""),
                    rec.get("quantity", ""),
                    rec.get("contact_info", "") or "—",
                    status_txt,
                ),
                tags=(tag,),
            )

        # Row colour tags
        self.tree.tag_configure("purchased", foreground="#4ade80")
        self.tree.tag_configure("simulated", foreground="#60a5fa")
        self.tree.tag_configure("filtered",  foreground="#fbbf24")

        n = len(records)
        self.count_label.configure(text=f"{n} record{'s' if n != 1 else ''}")

    @staticmethod
    def _status_display(rec: Dict[str, Any]):
        if rec.get("purchased"):
            return "✅ Purchased", "purchased"
        if rec.get("simulated"):
            return "🔵 Simulated", "simulated"
        return "🟡 Filtered", "filtered"

    # =========================================================================
    # Events
    # =========================================================================

    def _on_search(self, *_):
        """Live-filter the table as the user types in the search box."""
        self._apply_search_filter()

    def _on_select(self, _event):
        """Show full record details in the bottom panel when a row is clicked."""
        sel = self.tree.selection()
        if not sel:
            return
        try:
            rec_id = int(sel[0])
        except ValueError:
            return

        rec = next((r for r in self._all_records if r["id"] == rec_id), None)
        if not rec:
            return

        status_txt, _ = self._status_display(rec)
        lines = [
            f"Timestamp     : {rec.get('timestamp', 'N/A')}",
            f"Status        : {status_txt}",
            f"Title         : {rec.get('title', 'N/A')}",
            f"Location      : {rec.get('location', 'N/A')}",
            f"Country       : {rec.get('country', 'N/A')}",
            f"Quantity      : {rec.get('quantity', 'N/A')}",
            f"Display Name  : {rec.get('display_name', 'N/A')}",
            f"Contact Info  : {rec.get('contact_info', 'N/A')}",
        ]
        if rec.get("filtered_reason"):
            lines.append(f"Filter Reason : {rec['filtered_reason']}")

        self.detail_box.configure(state="normal")
        self.detail_box.delete("1.0", "end")
        self.detail_box.insert("1.0", "\n".join(lines))
        self.detail_box.configure(state="disabled")

    def _sort_by(self, col: str):
        """Sort the visible Treeview rows by the clicked column."""
        reverse = (col == self._sort_col) and not self._sort_reverse
        self._sort_col, self._sort_reverse = col, reverse

        items = [
            (self.tree.set(item, col), item)
            for item in self.tree.get_children("")
        ]
        items.sort(key=lambda x: x[0].lower(), reverse=reverse)
        for idx, (_, item) in enumerate(items):
            self.tree.move(item, "", idx)

    # =========================================================================
    # TXT Export
    # =========================================================================

    def _export_txt(self):
        """Open a file-save dialog and export the currently displayed records."""
        # Export what is currently visible (may be a filtered subset)
        visible_ids = [int(iid) for iid in self.tree.get_children()]
        if not visible_ids:
            self._flash_btn("⚠️ Nothing to export", "#7c2d00")
            return

        id_set  = set(visible_ids)
        records = [r for r in self._all_records if r["id"] in id_set]

        filepath = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
            initialfile=f"safeim_leads_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            title="Export Lead History — SafeIM Lead Monitor V2",
        )
        if not filepath:
            return  # User cancelled

        try:
            purchased = [r for r in records if r.get("purchased")]
            simulated = [r for r in records if r.get("simulated")]
            filtered_ = [r for r in records if not r.get("purchased") and not r.get("simulated")]

            with open(filepath, "w", encoding="utf-8") as fh:
                fh.write("=" * 72 + "\n")
                fh.write("  SafeIM Lead Monitor V2  —  Lead Export Report\n")
                fh.write(f"  Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                fh.write(f"  Records   : {len(records)}\n")
                fh.write(
                    f"  Summary   : {len(purchased)} Purchased  |  "
                    f"{len(simulated)} Simulated  |  {len(filtered_)} Filtered\n"
                )
                fh.write("=" * 72 + "\n\n")

                for i, rec in enumerate(records, start=1):
                    status_txt, _ = self._status_display(rec)
                    fh.write("─" * 72 + "\n")
                    fh.write(
                        f"  Lead #{i:04d}  |  {status_txt}  |  "
                        f"{rec.get('timestamp', 'N/A')}\n"
                    )
                    fh.write("─" * 72 + "\n")
                    fh.write(f"  Title         : {rec.get('title', 'N/A')}\n")
                    fh.write(f"  Location      : {rec.get('location', 'N/A')}\n")
                    fh.write(f"  Country       : {rec.get('country', 'N/A')}\n")
                    fh.write(f"  Quantity      : {rec.get('quantity', 'N/A')}\n")
                    fh.write(f"  Display Name  : {rec.get('display_name', 'N/A')}\n")
                    fh.write(f"  Contact Info  : {rec.get('contact_info', 'N/A')}\n")
                    if rec.get("filtered_reason"):
                        fh.write(f"  Filter Reason : {rec['filtered_reason']}\n")
                    fh.write("\n")

                fh.write("=" * 72 + "\n")
                fh.write("  End of Report  —  SafeIM Lead Monitor V2\n")
                fh.write("=" * 72 + "\n")

            logger.info(f"Exported {len(records)} records to {filepath}")
            self._flash_btn("✅ Exported!", "#14532d")

        except Exception as exc:
            logger.error(f"Export failed: {exc}")
            self._flash_btn("❌ Export Failed", "#7f1d1d")

    def _flash_btn(self, text: str, color: str, restore_ms: int = 2800):
        """Temporarily change the export button label and color, then restore."""
        self.export_btn.configure(text=text, fg_color=color)
        
        if self._flash_job is not None:
            self.after_cancel(self._flash_job)
            
        def _restore():
            self._flash_job = None
            if self.winfo_exists() and self.export_btn.winfo_exists():
                self.export_btn.configure(
                    text="📥  Export to TXT", fg_color="#7c3aed"
                )
                
        self._flash_job = self.after(restore_ms, _restore)

    def destroy(self):
        """Clean up pending callbacks before destruction."""
        if self._flash_job is not None:
            self.after_cancel(self._flash_job)
            self._flash_job = None
        super().destroy()
