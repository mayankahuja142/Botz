/**
 * content.js — IndiaMART Lead Bridge v3 (Network-Layer, Credit-Safe)
 * ====================================================================
 * Runs in the ISOLATED world on seller.indiamart.com/bltxn/* pages.
 *
 * Architecture (v3 — network-layer interception)
 * -----------------------------------------------
 *  • inject-im-hook.js (MAIN world) monkey-patches window.fetch and XHR to
 *    intercept /blreact/getBLDisplayData responses and emit them as:
 *       CustomEvent("im:leadsData", { detail: <raw JSON> })
 *
 *  • This script listens for that event, normalises each lead, runs it through
 *    the 6-stage filter pipeline, and — in Live Mode — fires a direct API
 *    purchase request to /blreact/contactBuyNow with credentials: "include".
 *
 * What was removed from v2
 * -------------------------
 *  ✗  MutationObserver / DOM card scanning
 *  ✗  extractLeadFromCard(), findLeadCards(), extractCountryAndAge()
 *  ✗  detectContactIconsOnCard(), parseAvailableContacts()
 *  ✗  waitForReplyModal(), waitForSendReplyButton(), closeModal()
 *  ✗  executePurchase() DOM-click version, _postClickWorkflow()
 *  ✗  sleep()-based delays waiting for modal/button renders
 *
 * What was kept from v2
 * ----------------------
 *  ✓  fetchConfig(), getConfig(), postLog()  — Python backend comms
 *  ✓  runStages1to6()                        — text-based filter pipeline
 *  ✓  syncHash(), buildLeadHash()            — deduplication helpers
 *  ✓  parseAgeMinutes()                      — age-string parser
 *  ✓  COUNTRY_ALIASES                        — country normalisation map
 *  ✓  _autoWakerInterval                     — inactivity-modal dismisser
 *  ✓  scheduleAutoRefresh()                  — tab-toggle AJAX refresh trick
 *
 * Anti-detection & throttling constants (tune here)
 * --------------------------------------------------
 *  CONSUME_CONCURRENCY = 4     Max parallel purchase requests
 *  DEDUP_TTL_MS        = 180 000  3-minute TTL on the seen-ID cache
 *  FALLBACK_POLL_MS    = 1 200  Min gap between active fallback polls
 */

"use strict";

// =============================================================================
// Constants & State
// =============================================================================

const DEFAULT_PORT         = 9223;
const DEDUP_TTL_MS         = 3 * 60 * 1000;   // 3 minutes
const CONSUME_CONCURRENCY  = 4;                // max parallel purchases
const FALLBACK_POLL_MS     = 1200;             // min ms between fallback polls

let _config          = null;    // cached settings from Python backend
let _configFetchTime = 0;       // epoch ms of last successful config fetch
let _activePurchases = 0;       // concurrency counter

/**
 * TTL-based dedup cache.
 * Map<ofrid: string, timestampMs: number>
 * Entries expire after DEDUP_TTL_MS.
 */
const _seenIds = new Map();

let _lastPollTime = 0;          // epoch ms of last fallback poll

/**
 * Mirrors chrome.storage.local botPaused value.
 * Updated immediately by a chrome.storage.onChanged listener in boot()
 * so pausing from the popup takes effect without a page reload.
 */
let _botPaused = false;

// =============================================================================
// Utility: Hashing (kept for postLog lead_hash)
// =============================================================================

async function sha256(str) {
    const buf = await crypto.subtle.digest(
        "SHA-256",
        new TextEncoder().encode(str)
    );
    return Array.from(new Uint8Array(buf))
        .map(b => b.toString(16).padStart(2, "0"))
        .join("");
}

/** Fast synchronous djb2 hash for session-level dedup. */
function syncHash(title, location, quantity) {
    const raw = `${(title || "").trim().toLowerCase()}|${(location || "").trim().toLowerCase()}|${(quantity || "").trim().toLowerCase()}`;
    let h = 5381;
    for (let i = 0; i < raw.length; i++) h = ((h << 5) + h) ^ raw.charCodeAt(i);
    return (h >>> 0).toString(16);
}

/** Async SHA-256 hash sent as lead_hash in POST /log. */
async function buildLeadHash(title, location, quantity) {
    const raw = `${(title || "").trim().toLowerCase()}|${(location || "").trim().toLowerCase()}|${(quantity || "").trim().toLowerCase()}`;
    return sha256(raw);
}

// =============================================================================
// Utility: Timestamp
// =============================================================================

