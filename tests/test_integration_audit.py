import os
import sys
import json
import queue
import time
import urllib.request
import threading
import unittest
from datetime import datetime

# Ensure the app package is in the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.server import LocalServer

class DummyDB:
    def __init__(self):
        self.leads = []
    
    def add_lead(self, data):
        self.leads.append(data)
        
    def lead_exists(self, hash_str):
        return any(l.get("lead_hash") == hash_str for l in self.leads)
        
    def compute_hash(self, *args):
        return "fallback_hash"
        
    def get_today_purchase_count(self):
        return 0
        
    def increment_today_count(self):
        pass

class TestIntegrationAudit(unittest.TestCase):
    def setUp(self):
        self.db = DummyDB()
        self.settings = {
            "server_port": 9333,
            "max_daily_purchases": 20,
            "simulation_mode": True
        }
        self.ui_queue = queue.Queue(maxsize=100)
        self.ctrl_queue = queue.Queue()
        
        self.server = LocalServer(
            db=self.db,
            settings=self.settings,
            ui_queue=self.ui_queue,
            ctrl_queue=self.ctrl_queue,
            port=self.settings["server_port"]
        )
        self.server.start()
        
        # Wait for server to bind
        time.sleep(0.1)

    def tearDown(self):
        self.server.stop()

    def test_synthetic_payload_integration(self):
        """
        Tests the boundary from JS source object -> normalization -> POST /log -> LocalServer -> DB -> ui_queue
        """
        # 1. Synthetic Source Object (representing what getBLDisplayData would yield)
        source_object = {
            "ETO_OFR_ID": "synthetic_123",
            "subject": "Need 5000 units of Widgets",
            "country": "United States",
            "qty": "5000",
            "city": "New York",
            "posted_time_ago": "2 min ago",
            "mobile": True,
            "email": True,
            "sourceDisplayName": "Test Customer"
        }
        
        # 2. Run JS Normalization via Node.js
        content_js_path = os.path.join(os.path.dirname(__file__), "..", "extension", "content.js")
        with open(content_js_path, "r", encoding="utf-8") as f:
            js_code = f.read()
            
        test_script = """
const window = { location: { href: "" }, addEventListener: () => {} };
const chrome = { storage: { local: { get: (k, cb) => cb({botPaused: false}) }, onChanged: { addListener: () => {} } } };
const crypto = { subtle: { digest: async () => new Uint8Array(32) } };
""" + js_code + f"""
// Override fetchConfig / postLog / etc for the test
_config = {{ simulation_mode: true, target_countries: [], keyword_blacklist: [] }};

const rawLead = {json.dumps(source_object)};
const lead = normaliseLead(rawLead);
const stageResult = runStages1to6(lead, _config);
const contactResult = applyContactFilterAPI(lead, _config);

// Mimic what safePostLog does
async function getFinalPayload() {{
    const hash = await buildLeadHash(lead.title, lead.location, lead.quantity);
    const payload = {{
        status: "simulated",
        title: lead.title,
        country: lead.country,
        quantity: lead.quantity,
        location: lead.location,
        lead_hash: hash,
        reason: "Simulated",
        score: stageResult.score,
        display_name: lead.display_name,
        contact_info: lead.contactInfo
    }};
    console.log(JSON.stringify(payload));
}}
getFinalPayload();
"""
        import tempfile
        import subprocess
        
        with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8") as tmp:
            tmp.write(test_script)
            tmp_path = tmp.name
            
        try:
            result = subprocess.run(
                ["node", tmp_path], 
                capture_output=True, text=True, check=True
            )
        except subprocess.CalledProcessError as e:
            print("Node stderr:", e.stderr)
            raise
        finally:
            os.remove(tmp_path)
            
        mapped_payload = json.loads(result.stdout.strip())
        
        # Assert JS mapping was successful
        self.assertEqual(mapped_payload["display_name"], "Test Customer")
        
        # 3. POST to LocalServer
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.settings['server_port']}/log",
            data=json.dumps(mapped_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            
        # 4. Verify Database Operation
        self.assertEqual(len(self.db.leads), 1)
        saved_lead = self.db.leads[0]
        self.assertEqual(saved_lead["title"], "Need 5000 units of Widgets")
        self.assertEqual(saved_lead.get("display_name"), "Test Customer") # LocalServer emits it as display_name for the DB to map
        
        # 5. Verify ui_queue Event
        # Get log event
        ev1 = self.ui_queue.get(timeout=1)
        self.assertEqual(ev1["type"], "log")
        
        # Get lead event
        ev2 = self.ui_queue.get(timeout=1)
        self.assertEqual(ev2["type"], "lead")
        
        # 6. Verify the data passed to UI payload
        ui_data = ev2["data"]
        self.assertEqual(ui_data.get("display_name"), "Test Customer")

    def test_cors_preflight_and_post(self):
        """
        Verify that OPTIONS requests authorize X-Request-Id and that the
        subsequent POST succeeds when using that header.
        """
        # 1. Send OPTIONS request
        req_options = urllib.request.Request(
            f"http://127.0.0.1:{self.settings['server_port']}/log",
            headers={
                "Origin": "https://seller.indiamart.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type, x-request-id"
            },
            method="OPTIONS"
        )

        with urllib.request.urlopen(req_options) as resp_options:
            self.assertEqual(resp_options.status, 200)
            headers = {k.lower(): v.lower() for k, v in resp_options.headers.items()}
            self.assertIn("access-control-allow-headers", headers)
            self.assertIn("x-request-id", headers["access-control-allow-headers"])

        # 2. Send POST request (simulate browser proceeding after preflight)
        payload = {"status": "simulated", "lead_hash": "cors_test_123"}
        req_post = urllib.request.Request(
            f"http://127.0.0.1:{self.settings['server_port']}/log",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Request-Id": "test_cors_123",
                "Origin": "https://seller.indiamart.com"
            },
            method="POST"
        )
        with urllib.request.urlopen(req_post) as resp_post:
            self.assertEqual(resp_post.status, 200)
            self.assertTrue(json.loads(resp_post.read().decode())["ok"])

if __name__ == "__main__":
    unittest.main()

