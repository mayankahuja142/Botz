import os
import json
import unittest
import tempfile
import subprocess

class TestDomainLogic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        content_js_path = os.path.join(os.path.dirname(__file__), "..", "extension", "content.js")
        with open(content_js_path, "r", encoding="utf-8") as f:
            cls.js_code = f.read()
            
    def _run_js(self, script_body: str) -> dict:
        test_script = """
const window = { location: { href: "" }, addEventListener: () => {} };
const chrome = { storage: { local: { get: (k, cb) => cb({botPaused: false}) }, onChanged: { addListener: () => {} } } };
const crypto = { subtle: { digest: async () => new Uint8Array(32) } };
""" + self.js_code + "\n" + script_body

        with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8") as tmp:
            tmp.write(test_script)
            tmp_path = tmp.name
            
        try:
            result = subprocess.run(
                ["node", tmp_path], 
                capture_output=True, text=True, check=True
            )
            return json.loads(result.stdout.strip())
        except subprocess.CalledProcessError as e:
            print("Node stderr:", e.stderr)
            raise
        finally:
            os.remove(tmp_path)

    def test_valid_synthetic_input(self):
        script = """
const config = {
    target_countries: ["united states"],
    keywords: ["widget"],
    min_quantity: 100
};
const raw = {
    ofrid: "1001", subject: "Need 500 widgets", country: "United States", qty: "500",
    city: "New York", posted_time_ago: "5 min ago", mobile: true
};
const lead = normaliseLead(raw);
const s16 = runStages1to6(lead, config);
const s7 = applyContactFilterAPI(lead, config);
console.log(JSON.stringify({lead, s16, s7}));
"""
        res = self._run_js(script)
        self.assertTrue(res["s16"]["passed"])
        self.assertTrue(res["s7"]["passed"])
        self.assertEqual(res["lead"]["quantity"], "500")

    def test_missing_optional_fields(self):
        script = """
const config = { target_countries: [], keywords: [] };
const raw = { ofrid: "1002" }; // Missing almost everything
const lead = normaliseLead(raw);
const s16 = runStages1to6(lead, config);
console.log(JSON.stringify({lead, s16}));
"""
        res = self._run_js(script)
        self.assertEqual(res["lead"]["title"], "Unknown Lead")
        self.assertEqual(res["lead"]["country"], "India")
        self.assertEqual(res["lead"]["quantity"], "1")
        self.assertTrue(res["s16"]["passed"])

    def test_malformed_input(self):
        script = """
const raw = null;
const lead = normaliseLead(raw);
console.log(JSON.stringify({lead}));
"""
        res = self._run_js(script)
        self.assertIsNone(res["lead"])

    def test_duplicate_input(self):
        script = """
const r1 = isDuplicate("dup1");
const r2 = isDuplicate("dup1");
console.log(JSON.stringify({r1, r2}));
"""
        res = self._run_js(script)
        self.assertFalse(res["r1"])
        self.assertTrue(res["r2"])

    def test_rejected_input(self):
        script = """
const config = { target_countries: [], keyword_blacklist: ["spam"] };
const raw = { ofrid: "1003", subject: "Buy spam products" };
const lead = normaliseLead(raw);
const s16 = runStages1to6(lead, config);
console.log(JSON.stringify({s16}));
"""
        res = self._run_js(script)
        self.assertFalse(res["s16"]["passed"])
        self.assertIn("Blacklisted keyword", res["s16"]["reason"])

    def test_boundary_values(self):
        script = """
const config = { min_quantity: 50 };
const raw1 = { ofrid: "B1", qty: "49" };
const raw2 = { ofrid: "B2", qty: "50" };
const r1 = runStages1to6(normaliseLead(raw1), config);
const r2 = runStages1to6(normaliseLead(raw2), config);
console.log(JSON.stringify({r1, r2}));
"""
        res = self._run_js(script)
        self.assertFalse(res["r1"]["passed"])
        self.assertTrue(res["r2"]["passed"])

    def test_quantity_parsing(self):
        script = """
const cases = ["10", "10-20", "10 MT", "500 KG", "1,000", "1.5 tons", "unknown"];
const results = cases.map(c => parseQuantity(c));
console.log(JSON.stringify(results));
"""
        res = self._run_js(script)
        self.assertEqual(res, [10, 10, 10, 500, 1000, 1.5, 0])

    def test_http_transport(self):
        script = """
const http = require('http');

(async () => {
    const server = http.createServer((req, res) => {
        let body = '';
        req.on('data', chunk => body += chunk.toString());
        req.on('end', () => {
            const data = JSON.parse(body || "{}");
            if (data.status === 'test200') {
                res.writeHead(200); res.end('{"ok":true}');
            } else if (data.status === 'test400') {
                res.writeHead(400); res.end('{"error":"bad request"}');
            } else if (data.status === 'test500') {
                res.writeHead(500); res.end('{"error":"internal error"}');
            } else {
                res.writeHead(404); res.end();
            }
        });
    });

    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const port = server.address().port;
    _config = { server_port: port }; // Override port for postLog
    
    const results = {};
    
    results.r200 = await postLog({ status: 'test200' });
    results.r400 = await postLog({ status: 'test400' });
    results.r500 = await postLog({ status: 'test500' });
    
    // connection refused
    _config.server_port = 1; // Unlikely to be in use
    results.refused = await postLog({ status: 'test_refused' });
    
    server.close();
    
    console.log(JSON.stringify(results));
})();
"""
        res = self._run_js(script)
        self.assertTrue(res["r200"]["success"])
        self.assertFalse(res["r400"]["success"])
        self.assertIn("HTTP 400", res["r400"]["error"])
        self.assertFalse(res["r500"]["success"])
        self.assertIn("HTTP 500", res["r500"]["error"])
        self.assertFalse(res["refused"]["success"])
        self.assertIn("fetch", res["refused"]["error"].lower())

if __name__ == "__main__":
    unittest.main()
