"""
app/core/scraper.py
===================
Playwright-based IndiaMART automation engine for SafeIM Lead Monitor V2.

Key responsibilities:
  1. Launch and manage a persistent Chromium browser context so the user's
     IndiaMART session (cookies) survives app restarts.
  2. Navigate to the IndiaMART seller message centre / buy-leads page.
  3. Detect session expiry by checking for known login/OTP URL patterns
     BEFORE every scrape attempt.
  4. Parse lead cards to extract title, location, country, quantity,
     description, and a unique fingerprint hash.
  5. In Live mode — click "Contact Buyer Now" and scrape revealed contact
     details (name, phone/email).
  6. In Simulation mode — skip the purchase click and return mock data.

Viewport stability design (no black-screen flicker):
  - _is_page_alive()  : Non-throwing check of the page handle.
  - _ensure_page()    : If the page tab was closed by the user, re-open a tab
                        inside the EXISTING browser context (no browser restart,
                        no window flash).
  - _navigate_to_leads(): Uses page.reload() when already on the dashboard URL
                        so the browser window stays stable between poll cycles.
                        Only falls back to page.goto() for the first visit or
                        after a page-recovery event.

──────────────────────────────────────────────────────────────────────────────
SELECTOR GUIDE
──────────────────────────────────────────────────────────────────────────────
Every CSS selector used in this file is annotated with a # SELECTOR: comment.
If IndiaMART updates their page structure, search for "# SELECTOR:" to find
all places that need updating.

To discover the correct selectors:
  1. Open https://seller.indiamart.com/messagecentre/ in the persistent browser.
  2. Right-click a lead card → Inspect Element.
  3. Find the class name / attribute for the element you need.
  4. Update the corresponding constant or fallback list below.
──────────────────────────────────────────────────────────────────────────────
"""

import hashlib
import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# =============================================================================
# IndiaMART URL Constants
# =============================================================================

DEFAULT_LEADS_URL = "https://seller.indiamart.com/bltxn/?pref=recent"

# URL fragments that indicate the browser has been redirected to a login screen.
# Add more patterns here if IndiaMART introduces new auth flows.
LOGIN_URL_PATTERNS: List[str] = [
    "accounts.indiamart.com",
    "/login",
    "/otp",
    "/signin",
    "login.indiamart",
    "auth.indiamart",
    "sso.indiamart",
]

# =============================================================================
# Selector Constants
# Primary selectors — update these first when the page structure changes.
# =============================================================================

# SELECTOR: Top-level container elements for each lead card on the page.
# Multiple patterns are tried in order — the first one that matches wins.
# These MUST target the actual parent wrapper that holds a product title AND
# a purchase/contact action button.  Avoid broad selectors like 'div.card'
# or 'div[class*="lst"]' — those catch small UI controls (Shortlist, Save…).
SEL_CARD_PRIMARY: List[str] = [
    ".bl_card",                            # SELECTOR: Original BuyLead card class
    "div.bld_card",                        # SELECTOR: Bold lead card variant
    "div[class*='bld_']",                  # SELECTOR: BuyLead-bold prefixed containers
    "div[class*='bl_card']",               # SELECTOR: BuyLead card explicit match
    "div[class*='lead-card']",             # SELECTOR: Hyphenated lead-card class
    "div[class*='buy-lead']",              # SELECTOR: Buy-lead section wrapper
    "div[class*='enquiry-card']",          # SELECTOR: Enquiry card variant
    "div[class*='msg-card']",              # SELECTOR: Message card variant
]

# SELECTOR: Product / requirement title inside a card.
SEL_TITLE          = ".prd_name"

# SELECTOR: Buyer city / state location inside a card.
SEL_LOCATION       = ".lctn"

# SELECTOR: Buyer country inside a card.
SEL_COUNTRY        = ".cntry"

# SELECTOR: Requested quantity string inside a card.
SEL_QUANTITY       = ".qty"

# SELECTOR: Full requirement description / message inside a card.
SEL_DESCRIPTION    = ".msg"

# SELECTOR: "Contact Buyer Now" call-to-action button inside a card.
# Multiple alternatives are tried in order — add more as needed.
SEL_CTA_BUTTONS: List[str] = [
    "button:has-text('Contact Buyer Now')",     # SELECTOR: Primary CTA text
    "a:has-text('Contact Buyer Now')",           # SELECTOR: Link-style CTA
    "button:has-text('View Contact')",            # SELECTOR: Alternative CTA
    "button:has-text('Get Contact')",             # SELECTOR: Alternative CTA
    "button:has-text('Buy')",                     # SELECTOR: Buy-lead CTA
    "a:has-text('Buy')",                          # SELECTOR: Buy-lead link CTA
    "button:has-text('Enquire')",                 # SELECTOR: Enquire CTA
    "a:has-text('Enquire')",                      # SELECTOR: Enquire link CTA
    "button:has-text('Enquiry')",                 # SELECTOR: Enquiry CTA
    "a:has-text('Enquiry')",                      # SELECTOR: Enquiry link CTA
    "[class*='contact']:has-text('Contact')",    # SELECTOR: Class-based fallback
    "[class*='btn']:has-text('Contact')",        # SELECTOR: Generic button fallback
    "[class*='btn']:has-text('Buy')",            # SELECTOR: Generic Buy button
    "[class*='btn']:has-text('Enquir')",         # SELECTOR: Generic Enquire button
    # NOTE: bare 'button' and 'a[class*="btn"]' removed — they match UI
    # controls (Shortlist, Save, Share) and cause false-positive card hits.
]

# SELECTOR: Phone / contact info revealed after purchase click.
SEL_CONTACT_INFO: List[str] = [
    ".cntct_no",           # SELECTOR: Contact number element
    "[class*='phone']",    # SELECTOR: Phone number container
    "[class*='mobile']",   # SELECTOR: Mobile number container
    "[class*='contact']",  # SELECTOR: Generic contact container
    "[data-type='phone']", # SELECTOR: Data-attribute phone
    "a[href^='tel:']",     # SELECTOR: Tel-link (auto-includes number)
]

# SELECTOR: Buyer name revealed after purchase click.
SEL_BUYER_NAME: List[str] = [
    ".buyer_name",           # SELECTOR: Buyer name (underscore variant)
    "[class*='buyer-name']", # SELECTOR: Buyer name (hyphen variant)
    "[class*='buyer_name']", # SELECTOR: Buyer name (underscore variant)
    "[class*='name']",       # SELECTOR: Generic name element
]

# =============================================================================
# Reply Modal Selectors
# Added for the 7-step purchase workflow that navigates the contact/reply popup.
# Update these if IndiaMART redesigns the reply modal.
# =============================================================================

# SELECTOR: The reply/contact modal container that appears after clicking CTA.
SEL_REPLY_MODAL: List[str] = [
    "[class*='reply-modal']",   # SELECTOR: Hyphen-variant reply modal
    "[class*='replyModal']",    # SELECTOR: CamelCase reply modal
    "[class*='contact-modal']", # SELECTOR: Contact-specific modal
    "[class*='msgModal']",      # SELECTOR: Message modal variant
    "[class*='blModal']",       # SELECTOR: BuyLead modal variant
    "[role='dialog']",          # SELECTOR: ARIA dialog role
    "[class*='popup']",         # SELECTOR: Generic popup wrapper
    "[class*='modal']",         # SELECTOR: Generic modal wrapper (last resort)
]

# SELECTOR: Phone number in the modal left panel.
SEL_MODAL_PHONE: List[str] = [
    "[class*='mobile_no']",     # SELECTOR: Mobile number (underscore)
    "[class*='mobileNo']",      # SELECTOR: Mobile number (camelCase)
    "[class*='phone_no']",      # SELECTOR: Phone number (underscore)
    "[class*='phoneNo']",       # SELECTOR: Phone number (camelCase)
    "[class*='mob_num']",       # SELECTOR: Mobile number abbreviation
    "[class*='cntct_no']",      # SELECTOR: Contact number abbreviation
    "a[href^='tel:']",          # SELECTOR: Tel href link
    "[class*='mobile']:not([class*='icon'])",  # SELECTOR: Mobile class (no icons)
    "[class*='phone']:not([class*='icon'])",   # SELECTOR: Phone class (no icons)
]

# SELECTOR: WhatsApp contact in the modal left panel.
SEL_MODAL_WHATSAPP: List[str] = [
    "[class*='whatsapp']",      # SELECTOR: WhatsApp class
    "[class*='whatsApp']",      # SELECTOR: WhatsApp camelCase
    "[class*='wa_link']",       # SELECTOR: WhatsApp link abbreviation
    "a[href*='wa.me']",         # SELECTOR: wa.me link
    "a[href*='whatsapp.com']",  # SELECTOR: whatsapp.com link
    "a[href*='api.whatsapp']",  # SELECTOR: API WhatsApp link
]