/**
 * Returns the current local datetime formatted as "DD-MM-YYYY HH:mm:ss".
 * Required by the IndiaMART contactBuyNow API (ptime field).
 */
function nowPtime() {
    const d   = new Date();
    const pad = n => String(n).padStart(2, "0");
    return (
        `${pad(d.getDate())}-${pad(d.getMonth() + 1)}-${d.getFullYear()} ` +
        `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
    );
}

// =============================================================================
// Utility: Age parsing (kept for Stage 3)
// =============================================================================

/**
 * Parse a relative time string into minutes.
 *   "5 min ago"   → 5
 *   "2 hours ago" → 120
 *   "1 day ago"   → 1440
 */
function parseAgeMinutes(ageStr) {
    if (!ageStr) return 0;
    const m = ageStr.match(/(\d+)\s*(sec|min|hour|hr|day)s?/i);
    if (!m) return 0;
    const n    = parseInt(m[1], 10);
    const unit = m[2].toLowerCase();
    if (unit === "sec")                   return n / 60;
    if (unit === "min")                   return n;
    if (unit === "hour" || unit === "hr") return n * 60;
    if (unit === "day")                   return n * 1440;
    return 0;
}

// =============================================================================
// Utility: Quantity parsing
// =============================================================================

/**
 * Parse a quantity string into a number.
 * - ranges use the first numeric value
 * - decimal values are preserved
 * - thousands separators are handled
 * - invalid/missing values become 0
 */
function parseQuantity(qtyStr) {
    if (!qtyStr || typeof qtyStr !== "string") return 0;
    
    // Remove commas used as thousands separators
    const cleanStr = qtyStr.replace(/,/g, "");
    
    // Match the first number (integer or decimal)
    const match = cleanStr.match(/\d+(?:\.\d+)?/);
    if (!match) return 0;
    
    return parseFloat(match[0]) || 0;
}

// =============================================================================
// TTL Dedup Cache
// =============================================================================

/**
 * Returns true if the ofrid has been seen within the last DEDUP_TTL_MS.
 * Otherwise records it and returns false.
 * Also evicts stale entries on every call to bound memory usage.
 */
function isDuplicate(ofrid) {
    const now = Date.now();

    // Evict expired entries
    for (const [id, ts] of _seenIds) {
        if (now - ts > DEDUP_TTL_MS) _seenIds.delete(id);
    }

    if (_seenIds.has(ofrid)) return true;
    _seenIds.set(ofrid, now);
    return false;
}

// =============================================================================
// Config: GET /config
// =============================================================================

async function fetchConfig() {
    const port = DEFAULT_PORT;
    try {
        const resp = await fetch(`http://127.0.0.1:${port}/config`, { method: "GET" });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        _config = await resp.json();
        _configFetchTime = Date.now();
        return _config;
    } catch (e) {
        console.warn("[LeadBridge] GET /config failed:", e.message,
            "— Is the Python bot running?");
        return null;
    }
}

async function getConfig() {
    if (!_config || Date.now() - _configFetchTime > 30_000) {
        await fetchConfig();
    }
    return _config;
}

// =============================================================================
// POST /log
// =============================================================================

async function postLog(payload) {
    const port  = (_config && _config.server_port) || DEFAULT_PORT;
    const reqId = Date.now() + "-" + Math.floor(Math.random() * 1000);
    try {
        const controller = new AbortController();
        const timeoutId  = setTimeout(() => controller.abort(), 5000);

        const resp = await fetch(`http://127.0.0.1:${port}/log`, {
            method:  "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Request-Id": reqId,
            },
            body:    JSON.stringify(payload),
            signal:  controller.signal,
        });
        clearTimeout(timeoutId);

        if (!resp.ok) {
            const errorStr = `HTTP ${resp.status} ${resp.statusText}`.trim();
            return { success: false, error: errorStr };
        }
        return { success: true };
    } catch (e) {
        const errorStr = e.name === "AbortError" ? "Request timed out" : (e.message || "Network Error");
        return { success: false, error: errorStr };
    }
}

// =============================================================================
// Filter Pipeline — Stages 1–6 (text-based, runs on API-normalised data)
// =============================================================================

