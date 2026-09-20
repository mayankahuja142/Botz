#!/usr/bin/env python3
"""
main.py
=======
SafeIM Lead Monitor V2 — Application Entry Point

Startup sequence:
  1. Configure logging (file + console).
  2. Load / create config/settings.json.
  3. Initialize the SQLite database (creates tables on first run).
  4. Launch the CustomTkinter AppWindow and enter the Tk mainloop.

Usage:
  python main.py

First-time setup:
  Double-click install.bat  (or run it from a terminal)
  This installs all Python packages listed in requirements.txt.

Extension-First Architecture:
  No browser is launched by Python. Instead, the app starts a local HTTP
  server on 127.0.0.1:9223.  Install the Chrome Extension (extension/ folder)
  as an unpacked extension in Chrome, then open IndiaMART — the extension
  handles all DOM scanning, filtering, and purchasing automatically.
"""

import logging
import json
import sys
from pathlib import Path

# ── Make sure the project root is on sys.path so sub-packages resolve ─────────
BASE_DIR = Path(__file__).parent.resolve()
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# ── File paths ─────────────────────────────────────────────────────────────────
LOG_FILE      = BASE_DIR / "app.log"
DB_PATH       = BASE_DIR / "leads_history.db"
CONFIG_DIR    = BASE_DIR / "config"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

# ── Default settings (used when settings.json doesn't exist yet) ───────────────
DEFAULT_SETTINGS: dict = {
    "keywords":               ["safety equipment", "fire safety"],
    "excluded_locations":     [],
    "target_countries":       ["India"],
    "min_quantity":           1,
    "max_daily_purchases":    20,
    "max_session_purchases":  10,
    "delay_seconds":          30.0,
    "simulation_mode":        True,          # Always start in safe mode
    "server_port":            9223,          # Local HTTP command center port
    "indiamart_leads_url":    "https://seller.indiamart.com/bltxn/?pref=recent",
    "keyword_blacklist":      [],
    "allow_missing_quantity": True,
    "max_lead_age_minutes":   0,
    "contact_filter_mode":    "Phone / Whatsapp",
    "reply_template":         "Catalog Link",
    "custom_reply_text":      "",
    "extension_port":         9223,
}


# =============================================================================
# Helpers
# =============================================================================

def setup_logging():
    """
    Configure logging to write to both app.log and the console (stdout).
    Log level: INFO for console, DEBUG for file (full detail for debugging).
    """
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    fmt = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(fmt))

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(fmt))

    logging.basicConfig(level=logging.DEBUG, handlers=[file_handler, console_handler])


def load_settings() -> dict:
    """
    Load settings.json from disk.
    If the file doesn't exist, write the defaults and return them.
    Always merges with DEFAULT_SETTINGS so new keys added in future versions
    are automatically present with sensible defaults.
    """
    log = logging.getLogger("settings")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if not SETTINGS_FILE.exists():
        log.info("settings.json not found — creating with defaults.")
        with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
            json.dump(DEFAULT_SETTINGS, fh, indent=2)
        return dict(DEFAULT_SETTINGS)

    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        merged = dict(DEFAULT_SETTINGS)
        merged.update(loaded)
        log.info(
            f"Settings loaded. "
            f"Simulation mode: {merged.get('simulation_mode')} | "
            f"Max daily: {merged.get('max_daily_purchases')}"
        )
        return merged
    except Exception as exc:
        log.error(f"Failed to parse settings.json ({exc}) — using defaults.")
        return dict(DEFAULT_SETTINGS)


# =============================================================================
# Main
# =============================================================================

def main():
    setup_logging()
    log = logging.getLogger("main")

    log.info("=" * 64)
    log.info("  SafeIM Lead Monitor V2  —  Extension-First Mode")
    log.info(f"  Base directory : {BASE_DIR}")
    log.info("=" * 64)

    # ── Settings ───────────────────────────────────────────────────────────────
    settings = load_settings()

    # ── Database ───────────────────────────────────────────────────────────────
    from app.db.database import LeadDatabase
    db = LeadDatabase(DB_PATH)
    db.initialize()
    log.info(f"Database ready at: {DB_PATH}")

    # ── UI ─────────────────────────────────────────────────────────────────────
    import customtkinter as ctk
    from app.ui.app_window import AppWindow

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    log.info("Launching UI window…")
    app = AppWindow(db=db, settings=settings, settings_path=SETTINGS_FILE)
    app.mainloop()

    log.info("Application closed cleanly.")


if __name__ == "__main__":
    main()