# SELECTOR: Email address in the modal left panel.
SEL_MODAL_EMAIL: List[str] = [
    "[class*='email_id']",      # SELECTOR: Email ID (underscore)
    "[class*='emailId']",       # SELECTOR: Email ID (camelCase)
    "[class*='email']:not([class*='icon'])",  # SELECTOR: Email class (no icons)
    "a[href^='mailto:']",       # SELECTOR: Mailto href link
]

# SELECTOR: Close / dismiss button on the reply modal.
SEL_MODAL_CLOSE: List[str] = [
    "button[aria-label='Close']",         # SELECTOR: ARIA-labelled close
    "button[aria-label='close']",         # SELECTOR: Lowercase ARIA close
    "[class*='close_btn']",               # SELECTOR: Close button (underscore)
    "[class*='closeBtn']",                # SELECTOR: Close button (camelCase)
    "[class*='modal'] [class*='close']",  # SELECTOR: Modal close child
    "[role='dialog'] [class*='close']",   # SELECTOR: Dialog close child
    "button:has-text('\u00d7')",                 # SELECTOR: × character button
    "button:has-text('Close')",           # SELECTOR: Text-based close
]

# SELECTOR: Send Reply button inside the reply modal.
SEL_SEND_REPLY: List[str] = [
    "button:has-text('Send Reply')",            # SELECTOR: Primary send text
    "button:has-text('Send')",                  # SELECTOR: Short send label
    "[class*='send_reply']",                    # SELECTOR: Send reply class
    "[class*='sendReply']",                     # SELECTOR: Send reply camelCase
    "[class*='send'][class*='btn']",            # SELECTOR: Send + btn combo
    "[class*='modal'] button[class*='primary']", # SELECTOR: Primary button in modal
    "[role='dialog'] button[class*='primary']",  # SELECTOR: Dialog primary button
]

SEL_CARD_FALLBACKS: List[str] = [
    ".lst_card",                      # SELECTOR: List-style lead card
    ".bld_card",                      # SELECTOR: Bold lead card variant
    ".byr_info",                      # SELECTOR: Legacy buyer info card
    "div[id*='card']",                # SELECTOR: ID-based card container
    "div[class*='buy-lead']",         # SELECTOR: Buy-lead section card
    ".leads-card",                    # SELECTOR: Generic leads card
    ".lead-card",                     # SELECTOR: Generic lead card
    ".msg-list-item",                 # SELECTOR: Message list item
]

# Generic selectors that often cause false-positive card matches.
# Elements matching these are skipped unless they also pass validation.
_IGNORE_WRAPPERS: List[str] = [
    "[class*='lead']",                # SELECTOR: Overly broad — matches headers/wrappers
    "[class*='buyer']",               # SELECTOR: Overly broad — matches buyer info panels
    "[class*='enquiry']",             # SELECTOR: Overly broad — matches enquiry sections
]

# Text that identifies a UI control element rather than a lead card.
# If a candidate element's ENTIRE inner text (lowercased, stripped) matches
# one of these, or ALL of its lines are in this set, it is instantly rejected.
_CONTROL_TEXT_BLACKLIST: frozenset = frozenset({
    "shortlist", "save", "share", "details", "more", "less",
    "view details", "view more", "view less", "show more", "show less",
    "hide", "close", "cancel", "dismiss", "filter", "sort",
    "select", "select all", "deselect", "deselect all",
    "bookmark", "bookmarked", "saved", "shared", "shortlisted",
    "refresh", "reload", "load more", "next", "previous", "prev",
    "...", "•••", "⋮", "☰",
})


# =============================================================================
# Custom Exception
# =============================================================================

class SessionExpiredException(Exception):
    """Raised when a login/OTP redirect is detected before or during scraping."""


# =============================================================================
# Scraper Class
# =============================================================================

