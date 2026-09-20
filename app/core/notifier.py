"""
app/core/notifier.py
====================
Windows toast notification wrapper for SafeIM Lead Monitor V2.

Uses the `plyer` library for native Windows toast notifications.
All functions degrade gracefully (log a warning) if plyer is not installed
or if the notification platform is unsupported — the app never crashes on
notification failure.
"""

import logging

logger = logging.getLogger(__name__)

# plyer is optional — the app works fine without toast notifications
try:
    from plyer import notification as _plyer_notify
    _PLYER_AVAILABLE = True
except ImportError:
    _PLYER_AVAILABLE = False
    logger.warning(
        "plyer not installed — Windows toast notifications are disabled. "
        "Run `pip install plyer` to enable them."
    )

APP_NAME = "SafeIM Lead Monitor V2"
# Set to an absolute path to a .ico file to show a custom tray icon:
APP_ICON  = None


# =============================================================================
# Internal Helper
# =============================================================================

def _notify(title: str, message: str, timeout: int = 8):
    """Send a toast notification, silently logging any failure."""
    if not _PLYER_AVAILABLE:
        logger.debug(f"[Toast skipped] {title}: {message}")
        return
    try:
        _plyer_notify.notify(
            title=title,
            message=message,
            app_name=APP_NAME,
            app_icon=APP_ICON,
            timeout=timeout,
        )
    except Exception as exc:
        logger.warning(f"Toast notification failed (non-critical): {exc}")


# =============================================================================
# Public API
# =============================================================================

def notify_lead_found(lead_title: str, contact_info: str):
    """
    Fire a toast when a lead is successfully purchased (credits spent).
    Only called in Live mode — Simulation mode uses notify_lead_simulated().
    """
    short = lead_title[:55] + "…" if len(lead_title) > 55 else lead_title
    _notify(
        title="✅ New Lead Purchased!",
        message=f"{short}\nContact: {contact_info or 'N/A'}",
        timeout=10,
    )


def notify_lead_simulated(lead_title: str):
    """Fire a toast when a lead passes filters in Simulation (Dry-Run) mode."""
    short = lead_title[:55] + "…" if len(lead_title) > 55 else lead_title
    _notify(
        title="🔵 Lead Simulated (Dry-Run)",
        message=f"{short}\nNo credits spent.",
        timeout=6,
    )


def notify_limit_reached(limit: int):
    """
    Fire a high-priority toast when the daily purchase cap is hit.
    The monitor will pause automatically; the user must increase the limit
    in Settings or wait for the next calendar day.
    """
    _notify(
        title="⛔ Daily Limit Reached",
        message=(
            f"Maximum of {limit} lead purchase(s) reached today.\n"
            "Monitor has been paused. Adjust limit in ⚙️ Settings."
        ),
        timeout=15,
    )


def notify_session_expired():
    """
    Fire an urgent toast when the IndiaMART session expires and the
    scraper detects a redirect to a login/OTP screen.
    """
    _notify(
        title="⚠️ IndiaMART Session Expired",
        message=(
            "Login session expired or timed out.\n"
            "Open the browser window and log in to resume monitoring."
        ),
        timeout=20,
    )
