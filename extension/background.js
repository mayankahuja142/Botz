/**
 * background.js — IndiaMART Lead Bridge service worker (v3.0)
 * ============================================================
 * Architecture change (v3):
 *   - content.js now owns ALL lead interception, filtering, and purchasing.
 *   - inject-im-hook.js (MAIN world) intercepts raw API feed JSON.
 *   - Purchases are fired directly from content.js using credentials:"include"
 *     (same-origin to seller.indiamart.com, no CORS issues).
 *
 * This service worker retains its original responsibilities:
 *   1. Respond to { type: "getPort" } messages from content.js.
 *   2. Seed the default port into storage on install/update.
 *
 * Endpoints used by content.js (directly, not via this worker):
 *   GET  http://127.0.0.1:<port>/config
 *   POST http://127.0.0.1:<port>/log
 *   GET  https://seller.indiamart.com/blreact/getBLDisplayData  (fallback poll)
 *   POST https://seller.indiamart.com/blreact/contactBuyNow     (purchase)
 *
 * Security: zero external endpoints, zero remote CDN imports.
 */

"use strict";

const DEFAULT_PORT = 9223;
const STORAGE_KEY = "extension_port";

/**
 * Respond to content-script requests for the current port.
 * content.js can send: { type: "getPort" }
 * and receives back:   { port: number }
 */
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || message.type !== "getPort") return false;

    chrome.storage.local.get([STORAGE_KEY], result => {
        const port = parseInt(result[STORAGE_KEY], 10);
        sendResponse({ port: (!isNaN(port) && port > 0) ? port : DEFAULT_PORT });
    });

    return true;   // Keep channel open for async sendResponse
});

/**
 * On install/update, seed the default port into storage so content.js
 * can read it without the Python backend needing to be running first.
 */
chrome.runtime.onInstalled.addListener(() => {
    chrome.storage.local.get([STORAGE_KEY], result => {
        if (!result[STORAGE_KEY]) {
            chrome.storage.local.set({ [STORAGE_KEY]: DEFAULT_PORT });
        }
    });
    console.log(
        "[LeadBridge BG] Service worker installed (v3.0 — Network-Layer).",
        "Default port:", DEFAULT_PORT
    );
});

/**
 * Continuous Extension-to-Python Handshake
 * Pings the local server every 4 seconds to keep the background worker alive
 * and ensure the Python backend knows the extension is connected.
 */
setInterval(() => {
    chrome.storage.local.get([STORAGE_KEY], async (result) => {
        const port = parseInt(result[STORAGE_KEY], 10) || DEFAULT_PORT;
        try {
            await fetch(`http://127.0.0.1:${port}/config`, {
                method: "GET",
                headers: { "Accept": "application/json" }
            });
        } catch (e) {
            // Network errors are ignored gracefully without crashing
        }
    });
}, 4000);
