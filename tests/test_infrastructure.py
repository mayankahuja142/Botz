"""
tests/test_infrastructure.py
============================
Tests for the generic infrastructure fixes:
1. Queue saturation resilience for critical state events.
2. Connection state updates during Pause mode.
3. Thread-safe settings dictionary sizing.
"""

import sys
import os
import time
import queue
import threading
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from app.core.server import LocalServer, STATE_RUNNING, STATE_STOPPED
from app.core.monitor import MonitorThread
from main import DEFAULT_SETTINGS

# Mock Database
class MockDatabase:
    def close_all(self): return 1
    def close(self): pass
    def get_today_purchase_count(self): return 0
    def lead_exists(self, lead_hash): return False
    def add_lead(self, data): pass
    def increment_today_count(self): pass
    def compute_hash(self, *args): return "mock_hash"

# Harness
results = []
_current_case = ""

def case(case_id, title):
    global _current_case
    _current_case = case_id
    print(f"\n[{case_id}] {title}")
    print("-" * 66)

def check(label, condition, detail=""):
    condition = bool(condition)
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if detail:
        print(f"         {detail}")
    results.append((_current_case, label, condition))
    return condition

def drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out

# --- Test Cases ---

def test_queue_saturation():
    case("01", "Queue saturation - Critical events")
    
    # Setup
    ui_queue = queue.Queue(maxsize=10) # tiny for test
    ctrl_queue = queue.Queue()
    db = MockDatabase()
    settings = dict(DEFAULT_SETTINGS)
    
    server = LocalServer(db=db, settings=settings, ui_queue=ui_queue, ctrl_queue=ctrl_queue, port=9501)
    
    # 1. Fill ui_queue
    for i in range(15):
        try:
            ui_queue.put_nowait({"type": "log", "msg": f"Fill {i}"})
        except queue.Full:
            pass
            
    check("ui_queue is saturated", ui_queue.full())
    
    # 2. Push critical events using _push_ctrl
    server._push_ctrl({"type": "conn_state", "state": "CONN_CLIENT_CONNECTED"})
    server._push_ctrl({"type": "ext_connected"})
    
    # 3. Ensure ctrl_queue received them
    ctrl_items = drain(ctrl_queue)
    check("ctrl_queue delivered critical events despite ui_queue saturation", len(ctrl_items) == 2, f"Delivered {len(ctrl_items)}")
    if len(ctrl_items) == 2:
        check("critical event 'conn_state' intact", ctrl_items[0].get("type") == "conn_state")
        check("critical event 'ext_connected' intact", ctrl_items[1].get("type") == "ext_connected")

def test_pause_freeze():
    case("02", "Pause mode connection updates")
    
    ui_queue = queue.Queue()
    ctrl_queue = queue.Queue()
    db = MockDatabase()
    settings = dict(DEFAULT_SETTINGS)
    settings["server_port"] = 9502
    
    monitor = MonitorThread(db=db, settings=settings, ui_queue=ui_queue, ctrl_queue=ctrl_queue)
    monitor.start()
    
    # wait for start
    time.sleep(0.5)
    
    # empty initial setup events
    drain(ctrl_queue)
    drain(ui_queue)
    
    # Pause the monitor
    monitor.pause()
    time.sleep(0.2)
    
    # Emulate extension contact
    monitor.server._last_client_contact = time.time()
    
    # wait for heartbeat to tick once inside _wait_while_paused
    time.sleep(1.2) 
    
    ctrl_items = drain(ctrl_queue)
    
    # Look for conn_state
    conn_states = [ev for ev in ctrl_items if ev.get("type") == "conn_state"]
    check("MonitorThread emits conn_state while PAUSED", len(conn_states) > 0, "conn_state found in ctrl_queue")
    
    monitor.stop()
    monitor.join(timeout=3.0)

def test_settings_race():
    case("03", "Settings dictionary sizing (RuntimeError)")
    
    from app.ui.settings_tab import SettingsTab
    import customtkinter as ctk
    
    app = ctk.CTk()
    settings = dict(DEFAULT_SETTINGS)
    
    # Before the fix, "extension_port" was missing from DEFAULT_SETTINGS.
    # The fix adds it. Let's ensure it exists.
    check("extension_port is present in DEFAULT_SETTINGS", "extension_port" in DEFAULT_SETTINGS)
    
    size_before = len(settings)
    
    # Simulate a SettingsTab save using its actual keys
    settings.update({
        "keywords": ["new"],
        "keyword_blacklist": [],
        "excluded_locations": [],
        "target_countries": [],
        "min_quantity": 5,
        "allow_missing_quantity": False,
        "max_lead_age_minutes": 10,
        "max_daily_purchases": 50,
        "max_session_purchases": 10,
        "delay_seconds": 15.0,
        "simulation_mode": False,
        "contact_filter_mode": "Email only",
        "reply_template": "Catalog Link",
        "custom_reply_text": "Hi",
        "extension_port": 9333,
        "indiamart_leads_url": "http://foo",
    })
    
    size_after = len(settings)
    
    check("Settings dictionary size does not change on update", size_before == size_after, f"{size_before} vs {size_after}")
    app.destroy()

def main():
    test_queue_saturation()
    test_pause_freeze()
    test_settings_race()
    
    total = len(results)
    passed = sum(1 for _, _, ok in results if ok)
    print("-" * 66)
    print(f"  {passed}/{total} individual checks passed")
    print("=" * 66 + "\n")
    sys.exit(0 if passed == total and total > 0 else 1)

if __name__ == "__main__":
    main()
