/**
 * popup.js — IndiaMART Lead Bridge popup controller (v3.0)
 *
 * Manages:
 *  - Play/Pause toggle: persists botPaused to chrome.storage.local
 *  - Port configuration (read/write chrome.storage.local)
 *  - Connection status check (OPTIONS request to 127.0.0.1:<port>)
 *  - Recent event log from chrome.storage.local["recent_events"]
 *
 * Zero external network calls. All traffic stays on 127.0.0.1.
 */

"use strict";

const STORAGE_KEY       = "extension_port";
const EVENTS_KEY        = "recent_events";
const SIGNAL_COUNT_KEY  = "signal_count";
const BOT_PAUSED_KEY    = "botPaused";
const DEFAULT_PORT      = 9223;
const CHECK_INTERVAL_MS = 5000;

// ─── DOM refs ─────────────────────────────────────────────────────────────────
const portInput   = document.getElementById("port-input");
const saveBtn     = document.getElementById("save-port");
const statusDot   = document.getElementById("status-dot");
const statusText  = document.getElementById("status-text");
const eventsList  = document.getElementById("events-list");
const signalCount = document.getElementById("signal-count");

const botToggle   = document.getElementById("bot-toggle");
const toggleIcon  = document.getElementById("toggle-icon");
const toggleLabel = document.getElementById("toggle-label");
const toggleSub   = document.getElementById("toggle-sub");

// ─── Play / Pause toggle ─────────────────────────────────────────────────────

/**
 * Render the toggle button to match the current paused state.
 * Does NOT write to storage — callers do that separately.
 */
function applyToggleVisual(isPaused) {
  if (isPaused) {
    botToggle.className   = "paused";
    toggleIcon.textContent  = "⏸";
    toggleLabel.textContent = "Paused";
    toggleSub.textContent   = "Bot is paused — click to resume.";
  } else {
    botToggle.className   = "running";
    toggleIcon.textContent  = "▶";
    toggleLabel.textContent = "Running";
    toggleSub.textContent   = "Monitoring leads…";
  }
}

/** Read the current paused state from storage and render accordingly. */
function loadToggleState() {
  chrome.storage.local.get([BOT_PAUSED_KEY], (result) => {
    applyToggleVisual(!!result[BOT_PAUSED_KEY]);
  });
}

/** Flip the paused state, persist it, and update the UI. */
botToggle.addEventListener("click", () => {
  chrome.storage.local.get([BOT_PAUSED_KEY], (result) => {
    const wasPaused = !!result[BOT_PAUSED_KEY];
    const nowPaused = !wasPaused;

    chrome.storage.local.set({ [BOT_PAUSED_KEY]: nowPaused }, () => {
      applyToggleVisual(nowPaused);
    });
  });
});

// ─── Port storage ─────────────────────────────────────────────────────────────

function loadPort(cb) {
  chrome.storage.local.get([STORAGE_KEY], (result) => {
    const p = parseInt(result[STORAGE_KEY], 10);
    cb(!isNaN(p) && p > 0 ? p : DEFAULT_PORT);
  });
}

function savePort(port) {
  chrome.storage.local.set({ [STORAGE_KEY]: port }, () => {
    saveBtn.textContent = "✓";
    setTimeout(() => { saveBtn.textContent = "Save"; }, 1500);
  });
}

saveBtn.addEventListener("click", () => {
  const val = parseInt(portInput.value, 10);
  if (!isNaN(val) && val > 0 && val < 65536) {
    savePort(val);
    checkConnection(val);
  }
});

// ─── Connection check ─────────────────────────────────────────────────────────

async function checkConnection(port) {
  // Use GET /config rather than OPTIONS /extension-event.
  // Rationale: do_OPTIONS returns 200 generically for any path, so any
  // process listening on port 9223 would falsely show as "Online".
  // GET /config returns real SafeIM JSON; we validate a known field to
  // confirm the correct server instance is running.
  const url = `http://127.0.0.1:${port}/config`;
  try {
    const resp = await fetch(url, {
      method: "GET",
      headers: { "Accept": "application/json" },
    });
    if (!resp.ok) {
      setStatus(false, port);
      return;
    }
    let body;
    try { body = await resp.json(); } catch (_) { setStatus(false, port); return; }
    // Validate that this is actually a SafeIM backend by checking a known field
    const isSafeIM = body && typeof body === "object" && "server_port" in body;
    setStatus(isSafeIM, port);
  } catch (_) {
    setStatus(false, port);
  }
}

function setStatus(online, port) {
  if (online) {
    statusDot.className    = "status-dot online";
    statusText.className   = "status-text online";
    statusText.textContent = `Online :${port}`;
  } else {
    statusDot.className    = "status-dot offline";
    statusText.className   = "status-text offline";
    statusText.textContent = "Offline";
  }
}

// ─── Recent events ────────────────────────────────────────────────────────────

function renderEvents(events, count) {
  signalCount.textContent = `${count} sent`;

  if (!events || events.length === 0) {
    eventsList.innerHTML =
      '<div class="event-item" style="color:#3a3a5a;font-style:italic;">No signals yet…</div>';
    return;
  }

  eventsList.innerHTML = events
    .slice(-10)
    .reverse()
    .map((ev) => {
      const ts = new Date(ev.timestamp).toLocaleTimeString();
      return `<div class="event-item">
        <b>${ts}</b> — ${ev.count} card(s) detected
      </div>`;
    })
    .join("");
}

function loadEvents() {
  chrome.storage.local.get([EVENTS_KEY, SIGNAL_COUNT_KEY], (result) => {
    renderEvents(result[EVENTS_KEY] || [], result[SIGNAL_COUNT_KEY] || 0);
  });
}

// ─── Boot ─────────────────────────────────────────────────────────────────────

// Load toggle state first so the button renders correctly before anything else
loadToggleState();

loadPort((port) => {
  portInput.value = port;
  checkConnection(port);
  setInterval(() => checkConnection(port), CHECK_INTERVAL_MS);
});

loadEvents();

// Refresh events & toggle state when popup re-focuses
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    loadToggleState();
    loadEvents();
    loadPort((p) => checkConnection(p));
  }
});