/** Country alias map. */
const COUNTRY_ALIASES = {
    "usa":     "united states",  "us":    "united states",
    "america": "united states",  "uk":    "united kingdom",
    "u.k.":   "united kingdom",  "britain":"united kingdom",
    "england":"united kingdom",  "uae":   "united arab emirates",
    "emirates":"united arab emirates", "ksa": "saudi arabia",
    "aus":    "australia",  "can":  "canada",
    "ger":    "germany",    "fra":  "france",
    "jpn":    "japan",      "jp":   "japan",
    "kor":    "south korea","kr":   "south korea",
    "chn":    "china",      "cn":   "china",
    "ind":    "india",      "in":   "india",
    "sgp":    "singapore",  "sg":   "singapore",
    "mys":    "malaysia",   "my":   "malaysia",
};

/**
 * Run stages 1–6 of the filter pipeline on a normalised lead object.
 *
 * lead must have:
 *   title, country, quantity, location, ageStr, rawText
 *
 * Returns { passed: bool, reason: string, score: number }
 */
function runStages1to6(lead, config) {
    const rawText  = (lead.rawText  || "").toLowerCase();
    const country  = (lead.country  || "").toLowerCase();
    const location = (lead.location || "").toLowerCase();
    const qty      = lead.quantity  || "";

    // Stage 1 — Location blacklist
    for (const bl of (config.excluded_locations || [])) {
        const b = bl.toLowerCase();
        if (b && (location.includes(b) || rawText.includes(b))) {
            return { passed: false, reason: `Location blacklisted: ${bl}`, score: 0 };
        }
    }

    // Stage 2 — Country whitelist
    const targetCountries = (config.target_countries || []).map(c => c.toLowerCase());
    if (targetCountries.length > 0) {
        const normCountry = COUNTRY_ALIASES[country] || country;
        const matched = targetCountries.some(tc => {
            const normTC = COUNTRY_ALIASES[tc] || tc;
            return (
                normTC === normCountry ||
                rawText.includes(tc) ||
                rawText.includes(COUNTRY_ALIASES[tc] || "__nomatch__")
            );
        });
        if (!matched) {
            return { passed: false, reason: `Country not in whitelist: '${lead.country}'`, score: 0 };
        }
    }

    // Stage 3 — Lead age
    const maxAge = parseInt(config.max_lead_age_minutes || 0, 10);
    if (maxAge > 0) {
        const ageMins = parseAgeMinutes(lead.ageStr);
        if (ageMins > maxAge) {
            return {
                passed: false,
                reason: `Lead too old: ${Math.round(ageMins)} min (max ${maxAge} min)`,
                score: 0,
            };
        }
    }

    // Stage 4 — Keyword whitelist
    const keywords = (config.keywords || []).map(k => k.toLowerCase());
    let keywordScore = 20;
    if (keywords.length > 0) {
        const matched = keywords.filter(kw => kw && rawText.includes(kw));
        if (matched.length === 0) {
            return { passed: false, reason: "No whitelist keyword matched", score: 0 };
        }
        keywordScore = Math.min(40, Math.round((matched.length / keywords.length) * 200));
    }

    // Stage 5 — Keyword blacklist
    for (const bl of (config.keyword_blacklist || [])) {
        const b = bl.toLowerCase();
        if (b && rawText.includes(b)) {
            return { passed: false, reason: `Blacklisted keyword: ${bl}`, score: 0 };
        }
    }

    // Stage 6 — Minimum quantity
    const minQty = parseInt(config.min_quantity || 1, 10);
    if (minQty > 1) {
        const qtyNum = parseQuantity(qty);
        if (qtyNum > 0 && qtyNum < minQty) {
            return {
                passed: false,
                reason: `Quantity ${qtyNum} below minimum ${minQty}`,
                score: 0,
            };
        }
    }

    const countryScore = 30;
    const qtyScore     = Math.min(20, Math.max(5, parseQuantity(qty) || 5));
    const recencyScore = 10;
    const score        = Math.min(100, keywordScore + countryScore + qtyScore + recencyScore);

    return { passed: true, reason: "All pre-click stages passed", score };
}

// =============================================================================
// Feed Payload Parser
// =============================================================================

/**
 * Attempt to extract a lead array from the raw getBLDisplayData JSON response.
 * IndiaMART's response shape has changed over time; we probe multiple paths
 * defensively so this survives API changes without a full rewrite.
 *
 * Returns an array of raw lead objects (may be empty).
 */
