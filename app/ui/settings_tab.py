"""
app/ui/settings_tab.py
======================
Settings configuration tab for SafeIM Lead Monitor V2.

Provides form fields for:
  - Target Keywords (multi-line textbox)
  - Excluded Locations / Target Countries (multi-line textboxes)
  - Minimum Quantity, Max Daily Purchases, Max Session Purchases (entries)
  - Contact Channel Preference (5-option dropdown: Phone only / Whatsapp only /
    Email only / Phone+Whatsapp / Phone+Whatsapp+Email)
  - Default Quick Reply Template (dropdown: Catalog Link / Product is available /
    Thanks for your enquiry / Share more details)
  - Simulation Mode toggle (CTkSwitch — ON by default for safety)
  - Dynamic Delay Slider (1.0–60.0 s, updates live settings dict instantly)
  - IndiaMART Leads URL (advanced)

On "Save Settings":
  - Validates all fields.
  - Updates the shared `settings` dict in-place (monitor reads it next cycle).
  - Persists to config/settings.json on disk.
  - Calls the on_settings_saved callback so AppWindow can refresh the banner.
"""

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import customtkinter as ctk

logger = logging.getLogger(__name__)


class SettingsTab(ctk.CTkFrame):
    """Full settings form embedded in the ⚙️ Settings tab."""

    def __init__(
        self,
        master,
        settings: Dict[str, Any],
        settings_path: Path,
        on_settings_saved: Optional[Callable] = None,
        **kwargs,
    ):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.settings          = settings           # Shared mutable dict
        self.settings_path     = settings_path
        self.on_settings_saved = on_settings_saved

        self._build_ui()
        self._load_values()

    # =========================================================================
    # UI Construction
    # =========================================================================

    def _build_ui(self):
        """Build all setting widgets inside a scrollable container."""
        self.scroll = ctk.CTkScrollableFrame(
            self, fg_color="transparent",
            scrollbar_button_color="#2a2a4a",
        )
        self.scroll.pack(fill="both", expand=True, padx=10, pady=10)

        # ── Section: Keyword Filters ───────────────────────────────────────────
        self._section("🔍  Keyword Filters")
        self._label("Target Keywords")
        self._hint("Enter one keyword per line. A lead must contain at least one to pass.")
        self.keywords_box = self._textbox(height=95)

        self._gap()

        self._label("❌  Keyword Blacklist")
        self._hint(
            "Leads containing ANY of these terms are rejected, even if a whitelist "
            "keyword also matches. One term per line."
        )
        self.blacklist_box = self._textbox(height=75)

        self._divider()

        # ── Section: Location Rules ────────────────────────────────────────────
        self._section("📍  Location Rules")

        self._label("Excluded Locations (Blacklist)")
        self._hint("Leads from these cities/regions are rejected. One per line.")
        self.excluded_box = self._textbox(height=75)

        self._gap()

        self._label("Target Countries (Whitelist)")
        self._hint("Only leads from these countries are accepted. Leave blank to accept all.")
        self.countries_box = self._textbox(height=75)

        self._divider()

        # ── Section: Quantity ──────────────────────────────────────────────────
        self._section("📦  Quantity Filter")

        qty_row = ctk.CTkFrame(self.scroll, fg_color="transparent")
        qty_row.pack(fill="x", pady=(2, 6))

        ctk.CTkLabel(
            qty_row, text="Minimum Quantity Threshold",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(side="left")

        self.min_qty_entry = ctk.CTkEntry(
            qty_row, width=110, placeholder_text="e.g. 50",
            font=ctk.CTkFont(size=13),
        )
        self.min_qty_entry.pack(side="left", padx=(12, 0))

        self._hint("Leads where parsed quantity is below this number are rejected.")

        # Checkbox: allow leads that have no quantity at all
        self.allow_missing_qty_var = ctk.BooleanVar(value=True)
        self.allow_missing_qty_check = ctk.CTkCheckBox(
            self.scroll,
            text="Allow leads with missing / no quantity",
            variable=self.allow_missing_qty_var,
            font=ctk.CTkFont(size=12),
            checkbox_width=18,
            checkbox_height=18,
            checkmark_color="#ffffff",
            fg_color="#4facfe",
            hover_color="#007bff",
            border_color="#4facfe",
        )
        self.allow_missing_qty_check.pack(anchor="w", pady=(4, 2))
        self._hint(
            "When checked, leads where no quantity could be extracted are still accepted. "
            "Leads with an explicit quantity below the threshold are always rejected."
        )
        self._divider()

        # ── Section: Lead Age Filter ───────────────────────────────────────────────
        self._section("⏱️  Lead Age Filter")

        age_row = ctk.CTkFrame(self.scroll, fg_color="transparent")
        age_row.pack(fill="x", pady=(2, 4))

        ctk.CTkLabel(
            age_row, text="Max Lead Age (minutes)",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(side="left")

        self.max_age_entry = ctk.CTkEntry(
            age_row, width=110, placeholder_text="e.g. 60",
            font=ctk.CTkFont(size=13),
        )
        self.max_age_entry.pack(side="left", padx=(12, 0))

        ctk.CTkLabel(
            age_row, text="min",
            font=ctk.CTkFont(size=12), text_color="#888899",
        ).pack(side="left", padx=(6, 0))

        self.disable_age_var = ctk.BooleanVar(value=True)
        self.disable_age_check = ctk.CTkCheckBox(
            self.scroll,
            text="Disable age filter (accept leads of any age)",
            variable=self.disable_age_var,
            command=self._on_disable_age_toggle,
            font=ctk.CTkFont(size=12),
            checkbox_width=18,
            checkbox_height=18,
            checkmark_color="#ffffff",
            fg_color="#4facfe",
            hover_color="#007bff",
            border_color="#4facfe",
        )
        self.disable_age_check.pack(anchor="w", pady=(4, 2))
        self._hint(
            "When checked, all leads pass the age filter regardless of how old they are. "
            "Uncheck to reject leads older than the specified number of minutes."
        )
        self._divider()

        # ── Section: Contact Requirement ───────────────────────────────────────────
        self._section("📇  Contact Channel Preference")

        contact_row = ctk.CTkFrame(self.scroll, fg_color="transparent")
        contact_row.pack(fill="x", pady=(2, 6))

        ctk.CTkLabel(
            contact_row, text="Required Contact Type",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(side="left")

        # 5 specific filter options — evaluated against has_phone / has_whatsapp / has_email
        _CONTACT_FILTER_OPTIONS = [
            "Phone only",
            "Whatsapp only",
            "Email only",
            "Phone / Whatsapp",
            "Phone / Whatsapp / Email",
        ]
        self.contact_filter_var = ctk.StringVar(value="Phone / Whatsapp")
        self.contact_filter_menu = ctk.CTkOptionMenu(
            contact_row,
            variable=self.contact_filter_var,
            values=_CONTACT_FILTER_OPTIONS,
            width=220,
            font=ctk.CTkFont(size=13),
            fg_color="#1a1a30",
            button_color="#4facfe",
            button_hover_color="#007bff",
            dropdown_fg_color="#1a1a30",
            dropdown_hover_color="#2a2a4a",
        )
        self.contact_filter_menu.pack(side="left", padx=(12, 0))

        self._hint(
            "Phone only — lead must show a phone number in the reply modal.  "
            "Whatsapp only — lead must show a WhatsApp link.  "
            "Email only — lead must show an email address.  "
            "Phone / Whatsapp — either phone or WhatsApp is sufficient.  "
            "Phone / Whatsapp / Email — any contact channel is accepted."
        )
        self._divider()

        # ── Section: Quick Reply Template ──────────────────────────────────────────
        self._section("💬  Default Quick Reply Template")

        reply_row = ctk.CTkFrame(self.scroll, fg_color="transparent")
        reply_row.pack(fill="x", pady=(2, 6))

        ctk.CTkLabel(
            reply_row, text="Quick Reply Chip",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(side="left")

        _REPLY_TEMPLATE_OPTIONS = [
            "Catalog Link",
            "Product is available",
            "Thanks for your enquiry",
            "Share more details",
        ]
        self.reply_template_var = ctk.StringVar(value="Catalog Link")
        self.reply_template_menu = ctk.CTkOptionMenu(
            reply_row,
            variable=self.reply_template_var,
            values=_REPLY_TEMPLATE_OPTIONS,
            width=240,
            font=ctk.CTkFont(size=13),
            fg_color="#1a1a30",
            button_color="#4facfe",
            button_hover_color="#007bff",
            dropdown_fg_color="#1a1a30",
            dropdown_hover_color="#2a2a4a",
        )
        self.reply_template_menu.pack(side="left", padx=(12, 0))

        self._hint(
            "The bot will click this quick reply chip inside the reply modal "
            "before sending. Make sure the chip text matches exactly what "
            "IndiaMART shows in the reply popup."
        )
        self._divider()

        # ── Section: Custom Reply Text ───────────────────────────────────────────
        self._section("✏️  Custom Reply Text (Optional)")
        self._hint(
            "If filled, the bot types this message into the reply text box in addition to "
            "(or instead of) clicking a chip. Leave blank to use only the chip above.\n"
            "Supported placeholders: {{buyerName}}  {{product}}  {{country}}  {{qty}}"
        )
        self.custom_reply_box = self._textbox(height=70)
        self._divider()

        # ── Section: Purchase Limits ───────────────────────────────────────────
        self._section("💰  Purchase Limits & Guardrails")

        limits_row = ctk.CTkFrame(self.scroll, fg_color="transparent")
        limits_row.pack(fill="x", pady=(2, 10))

        # Max daily
        daily_col = ctk.CTkFrame(limits_row, fg_color="transparent")
        daily_col.pack(side="left", fill="x", expand=True, padx=(0, 12))
        ctk.CTkLabel(
            daily_col, text="Max Daily Purchases",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            daily_col, text="Bot halts automatically when reached.",
            font=ctk.CTkFont(size=11), text_color="#888899",
        ).pack(anchor="w")
        self.max_daily_entry = ctk.CTkEntry(
            daily_col, width=100, placeholder_text="e.g. 20",
            font=ctk.CTkFont(size=13),
        )
        self.max_daily_entry.pack(anchor="w", pady=(6, 0))

        # Max session
        session_col = ctk.CTkFrame(limits_row, fg_color="transparent")
        session_col.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            session_col, text="Max Session Purchases",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            session_col, text="Resets when you Stop & Start.",
            font=ctk.CTkFont(size=11), text_color="#888899",
        ).pack(anchor="w")
        self.max_session_entry = ctk.CTkEntry(
            session_col, width=100, placeholder_text="e.g. 10",
            font=ctk.CTkFont(size=13),
        )
        self.max_session_entry.pack(anchor="w", pady=(6, 0))

        self._divider()

        # ── Section: Bot Behaviour ─────────────────────────────────────────────
        self._section("🤖  Bot Behaviour")

        # Simulation mode toggle card
        sim_card = ctk.CTkFrame(
            self.scroll,
            fg_color=("#1a1a30", "#1a1a30"),
            corner_radius=10,
            border_width=1,
            border_color="#2a2a4a",
        )
        sim_card.pack(fill="x", pady=(4, 14), ipady=6)

        sim_left = ctk.CTkFrame(sim_card, fg_color="transparent")
        sim_left.pack(side="left", fill="x", expand=True, padx=14, pady=4)

        ctk.CTkLabel(
            sim_left,
            text="🔵  Simulation Mode (Dry-Run)",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            sim_left,
            text=(
                "ON  → All filters run normally. Browser opens. No credits are spent.\n"
                "OFF → Real credits ARE spent for every lead that passes all filters.\n"
                "⚠️  Only disable when you are ready to make live purchases."
            ),
            font=ctk.CTkFont(size=11),
            text_color="#888899",
            justify="left",
        ).pack(anchor="w", pady=(2, 0))

        self.sim_switch = ctk.CTkSwitch(
            sim_card,
            text="",
            width=56,
            onvalue=True,
            offvalue=False,
            progress_color="#f7971e",
            button_color="#ffffff",
            button_hover_color="#eeeeee",
        )
        self.sim_switch.pack(side="right", padx=18, pady=8)

        # ── Delay Slider ───────────────────────────────────────────────────────
        delay_header = ctk.CTkFrame(self.scroll, fg_color="transparent")
        delay_header.pack(fill="x", pady=(0, 2))

        ctk.CTkLabel(
            delay_header,
            text="⏱️  Scan Delay",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(side="left")

        self.delay_val_label = ctk.CTkLabel(
            delay_header,
            text="30.0 s",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#4facfe",
        )
        self.delay_val_label.pack(side="right")

        self._hint(
            "Time the bot waits between each scan cycle. "
            "Changes apply instantly — no restart needed."
        )

        self.delay_slider = ctk.CTkSlider(
            self.scroll,
            from_=1.0, to=60.0,
            command=self._on_delay_slide,
            progress_color="#4facfe",
            button_color="#4facfe",
            button_hover_color="#007bff",
        )
        self.delay_slider.pack(fill="x", pady=(4, 2))

        ends = ctk.CTkFrame(self.scroll, fg_color="transparent")
        ends.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(ends, text="1 s  (fast / aggressive)",  font=ctk.CTkFont(size=10), text_color="#555566").pack(side="left")
        ctk.CTkLabel(ends, text="60 s  (slow / safe)",       font=ctk.CTkFont(size=10), text_color="#555566").pack(side="right")

        self._divider()

        # ── Advanced ────────────────────────────────────────────────────────────
        self._section("🔗  Advanced")

        # Extension port
        ext_row = ctk.CTkFrame(self.scroll, fg_color="transparent")
        ext_row.pack(fill="x", pady=(2, 4))
        ctk.CTkLabel(
            ext_row, text="Chrome Extension Port",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(side="left")
        self.ext_port_entry = ctk.CTkEntry(
            ext_row, width=100, placeholder_text="9223",
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        self.ext_port_entry.pack(side="left", padx=(12, 0))
        self._hint(
            "The local port where the Chrome Extension sends card-detection signals. "
            "Set to 0 to disable the extension bridge. Default: 9223."
        )

        self._label("IndiaMART Leads URL")
        self._hint("The page the bot navigates to for each scan cycle.")
        self.url_entry = ctk.CTkEntry(
            self.scroll,
            placeholder_text="https://seller.indiamart.com/messagecentre/",
            font=ctk.CTkFont(family="Consolas", size=11),
            height=32,
        )
        self.url_entry.pack(fill="x", pady=(0, 6))

        self._divider()

        # ── Save Button ────────────────────────────────────────────────────────
        self.save_btn = ctk.CTkButton(
            self.scroll,
            text="💾  Save Settings",
            font=ctk.CTkFont(size=14, weight="bold"),
            height=46,
            fg_color="#4facfe",
            hover_color="#007bff",
            corner_radius=8,
            command=self._save,
        )
        self.save_btn.pack(fill="x", pady=(10, 4))

        self.save_status = ctk.CTkLabel(
            self.scroll, text="",
            font=ctk.CTkFont(size=12),
        )
        self.save_status.pack(pady=(0, 24))

    # =========================================================================
    # Widget Helpers
    # =========================================================================

    def _section(self, title: str):
        f = ctk.CTkFrame(self.scroll, fg_color=("#22223a", "#22223a"), corner_radius=6)
        f.pack(fill="x", pady=(14, 5))
        ctk.CTkLabel(
            f, text=title,
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#4facfe",
        ).pack(anchor="w", padx=12, pady=6)

    def _label(self, text: str):
        ctk.CTkLabel(
            self.scroll, text=text,
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", pady=(4, 0))

    def _hint(self, text: str):
        ctk.CTkLabel(
            self.scroll, text=text,
            font=ctk.CTkFont(size=11),
            text_color="#666677",
            justify="left",
            wraplength=640,
        ).pack(anchor="w", pady=(1, 3))

    def _textbox(self, height: int) -> ctk.CTkTextbox:
        box = ctk.CTkTextbox(
            self.scroll, height=height,
            font=ctk.CTkFont(family="Consolas", size=12),
            fg_color=("#0d0d1a", "#0d0d1a"),
        )
        box.pack(fill="x", pady=(0, 4))
        return box

    def _divider(self):
        ctk.CTkFrame(self.scroll, height=1, fg_color="#2a2a4a").pack(
            fill="x", pady=(10, 0)
        )

    def _gap(self):
        ctk.CTkFrame(self.scroll, height=8, fg_color="transparent").pack()

    # =========================================================================
    # Load / Save
    # =========================================================================

    def _load_values(self):
        """Populate all widgets from the current settings dict."""
        s = self.settings

        self._set_textbox(self.keywords_box,  s.get("keywords", []))
        self._set_textbox(self.blacklist_box,  s.get("keyword_blacklist", []))
        self._set_textbox(self.excluded_box,  s.get("excluded_locations", []))
        self._set_textbox(self.countries_box, s.get("target_countries", []))

        self._set_entry(self.min_qty_entry,    str(s.get("min_quantity", 1)))
        self._set_entry(self.max_daily_entry,  str(s.get("max_daily_purchases", 20)))
        self._set_entry(self.max_session_entry,str(s.get("max_session_purchases", 10)))

        # Allow missing quantity checkbox
        if s.get("allow_missing_quantity", True):
            self.allow_missing_qty_var.set(True)
        else:
            self.allow_missing_qty_var.set(False)

        # Lead age filter
        max_age = int(s.get("max_lead_age_minutes", 0))
        age_disabled = (max_age <= 0)
        self.disable_age_var.set(age_disabled)
        self._set_entry(self.max_age_entry, str(max_age) if max_age > 0 else "")
        self.max_age_entry.configure(state="disabled" if age_disabled else "normal")

        delay = float(s.get("delay_seconds", 30.0))
        self.delay_slider.set(delay)
        self.delay_val_label.configure(text=f"{delay:.1f} s")

        if s.get("simulation_mode", True):
            self.sim_switch.select()
        else:
            self.sim_switch.deselect()

        # Contact channel preference — migrate legacy values gracefully
        _LEGACY_MAP = {
            "Mobile Number": "Phone only",
            "Email ID":      "Email only",
            "Both":          "Phone / Whatsapp / Email",
        }
        _VALID_CONTACT = {
            "Phone only", "Whatsapp only", "Email only",
            "Phone / Whatsapp", "Phone / Whatsapp / Email",
        }
        raw_contact = s.get("contact_filter_mode", "Phone / Whatsapp")
        contact_val = _LEGACY_MAP.get(raw_contact, raw_contact)
        if contact_val not in _VALID_CONTACT:
            contact_val = "Phone / Whatsapp"
        self.contact_filter_var.set(contact_val)

        # Quick reply template
        _VALID_REPLY = {
            "Catalog Link", "Product is available",
            "Thanks for your enquiry", "Share more details",
        }
        reply_val = s.get("reply_template", "Catalog Link")
        if reply_val not in _VALID_REPLY:
            reply_val = "Catalog Link"
        self.reply_template_var.set(reply_val)

        # Custom reply text
        custom_text = s.get("custom_reply_text", "")
        self.custom_reply_box.delete("1.0", "end")
        if custom_text:
            self.custom_reply_box.insert("1.0", custom_text)

        # Extension port
        self._set_entry(
            self.ext_port_entry,
            str(s.get("extension_port", 9223)),
        )

        self._set_entry(
            self.url_entry,
            s.get("indiamart_leads_url", "https://seller.indiamart.com/bltxn/?pref=recent"),
        )

    @staticmethod
    def _set_textbox(box: ctk.CTkTextbox, items: list):
        box.delete("1.0", "end")
        box.insert("1.0", "\n".join(items))

    @staticmethod
    def _set_entry(entry: ctk.CTkEntry, value: str):
        entry.delete(0, "end")
        entry.insert(0, value)

    def _on_delay_slide(self, value: float):
        """Live-update the label AND the shared settings dict instantly."""
        self.delay_val_label.configure(text=f"{value:.1f} s")
        self.settings["delay_seconds"] = value   # Monitor reads this next cycle

    def _on_disable_age_toggle(self):
        """Grey-out the age entry when the 'disable' checkbox is ticked."""
        state = "disabled" if self.disable_age_var.get() else "normal"
        self.max_age_entry.configure(state=state)

    def _save(self):
        """Validate, update shared settings, persist to disk, notify parent."""
        try:
            keywords    = self._parse_lines(self.keywords_box)
            blacklist   = self._parse_lines(self.blacklist_box)
            excluded    = self._parse_lines(self.excluded_box)
            countries   = self._parse_lines(self.countries_box)
            min_qty     = int(self.min_qty_entry.get()     or 1)
            max_daily   = int(self.max_daily_entry.get()   or 20)
            max_session = int(self.max_session_entry.get() or 10)
            delay       = round(float(self.delay_slider.get()), 1)
            sim_mode    = bool(self.sim_switch.get())
            contact_mode = self.contact_filter_var.get()
            url         = self.url_entry.get().strip() or "https://seller.indiamart.com/messagecentre/"

            # Lead age
            age_disabled = self.disable_age_var.get()
            if age_disabled:
                max_age = 0
            else:
                try:
                    max_age = max(0, int(self.max_age_entry.get() or 0))
                except ValueError:
                    max_age = 0

            # Custom reply text (strip trailing whitespace, preserve newlines)
            custom_reply = self.custom_reply_box.get("1.0", "end").rstrip("\n ").strip()

            # Extension port
            try:
                ext_port = max(0, int(self.ext_port_entry.get() or 0))
            except ValueError:
                ext_port = 9223

            if min_qty < 0 or max_daily < 1 or max_session < 1:
                raise ValueError("Quantity and limits must be positive numbers.")

            # Update shared dict in-place (monitor reads it live)
            self.settings.update({
                "keywords":              keywords,
                "keyword_blacklist":     blacklist,
                "excluded_locations":    excluded,
                "target_countries":      countries,
                "min_quantity":          min_qty,
                "allow_missing_quantity": bool(self.allow_missing_qty_var.get()),
                "max_lead_age_minutes":  max_age,
                "max_daily_purchases":   max_daily,
                "max_session_purchases": max_session,
                "delay_seconds":         delay,
                "simulation_mode":       sim_mode,
                "contact_filter_mode":   self.contact_filter_var.get(),
                "reply_template":        self.reply_template_var.get(),
                "custom_reply_text":     custom_reply,
                "extension_port":        ext_port,
                "indiamart_leads_url":   url,
            })

            # Persist to disk
            with open(self.settings_path, "w", encoding="utf-8") as fh:
                json.dump(self.settings, fh, indent=2)

            # Trigger banner refresh in AppWindow
            if self.on_settings_saved:
                self.on_settings_saved()

            self.save_status.configure(text="✅ Settings saved!", text_color="#4ade80")
            self.after(3_500, lambda: self.save_status.configure(text=""))
            logger.info("Settings saved to disk.")

        except ValueError as exc:
            self.save_status.configure(text=f"⚠️ Invalid value: {exc}", text_color="#f87171")
        except Exception as exc:
            self.save_status.configure(text=f"❌ Save error: {exc}", text_color="#f87171")
            logger.error(f"Settings save error: {exc}")

    @staticmethod
    def _parse_lines(box: ctk.CTkTextbox) -> list:
        """Extract non-empty stripped lines from a CTkTextbox."""
        return [
            ln.strip()
            for ln in box.get("1.0", "end").splitlines()
            if ln.strip()
        ]
