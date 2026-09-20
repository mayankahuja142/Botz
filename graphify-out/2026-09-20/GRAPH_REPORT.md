# Graph Report - Bot 2.0  (2026-09-20)

## Corpus Check
- 100 files · ~278,772 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 1484 file(s) not represented in the graph (top: (none) 1345, .gz 52, .old 29)

## Summary
- 528 nodes · 862 edges · 25 communities (18 shown, 7 thin omitted)
- Extraction: 98% EXTRACTED · 2% INFERRED · 0% AMBIGUOUS · INFERRED: 15 edges (avg confidence: 0.89)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Local HTTP Server
- Core Evaluator & Filter Scaffold
- IndiaMART Scraper
- App Window & UI Shell
- Server Lifecycle Tests
- Lead Evaluator Engine
- Monitor Thread
- Lead Database Layer
- Settings Tab UI
- Lead Filter Pipeline
- History Tab UI
- Content Script Extraction
- Monitor Tab UI
- Extension Popup Logic
- Extension Manifest
- Desktop Notifications
- Extension-First Architecture
- XHR Injection Hook
- Python Dependencies

## God Nodes (most connected - your core abstractions)
1. `AppWindow` - 31 edges
2. `LocalServer` - 30 edges
3. `MonitorThread` - 22 edges
4. `IndiaMArtScraper` - 21 edges
5. `SettingsTab` - 20 edges
6. `LeadEvaluator` - 17 edges
7. `LeadDatabase` - 16 edges
8. `HistoryTab` - 16 edges
9. `MonitorTab` - 15 edges
10. `MatchResult` - 12 edges

## Surprising Connections (you probably didn't know these)
- `new_server()` --calls--> `LocalServer`  [EXTRACTED]
  tests/test_server_lifecycle.py → app/core/server.py
- `main()` --calls--> `LeadDatabase`  [EXTRACTED]
  main.py → app/db/database.py
- `main()` --calls--> `AppWindow`  [EXTRACTED]
  main.py → app/ui/app_window.py
- `MonitorThread` --uses--> `LocalServer`  [INFERRED]
  app/core/monitor.py → app/core/server.py
- `AppWindow` --uses--> `MonitorThread`  [INFERRED]
  app/ui/app_window.py → app/core/monitor.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Extension-Python Communication Flow** — localhost_communication_pattern, extension_first_architecture, extension_popup_port_configuration [INFERRED 0.85]
- **Lead Processing Pipeline** — network_layer_interception_pattern, six_stage_filter_pipeline, ownership_chain_pattern [INFERRED 0.75]

## Communities (25 total, 7 thin omitted)

### Community 0 - "Local HTTP Server"
Cohesion: 0.06
Nodes (29): _LifecycleHTTPServer, LocalServer, do_GET(), do_OPTIONS(), do_POST(), _handle_config(), _handle_health(), _send_cors() (+21 more)

### Community 1 - "Core Evaluator & Filter Scaffold"
Cohesion: 0.07
Nodes (40): FilterResult, app/core/evaluator.py ===================== Unified lead filtering & scoring…, Deprecated — use MatchResult., app/core/filter.py ================== Multi-stage lead filter pipeline for…, app/core/monitor.py =================== Heartbeat daemon thread for SafeIM Lead…, app/core/scraper.py =================== Playwright-based IndiaMART automation…, # NOTE: bare 'button' and 'a[class*="btn"]' removed — they match UI, app/core/server.py ================== Local HTTP Command Center. This module is… (+32 more)

### Community 2 - "IndiaMART Scraper"
Cohesion: 0.06
Nodes (28): _compute_hash(), IndiaMArtScraper, first_text(), text(), Any, Log a compact debug snippet for a rejected card element so that layout changes…, Extract all fields from a single lead card element. Strategy: 1. Try broadened…, Best-effort extraction of lead fields from a card's raw inner text. Heuristics… (+20 more)

### Community 3 - "App Window & UI Shell"
Cohesion: 0.07
Nodes (23): AppWindow, Any, Path, Schedule `callback` after `delay_ms`, replacing any pending job that was…, Wrapper that de-registers a job before running it., Cancel one pending job by key, if present., Cancel every pending after() job. Returns how many were cancelled. Called…, Refresh the mode banner to reflect the current state. Priority order: 1.… (+15 more)

### Community 4 - "Server Lifecycle Tests"
Cohesion: 0.10
Nodes (33): probe_listener(), Return True if *something* is currently accepting TCP connections on host:port.…, platform, bind_like_server(), case(), check(), drain(), http_get() (+25 more)

### Community 5 - "Lead Evaluator Engine"
Cohesion: 0.10
Nodes (19): EvaluatorConfig, LeadEvaluator, _fail(), LeadFilter, MatchResult, Any, Outcome of evaluating a single lead through the pipeline., All tunable parameters for the lead evaluation pipeline. Constructed by… (+11 more)

### Community 6 - "Monitor Thread"
Cohesion: 0.10
Nodes (17): MonitorThread, Any, Queue, The LocalServer this thread owns, or None before run() starts it., Block (bounded) until run() has attempted to start the server. Returns True if…, Ask the owned LocalServer to prove it released its socket and thread. Returns…, Signal the thread to exit cleanly after the current tick., Pause the heartbeat loop after the current tick completes. (+9 more)

