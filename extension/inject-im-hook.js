/**
 * inject-im-hook.js — Main World Network Interceptor
 * ====================================================
 * Injected at document_start into the MAIN execution world so it shares
 * the host page's JS environment.  This file MUST remain dependency-free
 * and side-effect-free beyond the two patches below.
 *
 * Strategy
 * --------
 *  1. Monkey-patch window.fetch → clone the response to read the JSON body
 *     without consuming the original stream that IndiaMART's own JS expects.
 *  2. Monkey-patch XMLHttpRequest.open + .send → attach a hidden "load"
 *     listener to parse responseText for matching XHR calls.
 *  3. Dispatch a CustomEvent("im:leadsData") on window for each matching
 *     response.  The isolated content script receives it via window.addEventListener.
 *
 * Target pattern:  /blreact\/getBLDisplayData/i
 * Event fired:     CustomEvent("im:leadsData", { detail: <parsed JSON> })
 *
 * Security: no external network calls, no data leaves the browser.
 */

(function () {
    "use strict";

    const TAG       = "[LeadBridge Hook]";
    const TARGET_RE = /blreact\/getBLDisplayData/i;

    /**
     * Emit the captured payload back to the isolated content script.
     * Using CustomEvent on window is the standard MV3 cross-world channel.
     */
    function emit(data) {
        try {
            const jsonData = JSON.stringify(data);
            window.dispatchEvent(new CustomEvent("im:leadsData", { detail: jsonData }));
        } catch (e) {
            // Should never fail, but never let a hook error surface to the page.
        }
    }

    // =========================================================================
    // Patch 1: window.fetch
    // =========================================================================

    const _origFetch = window.fetch.bind(window);

    window.fetch = async function (input, init) {
        // Resolve the URL regardless of whether input is a string or Request.
        const url = (input instanceof Request) ? input.url : String(input ?? "");

        const response = await _origFetch(input, init);

        if (TARGET_RE.test(url)) {
            // Clone before consuming so IndiaMART's own .json() call still works.
            response.clone().json().then(emit).catch(() => {});
        }

        return response;
    };

    console.log(TAG, "fetch interceptor armed.");

    // =========================================================================
    // Patch 2: XMLHttpRequest
    // =========================================================================

    const _XHROpen = XMLHttpRequest.prototype.open;
    const _XHRSend = XMLHttpRequest.prototype.send;

    // WeakMap avoids memory leaks — entries are GC'd with the XHR object.
    const _xhrUrls = new WeakMap();

    XMLHttpRequest.prototype.open = function (method, url) {
        _xhrUrls.set(this, String(url ?? ""));
        // Pass ALL arguments through unchanged to preserve arity.
        return _XHROpen.apply(this, arguments);
    };

    XMLHttpRequest.prototype.send = function () {
        const url = _xhrUrls.get(this) ?? "";

        if (TARGET_RE.test(url)) {
            // Use a named listener so duplicate registration is impossible.
            this.addEventListener("load", function onHookLoad() {
                this.removeEventListener("load", onHookLoad);
                try {
                    const data = JSON.parse(this.responseText);
                    emit(data);
                } catch (e) {
                    // Malformed JSON — ignore silently.
                }
            });
        }

        return _XHRSend.apply(this, arguments);
    };

    console.log(TAG, "XHR interceptor armed.");
})();
