# Graph Report - Bot 2.0  (2026-09-20)

## Corpus Check
- 29 files · ~277,651 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 1484 file(s) not represented in the graph (top: (none) 1345, .gz 52, .old 29)

## Summary
- 530 nodes · 879 edges · 31 communities (18 shown, 13 thin omitted)
- Extraction: 98% EXTRACTED · 2% INFERRED · 0% AMBIGUOUS · INFERRED: 19 edges (avg confidence: 0.9)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- LocalServer
- server.py
- IndiaMArtScraper
- AppWindow
- test_server_lifecycle.py
- test_infrastructure.py
- MonitorThread
- LeadDatabase
- SettingsTab
- ._make_handler
- HistoryTab
- content.js
- MonitorTab
- popup.js
- manifest.json
- notifier.py
- IndiaMART Lead Bridge Popup UI
- emit
- CustomTkinter Dependency
- TestDomainLogic
- DummyDB
- rules/graphify.md
- workflows/graphify.md
- dataclasses
- math

## God Nodes (most connected - your core abstractions)
1. `LocalServer` - 36 edges
2. `AppWindow` - 32 edges
3. `MonitorThread` - 25 edges
4. `HistoryTab` - 22 edges
5. `IndiaMArtScraper` - 21 edges
6. `SettingsTab` - 21 edges
7. `LeadDatabase` - 16 edges
8. `MonitorTab` - 16 edges
9. `main()` - 12 edges
10. `TestDomainLogic` - 11 edges

## Surprising Connections (you probably didn't know these)
- `TestUI` --uses--> `MonitorTab`  [INFERRED]
  tests/test_ui.py → app/ui/monitor_tab.py
- `test_pause_freeze()` --calls--> `MonitorThread`  [EXTRACTED]
  tests/test_infrastructure.py → app/core/monitor.py
- `test_queue_saturation()` --calls--> `LocalServer`  [EXTRACTED]
  tests/test_infrastructure.py → app/core/server.py
- `TestIntegrationAudit` --uses--> `LocalServer`  [INFERRED]
  tests/test_integration_audit.py → app/core/server.py
- `new_server()` --calls--> `LocalServer`  [EXTRACTED]
  tests/test_server_lifecycle.py → app/core/server.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Lead Processing Pipeline** — network_layer_interception_pattern, six_stage_filter_pipeline, ownership_chain_pattern [INFERRED 0.75]
- **Extension-Python Communication Flow** — localhost_communication_pattern, extension_first_architecture, extension_popup_port_configuration [INFERRED 0.85]

## Communities (31 total, 13 thin omitted)

### Community 0 - "LocalServer"
Cohesion: 0.06
Nodes (22): LocalServer, Any, Queue, Non-blocking push to the UI queue; silently drops if full., Push a critical state event to the unbounded control queue., Owns exactly one HTTPServer and exactly one serving thread. Ownership contract…, Bind and start serving in a dedicated thread. Returns True on success, False on…, Shut down the HTTP server and release the socket. Idempotent and safe from any… (+14 more)

### Community 1 - "server.py"
Cohesion: 0.08
Nodes (39): app/core/monitor.py =================== Heartbeat daemon thread for SafeIM Lead…, app/core/scraper.py =================== Playwright-based IndiaMART automation…, # NOTE: bare 'button' and 'a[class*="btn"]' removed — they match UI, app/core/server.py ================== Local HTTP Command Center. This module is…, app/db/database.py ================== Thread-safe SQLite database layer for…, app/ui/app_window.py ==================== Root CustomTkinter window for SafeIM…, app/ui/history_tab.py ===================== Lead history browser tab for SafeIM…, app/ui/monitor_tab.py ===================== Live monitoring tab for SafeIM Lead… (+31 more)

### Community 2 - "IndiaMArtScraper"
Cohesion: 0.06
Nodes (28): _compute_hash(), IndiaMArtScraper, first_text(), text(), Any, Log a compact debug snippet for a rejected card element so that layout changes…, Extract all fields from a single lead card element. Strategy: 1. Try broadened…, Best-effort extraction of lead fields from a card's raw inner text. Heuristics… (+20 more)

### Community 3 - "AppWindow"
Cohesion: 0.07
Nodes (23): AppWindow, Any, Path, Schedule `callback` after `delay_ms`, replacing any pending job that was…, Wrapper that de-registers a job before running it., Cancel one pending job by key, if present., Cancel every pending after() job. Returns how many were cancelled. Called…, Refresh the mode banner to reflect the current state. Priority order: 1.… (+15 more)

### Community 4 - "test_server_lifecycle.py"
Cohesion: 0.11
Nodes (32): probe_listener(), Return True if *something* is currently accepting TCP connections on host:port.…, platform, bind_like_server(), case(), check(), drain(), http_get() (+24 more)

### Community 5 - "test_infrastructure.py"
Cohesion: 0.21
Nodes (9): case(), check(), drain(), main(), MockDatabase, tests/test_infrastructure.py ============================ Tests for the generic…, test_pause_freeze(), test_queue_saturation() (+1 more)

### Community 6 - "MonitorThread"
Cohesion: 0.09
Nodes (18): MonitorThread, Any, Queue, The LocalServer this thread owns, or None before run() starts it., Block (bounded) until run() has attempted to start the server. Returns True if…, Ask the owned LocalServer to prove it released its socket and thread. Returns…, Signal the thread to exit cleanly after the current tick., Pause the heartbeat loop after the current tick completes. (+10 more)