function extractLeadsFromPayload(payload) {
    if (!payload || typeof payload !== "object") return [];

    // Known payload shapes (most-specific first)
    const candidates = [
        payload?.DisplayList,
        payload?.data?.DisplayList,
        payload?.data?.RESPONSE?.items,
        payload?.data?.leads,
        payload?.data?.buyerList,
        payload?.data?.listData,
        payload?.RESPONSE?.items,
        payload?.leads,
        payload?.items,
    ];

    for (const c of candidates) {
        if (Array.isArray(c) && c.length > 0) return c;
    }

    // Brute-force: look for any array value with objects containing "ofrid" or alternative IDs
    function deepFind(obj, depth) {
        if (depth > 4 || !obj || typeof obj !== "object") return null;
        for (const v of Object.values(obj)) {
            if (Array.isArray(v) && v.length > 0 && (v[0]?.ofrid || v[0]?.queryId || v[0]?.lead_id || v[0]?.OFRID)) return v;
            const found = deepFind(v, depth + 1);
            if (found) return found;
        }
        return null;
    }
    return deepFind(payload, 0) || [];
}

/**
 * Normalise a raw lead object from the API into the shape our filter
 * pipeline expects.
 *
 * Returns null if the lead is malformed or lacks a usable ofrid.
 */
function normaliseLead(raw) {
    if (!raw || typeof raw !== "object") return null;

    const ofrid = String(
        raw.ofrid || raw.OFRID || raw.buyLeadId || raw.lead_id || raw.queryId || raw.enqId || raw.offer_id || raw.OFFER_ID || raw.ETO_OFR_ID || ""
    ).trim();
    if (!ofrid) return null;

    const title    = (raw.subject || raw.title || raw.reqSubject || raw.SUBJECT || raw.productName || raw.mcatName || raw.buyerRequirements || raw.ETO_OFR_TITLE || "").trim();
    const country  = (raw.country || raw.COUNTRY || raw.buyerCountry || raw.senderCountry || raw.GLUSR_COUNTRY || "India").trim();
    const quantity = String(raw.qty || raw.quantity || raw.QUANTITY || raw.enqQty || raw.ETO_OFR_QTY || "1").trim();
    const location = (raw.city || raw.location || raw.LOCATION || raw.buyerCity || raw.senderCity || raw.GLUSR_CITY || "").trim();

    // Age: prefer a pre-computed field; fall back to posted_on epoch diff
    let ageStr = "";
    if (raw.posted_time_ago || raw.time_ago) {
        ageStr = (raw.posted_time_ago || raw.time_ago).trim();
    } else if (raw.postedOn || raw.posted_on) {
        const postedMs = new Date(raw.postedOn || raw.posted_on).getTime();
        if (!isNaN(postedMs)) {
            const diffMin = Math.round((Date.now() - postedMs) / 60000);
            ageStr = `${diffMin} min ago`;
        }
    }

    // Contact availability — read from API flags when present
    const hasPhone    = !!(raw.mobile    || raw.mobileAvail    || raw.isMobile || raw.PNS_NUMBER || raw.mobileNumber);
    const hasWhatsapp = !!(raw.whatsapp  || raw.whatsappAvail  || raw.isWhatsapp);
    const hasEmail    = !!(raw.email     || raw.emailAvail     || raw.isEmail || raw.EMAIL);
    const contactInfo = [
        hasPhone    ? "Phone"    : null,
        hasWhatsapp ? "WhatsApp" : null,
        hasEmail    ? "Email"    : null,
    ].filter(Boolean).join(", ") || "Unknown";

    // Build rawText from all string values for keyword matching
    const rawText = Object.values(raw)
        .filter(v => typeof v === "string")
        .join(" ");

    // Generic display name extraction (synthetic test field first)
    const display_name = (raw.sourceDisplayName || raw.display_name || raw.displayName || raw.senderName || raw.buyerName || raw.SENDERNAME || "").trim();

    return {
        ofrid,
        title:    title || "Unknown Lead",
        country,
        quantity,
        location,
        ageStr,
        rawText,
        contactInfo,
        hasPhone,
        hasWhatsapp,
        hasEmail,
        display_name,
    };
}

// =============================================================================
// Contact Filter (Stage 7 equivalent — on API data, not DOM icons)
// =============================================================================

/**
 * Apply the contact-channel filter using API-sourced availability flags.
 * Returns { passed: bool, reason: string }
 */