class IndiaMArtScraper:
    """
    Playwright-powered IndiaMART scraper.

    Thread Safety: Intended to run exclusively inside the MonitorThread.
    Do NOT call from the Tkinter UI thread.

    Lifecycle:
        scraper = IndiaMArtScraper(profile_dir="...", log_callback=...)
        scraper.start()         # Launches browser
        leads = scraper.get_leads()
        lead  = scraper.purchase_lead(lead, simulation_mode=False)
        scraper.stop()          # Closes browser
    """

    def __init__(
        self,
        profile_dir: str,
        leads_url: str = DEFAULT_LEADS_URL,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        self.profile_dir  = profile_dir
        self.leads_url    = leads_url
        self.log_callback = log_callback or (lambda msg: logger.info(msg))

        self._playwright       = None
        self._browser          = None
        self._browser_context = None
        self._page             = None
        self._running          = False
        self._on_leads_url     = False   # True once we have navigated to leads_url

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def start(self):
        """
        Launch Google Chrome with a persistent user profile and the bundled
        unpacked extension pre-loaded.

        Behaviour
        ─────────
        • Uses the system-installed Chrome binary (channel="chrome") — not
          Playwright's bundled Chromium — so all existing Chrome extensions,
          saved passwords, and profile state remain accessible.
        • Loads the bot's unpacked Manifest V3 extension automatically via
          --load-extension so the DOM bridge is active from the first page load.
        • ignore_default_args=["--enable-automation"] suppresses the "Chrome
          is being controlled by automated software" infobar.
        • After launch, navigates directly to the IndiaMART leads URL so the
          monitor cycle can start immediately without an extra navigation step.
        • profile_dir comes from settings (or the constructor default) so the
          user can point it at any Chrome profile they like.
        """
        import os
        from playwright.sync_api import sync_playwright

        # ── Resolve paths ─────────────────────────────────────────────────────
        _here    = Path(__file__).resolve().parent.parent.parent   # project root
        ext_path = str((_here / "extension").resolve())

        # profile_dir: use the value passed to __init__ (read from settings),
        # falling back to a dedicated ./chrome_profile next to the project root.
        profile_dir = self.profile_dir or str((_here / "chrome_profile").resolve())
        Path(profile_dir).mkdir(parents=True, exist_ok=True)

        self.log_callback(
            f"🌐 Launching Chrome...\n"
            f"   Profile : {profile_dir}\n"
            f"   Extension: {ext_path}"
        )
        logger.info(
            "start(): profile_dir=%s  ext_path=%s  leads_url=%s",
            profile_dir, ext_path, self.leads_url,
        )

        self._playwright = sync_playwright().start()

        try:
            self._browser_context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                channel="chrome",        # use the system-installed Google Chrome
                headless=False,          # extensions require a headed browser
                slow_mo=100,
                args=[
                    f"--disable-extensions-except={ext_path}",
                    f"--load-extension={ext_path}",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
                # Suppress "Chrome is being controlled by automated software" banner
                ignore_default_args=["--enable-automation"],
                viewport={"width": 1280, "height": 800},
            )
        except Exception as exc:
            _err = (
                f"❌ Failed to launch Chrome: {exc}\n\n"
                "   Possible causes:\n"
                "   • Google Chrome is not installed on this machine.\n"
                "   • Another Chrome process is already using the same profile directory.\n"
                f"   • Profile path: {profile_dir}"
            )
            logger.error(_err)
            self.log_callback(_err)
            try:
                self._playwright.stop()
            except Exception:
                pass
            raise RuntimeError(_err) from exc

        # ── Select or create the active tab ───────────────────────────────────
        if self._browser_context.pages:
            self._page = self._browser_context.pages[0]
        else:
            self._page = self._browser_context.new_page()

        # ── Auto-navigate to the IndiaMART leads page ─────────────────────────
        try:
            self._page.goto(self.leads_url, wait_until="domcontentloaded", timeout=30_000)
            self._on_leads_url = True
            self.log_callback(f"✅ Chrome launched and navigated to leads page.")
        except Exception as exc:
            # Non-fatal — the monitor will retry navigation on the first cycle
            logger.warning("start(): initial navigation failed (%s). Will retry.", exc)
            self.log_callback(
                f"⚠️ Initial navigation to leads page failed: {exc}\n"
                "   The monitor will retry on the first cycle."
            )

        self._running = True

    def stop(self):
        """
        Close the persistent browser context and stop the Playwright engine.
        """
        self._running      = False
        self._on_leads_url = False
        try:
            if self._browser_context:
                self._browser_context.close()
        except Exception as exc:
            logger.warning("Error closing browser context: %s", exc)
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception as exc:
            logger.warning("Error stopping Playwright: %s", exc)
        self.log_callback("🛑 Browser closed.")

    # =========================================================================
    # Page Stability Helpers
    # =========================================================================

    def _is_page_alive(self) -> bool:
        """
        Non-throwing check: returns True if the current page handle is still
        connected to an open browser tab.

        Playwright raises if you call most APIs on a closed page, so we use
        page.url as a cheap liveness probe — it never navigates, never flashes.
        """
        if self._page is None:
            return False
        try:
            _ = self._page.url   # Will raise if tab/context is closed
            return True
        except Exception:
            return False

    def _ensure_page(self):
        """
        Guarantee that self._page points to a live, usable browser tab.

        Recovery strategy (NO browser restart — no window flash):
          1. If the current page handle is alive → do nothing.
          2. If the browser context still has open pages → adopt the first one.
          3. Otherwise → open a fresh tab inside the existing context.

        Only raises if the context itself has been destroyed (i.e., the user
        closed the entire Chromium window, not just a tab).
        """
        if self._is_page_alive():
            return  # Happy path — page is fine

        logger.warning("Page handle is stale — attempting recovery without browser restart.")
        self.log_callback("⚠️ Browser tab was closed. Recovering page from existing context...")
        self._on_leads_url = False   # Force a fresh goto on next navigation

        # Try to adopt an existing open tab first
        try:
            if self._browser_context and self._browser_context.pages:
                self._page = self._browser_context.pages[0]
                logger.info("Recovered: adopted existing tab from browser context.")
                self.log_callback("✅ Page recovered from existing browser tab.")
                return
        except Exception as exc:
            logger.debug(f"Could not adopt existing page: {exc}")

        # Open a completely new tab in the surviving context
        try:
            if self._browser_context:
                self._page = self._browser_context.new_page()
                logger.info("Recovered: opened new tab in existing browser context.")
                self.log_callback("✅ New browser tab opened — context still alive.")
                return
        except Exception as exc:
            # Context itself is gone — nothing we can do without a full restart
            raise RuntimeError(
                f"Browser context is no longer available ({exc}). "
                "Stop the monitor and click Start to relaunch the browser."
            ) from exc

    # =========================================================================
    # Session Detection  ← Key feature: login redirect guard
    # =========================================================================

    def _check_session(self):
        """
        Inspect the current browser URL for known login/OTP redirect patterns.

        Called automatically at the start of every scrape cycle.
        Raises SessionExpiredException if a redirect is detected, which causes
        the monitor thread to pause gracefully and notify the user.
        """
        try:
            current_url = self._page.url.lower()
            for pattern in LOGIN_URL_PATTERNS:
                if pattern in current_url:
                    msg = (
                        "⚠️ IndiaMART session expired. "
                        "Please open the browser window to log in."
                    )
                    self.log_callback(msg)
                    logger.warning(f"Login redirect detected. URL: {self._page.url}")
                    raise SessionExpiredException(msg)
        except SessionExpiredException:
            raise
        except Exception as exc:
            # Never block on an unexpected URL-check error
            logger.debug(f"Session check warning (non-fatal): {exc}")

    # =========================================================================
    # Navigation
    # =========================================================================

    def _navigate_to_leads(self):
        """
        Navigate to the IndiaMART leads page in the most viewport-stable way:

          • Already on the leads URL  →  page.reload()   (no full navigation
                                          flash; browser window stays still)
          • First visit / post-recovery →  page.goto()  (standard navigation)

        Both paths wait for domcontentloaded (not networkidle) to avoid
        ERR_CONNECTION_CLOSED caused by IndiaMART's persistent background
        telemetry sockets keeping connections open for ~50 s per reload.
        Card readiness is confirmed by an explicit wait_for_selector instead.
        """
        # Always ensure we have a live page before touching the DOM
        self._ensure_page()

        try:
            current_url = ""
            try:
                current_url = self._page.url.lower().rstrip("/")
            except Exception:
                pass   # page was just recovered; current_url stays empty

            target_url = self.leads_url.lower().rstrip("/")

            if self._on_leads_url and target_url in current_url:
                # ── Already on the dashboard — reload (domcontentloaded) ──────────
                # Wrap in try-except: ERR_CONNECTION_CLOSED or timeout on reload
                # means IndiaMART's telemetry closed the socket. Re-establish via
                # a full page.goto() after a brief pause.
                self.log_callback("🔄 Refreshing leads page...")
                try:
                    self._page.reload(wait_until="domcontentloaded", timeout=30_000)
                except Exception as exc:
                    _exc_str = str(exc).lower()
                    if "err_connection_closed" in _exc_str or "net::err" in _exc_str or "timeout" in _exc_str:
                        logger.warning(
                            f"Reload failed ({exc}); sleeping 3 s then re-navigating via goto..."
                        )
                        self.log_callback(
                            "⚠️ Reload connection error — re-establishing TCP connection..."
                        )
                        time.sleep(3)
                        self._page.goto(
                            self.leads_url,
                            wait_until="domcontentloaded",
                            timeout=30_000,
                        )
                    else:
                        raise
            else:
                # ── First visit or post-recovery — full navigation ────────────────
                self.log_callback("🔄 Navigating to leads page...")
                self._page.goto(
                    self.leads_url,
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                self._on_leads_url = True

            # ── Explicit element wait: confirm card readiness ─────────────────────
            # Use selector-based wait instead of networkidle so we don't hang on
            # IndiaMART's background telemetry connections.
            try:
                self._page.wait_for_selector(
                    "text=Contact Buyer Now",
                    timeout=15_000,
                )
                self.log_callback("✅ Page ready — 'Contact Buyer Now' visible.")
            except Exception:
                logger.debug("'Contact Buyer Now' not visible within 15 s — proceeding anyway.")

            # Anti-bot delay after page load (4-9 s)
            load_delay = random.uniform(4.0, 9.0)
            self._page.wait_for_timeout(int(load_delay * 1000))
            self._check_session()               # Abort if redirected to login

        except SessionExpiredException:
            raise
        except Exception as exc:
            self._on_leads_url = False          # Force fresh goto on next attempt
            logger.error(f"Navigation error: {exc}")
            self.log_callback(f"❌ Navigation error: {exc}")
            raise

    # =========================================================================
    # Lead Scraping
    # =========================================================================

    def get_leads(self) -> List[Dict[str, Any]]:
        """
        Navigate to the leads page and scrape all visible lead cards.

        Strategy (SPA-aware, asset-synced, failsafe):
          1. Navigate / reload with networkidle asset load syncing.
          2. Explicitly wait for 'Contact Buyer Now' or lead cards to be VISIBLE.
          3. Extract lead cards via single JS evaluation.
          4. Empty array failsafe guard clause to handle zero-card states safely.
        """
        if not self._running:
            return []

        # _navigate_to_leads() calls _ensure_page() internally, waits for
        # domcontentloaded, and confirms card readiness via wait_for_selector.
        self._navigate_to_leads()   # Raises SessionExpiredException on login redirect

        cards_visible = False
        try:
            self._page.wait_for_selector(
                "text='Contact Buyer Now'",
                timeout=6_000,
                state="visible",  # Force explicit visibility
            )
            cards_visible = True
            self.log_callback("✅ Page synced — 'Contact Buyer Now' is visible.")
        except Exception:
            card_sel = self._resolve_card_selector()
            if card_sel:
                try:
                    self._page.wait_for_selector(card_sel, timeout=4_000, state="visible")
                    cards_visible = True
                    self.log_callback(f"✅ Lead card selector '{card_sel}' is visible.")
                except Exception:
                    pass

        if not cards_visible:
            logger.info("No lead cards or CTA buttons became visible within wait threshold.")
            self.log_callback("ℹ️ No lead elements visible on screen — page may be empty or loading.")

        # ── PRIMARY: Single JS evaluate — extracts all cards at once ──────────
        leads = self._extract_leads_via_js()

        # ── Empty Array Failsafe Guard Clause ──────────────────────────────────
        if not leads or len(leads) == 0:
            logger.warning("Primary JS extractor returned empty or None leads array. Trying CSS fallback...")

            card_selector = self._resolve_card_selector()
            if not card_selector:
                self.log_callback("⚠️ 0 lead cards found (empty array failsafe triggered).")
                return []

            raw_cards = self._page.query_selector_all(card_selector)
            if not raw_cards or len(raw_cards) == 0:
                self.log_callback("⚠️ 0 lead cards found on page.")
                return []

            card_elements = [c for c in raw_cards if self._is_valid_lead_card(c)]
            if not card_elements:
                self.log_callback("⚠️ 0 valid lead cards found after filtering.")
                return []

            leads = []
            for idx, card in enumerate(card_elements):
                try:
                    lead = self._parse_card(card, idx)
                    if lead:
                        leads.append(lead)
                except Exception as exc:
                    logger.warning(f"Error parsing card #{idx}: {exc}")

        # Final empty array guard clause check
        if not leads:
            self.log_callback("ℹ️ Extracted 0 leads in this cycle — safely proceeding to next cycle.")
            return []

        self.log_callback(f"🔍 Found {len(leads)} valid lead card(s).")
        return leads

    # =========================================================================
    # JS-Based Lead Extraction  (single page.evaluate — zero per-card calls)
    # =========================================================================

    # The entire extraction runs inside the browser in one shot.
    # It finds all "Contact Buyer Now" buttons, walks up to the true card
    # wrapper, then extracts title (via time-anchor), country (from the
    # time-anchor line or the line after it), and quantity (from the
    # "Quantity" label).  All parsing is done in JS to avoid Python regex
    # failures and incorrect country defaults.
    _JS_LEAD_EXTRACTOR: str = """
    () => {
        let leads = [];
        let elements = Array.from(document.querySelectorAll('*'));
        let btns = elements.filter(el =>
            el.textContent.trim() === 'Contact Buyer Now' &&
            (el.tagName === 'BUTTON' || el.tagName === 'A' || el.tagName === 'DIV')
        );

        btns.forEach((btn, index) => {
            let card = btn.parentElement;
            while (card && card.tagName !== 'BODY') {
                if (card.innerText && card.innerText.includes('Quantity') && card.innerText.includes('Buyer Details')) {
                    break;
                }
                card = card.parentElement;
            }

            if (card && card.tagName !== 'BODY') {
                let lines = card.innerText.split('\\n').map(l => l.trim()).filter(l => l.length > 0);

                // 1. Find the Time Anchor
                let anchorIndex = lines.findIndex(l => l.match(/\\b(sec|min|hour|hr|days)\\b/i));

                let title = 'Unknown Title';
                let country = 'Unknown';
                let quantity = '1';

                if (anchorIndex !== -1) {
                    // 2. Extract Country (usually on the same line as time, e.g. "0 sec USA")
                    let timeStr = lines[anchorIndex];
                    let parts = timeStr.split(/(?:sec|min|hour|hr|days)s?/i);
                    if (parts.length > 1 && parts[1].trim() !== '') {
                        country = parts[1].replace(/[^a-zA-Z\\s]/g, '').trim();
                    } else if (anchorIndex + 1 < lines.length && lines[anchorIndex + 1].length < 20) {
                        country = lines[anchorIndex + 1].trim();
                    }

                    // 3. Extract Title (protection against grabbing isolated country names)
                    title = lines[anchorIndex - 1];
                    let knownCountries = ['usa', 'canada', 'turkey', 'poland', 'finland', 'uk', 'australia', 'india',
                                          'united states', 'united kingdom', 'malaysia', 'japan', 'south korea', 'china',
                                          'germany', 'france', 'brazil', 'mexico', 'russia', 'uae', 'saudi arabia'];
                    if (anchorIndex > 1 && (title.length <= 3 || knownCountries.some(c => title.toLowerCase().includes(c)))) {
                        title = lines[anchorIndex - 2]; // Go one line higher if previous was a country flag/name
                    }
                } else {
                    title = lines[0];
                }

                // Clean title of breadcrumbs
                if (title && title.includes('>')) {
                    title = title.split('>').pop().trim();
                }

                // 4. Extract Quantity (Fixing the colon bug)
                let qIndex = lines.findIndex(l => l.includes('Quantity'));
                if (qIndex !== -1) {
                    // First check if it's on the same line (e.g., "Quantity : 2 Vial")
                    let inlineMatch = lines[qIndex].split(':');
                    if (inlineMatch.length > 1 && inlineMatch[1].trim().length > 0) {
                        quantity = inlineMatch[1].trim();
                    } else {
                        // Look ahead up to 2 lines to skip standalone colons
                        for (let i = 1; i <= 2; i++) {
                            if (qIndex + i < lines.length) {
                                let val = lines[qIndex + i].replace(/^:?\\s*/, '').trim();
                                if (val.length > 0 && val !== ':') {
                                    quantity = val;
                                    break;
                                }
                            }
                        }
                    }
                }

                if (title) {
                    // Extract the time-ago string for the Lead Age Filter
                    let ageString = '';
                    if (anchorIndex !== -1) {
                        ageString = lines[anchorIndex] || '';
                    }
                    leads.push({
                        title: title,
                        country: country,
                        quantity: quantity,
                        raw_text: card.innerText,
                        button_index: index,
                        age_string: ageString
                    });
                }
            }
        });
        return leads;
    }
    """

    def _extract_leads_via_js(self) -> List[Dict[str, Any]]:
        """
        Run a single page.evaluate() call that extracts all lead cards
        entirely inside the browser.  Returns fully parsed lead dicts.

        Title, country, and quantity are all parsed inside JS — Python
        only does de-duplication, location enrichment, and hash generation.
        """
        try:
            extracted = self._page.evaluate(self._JS_LEAD_EXTRACTOR)
        except Exception as exc:
            logger.warning("JS lead extraction failed: %s", exc)
            return []

        if not extracted:
            return []

        self.log_callback(f"ℹ️ JS extractor returned {len(extracted)} raw card(s).")

        seen_titles: set = set()
        leads: List[Dict[str, Any]] = []

        for item in extracted:
            title    = (item.get("title") or "").strip()
            country  = (item.get("country") or "").strip()
            quantity = (item.get("quantity") or "1").strip()
            raw_text = item.get("raw_text", "")
            btn_idx  = item.get("button_index", 0)

            if not title or len(title) < 3 or title == "Unknown Title":
                continue

            # Skip control labels that leaked through
            if title.lower() in _CONTROL_TEXT_BLACKLIST:
                continue
            if title.lower() in self._CTA_LABELS:
                continue

            # De-duplicate by title
            if title in seen_titles:
                continue
            seen_titles.add(title)

            # Normalise country: "Unknown" or empty → try Python fallback
            if not country or country.lower() == "unknown":
                country = self._detect_country_from_text(raw_text)

            # Extract location from raw_text (Indian states, city names)
            location = self._detect_location_from_text(raw_text)

            # Extract description — longest non-label line
            description = self._extract_description(raw_text)

            lead_hash = _compute_hash(title, location, quantity)
            leads.append({
                "title":         title,
                "location":      location,
                "country":       country,
                "quantity":      quantity,
                "description":   description,
                "lead_hash":     lead_hash,
                "card_index":    btn_idx,
                "customer_name": "",
                "contact_info":  "",
                "raw_text":      raw_text,
                "age_string":    (item.get("age_string") or "").strip(),
                "scraped_at":    time.time(),   # Unix epoch — used by Lead Age Filter
            })

        return leads

    # ── Lightweight Python helpers for fields JS can't always resolve ──────

    def _detect_country_from_text(self, raw_text: str) -> str:
        """Scan raw card text for a known country name. Returns 'Unknown' if none found."""
        lower = raw_text.lower()
        # Check common target countries first
        _countries = [
            "usa", "united states", "australia", "united kingdom", "uk",
            "canada", "malaysia", "japan", "south korea", "china",
            "germany", "france", "brazil", "mexico", "uae",
            "saudi arabia", "turkey", "poland", "finland", "russia",
            "india",  # Check India last so international leads aren't missed
        ]
        for c in _countries:
            if c in lower:
                return c.title()
        return "Unknown"

    def _detect_location_from_text(self, raw_text: str) -> str:
        """Scan raw card text for a known Indian state/city."""
        lower = raw_text.lower()
        for state in self._INDIAN_STATES:
            if state in lower:
                # Find the actual line containing this state
                for line in raw_text.split('\n'):
                    if state in line.lower():
                        return line.strip()
        return ""

    def _extract_description(self, raw_text: str) -> str:
        """Return the longest non-label line from raw card text as description."""
        skip = self._CTA_LABELS | _CONTROL_TEXT_BLACKLIST
        lines = [
            ln.strip() for ln in raw_text.split('\n')
            if ln.strip() and len(ln.strip()) > 10
            and ln.strip().lower() not in skip
        ]
        return max(lines, key=len) if lines else ""

    # ── CTA / control labels used by multiple methods ──────────────────────
    _CTA_LABELS: frozenset = frozenset({
        "contact buyer now", "view contact", "get contact",
        "contact now", "buy lead", "buy now",
        "shortlist", "save", "share", "details",
        "view details", "view more", "show more",
        "buyer also viewed:", "buyer also viewed",
    })

    def _resolve_card_selector(self) -> Optional[str]:
        """
        Find the first selector (primary or fallback) that matches at least
        one element on the current page.

        Timeout is kept at 3 000 ms per selector so the overall fallback
        chain completes quickly instead of hanging 20+ seconds per cycle.
        The primary list (SEL_CARD_PRIMARY) is checked first, then the
        broader fallback list (SEL_CARD_FALLBACKS).
        """
        # ── Phase 1: Primary selectors (fast, specific) ─────────────────────
        for sel in SEL_CARD_PRIMARY:
            try:
                self._page.wait_for_selector(sel, timeout=3_000)
                if sel != SEL_CARD_PRIMARY[0]:
                    logger.info(f"Matched primary card selector: '{sel}'")
                    self.log_callback(
                        f"ℹ️ Using primary selector '{sel}'. "
                        "Update SEL_CARD_PRIMARY in scraper.py if this is unexpected."
                    )
                return sel
            except Exception:
                continue

        # ── Phase 2: Named fallback selectors ───────────────────────────────
        for sel in SEL_CARD_FALLBACKS:
            try:
                self._page.wait_for_selector(sel, timeout=3_000)
                logger.info(f"Using fallback card selector: '{sel}'")
                self.log_callback(
                    f"ℹ️ Using fallback selector '{sel}'. "
                    "Update SEL_CARD_PRIMARY in scraper.py if this is unexpected."
                )
                return sel
            except Exception:
                continue

        # ── Phase 3: Broad / generic selectors (validated) ──────────────────
        for sel in _IGNORE_WRAPPERS:
            try:
                self._page.wait_for_selector(sel, timeout=3_000)
                candidates = self._page.query_selector_all(sel)
                if any(self._is_valid_lead_card(c) for c in candidates):
                    logger.info(f"Using broad selector with validation: '{sel}'")
                    self.log_callback(
                        f"ℹ️ Using broad selector '{sel}' (validated). "
                        "Update SEL_CARD_PRIMARY in scraper.py if this is unexpected."
                    )
                    return sel
            except Exception:
                continue

        # ── No cards found — dump debug snapshot ────────────────────────────
        self.log_callback(
            "⚠️ No lead cards detected on page. "
            "IndiaMART may have changed their layout — update selectors in scraper.py."
        )
        self._dump_debug_snapshot()
        return None

    def _is_valid_lead_card(self, card) -> bool:
        """
        Verify that a candidate DOM element is a genuine lead card and not
        a generic wrapper, header, or nav element that happened to match
        a broad CSS selector.

        Validation strategy (fast rejects first):
          0. Reject if inner text is purely UI control text (blacklist).
          1. Reject known non-card wrapper tags (header/nav/footer/…).
          2. Accept if a structured title + CTA child are found.
          3. Accept if the card's inner text is rich enough (≥ 40 chars,
             ≥ 3 non-blacklisted lines) — this covers layout variants
             where class names changed but the data is still present.
          4. Otherwise reject, and log a debug snippet of the card.
        """
        try:
            # ── Reject 0: Control-element blacklist (instant skip) ─────────
            # Catches "Shortlist", "Save", "Share", "Details" buttons etc.
            try:
                inner_raw = card.inner_text().strip()
                inner_lower = inner_raw.lower()

                # Single-word / single-phrase control?
                if inner_lower in _CONTROL_TEXT_BLACKLIST:
                    return False

                # Every line is a control word?
                raw_lines = [
                    ln.strip() for ln in inner_raw.splitlines() if ln.strip()
                ]
                if raw_lines and all(
                    ln.lower() in _CONTROL_TEXT_BLACKLIST for ln in raw_lines
                ):
                    return False

                # Too short to be a real card (< 10 chars with no newlines)
                if len(inner_raw) < 10 and len(raw_lines) <= 1:
                    return False
            except Exception:
                pass

            # ── Reject 1: Known non-card wrapper tags ─────────────────────
            try:
                tag = card.evaluate("el => el.tagName.toLowerCase()")
                if tag in ("header", "nav", "footer", "aside", "script", "style"):
                    return False
            except Exception:
                pass

            # ── Check 2: Structured title child ───────────────────────────
            title_selectors = [
                SEL_TITLE,                           # .prd_name
                "[class*='product']",
                "[class*='title']",
                "[class*='prd']",
                "[class*='name']",
                "h3", "h4", "h5",
                "strong", "b",
                "span",
                "div > a",
            ]
            has_title = False
            for sel in title_selectors:
                try:
                    el = card.query_selector(sel)
                    if el:
                        txt = el.inner_text().strip()
                        # Title must not be a blacklisted control word
                        if (txt and len(txt) > 2
                                and txt.lower() not in _CONTROL_TEXT_BLACKLIST):
                            has_title = True
                            break
                except Exception:
                    continue

            # ── Check 3: CTA / any clickable element ──────────────────────
            has_cta = False
            for btn_sel in SEL_CTA_BUTTONS:
                try:
                    btn = card.query_selector(btn_sel)
                    if btn:
                        has_cta = True
                        break
                except Exception:
                    continue

            if has_title and has_cta:
                return True

            # ── Check 4: Fallback — inner text heuristic (tightened) ──────
            # Only accept if the card has substantial, non-control text.
            try:
                inner = card.inner_text().strip()
                lines = [ln.strip() for ln in inner.splitlines() if ln.strip()]
                # Exclude lines that are just control labels
                content_lines = [
                    ln for ln in lines
                    if ln.lower() not in _CONTROL_TEXT_BLACKLIST
                ]
                if len(inner) >= 40 and len(content_lines) >= 3:
                    logger.debug(
                        "Card accepted via inner-text heuristic "
                        "(title=%s, cta=%s, %d chars, %d content lines)",
                        has_title, has_cta, len(inner), len(content_lines),
                    )
                    return True
            except Exception:
                pass

            # ── Rejected — log debug snippet ──────────────────────────────
            self._log_skipped_card(card)
            return False
        except Exception:
            return False

    def _log_skipped_card(self, card):
        """
        Log a compact debug snippet for a rejected card element so that
        layout changes can be diagnosed from the activity log.
        """
        try:
            snippet = ""
            try:
                snippet = card.inner_text().strip()
            except Exception:
                pass
            if not snippet:
                try:
                    snippet = card.evaluate(
                        "el => el.outerHTML.substring(0, 500)"
                    )
                except Exception:
                    snippet = "(could not read element)"

            # Truncate for log readability
            if len(snippet) > 300:
                snippet = snippet[:300] + "…"

            logger.info("SKIPPED CARD — snippet: %s", snippet)
            self.log_callback(
                f"🐛 Skipped card (debug): {snippet[:120]}…"
                if len(snippet) > 120 else
                f"🐛 Skipped card (debug): {snippet}"
            )
        except Exception:
            pass

    def _parse_card(self, card, idx: int) -> Optional[Dict[str, Any]]:
        """
        Extract all fields from a single lead card element.

        Strategy:
          1. Try broadened CSS sub-selectors for each field.
          2. If the title is still the generic fallback, fall back to
             regex-based extraction from the card's full inner text.

        Returns None only if no usable data can be extracted at all.
        """

        def text(selector: str, fallback: str = "") -> str:
            """Safely retrieve inner text from a child element."""
            try:
                el = card.query_selector(selector)
                return el.inner_text().strip() if el else fallback
            except Exception:
                return fallback

        def first_text(selectors: List[str], fallback: str = "") -> str:
            """Try multiple selectors and return the first non-empty result."""
            for sel in selectors:
                val = text(sel)
                if val:
                    return val
            return fallback

        default_title = f"Lead #{idx + 1}"

        # ── Title (broadened) ──────────────────────────────────────────────────
        title = first_text([
            SEL_TITLE,                             # SELECTOR: Primary title
            "[class*='product']",                  # SELECTOR: Fallback product class
            "[class*='prd']",                      # SELECTOR: Abbreviated product class
            "[class*='title']",                    # SELECTOR: Fallback title class
            "[class*='name']",                     # SELECTOR: Fallback name class
            "h3", "h4", "h5", "h2",                # SELECTOR: Any heading tag
            "strong", "b",                         # SELECTOR: Bold/emphasized text
            "div > a",                             # SELECTOR: First link inside card
            "span > a",                            # SELECTOR: Linked span text
        ], fallback=default_title)

        # ── Location (broadened) ───────────────────────────────────────────────
        location = first_text([
            SEL_LOCATION,                          # SELECTOR: Primary location
            "[class*='location']",                 # SELECTOR: Fallback location class
            "[class*='lctn']",                     # SELECTOR: Abbreviated location
            "[class*='city']",                     # SELECTOR: Fallback city class
            "[class*='place']",                    # SELECTOR: Fallback place class
            "[class*='addr']",                     # SELECTOR: Fallback address class
        ])

        # ── Country (broadened) ────────────────────────────────────────────────
        country = first_text([
            SEL_COUNTRY,                           # SELECTOR: Primary country
            "[class*='country']",                  # SELECTOR: Fallback country class
            "[class*='cntry']",                    # SELECTOR: Abbreviated country
        ], fallback="India")

        # ── Quantity (broadened) ───────────────────────────────────────────────
        quantity = first_text([
            SEL_QUANTITY,                          # SELECTOR: Primary quantity
            "[class*='quantity']",                 # SELECTOR: Fallback quantity class
            "[class*='qty']",                      # SELECTOR: Fallback qty abbreviation
            "[class*='unit']",                     # SELECTOR: Fallback units class
            "[class*='qnt']",                      # SELECTOR: Another abbreviation
        ], fallback="")

        # ── Description (broadened) ────────────────────────────────────────────
        description = first_text([
            SEL_DESCRIPTION,                       # SELECTOR: Primary description
            "[class*='desc']",                     # SELECTOR: Fallback description class
            "[class*='msg']",                      # SELECTOR: Fallback message class
            "[class*='detail']",                   # SELECTOR: Fallback detail class
            "[class*='requirement']",              # SELECTOR: Requirement text
            "[class*='rqrmnt']",                   # SELECTOR: Abbreviated requirement
            "p",                                   # SELECTOR: Any paragraph inside card
        ])

        # ── Fallback: regex-based full-text extraction ─────────────────────────
        # If broadened selectors failed to find a real title, try parsing
        # the card's entire inner text line-by-line.
        if title == default_title:
            try:
                full_text = card.inner_text().strip()
                if full_text and len(full_text) > 5:
                    parsed = self._parse_card_from_text(full_text, idx)
                    if parsed:
                        # Overlay any fields that our text parser found
                        title       = parsed.get("title") or title
                        location    = parsed.get("location") or location
                        country     = parsed.get("country") or country
                        quantity    = parsed.get("quantity") or quantity
                        description = parsed.get("description") or description
                        logger.info(
                            "Card #%d: used full-text fallback parser (title=%s)",
                            idx, title[:60],
                        )
            except Exception as exc:
                logger.debug("Full-text fallback failed for card #%d: %s", idx, exc)

        # Final quantity fallback
        if not quantity:
            quantity = "1"

        # ── Unique fingerprint hash ────────────────────────────────────────────
        lead_hash = _compute_hash(title, location, quantity)

        full_text = ""
        try:
            full_text = card.inner_text().strip()
        except Exception:
            pass

        return {
            "title":        title,
            "location":     location,
            "country":      country,
            "quantity":     quantity,
            "description":  description,
            "lead_hash":    lead_hash,
            "card_index":   idx,
            "customer_name": "",
            "contact_info":  "",
            "raw_text":     full_text,
        }

    # ── Regex-based full-text card parser ──────────────────────────────────────

    # Common Indian states / territories used for location detection
    _INDIAN_STATES = {
        "andhra pradesh", "arunachal pradesh", "assam", "bihar",
        "chhattisgarh", "goa", "gujarat", "haryana", "himachal pradesh",
        "jharkhand", "karnataka", "kerala", "madhya pradesh",
        "maharashtra", "manipur", "meghalaya", "mizoram", "nagaland",
        "odisha", "punjab", "rajasthan", "sikkim", "tamil nadu",
        "telangana", "tripura", "uttar pradesh", "uttarakhand",
        "west bengal", "delhi", "chandigarh", "puducherry", "jammu",
        "kashmir", "ladakh", "dadra", "daman", "lakshadweep",
        "andaman", "nicobar",
    }

    # Regex: quantity-like patterns  e.g. "500 Pieces", "2,000 Kg", "10 Units"
    _QTY_RE = re.compile(
        r'(\d[\d,.]*)\s*'
        r'(piece|pcs|pc|unit|kg|ton|liter|litre|meter|metre|'
        r'pair|set|box|bag|bundle|roll|dozen|nos|number|quintal|'
        r'square\s*(?:feet|ft|meter)|running\s*(?:feet|ft|meter))s?',
        re.IGNORECASE,
    )

    def _parse_card_from_text(
        self, full_text: str, idx: int,
    ) -> Optional[Dict[str, str]]:
        """
        Best-effort extraction of lead fields from a card's raw inner text.

        Heuristics applied line-by-line:
          • Title   → first non-trivial line (> 5 chars, not a button label)
          • Location→ line containing a known Indian state name
          • Quantity→ line matching a number + unit pattern
          • Desc    → longest remaining line
          • Country → "India" if an Indian state was found, else empty
    """
        lines = [ln.strip() for ln in full_text.splitlines() if ln.strip()]
        if not lines:
            return None

        result: Dict[str, str] = {
            "title": "", "location": "", "country": "",
            "quantity": "", "description": "",
        }

        # Skip lines that are clearly button labels
        _btn_words = {"contact", "buy", "enquire", "enquiry", "view", "get",
                      "send", "submit", "login", "sign"}
        desc_candidates: List[str] = []

        for line in lines:
            lower = line.lower()

            # Skip obvious button / action labels
            if len(line) < 30 and any(w in lower for w in _btn_words):
                continue

            # ── Title: first substantial line ──────────────────────────
            if not result["title"] and len(line) > 5:
                result["title"] = line
                continue

            # ── Quantity: number + unit ────────────────────────────────
            if not result["quantity"]:
                m = self._QTY_RE.search(line)
                if m:
                    result["quantity"] = m.group(0).strip()
                    continue

            # ── Location: contains a known state name ─────────────────
            if not result["location"]:
                for state in self._INDIAN_STATES:
                    if state in lower:
                        result["location"] = line
                        result["country"] = "India"
                        break
                if result["location"]:
                    continue

            # Everything else is a description candidate
            desc_candidates.append(line)

        # Use the longest remaining line as description
        if desc_candidates:
            result["description"] = max(desc_candidates, key=len)

        # Only return if we found at least a title
        if result["title"]:
            return result
        return None

    # =========================================================================
    # Lead Purchase
    # =========================================================================

    def purchase_lead(
        self,
        lead: Dict[str, Any],
        simulation_mode: bool = True,
        config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Unlock a lead's contact information via IndiaMART's reply modal.

        purchase_status key in the returned dict:
          "simulated"         — Simulation mode; no click, no credits spent.
          "purchased"         — All steps verified; contact revealed and logged.
          "filtered_contact"  — Modal opened but contact type didn't match the
                                 user's Contact Channel Preference; modal closed.
          "failed"            — One or more steps failed; NOT counted as purchased.

        7-step live workflow:
          1. Locate & click "Contact Buyer Now" on the target card.
          2. Wait for the reply modal to render.
          3. Extract phone/WhatsApp/email from the modal left panel;
             apply Contact Channel Preference guard — close modal & abort if unmet.
          4. Click the Quick Reply chip matching config["reply_template"].
          5. Click the green "Send Reply" button inside the modal.
          6. Verify: wait for modal close or a success banner.
          7. Reload page (domcontentloaded) to clear any residual UI state.
        """
        _cfg = config or {}

        # ── Guard Clause: Invalid Lead ───────────────────────────────────────────────
        if not lead or not isinstance(lead, dict):
            logger.warning("purchase_lead(): empty or invalid lead object. Skipping.")
            return lead or {}

        title_snippet = lead.get("title", "Untitled")[:50]

        # ── Simulation Mode ───────────────────────────────────────────────────
        if simulation_mode:
            self.log_callback(
                f"🔵 [SIM] Would purchase: '{title_snippet}' — No credits spent."
            )
            lead["customer_name"]   = "Simulated Buyer"
            lead["contact_info"]    = "+91-XXXX-XXXX (Simulation)"
            lead["purchase_status"] = "simulated"
            return lead

        # ── Live Mode ───────────────────────────────────────────────────────────────
        # Safe default — overwritten only when all steps pass.
        lead["purchase_status"] = "failed"

        try:
            # ====================================================================
            # STEP 1 — Locate & click "Contact Buyer Now" on the target card
            # ====================================================================
            card_selector = self._resolve_card_selector() or SEL_CARD_PRIMARY[0]
            cards = self._page.query_selector_all(card_selector)

            if not cards:
                self.log_callback(
                    f"⚠️ [Step 1] No cards found on page for '{title_snippet}'. "
                    "Skipping purchase — status: FAILED."
                )
                logger.warning("purchase_lead(): no cards in DOM. Aborting.")
                return lead

            card_idx = lead.get("card_index", 0)
            if card_idx < 0 or card_idx >= len(cards):
                self.log_callback(
                    f"⚠️ [Step 1] Card index {card_idx} out of range "
                    f"({len(cards)} cards). Status: FAILED."
                )
                logger.warning(
                    f"purchase_lead(): index {card_idx} out of range for {len(cards)} cards."
                )
                return lead

            # ── Fuzzy title verification ─────────────────────────────────────
            # Build a short, normalised substring from the scraped title so
            # trailing spaces, ellipses, and case differences don't matter.
            raw_title   = lead.get("title", "").strip()
            title_key   = raw_title[:40].strip()          # first 40 chars as anchor

            # Use Playwright's Locator API with .filter(has_text=) for a
            # case-insensitive substring match — avoids exact-text hangs.
            try:
                if title_key:
                    filtered_loc = (
                        self._page
                        .locator(card_selector)
                        .filter(has_text=title_key)
                        .first
                    )
                    # 5-second timeout: fail fast if the DOM text doesn't match
                    filtered_loc.wait_for(state="visible", timeout=5_000)
                    target_card_el = filtered_loc.element_handle(timeout=5_000)
                    if target_card_el:
                        target_card = target_card_el   # prefer title-matched card
                        self.log_callback(
                            f"🔍 [Step 1] Fuzzy title match confirmed: '{title_key[:30]}…'"
                        )
                    else:
                        # element_handle returned None despite visible check — fall
                        # through to the index-based card already resolved above.
                        self.log_callback(
                            "⚠️ [Step 1] Fuzzy locator returned no handle; "
                            "falling back to index-based card."
                        )
                else:
                    self.log_callback(
                        "⚠️ [Step 1] No title key available; using index-based card."
                    )
            except Exception as _loc_exc:          # TimeoutError or any Playwright error
                _exc_name = type(_loc_exc).__name__
                logger.warning(
                    "purchase_lead(): card locator timed out / text mismatch "
                    "for '%s' (%s). Aborting purchase.",
                    title_snippet, _exc_name,
                )
                self.log_callback(
                    f"⚠️ [Step 1] Card locator timed out ({_exc_name}) — "
                    f"text mismatch for '{title_snippet}'. Aborting purchase."
                )
                # Return immediately; `finally` block will execute Step 7 reload.
                return lead

            # Find the "Contact Buyer Now" button inside this card
            btn = None
            for btn_sel in SEL_CTA_BUTTONS:
                try:
                    btn = target_card.query_selector(btn_sel)
                    if btn:
                        break
                except Exception:
                    continue

            if not btn:
                # Fallback: fuzzy page-wide locator scoped to card_idx position.
                # Uses :has-text() (substring) instead of text= (exact) to avoid
                # the 30-50 s default Playwright timeout on a missing exact match.
                try:
                    loc = (
                        self._page
                        .locator(f"{card_selector}:has-text('Contact Buyer Now')")
                        .nth(card_idx)
                    )
                    if loc.count() > 0:
                        btn = loc.element_handle(timeout=5_000)
                except Exception:
                    pass

            if not btn:
                self.log_callback(
                    f"⚠️ [Step 1] 'Contact Buyer Now' not found for '{title_snippet}'. "
                    "Status: FAILED."
                )
                return lead

            self.log_callback(f"🖱️ [Step 1] Clicking 'Contact Buyer Now': '{title_snippet}'")
            # Explicit 5-second timeout — prevents the default 30-second hang
            # when the button becomes stale or the SPA unhydrates mid-click.
            btn.click(timeout=5_000)

            # ====================================================================
            # STEP 2 — Wait for the reply / contact modal to render
            # ====================================================================
            modal_el = None
            for modal_sel in SEL_REPLY_MODAL:
                try:
                    self._page.wait_for_selector(
                        modal_sel, timeout=10_000, state="visible"
                    )
                    modal_el = self._page.query_selector(modal_sel)
                    self.log_callback(
                        f"✅ [Step 2] Reply modal detected (selector: '{modal_sel}')."
                    )
                    break
                except Exception:
                    continue

            if not modal_el:
                self.log_callback(
                    f"⚠️ [Step 2] Reply modal did not appear for '{title_snippet}'. "
                    "Status: FAILED."
                )
                logger.warning("purchase_lead(): reply modal not found after click.")
                return lead

            # ====================================================================
            # STEP 3 — Extract contacts from modal left panel & apply filter
            # ====================================================================

            def _extract_text_from_selectors(selectors: List[str], scope) -> str:
                """Return first non-empty text found among the given selectors."""
                for sel in selectors:
                    try:
                        el = scope.query_selector(sel)
                        if el and el.is_visible():
                            # For tel/mailto links prefer the href over inner text
                            if sel.startswith("a[href^='tel:"):
                                href = el.get_attribute("href") or ""
                                val = href.replace("tel:", "").strip()
                            elif sel.startswith("a[href^='mailto:"):
                                href = el.get_attribute("href") or ""
                                val = href.replace("mailto:", "").strip()
                            elif "wa.me" in sel or "whatsapp" in sel:
                                href = el.get_attribute("href") or ""
                                val = href
                            else:
                                val = el.inner_text().strip()
                            if val and len(val) >= 5:
                                return val
                    except Exception:
                        continue
                return ""

            phone_text    = _extract_text_from_selectors(SEL_MODAL_PHONE,    modal_el)
            whatsapp_text = _extract_text_from_selectors(SEL_MODAL_WHATSAPP, modal_el)
            email_text    = _extract_text_from_selectors(SEL_MODAL_EMAIL,    modal_el)

            has_phone    = bool(phone_text)
            has_whatsapp = bool(whatsapp_text)
            has_email    = bool(email_text)

            self.log_callback(
                f"📇 [Step 3] Contacts found — "
                f"Phone: {'✅' if has_phone else '❌'} "
                f"| WhatsApp: {'✅' if has_whatsapp else '❌'} "
                f"| Email: {'✅' if has_email else '❌'}"
            )

            # ─ Apply Contact Channel Preference filter ───────────────────────────
            contact_filter_mode = _cfg.get("contact_filter_mode", "Phone / Whatsapp")
            filter_pass = True

            if contact_filter_mode == "Phone only":
                filter_pass = has_phone
            elif contact_filter_mode == "Whatsapp only":
                filter_pass = has_whatsapp
            elif contact_filter_mode == "Email only":
                filter_pass = has_email
            elif contact_filter_mode == "Phone / Whatsapp":
                filter_pass = has_phone or has_whatsapp
            elif contact_filter_mode == "Phone / Whatsapp / Email":
                filter_pass = has_phone or has_whatsapp or has_email
            else:
                # Legacy / unknown mode — treat as "any contact"
                filter_pass = has_phone or has_whatsapp or has_email

            if not filter_pass:
                # Guard clause: close modal and mark as filtered
                self.log_callback(
                    f"🟡 [Step 3] Contact filter NOT met "
                    f"(mode='{contact_filter_mode}') for '{title_snippet}'. "
                    "Closing modal — status: FILTERED_CONTACT."
                )
                logger.info(
                    "purchase_lead(): contact filter '%s' not satisfied for '%s'. "
                    "phone=%s wa=%s email=%s",
                    contact_filter_mode, title_snippet,
                    has_phone, has_whatsapp, has_email,
                )
                # Attempt to close the modal gracefully
                for close_sel in SEL_MODAL_CLOSE:
                    try:
                        close_btn = self._page.query_selector(close_sel)
                        if close_btn and close_btn.is_visible():
                            close_btn.click()
                            break
                    except Exception:
                        continue
                lead["purchase_status"] = "filtered_contact"
                return lead

            # ====================================================================
            # STEP 4 — Click Quick Reply chip + optional Custom Reply Text
            # ====================================================================
            reply_template = _cfg.get("reply_template", "Catalog Link")
            chip_clicked = False

            # ── Template variable substitution helper ────────────────────────────
            def _substitute_template(text: str) -> str:
                """Replace {{placeholder}} tokens with values from the lead dict."""
                buyer_name = lead.get("customer_name", "") or "Buyer"
                product    = lead.get("title", "")
                country    = lead.get("country", "")
                qty        = lead.get("quantity", "")
                return (
                    text
                    .replace("{{buyerName}}", buyer_name)
                    .replace("{{product}}",   product)
                    .replace("{{country}}",   country)
                    .replace("{{qty}}",       qty)
                )

            # ── Click the quick-reply chip ───────────────────────────────────────
            chip_selectors = [
                f"button:has-text('{reply_template}')",
                f"[class*='chip']:has-text('{reply_template}')",
                f"[class*='quick']:has-text('{reply_template}')",
                f"[class*='tag']:has-text('{reply_template}')",
                f"[class*='pill']:has-text('{reply_template}')",
                f"*:has-text('{reply_template}')",  # broad fallback
            ]

            for chip_sel in chip_selectors:
                try:
                    chip_el = modal_el.query_selector(chip_sel)
                    if not chip_el:
                        chip_el = self._page.query_selector(chip_sel)
                    if chip_el and chip_el.is_visible():
                        chip_el.click()
                        chip_clicked = True
                        self.log_callback(
                            f"✅ [Step 4] Quick reply chip clicked: '{reply_template}'."
                        )
                        break
                except Exception:
                    continue

            if not chip_clicked:
                self.log_callback(
                    f"⚠️ [Step 4] Quick reply chip '{reply_template}' not found. "
                    "Proceeding without chip selection — will attempt Send Reply anyway."
                )
                logger.warning(
                    "purchase_lead(): quick reply chip '%s' not found for '%s'.",
                    reply_template, title_snippet,
                )

            # ── Custom Reply Text: type into modal text input ─────────────────
            custom_reply_raw = _cfg.get("custom_reply_text", "").strip()
            if custom_reply_raw:
                custom_reply_msg = _substitute_template(custom_reply_raw)
                _REPLY_INPUT_SELS = [
                    "textarea[placeholder*='reply']",
                    "textarea[placeholder*='message']",
                    "textarea[placeholder*='type']",
                    "textarea[placeholder*='write']",
                    "[contenteditable='true']",
                    "textarea",          # broad fallback
                ]
                _typed = False
                for inp_sel in _REPLY_INPUT_SELS:
                    try:
                        inp_el = modal_el.query_selector(inp_sel)
                        if not inp_el:
                            inp_el = self._page.query_selector(inp_sel)
                        if inp_el and inp_el.is_visible():
                            inp_el.triple_click()          # Select all existing text
                            inp_el.type(custom_reply_msg, delay=30)
                            _typed = True
                            self.log_callback(
                                f"✍️ [Step 4] Custom reply typed "
                                f"({len(custom_reply_msg)} chars)."
                            )
                            break
                    except Exception:
                        continue
                if not _typed:
                    logger.warning(
                        "purchase_lead(): could not find reply text input for '%s'.",
                        title_snippet,
                    )



            # ====================================================================
            # STEP 5 — Click the Send Reply button
            # ====================================================================
            send_clicked = False
            for send_sel in SEL_SEND_REPLY:
                try:
                    # Search inside the modal, fall back to page-wide
                    send_el = modal_el.query_selector(send_sel)
                    if not send_el:
                        send_el = self._page.query_selector(send_sel)
                    if send_el and send_el.is_visible():
                        send_el.click()
                        send_clicked = True
                        self.log_callback(
                            f"✅ [Step 5] 'Send Reply' button clicked."
                        )
                        break
                except Exception:
                    continue

            if not send_clicked:
                self.log_callback(
                    f"⚠️ [Step 5] 'Send Reply' button not found for '{title_snippet}'. "
                    "Status: FAILED."
                )
                logger.warning(
                    "purchase_lead(): Send Reply button not found for '%s'.",
                    title_snippet,
                )
                return lead

            # ====================================================================
            # STEP 6 — Verify: modal closes OR success banner appears
            # ====================================================================
            purchase_verified = False

            # 6a. Wait for modal to disappear (strongest signal of success)
            for modal_sel in SEL_REPLY_MODAL:
                try:
                    self._page.wait_for_selector(
                        modal_sel, timeout=8_000, state="hidden"
                    )
                    purchase_verified = True
                    self.log_callback("✅ [Step 6] Modal closed — reply sent successfully.")
                    break
                except Exception:
                    continue

            # 6b. If modal is still open, look for a success text banner
            if not purchase_verified:
                _SUCCESS_TEXTS = [
                    "Reply Sent",
                    "Message Sent",
                    "sent successfully",
                    "Contact added through BuyLead consumed",
                    "contact added",
                    "credit consumed",
                    "lead purchased",
                    "buylead consumed",
                ]
                for success_text in _SUCCESS_TEXTS:
                    try:
                        self._page.wait_for_selector(
                            f"text='{success_text}'",
                            timeout=5_000,
                            state="visible",
                        )
                        purchase_verified = True
                        self.log_callback(
                            f"✅ [Step 6] Success banner found: '{success_text}'."
                        )
                        break
                    except Exception:
                        continue

            if not purchase_verified:
                self.log_callback(
                    f"⚠️ [Step 6] Could not verify reply was sent for '{title_snippet}'. "
                    "Status: FAILED — NOT counting toward daily limit."
                )
                logger.warning(
                    "purchase_lead(): reply verification failed for '%s'.",
                    title_snippet,
                )
                # purchase_status stays "failed"
            else:
                # ─ Verified: compose contact string from what was extracted in Step 3 ─
                contact_parts = []
                if phone_text:
                    contact_parts.append(f"Phone: {phone_text}")
                if whatsapp_text:
                    contact_parts.append(f"WA: {whatsapp_text}")
                if email_text:
                    contact_parts.append(f"Email: {email_text}")
                contact_info = " | ".join(contact_parts)

                # Try to extract buyer name from the modal
                customer_name = lead.get("customer_name", "")
                if not customer_name and modal_el:
                    for sel in SEL_BUYER_NAME:
                        try:
                            el = modal_el.query_selector(sel)
                            if el:
                                raw = el.inner_text().strip()
                                if raw and raw.lower() not in _CONTROL_TEXT_BLACKLIST:
                                    customer_name = raw
                                    break
                        except Exception:
                            continue

                lead["customer_name"]   = customer_name
                lead["contact_info"]    = contact_info
                lead["purchase_status"] = "purchased"

                self.log_callback(
                    f"✅ [Step 6] Purchase VERIFIED — "
                    f"Name: {customer_name or 'N/A'} | "
                    f"Contact: {contact_info or 'N/A'} | "
                    f"Status: PURCHASED."
                )

        except SessionExpiredException:
            raise
        except Exception as exc:
            logger.error(
                "purchase_lead(): unexpected error for '%s': %s",
                title_snippet, exc,
            )
            self.log_callback(f"❌ [Purchase] Unexpected error: {exc} — Status: FAILED.")
            lead["purchase_status"] = "failed"

        finally:
            # ====================================================================
            # STEP 7 — Post-purchase cleanup: reload page (domcontentloaded)
            # ====================================================================
            try:
                self.log_callback("🔄 [Step 7] Post-action cleanup: reloading page...")
                try:
                    self._page.reload(wait_until="domcontentloaded", timeout=30_000)
                except Exception as reload_exc:
                    _exc_str = str(reload_exc).lower()
                    if (
                        "err_connection_closed" in _exc_str
                        or "net::err" in _exc_str
                        or "timeout" in _exc_str
                    ):
                        logger.warning(
                            "[Step 7] Reload failed (%s); sleeping 3 s then goto...",
                            reload_exc,
                        )
                        time.sleep(3)
                        self._page.goto(
                            self.leads_url,
                            wait_until="domcontentloaded",
                            timeout=30_000,
                        )
                    else:
                        raise
                cleanup_delay = random.uniform(4.0, 9.0)
                self._page.wait_for_timeout(int(cleanup_delay * 1000))
                self._on_leads_url = True
            except Exception as cleanup_exc:
                logger.warning("[Step 7] Post-action cleanup warning: %s", cleanup_exc)

        return lead

    # =========================================================================
    # Debug Helpers
    # =========================================================================

    def _dump_debug_snapshot(self):
        """
        Save a lightweight HTML snapshot and log the top-level body structure
        when zero lead cards are found.  Helps diagnose IndiaMART layout changes
        without requiring a live debugging session.

        Snapshots are written to  <project_root>/debug_snapshots/  with a
        timestamped filename.  Only the first 200 KB of HTML is saved to avoid
        bloating disk usage.
        """
        try:
            # ── Log top-level body children for quick inspection ────────────
            body_structure = self._page.evaluate("""
                () => {
                    const body = document.body;
                    if (!body) return 'No <body> element found';
                    const children = Array.from(body.children).slice(0, 30);
                    return children.map(el => {
                        const tag = el.tagName.toLowerCase();
                        const id  = el.id ? '#' + el.id : '';
                        const cls = el.className && typeof el.className === 'string'
                            ? '.' + el.className.trim().split(/\\s+/).join('.')
                            : '';
                        const childCount = el.children ? el.children.length : 0;
                        return `<${tag}${id}${cls}> (${childCount} children)`;
                    }).join('\\n');
                }
            """)

            logger.info(
                "DEBUG SNAPSHOT — Top-level <body> children:\n%s", body_structure
            )
            self.log_callback(
                f"🐛 Debug: page body structure logged "
                f"({len(body_structure)} chars). Check app.log for details."
            )

            # ── Optionally save HTML snapshot to disk ───────────────────────
            try:
                snapshot_dir = Path(self.profile_dir).parent / "debug_snapshots"
                snapshot_dir.mkdir(parents=True, exist_ok=True)

                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                snapshot_path = snapshot_dir / f"page_{timestamp}.html"

                html_content = self._page.content()
                # Cap at 200 KB to avoid disk bloat
                max_bytes = 200 * 1024
                if len(html_content) > max_bytes:
                    html_content = html_content[:max_bytes] + "\n<!-- TRUNCATED -->"

                snapshot_path.write_text(html_content, encoding="utf-8")
                logger.info("HTML snapshot saved to: %s", snapshot_path)
                self.log_callback(
                    f"💾 Debug snapshot saved: {snapshot_path.name}"
                )
            except Exception as file_exc:
                logger.warning("Could not save HTML snapshot: %s", file_exc)

        except Exception as exc:
            logger.warning("Debug snapshot failed: %s", exc)


# =============================================================================
# Utilities
# =============================================================================

def _compute_hash(title: str, location: str, quantity: str) -> str:
    """SHA-256 fingerprint for duplicate detection."""
    raw = (
        f"{title.strip().lower()}|"
        f"{location.strip().lower()}|"
        f"{quantity.strip().lower()}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