### Community 7 - "LeadDatabase"
Cohesion: 0.10
Nodes (16): LeadDatabase, Any, Path, Build a SHA-256 fingerprint for a lead. Used to detect exact duplicates across…, Return True if a lead with this hash is already in the database., Insert a lead record. Returns True if inserted, False if it was a duplicate.…, Atomically bump today's purchase counter. Uses UPSERT so the row is created on…, Return the number of real (non-simulated) purchases made today. (+8 more)

### Community 8 - "SettingsTab"
Cohesion: 0.11
Nodes (12): Any, Path, Full settings form embedded in the ⚙️ Settings tab., Populate all widgets from the current settings dict., Live-update the label AND the shared settings dict instantly., Build all setting widgets inside a scrollable container., Grey-out the age entry when the 'disable' checkbox is ticked., Validate, update shared settings, persist to disk, notify parent. (+4 more)

### Community 9 - "._make_handler"
Cohesion: 0.22
Nodes (10): _LifecycleHTTPServer, do_GET(), do_OPTIONS(), do_POST(), _handle_config(), _handle_health(), _send_cors(), HTTPServer with platform-correct address-reuse semantics. See the "Windows… (+2 more)

### Community 10 - "HistoryTab"
Cohesion: 0.08
Nodes (16): HistoryTab, Any, Override ttk default styles so the Treeview matches the dark theme., Reload all records from the database and repopulate the table., Filter self._all_records by the active search query and populate., Clear and fill the Treeview with the given list of records., Live-filter the table as the user types in the search box., Show full record details in the bottom panel when a row is clicked. (+8 more)

### Community 11 - "content.js"
Cohesion: 0.16
Nodes (21): applyContactFilterAPI(), buildLeadHash(), COUNTRY_ALIASES, executePurchase(), extractLeadsFromPayload(), deepFind(), fallbackPoll(), fetchConfig() (+13 more)

### Community 12 - "MonitorTab"
Cohesion: 0.13
Nodes (9): MonitorTab, Any, Append a timestamped, colour-coded line to the activity log., Prepend a lead card to the top of the right-side feed panel., Refresh the daily purchase counter badge with colour-coded thresholds., Render granular network and lifecycle state exactly as dictated by the server.…, Wipe the activity log., Return the first child of the cards_frame so new cards can be inserted before… (+1 more)

### Community 13 - "popup.js"
Cohesion: 0.12
Nodes (16): applyToggleVisual(), botToggle, checkConnection(), eventsList, loadEvents(), loadToggleState(), portInput, renderEvents() (+8 more)

### Community 14 - "manifest.json"
Cohesion: 0.12
Nodes (16): action, default_popup, default_title, background, service_worker, content_scripts, description, host_permissions (+8 more)

### Community 15 - "notifier.py"
Cohesion: 0.21
Nodes (12): _notify(), notify_lead_found(), notify_lead_simulated(), notify_limit_reached(), notify_session_expired(), app/core/notifier.py ==================== Windows toast notification wrapper…, Send a toast notification, silently logging any failure., Fire a toast when a lead is successfully purchased (credits spent). Only called… (+4 more)

### Community 16 - "IndiaMART Lead Bridge Popup UI"
Cohesion: 0.20
Nodes (10): Extension-First Architecture, Bot Toggle Control, IndiaMART Lead Bridge Popup UI, Port Configuration, Signal Events Display, Localhost Communication Pattern, Network Layer Interception Pattern, Ownership Chain Pattern (+2 more)

### Community 18 - "CustomTkinter Dependency"
Cohesion: 0.67
Nodes (3): CustomTkinter Dependency, Pillow Image Processing Dependency, Plyer Notifications Dependency

## Knowledge Gaps
- **31 isolated node(s):** `_seenIds`, `COUNTRY_ALIASES`, `manifest_version`, `name`, `version` (+26 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 243 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **13 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `LocalServer` connect `LocalServer` to `server.py`, `test_server_lifecycle.py`, `test_infrastructure.py`, `MonitorThread`, `._make_handler`?**
  _High betweenness centrality (0.183) - this node is a cross-community bridge._
- **Why does `AppWindow` connect `AppWindow` to `server.py`, `MonitorThread`, `SettingsTab`, `HistoryTab`, `MonitorTab`?**
  _High betweenness centrality (0.176) - this node is a cross-community bridge._
- **Why does `MonitorThread` connect `MonitorThread` to `LocalServer`, `server.py`, `AppWindow`, `test_infrastructure.py`?**
  _High betweenness centrality (0.148) - this node is a cross-community bridge._
- **Are the 2 inferred relationships involving `LocalServer` (e.g. with `MonitorThread` and `TestIntegrationAudit`) actually correct?**
  _`LocalServer` has 2 INFERRED edges - model-reasoned connections that need verification._
- **Are the 4 inferred relationships involving `AppWindow` (e.g. with `MonitorThread` and `HistoryTab`) actually correct?**
  _`AppWindow` has 4 INFERRED edges - model-reasoned connections that need verification._
- **Are the 2 inferred relationships involving `MonitorThread` (e.g. with `LocalServer` and `AppWindow`) actually correct?**
  _`MonitorThread` has 2 INFERRED edges - model-reasoned connections that need verification._
- **Are the 2 inferred relationships involving `HistoryTab` (e.g. with `AppWindow` and `TestUI`) actually correct?**
  _`HistoryTab` has 2 INFERRED edges - model-reasoned connections that need verification._