function applyContactFilterAPI(lead, config) {
    const mode = (config.contact_filter_mode || "Phone / Whatsapp").toLowerCase();

    // If the API gave us no contact info at all, default to passing
    // (we cannot penalise for data the API chose not to include).
    if (!lead.hasPhone && !lead.hasWhatsapp && !lead.hasEmail &&
        lead.contactInfo === "Unknown") {
        return { passed: true, reason: "" };
    }

    let passed = false;
    if      (mode === "phone only")      passed = lead.hasPhone;
    else if (mode === "whatsapp only")   passed = lead.hasWhatsapp;
    else if (mode === "email only")      passed = lead.hasEmail;
    else if (mode === "phone / whatsapp")passed = lead.hasPhone || lead.hasWhatsapp;
    else                                 passed = lead.hasPhone || lead.hasWhatsapp || lead.hasEmail;

    return {
        passed,
        reason: passed
            ? ""
            : `Contact filter '${mode}' not met. Available: ${lead.contactInfo}`,
    };
}

// =============================================================================
// Instant API Purchase
// =============================================================================

/**
 * Fire a direct POST to /blreact/contactBuyNow.
 *
 * Authentication: credentials: "include" instructs Chrome to attach the
 * active session cookies (v4iilex, userDet, etc.) automatically.
 * No cookie spoofing, no API key storage.
 *
 * Returns { status: string, reason: string }
 */