### Community 7 - "Lead Database Layer"
Cohesion: 0.10
Nodes (16): LeadDatabase, Any, Path, Build a SHA-256 fingerprint for a lead. Used to detect exact duplicates across…, Return True if a lead with this hash is already in the database., Insert a lead record. Returns True if inserted, False if it was a duplicate.…, Atomically bump today's purchase counter. Uses UPSERT so the row is created on…, Return the number of real (non-simulated) purchases made today. (+8 more)

### Community 8 - "Settings Tab UI"
Cohesion: 0.11
Nodes (12): Any, Path, Full settings form embedded in the ⚙️ Settings tab., Populate all widgets from the current settings dict., Live-update the label AND the shared settings dict instantly., Build all setting widgets inside a scrollable container., Grey-out the age entry when the 'disable' checkbox is ticked., Validate, update shared settings, persist to disk, notify parent. (+4 more)

### Community 9 - "Lead Filter Pipeline"
Cohesion: 0.13
Nodes (15): FilterConfig, FilterResult, LeadFilter, Any, Reject if the lead's location contains any excluded location string., Accept only leads whose country appears in the target list. An empty…, Reject if the parsed quantity integer is below the minimum threshold. Special…, Extract the first integer from a quantity string. Examples: "50-100 Pieces" →… (+7 more)

### Community 10 - "History Tab UI"
Cohesion: 0.13
Nodes (11): HistoryTab, Any, Override ttk default styles so the Treeview matches the dark theme., Reload all records from the database and repopulate the table., Clear and fill the Treeview with the given list of records., Live-filter the table as the user types in the search box., Show full record details in the bottom panel when a row is clicked., Sort the visible Treeview rows by the clicked column. (+3 more)

### Community 11 - "Content Script Extraction"
Cohesion: 0.16
Nodes (20): applyContactFilterAPI(), buildLeadHash(), COUNTRY_ALIASES, executePurchase(), extractLeadsFromPayload(), deepFind(), fallbackPoll(), fetchConfig() (+12 more)

### Community 12 - "Monitor Tab UI"
Cohesion: 0.10
Nodes (11): MonitorTab, Any, Append a timestamped, colour-coded line to the activity log., Prepend a lead card to the top of the right-side feed panel., Refresh the daily purchase counter badge with colour-coded thresholds., Update the server status indicator in the top bar., Called when the first POST /log arrives from the Chrome Extension. Switches the…, Update the status indicator from an explicit connection state. This is additive… (+3 more)

### Community 13 - "Extension Popup Logic"
Cohesion: 0.12
Nodes (16): applyToggleVisual(), botToggle, checkConnection(), eventsList, loadEvents(), loadToggleState(), portInput, renderEvents() (+8 more)

### Community 14 - "Extension Manifest"
Cohesion: 0.12
Nodes (16): action, default_popup, default_title, background, service_worker, content_scripts, description, host_permissions (+8 more)

### Community 15 - "Desktop Notifications"
Cohesion: 0.21
Nodes (12): _notify(), notify_lead_found(), notify_lead_simulated(), notify_limit_reached(), notify_session_expired(), app/core/notifier.py ==================== Windows toast notification wrapper…, Send a toast notification, silently logging any failure., Fire a toast when a lead is successfully purchased (credits spent). Only called… (+4 more)

### Community 16 - "Extension-First Architecture"
Cohesion: 0.20
Nodes (10): Extension-First Architecture, Bot Toggle Control, IndiaMART Lead Bridge Popup UI, Port Configuration, Signal Events Display, Localhost Communication Pattern, Network Layer Interception Pattern, Ownership Chain Pattern (+2 more)

### Community 18 - "Python Dependencies"
Cohesion: 0.67
Nodes (3): CustomTkinter Dependency, Pillow Image Processing Dependency, Plyer Notifications Dependency

## Knowledge Gaps
- **29 isolated node(s):** `_seenIds`, `COUNTRY_ALIASES`, `manifest_version`, `name`, `version` (+24 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 238 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **7 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `AppWindow` connect `App Window & UI Shell` to `Core Evaluator & Filter Scaffold`, `Monitor Thread`, `Settings Tab UI`, `History Tab UI`, `Monitor Tab UI`?**
  _High betweenness centrality (0.183) - this node is a cross-community bridge._
- **Why does `LocalServer` connect `Local HTTP Server` to `Core Evaluator & Filter Scaffold`, `Server Lifecycle Tests`, `Monitor Thread`?**
  _High betweenness centrality (0.164) - this node is a cross-community bridge._
- **Why does `MonitorThread` connect `Monitor Thread` to `Local HTTP Server`, `Core Evaluator & Filter Scaffold`, `App Window & UI Shell`?**
  _High betweenness centrality (0.145) - this node is a cross-community bridge._
- **Are the 4 inferred relationships involving `AppWindow` (e.g. with `MonitorThread` and `HistoryTab`) actually correct?**
  _`AppWindow` has 4 INFERRED edges - model-reasoned connections that need verification._
- **Are the 2 inferred relationships involving `MonitorThread` (e.g. with `LocalServer` and `AppWindow`) actually correct?**
  _`MonitorThread` has 2 INFERRED edges - model-reasoned connections that need verification._
- **What connects `_seenIds`, `COUNTRY_ALIASES`, `manifest_version` to the rest of the system?**
  _29 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Local HTTP Server` be split into smaller, more focused modules?**
  _Cohesion score 0.05925925925925926 - nodes in this community are weakly interconnected._