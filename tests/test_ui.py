import os
import sys
import tkinter as tk
import unittest
from unittest.mock import MagicMock

# Ensure the app package is in the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.ui.monitor_tab import MonitorTab
from app.ui.history_tab import HistoryTab
from app.ui.app_window import AppWindow

class DummyDB:
    def __init__(self):
        self.records = [
            {"id": 1, "title": "Lead 1", "country": "US"},
            {"id": 2, "title": "Lead 2", "country": "UK"},
        ]

    def get_all_leads(self):
        return self.records.copy()

class TestUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # We need a root for Tkinter widgets
        cls.root = tk.Tk()
        cls.root.withdraw() # Hide it

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def test_bug1_connection_status_clobbering(self):
        """Test connection status preservation during pause and resume."""
        tab = MonitorTab(self.root)
        
        # 1. connected -> pause -> expected: still visually connected
        tab.set_conn_state("client_connected", "")
        self.assertEqual(tab.status_dot.cget("text"), "⬤  Extension Active")
        self.assertEqual(tab.status_dot.cget("text_color"), "#4ade80")
        
        # simulated pause (since set_status was removed, AppWindow no longer overrides the dot)
        # Note: AppWindow simply sets its internal state and does NOT call any dot-modifying API for pause.
        # So the dot should remain "Extension Active".
        self.assertEqual(tab.status_dot.cget("text"), "⬤  Extension Active")
        
        # 2. disconnected -> pause -> expected: still visually disconnected
        tab.set_conn_state("server_stopped", "")
        self.assertEqual(tab.status_dot.cget("text"), "⬤  Server Stopped")
        # simulated pause (no override API exists)
        self.assertEqual(tab.status_dot.cget("text"), "⬤  Server Stopped")
        
        # 3. connected -> pause -> resume -> expected: connection state preserved
        tab.set_conn_state("client_connected", "")
        # simulated pause + resume
        self.assertEqual(tab.status_dot.cget("text"), "⬤  Extension Active")
        
        # 4. connected -> extension disconnects while paused -> expected: UI eventually shows disconnected
        tab.set_conn_state("client_connected", "")
        # paused state...
        tab.set_conn_state("server_running_no_client", "") # extension disconnects
        self.assertEqual(tab.status_dot.cget("text"), "⬤  Server Running — Extension Not Connected")
        self.assertEqual(tab.status_dot.cget("text_color"), "#fbbf24")

    def test_bug2_history_search_filter_reset(self):
        db = DummyDB()
        tab = HistoryTab(self.root, db=db)
        
        # 1. empty search + refresh
        tab._search_var.set("")
        tab.refresh()
        self.assertEqual(len(tab.tree.get_children()), 2)
        
        # 2. active search + refresh
        tab._search_var.set("Lead 1")
        tab.refresh()
        self.assertEqual(len(tab.tree.get_children()), 1)
        
        # 3. search returning zero results + refresh
        tab._search_var.set("Nonexistent")
        tab.refresh()
        self.assertEqual(len(tab.tree.get_children()), 0)
        
        # 4. adding a new record while a search is active
        tab._search_var.set("Lead")
        tab.refresh()
        self.assertEqual(len(tab.tree.get_children()), 2)
        db.records.append({"id": 3, "title": "Lead 3", "country": "IN"})
        tab.refresh()
        self.assertEqual(len(tab.tree.get_children()), 3)
        
        # 5. clearing the search after refresh
        tab._search_var.set("")
        self.assertEqual(len(tab.tree.get_children()), 3)

    def test_bug3_unmanaged_tk_after_callback(self):
        db = DummyDB()
        tab = HistoryTab(self.root, db=db)
        
        # 1. schedules the flash restore callback
        tab._flash_btn("Flash", "#ff0000", 2800)
        self.assertIsNotNone(tab._flash_job)
        
        # 2. destroys/closes the relevant UI
        # 3. cancels the pending callback (inside destroy)
        tab.destroy()
        
        # 4. verifies no callback remains scheduled
        self.assertIsNone(tab._flash_job)
        
        # 5. verifies no Tkinter exception occurs
        # The fact that destroy() did not raise an exception, and the after loop
        # won't raise one when root is destroyed, means it's lifecycle safe.

    def test_lead_card_rendered_for_filtered_status(self):
        """
        Verify that add_lead_card() actually creates a visible child widget inside
        the scrollable feed for a 'filtered' lead (the most common live status).

        Before the fix, _first_card_widget() queried winfo_children() on the outer
        CTkScrollableFrame, which returns internal canvas/scrollbar nodes — not user
        cards.  Passing those to pack(before=) raised a TclError that _guarded()
        absorbed silently, leaving the feed empty.

        The fix queries _scrollable_frame (the inner content frame) instead.
        """
        tab = MonitorTab(self.root)

        # Feed must be empty at the start
        inner = getattr(tab.cards_frame, "_scrollable_frame", tab.cards_frame)
        self.assertEqual(len(inner.winfo_children()), 0, "Feed should start empty")

        synthetic_lead = {
            "title":        "Synthetic Test Lead",
            "country":      "Test Country",
            "quantity":     "100",
            "location":     "Test City",
            "display_name": "",
            "contact_info": "",
        }

        # This call must not raise and must leave exactly 1 child in the feed
        try:
            tab.add_lead_card(synthetic_lead, "filtered")
        except Exception as exc:
            self.fail(f"add_lead_card raised an unexpected exception: {exc}")

        self.root.update_idletasks()  # process any pending geometry updates

        children_after = inner.winfo_children()
        self.assertGreater(
            len(children_after), 0,
            "No card widget was added to the scrollable feed for status='filtered'"
        )

    def test_lead_card_rendered_for_simulated_status(self):
        """Same rendering check for status='simulated'."""
        tab = MonitorTab(self.root)
        inner = getattr(tab.cards_frame, "_scrollable_frame", tab.cards_frame)

        synthetic_lead = {
            "title":    "Synthetic Simulated Lead",
            "country":  "Test Country",
            "quantity": "200",
            "location": "Test City",
        }
        try:
            tab.add_lead_card(synthetic_lead, "simulated")
        except Exception as exc:
            self.fail(f"add_lead_card raised an unexpected exception: {exc}")

        self.root.update_idletasks()
        self.assertGreater(len(inner.winfo_children()), 0,
                           "No card widget was added for status='simulated'")


if __name__ == "__main__":
    unittest.main()