async function executePurchase(lead, config) {
    const payload = {
        ofrid:       lead.ofrid,
        purchasemod: "WEB",
        ptime:       nowPtime(),
    };

    try {
        const resp = await fetch("https://seller.indiamart.com/blreact/contactBuyNow", {
            method:      "POST",
            credentials: "include",         // attaches live session cookies
            headers: {
                "Content-Type":     "application/json",
                "Accept":           "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
            body: JSON.stringify(payload),
        });

        if (!resp.ok) {
            return { status: "failed", reason: `HTTP ${resp.status} from contactBuyNow` };
        }

        let body = {};
        try { body = await resp.json(); } catch (_) {}

        // IndiaMART returns a STATUS field in the response body
        const serverStatus = (body?.STATUS || body?.status || "").toString().toUpperCase();
        if (serverStatus === "SUCCESS" || serverStatus === "200") {
            return { status: "purchased", reason: "contactBuyNow confirmed success" };
        }

        const errMsg = body?.MESSAGE || body?.message || body?.msg || serverStatus || "Unknown error";
        return { status: "failed", reason: `contactBuyNow rejection: ${errMsg}` };

    } catch (e) {
        return { status: "failed", reason: `Network error: ${e.message}` };
    }
}

// =============================================================================
// Lead Orchestration
// =============================================================================

/**
 * Process a single normalised lead through the full pipeline.
 * Respects the concurrency semaphore; returns immediately if capped.
 */
async function handleLead(lead, config) {
    const simMode    = !!config.simulation_mode;
    const todayCount = config.today_count || 0;
    const maxDaily   = config.max_daily_purchases || 20;

    // Client-side daily cap (server enforces authoritatively)
    if (!simMode && todayCount >= maxDaily) {
        console.log("[LeadBridge] Daily cap reached — skipping:", lead.title);
        return;
    }

    // ── Concurrency gate (Live Mode only) ─────────────────────────────────
    if (!simMode && _activePurchases >= CONSUME_CONCURRENCY) {
        console.log(
            `[LeadBridge] Concurrency limit (${CONSUME_CONCURRENCY}) reached — ` +
            `deferring: ${lead.title}`
        );
        return;
    }

    // Helper to safely post logs without throwing unhandled rejections
    const safePostLog = async (status, reason, score) => {
        const hash = await buildLeadHash(lead.title, lead.location, lead.quantity);
        const result = await postLog({
            status:        status,
            title:         lead.title,
            country:       lead.country,
            quantity:      lead.quantity,
            location:      lead.location,
            lead_hash:     hash,
            reason:        reason,
            score:         score,
            display_name:  lead.display_name,
            contact_info:  lead.contactInfo,
        });

        if (!result.success) {
            console.error(`[LeadBridge] TRANSPORT FAILURE: Could not deliver lead to backend. Error: ${result.error}`);
            return false;
        }
        return true;
    };

    // ── Stages 1–6: text-based pre-purchase filtering ─────────────────────
    const stageResult = runStages1to6(lead, config);
    if (!stageResult.passed) {
        await safePostLog("filtered", stageResult.reason, stageResult.score);
        return;
    }

    // ── Stage 7: contact filter on API availability flags ─────────────────
    const contactResult = applyContactFilterAPI(lead, config);
    if (!contactResult.passed) {
        await safePostLog("filtered_contact", contactResult.reason, stageResult.score);
        return;
    }

    // ── Simulation Mode: all filters passed, no API call ──────────────────
    if (simMode) {
        console.log(
            "[LeadBridge] [Simulated]", lead.title,
            "| Contacts:", lead.contactInfo,
            "| ofrid:", lead.ofrid
        );
        await safePostLog("simulated", `Simulation mode: all 7 filters passed. Contacts: ${lead.contactInfo}`, stageResult.score);
        return;
    }

    // ── Live Mode: fire API purchase ───────────────────────────────────────
    _activePurchases++;
    console.log(
        `[LeadBridge] [Live] Purchasing ofrid=${lead.ofrid}`,
        `(active: ${_activePurchases}/${CONSUME_CONCURRENCY})`
    );

    try {
        const result = await executePurchase(lead, config);
        await safePostLog(result.status, result.reason, stageResult.score);

        console.log(
            `[LeadBridge] [Live] ofrid=${lead.ofrid} → ${result.status}:`,
            result.reason
        );

        // Refresh config after each live action (daily count may have changed)
        await fetchConfig();
    } catch (e) {
        console.warn("[LeadBridge] Purchase execution failed:", e);
    } finally {
        _activePurchases--;
    }
}

// =============================================================================
// Feed Event Handler
// =============================================================================

/**
 * Entry point for every intercepted getBLDisplayData payload.
 * Called both from the CustomEvent listener (passive intercept) and the
 * active fallback poll.
 */
async function processFeedPayload(payload) {
    const config = await getConfig();
    if (!config) return;

    const rawLeads = extractLeadsFromPayload(payload);
    if (rawLeads.length === 0) return;

    console.log(`[LeadBridge] Feed captured: ${rawLeads.length} leads.`);

    // 1. Format the leads array properly
    const formattedLeads = rawLeads.map(normaliseLead).filter(Boolean);
    console.log(`[LeadBridge] Extracted: ${formattedLeads.length} valid leads.`);

    // Process all leads in the batch; each handleLead is independent.
    const tasks = [];
    for (const lead of formattedLeads) {
        // TTL dedup — skip if seen within the last 3 minutes
        if (isDuplicate(lead.ofrid)) continue;

        tasks.push(handleLead(lead, config).catch(e => {
            console.warn("[LeadBridge] handleLead failed:", e);
        }));
    }

    await Promise.allSettled(tasks);
}

// =============================================================================
// Active Fallback Poll (≥1200 ms between requests)
// =============================================================================

/**
 * Directly poll getBLDisplayData at a controlled rate as a safety net in case
 * the Main World hook misses a request (e.g., service worker fetch, cache hit).
 *
 * Throttled to at most 1 request per FALLBACK_POLL_MS.
 */
async function fallbackPoll() {
    // Safety A: manual pause via popup toggle
    if (_botPaused) return;

    // Safety B: Python server must be reachable (config loaded within the last 30s)
    if (!_config || Date.now() - _configFetchTime > 30_000) return;

    const now = Date.now();
    if (now - _lastPollTime < FALLBACK_POLL_MS) return;
    _lastPollTime = now;

    const config = await getConfig();
    if (!config) return;

    try {
        const resp = await fetch(
            "https://seller.indiamart.com/blreact/getBLDisplayData",
            {
                method:      "GET",
                credentials: "include",
                headers: {
                    "Accept":           "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                },
            }
        );
        if (!resp.ok) return;
        const data = await resp.json();
        await processFeedPayload(data);
    } catch (e) {
        // Fail silently — the hook intercept is primary; this is just a net
    }
}

// =============================================================================
// Boot: MutationObserver-free startup
// =============================================================================

(async function boot() {
    if (!window.location.href.includes("seller.indiamart.com")) return;

    console.log("[LeadBridge] Content script booted (Network-Layer v3).");
    console.log("[LeadBridge] Python server expected at: http://127.0.0.1:" + DEFAULT_PORT);

    // ── Step 1: Resolve persisted pause state BEFORE anything else runs ───────
    // Wrapping chrome.storage.local.get in a Promise makes it awaitable, so the
    // rest of boot() cannot proceed until _botPaused is definitively set.
    // Without this, the callback fires asynchronously AFTER fetchConfig() and
    // after the kickstart, causing the safety checks to falsely read false.
    await new Promise((resolve) => {
        chrome.storage.local.get(["botPaused"], (result) => {
            _botPaused = !!result.botPaused;
            if (_botPaused) {
                console.log("[LeadBridge] Bot is PAUSED (persisted from previous session).");
            }
            resolve();
        });
    });

    // ── Step 2: Fetch config (establishes server connection, populates _config) ─
    // Also awaited so _config and _configFetchTime are populated before any
    // safety check that reads them can fire.
    await fetchConfig();

    // ── Step 3: Register the storage-change listener ──────────────────────────
    // Placed after the initial seed so the listener doesn't race with step 1.
    chrome.storage.onChanged.addListener((changes, area) => {
        if (area !== "local" || !("botPaused" in changes)) return;
        _botPaused = !!changes.botPaused.newValue;
        console.log(
            `[LeadBridge] Bot ${_botPaused ? "PAUSED" : "RESUMED"} via popup toggle.`
        );
    });

    // ── Step 4: One-shot Auto-Kickstart (runs once, immediately on boot) ──────
    // Both safety conditions are now definitively resolved, so this check is
    // guaranteed to be accurate — no race condition.
    if (!_botPaused && _config) {
        console.log("[LeadBridge] Auto-Kickstart: forcing initial feed fetch.");
        try {
            // Prefer the SPA's own refresh mechanism if available (zero visual flicker)
            if (typeof window.__thorForceBlListRefresh === "function") {
                window.__thorForceBlListRefresh();
                console.log("[LeadBridge] Auto-Kickstart: called __thorForceBlListRefresh().");
            } else {
                // Fallback: tab-toggle trick — same logic as scheduleAutoRefresh()
                const tabs      = document.querySelectorAll(".BuyLeadNavigation li");
                const tabList   = Array.from(tabs);
                const recentTab = tabList.find(el => (el.innerText || "").trim().toLowerCase() === "recent");
                const relTab    = tabList.find(el => (el.innerText || "").trim().toLowerCase() === "relevant");

                if (recentTab && relTab) {
                    relTab.click();
                    await new Promise(r => setTimeout(r, 150));
                    recentTab.click();
                    console.log("[LeadBridge] Auto-Kickstart: tab-toggle fired.");
                } else {
                    // Last resort: direct fallback poll — doesn't disturb the DOM
                    await fallbackPoll();
                    console.log("[LeadBridge] Auto-Kickstart: direct fallback poll fired.");
                }
            }
        } catch (e) {
            console.warn("[LeadBridge] Auto-Kickstart error (non-fatal):", e.message);
        }
    } else {
        console.log(
            `[LeadBridge] Auto-Kickstart skipped — ` +
            `paused: ${_botPaused}, config: ${!!_config}`
        );
    }

    // ── Step 5: Register primary event channel (MAIN world → ISOLATED world) ──
    // inject-im-hook.js dispatches CustomEvent("im:leadsData") on window.
    // Content scripts in ISOLATED world CAN receive window events fired by
    // the MAIN world in Chrome's MV3 cross-world communication model.
    window.addEventListener("im:leadsData", async (evt) => {
        // Safety A: manual pause
        if (_botPaused) return;
        // Safety B: server must be online
        if (!_config || Date.now() - _configFetchTime > 30_000) return;

        let data = evt.detail;
        if (typeof data === "string") {
            try {
                data = JSON.parse(data);
            } catch (e) {
                console.error("[LeadBridge] Error parsing im:leadsData detail:", e);
                return;
            }
        }
        
        if (data) {
            console.log("[LeadBridge] Feed captured via network event:", data);
            try {
                await processFeedPayload(data);
            } catch (e) {
                console.error("[LeadBridge] Error processing intercepted feed:", e);
            }
        }
    });

    // ── Step 6: Start recurring background intervals ───────────────────────────

    // Fallback active poll (throttled to ≥ FALLBACK_POLL_MS between requests)
    setInterval(fallbackPoll, FALLBACK_POLL_MS);

    // Heartbeat: call fetchConfig() directly every 5 s, bypassing the 30 s cache.
    // The Python backend updates its `last_seen` timestamp on every /config hit,
    // which keeps the UI connection indicator green between lead events.
    // Skips silently when the bot is paused to avoid unnecessary traffic.
    setInterval(() => {
        if (_botPaused) return;
        fetchConfig().catch(() => {});   // fire-and-forget; errors handled inside fetchConfig
    }, 5000);

    // Auto-Waker: dismiss IndiaMART inactivity modal every 1500ms.
    // Runs independent of pause state — we always want to clear the overlay so
    // the page stays usable and ready when the user resumes the bot.
    const _autoWakerInterval = setInterval(() => {
        try {
            let wakerBtn = null;

            // Strategy 1 — Direct ID lookup (fastest, most reliable)
            const byId = document.getElementById("Yes");
            if (byId && byId.offsetParent !== null) {
                wakerBtn = byId;
            }

            // Strategy 2 — Known class names used by the modal's yellow button
            if (!wakerBtn) {
                const CLASS_SELECTORS = [".send_quo", ".ys_quo", ".w_1"];
                for (const sel of CLASS_SELECTORS) {
                    const el = document.querySelector(sel);
                    if (el && el.offsetParent !== null) {
                        wakerBtn = el;
                        break;
                    }
                }
            }

            // Strategy 3 — Full button text scan (catches selector renames)
            if (!wakerBtn) {
                const allButtons = document.querySelectorAll(
                    "button, a, [role='button'], input[type='button'], input[type='submit']"
                );
                for (const el of allButtons) {
                    if (
                        el.offsetParent !== null &&
                        (el.innerText || el.value || "").trim() === "Get Fresh Leads"
                    ) {
                        wakerBtn = el;
                        break;
                    }
                }
            }

            if (wakerBtn) {
                console.log("[LeadBridge] Auto-Waker: Dismissed inactivity modal.");
                wakerBtn.click();
            }
        } catch (e) {
            console.warn("[LeadBridge] Auto-Waker error:", e.message);
        }
    }, 1500);

    // Auto-Refresh: tab-toggle trick to force fresh AJAX fetches on a
    // randomised 3–6 second cadence. Both safety checks run at the top of
    // each tick so they always reflect the latest _botPaused / _config state.
    function scheduleAutoRefresh() {
        const delay = Math.floor(Math.random() * (6000 - 3000 + 1)) + 3000;
        setTimeout(async () => {
            // Safety A: manual pause via popup toggle
            if (_botPaused) {
                console.log("[LeadBridge] Auto-refresh skipped: bot is paused.");
                scheduleAutoRefresh();
                return;
            }

            // Safety B: Python server must be reachable
            if (!_config || Date.now() - _configFetchTime > 30_000) {
                console.log("[LeadBridge] Auto-refresh skipped: Python server offline.");
                scheduleAutoRefresh();
                return;
            }

            // Safety C: do not refresh while a modal is open
            const isModalOpen = !!document.querySelector(
                "[class*='reply-modal'], .modal.show, [role='dialog']"
            );
            if (isModalOpen) {
                console.log("[LeadBridge] Auto-refresh paused: modal is open.");
                scheduleAutoRefresh();
                return;
            }

            console.log(`[LeadBridge] Soft AJAX refresh triggered after ${delay}ms`);

            // Humanised scroll
            window.scrollBy({ top: Math.floor(Math.random() * 300) + 100, behavior: "smooth" });
            await new Promise(r => setTimeout(r, Math.floor(Math.random() * 400) + 200));

            // Tab toggle: Relevant → Recent (forces fresh AJAX feed)
            const tabs      = document.querySelectorAll(".BuyLeadNavigation li");
            const tabList   = Array.from(tabs);
            const recentTab = tabList.find(el => (el.innerText || "").trim().toLowerCase() === "recent");
            const relTab    = tabList.find(el => (el.innerText || "").trim().toLowerCase() === "relevant");

            if (recentTab && relTab) {
                relTab.click();
                console.log("[LeadBridge] Tab toggle: 'Relevant' clicked.");
                await new Promise(r => setTimeout(r, 150));
                recentTab.click();
                console.log("[LeadBridge] Tab toggle: 'Recent' clicked — AJAX feed forced.");
                // The hook will capture the resulting getBLDisplayData call automatically.
            } else {
                console.warn("[LeadBridge] Nav tabs not found — falling back to hard reload.");
                window.location.reload();
            }

            scheduleAutoRefresh();
        }, delay);
    }

    // ── Step 7: Start the recurring Auto-Refresh loop ─────────────────────────
    scheduleAutoRefresh();

    console.log("[LeadBridge] Monitoring active. Sim mode:", _config && _config.simulation_mode);
    console.log(
        `[LeadBridge] Limits — concurrency: ${CONSUME_CONCURRENCY}, ` +
        `dedup TTL: ${DEDUP_TTL_MS / 60000} min, ` +
        `fallback poll: ${FALLBACK_POLL_MS} ms`
    );
})();
