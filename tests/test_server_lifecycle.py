"""
tests/test_server_lifecycle.py
==============================
Infrastructure test suite for the SafeIM LocalServer lifecycle.

This file tests GENERIC local-desktop / HTTP-server infrastructure only.
It does NOT test, exercise, or depend on any lead-acquisition, filtering, or
purchasing behaviour.

Safety properties of this suite
-------------------------------
  * Binds and connects to 127.0.0.1 ONLY -- no external network access.
  * Never launches Chrome and never loads the extension.
  * Never contacts IndiaMART or any remote host.
  * Never performs a purchase; the only POST body used is obviously-dummy data
    routed through a stub database that writes nothing to disk.
  * Uses a stub DB, so the real leads_history.db is never opened or modified.

Covered cases (13 required + supporting checks)
-----------------------------------------------
   1. Server startup
   2. Health endpoint
   3. Server instance identification
   4. Valid request handling
   5. Invalid request handling
   6. Queue event delivery
   7. Clean stop
   8. Idempotent stop
   9. Server thread termination
  10. Socket release
  11. Immediate restart
  12. Occupied-port failure
  13. Repeated startup/shutdown cycles

Windows note (important)
------------------------
  Two of these tests are only meaningful if they are written carefully on
  Windows:

  * Case 10 (socket release) must NOT be proved with a trial bind that sets
    SO_REUSEADDR on Windows.  There that flag lets a bind succeed even while
    another process is actively listening, so such a test passes whether or
    not the server released anything.  This suite proves release with a TCP
    connect probe plus a bind that uses the same platform-correct flags the
    server itself uses (SO_EXCLUSIVEADDRUSE on Windows, SO_REUSEADDR on POSIX
    where it only relaxes TIME_WAIT and cannot steal a live listener).

  * Case 12 (occupied port) must prove that a real listener actually prevents
    binding.  This suite starts a genuine listening socket (listen(), not just
    bind()) and asserts that LocalServer.start() returns False and reports
    state "bind_failed".

Run with:
    python tests/test_server_lifecycle.py

Exit code 0 = all checks passed, 1 = at least one check failed.
"""

import json
import os
import platform
import queue
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# Make sure the project root is on the path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from app.core.server import (          # noqa: E402
    LocalServer,
    SERVICE_NAME,
    STATE_BIND_FAILED,
    STATE_RUNNING,
    STATE_STOPPED,
    probe_listener,
)

# =============================================================================
# Configuration
# =============================================================================

HOST = "127.0.0.1"

# Default test port.  Deliberately NOT 9223 so that running this suite while
# the real application is open does not fight it for the production port.
# Override with:  SAFEIM_TEST_PORT=9401 python tests/test_server_lifecycle.py
PORT = int(os.environ.get("SAFEIM_TEST_PORT", "9401"))

# Second port used by the occupied-port test so it never disturbs PORT.
BUSY_PORT = PORT + 1

_USE_COLOR = sys.stdout.isatty() and os.name != "nt"
PASS = "\033[92mPASS\033[0m" if _USE_COLOR else "PASS"
FAIL = "\033[91mFAIL\033[0m" if _USE_COLOR else "FAIL"

results = []          # list[(case_id, label, bool)]
_current_case = "-"


# =============================================================================
# Stub DB -- writes nothing, touches no file
# =============================================================================

class _StubDB:
    """
    Minimal in-memory stub satisfying the interface LocalServer expects.

    Deliberately inert: no file is opened, nothing is persisted, and
    get_today_purchase_count() always returns 0 so no cap logic can trigger.
    """

    def __init__(self):
        self.added = []

    def get_today_purchase_count(self):
        return 0

    def lead_exists(self, lead_hash):
        return False

    def add_lead(self, data):
        self.added.append(data)

    def compute_hash(self, *args):
        return "stubhash"

    def increment_today_count(self):
        pass

    def close(self):
        pass


def make_settings(port):
    """Settings dict used by the suite -- simulation mode always ON."""
    return {
        "server_port":          port,
        "simulation_mode":      True,     # never leaves simulation
        "max_daily_purchases":  20,
        "max_session_purchases": 10,
        "keywords":             [],
        "excluded_locations":   [],
        "keyword_blacklist":    [],
        "target_countries":     [],
        "min_quantity":         1,
        "max_lead_age_minutes": 0,
        "contact_filter_mode":  "any",
    }


# =============================================================================
# Harness
# =============================================================================

def case(case_id, title):
    global _current_case
    _current_case = case_id
    print(f"\n[{case_id}] {title}")
    print("-" * 66)


def check(label, condition, detail=""):
    condition = bool(condition)
    status = PASS if condition else FAIL
    print(f"  [{status}] {label}")
    if detail:
        print(f"         {detail}")
    results.append((_current_case, label, condition))
    return condition


def http_get(path, port=PORT, timeout=3):
    url = f"http://{HOST}:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:
        return None, str(e)


def http_post(path, body, port=PORT, timeout=3, raw=None):
    """POST JSON (or `raw` bytes when supplied, to test malformed input)."""
    url = f"http://{HOST}:{port}{path}"
    data = raw if raw is not None else json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:
        return None, str(e)


def drain(q):
    """Drain a queue into a list."""
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def bind_like_server(host, port):
    """
    Try to bind a listening socket using the SAME platform-correct flags the
    fixed LocalServer uses.  Returns (ok, error_string).

    Why the flags differ per platform -- this is the crux of the bug class:

      Windows : SO_EXCLUSIVEADDRUSE, and explicitly NOT SO_REUSEADDR.
                On Windows SO_REUSEADDR permits binding a port that another
                socket is ACTIVELY LISTENING on, so a bind test that sets it
                would report "port free" even while a live server owns it.
                That is a false positive in exactly the case that matters, and
                it is what made the previous version of this test worthless.

      POSIX   : SO_REUSEADDR is set, because on POSIX it only relaxes TIME_WAIT
                and cannot steal an active listener.  Without it this check
                would fail spuriously whenever the server had just finished
                serving connections, since those sockets linger in TIME_WAIT.

    Case 12 independently proves that this helper cannot bind over a real
    listener, so a success here genuinely means the port was released.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.listen(1)
        return True, ""
    except OSError as exc:
        return False, str(exc)
    finally:
        try:
            s.close()
        except OSError:
            pass


class RealListener:
    """
    A genuine listening TCP socket used by the occupied-port test.

    It calls listen() and accepts connections in a background thread, so it is
    a real listener -- not merely a bound socket.  That distinction matters:
    a bound-but-not-listening socket behaves differently from a live server.
    """

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # No SO_REUSEADDR: we want to own this port the way a real server does.
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        self.sock.bind((host, port))
        self.sock.listen(5)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self):
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
                conn.close()
            except (socket.timeout, OSError):
                continue

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2.0)
        try:
            self.sock.close()
        except OSError:
            pass


def new_server(port=PORT, db=None, q=None):
    return LocalServer(
        db=db or _StubDB(),
        settings=make_settings(port),
        ui_queue=q if q is not None else queue.Queue(maxsize=200),
        host=HOST,
        port=port,
    )


DUMMY_LEAD = {
    "status":        "simulated",       # never "purchased"
    "title":         "Infrastructure Test Payload",
    "country":       "Testland",
    "quantity":      "1",
    "location":      "Localhost",
    "customer_name": "Test Harness",
    "contact_info":  "",
    "lead_hash":     "infratest-0000000001",
    "reason":        "Infrastructure test: simulation only",
    "score":         0,
}


# =============================================================================
# Test cases
# =============================================================================

def test_01_startup():
    case("01", "Server startup")
    q  = queue.Queue(maxsize=200)
    srv = new_server(q=q)

    ok = srv.start()
    check("start() returns True", ok, f"state={srv.get_state()}")
    check("state == 'running'", srv.get_state() == STATE_RUNNING, srv.get_state())
    check("is_running() is True", srv.is_running())
    check("serving thread is alive", srv.is_thread_alive())
    check("a listener is present on the port",
          probe_listener(HOST, PORT),
          f"{HOST}:{PORT}")
    check("uptime is being tracked", srv.uptime_seconds() >= 0.0,
          f"uptime={srv.uptime_seconds()}s")
    return srv, q


def test_02_health(srv):
    case("02", "Health endpoint")
    status, body = http_get("/health")
    check("GET /health returns HTTP 200", status == 200, f"got {status}")

    doc = None
    try:
        doc = json.loads(body)
        parsed = isinstance(doc, dict)
    except Exception as exc:
        parsed = False
        print(f"         parse error: {exc}")
    check("response body is a JSON object", parsed, f"body={body[:120]}")

    if not parsed:
        return None

    check("field 'ok' is True", doc.get("ok") is True, f"ok={doc.get('ok')}")
    check("field 'service' identifies this application",
          doc.get("service") == SERVICE_NAME,
          f"service={doc.get('service')!r}")
    for field in ("version", "instance_id", "state", "port"):
        check(f"field '{field}' present", field in doc, f"keys={sorted(doc)}")
    check("health response does NOT leak settings",
          "keywords" not in doc and "max_daily_purchases" not in doc,
          "a status probe must not be able to read configuration")

    # An OPTIONS response must NOT be treated as a health signal: it answers
    # 200 for paths the server does not even serve.
    req = urllib.request.Request(f"http://{HOST}:{PORT}/definitely-not-a-real-path",
                                 method="OPTIONS")
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            opt_status = r.status
    except Exception as exc:
        opt_status = str(exc)
    check("OPTIONS on a nonexistent path still returns 200 "
          "(so OPTIONS is NOT proof of health)",
          opt_status == 200,
          f"OPTIONS /definitely-not-a-real-path -> {opt_status}")

    return doc


def test_03_instance_identification(srv, health_doc):
    case("03", "Server instance identification")
    check("instance_id is non-empty", bool(srv.instance_id), srv.instance_id)

    if health_doc:
        check("/health reports the same instance_id as the object",
              health_doc.get("instance_id") == srv.instance_id,
              f"health={health_doc.get('instance_id')} obj={srv.instance_id}")

    # Two LocalServer objects in the SAME process must not share an id.
    # The previous implementation used one module-level uuid for the whole
    # process, so this check could never fail and the id proved nothing.
    other = new_server(port=PORT + 50)
    check("a second LocalServer object gets a DIFFERENT instance_id",
          other.instance_id != srv.instance_id,
          f"{srv.instance_id} vs {other.instance_id}")


def test_04_valid_request(srv, q):
    case("04", "Valid request handling")
    drain(q)

    status, body = http_get("/config")
    check("GET /config returns HTTP 200", status == 200, f"got {status}")
    try:
        cfg = json.loads(body)
        check("GET /config returns a JSON object with 'server_port'",
              isinstance(cfg, dict) and "server_port" in cfg,
              f"keys={sorted(cfg)[:8] if isinstance(cfg, dict) else type(cfg)}")
    except Exception as exc:
        check("GET /config returns a JSON object with 'server_port'", False, str(exc))

    status, body = http_post("/log", DUMMY_LEAD)
    check("POST /log with a valid body returns HTTP 200", status == 200,
          f"got {status} {body[:80]}")


def test_05_invalid_request(srv):
    case("05", "Invalid request handling")

    status, _ = http_get("/no-such-endpoint")
    check("GET on an unknown path returns 404", status == 404, f"got {status}")

    status, _ = http_post("/no-such-endpoint", {"a": 1})
    check("POST on an unknown path returns 404", status == 404, f"got {status}")

    status, _ = http_post("/log", None, raw=b"{this is not json")
    check("POST /log with malformed JSON returns 400", status == 400,
          f"got {status}")

    status, _ = http_post("/log", None, raw=b'"a bare string"')
    check("POST /log with a non-object JSON body returns 400", status == 400,
          f"got {status}")

    # After four bad requests the server must still be serving.
    status, _ = http_get("/health")
    check("server still healthy after invalid requests", status == 200,
          f"GET /health -> {status}")


def test_06_queue_delivery(srv, q):
    case("06", "Queue event delivery")
    drain(q)

    lead = dict(DUMMY_LEAD)
    lead["lead_hash"] = "infratest-0000000002"   # avoid the duplicate guard
    status, _ = http_post("/log", lead)
    check("POST /log accepted", status == 200, f"got {status}")

    # Bounded wait for the event to cross the thread boundary.  This is an
    # explicit deadline on an asynchronous handoff, not a fixed sleep used to
    # paper over a race.
    deadline = time.monotonic() + 2.0
    events = []
    while time.monotonic() < deadline:
        events.extend(drain(q))
        if any(e.get("type") == "lead" for e in events):
            break
        time.sleep(0.02)

    types = [e.get("type") for e in events]
    check("ui_queue received a 'log' event", "log" in types, f"types={types}")
    check("ui_queue received a 'lead' event", "lead" in types, f"types={types}")

    lead_events = [e for e in events if e.get("type") == "lead"]
    check("exactly one 'lead' event was delivered (no duplicates)",
          len(lead_events) == 1, f"count={len(lead_events)}")

    # 'ext_connected' must be emitted once per run, not once per request.
    check("'ext_connected' was NOT re-emitted on a later request",
          types.count("ext_connected") == 0,
          "first-contact event belongs to the earlier /config call only")

    check("client contact count increased",
          srv.client_contact_count() >= 2,
          f"contacts={srv.client_contact_count()}")


def test_07_08_09_10_stop(srv):
    case("07", "Clean stop")
    report = srv.stop()
    check("stop() returned a report dict", isinstance(report, dict), str(report)[:120])
    check("stop() signalled serve_forever() to shut down",
          report.get("shutdown_signalled"))
    check("stop() called server_close()", report.get("socket_closed"))
    check("stop() reported no errors", not report.get("errors"),
          f"errors={report.get('errors')}")
    check("state == 'stopped'", srv.get_state() == STATE_STOPPED, srv.get_state())
    check("stop() completed within its bound",
          report.get("duration_seconds", 99) < 6.0,
          f"{report.get('duration_seconds')}s")

    case("09", "Server thread termination")
    # The old suite asserted `server._thread is None`, which stop() set
    # unconditionally -- a tautology that passed even if the thread was alive.
    # These checks use the recorded join() result and an independent Event set
    # by the serve loop itself.
    check("join() confirmed the serving thread exited "
          "(not merely a cleared reference)",
          report.get("thread_joined") is True and
          report.get("thread_alive_after") is False,
          f"thread_joined={report.get('thread_joined')} "
          f"alive_after={report.get('thread_alive_after')}")
    check("serve_forever() loop reached its exit path",
          report.get("serve_loop_exited") is True)
    check("is_thread_alive() is False", srv.is_thread_alive() is False)

    case("10", "Socket release")
    verdict = srv.verify_stopped()
    check("no listener answers on the port any more",
          verdict.get("port_has_listener") is False,
          f"probe={verdict.get('port_has_listener')}")
    ok, err = bind_like_server(HOST, PORT)
    check("a new listening socket can claim the port "
          "(platform-correct flags; see case 12 for why this is not a "
          "false positive on Windows)",
          ok, err or "bound and listened successfully")
    check("verify_stopped() reports a clean shutdown", verdict.get("clean"),
          str(verdict))

    case("08", "Idempotent stop")
    try:
        second = srv.stop()
        raised = False
    except Exception as exc:
        second, raised = {}, True
        print(f"         exception: {exc}")
    check("a second stop() does not raise", not raised)
    check("a second stop() reports no new errors", not second.get("errors"),
          f"errors={second.get('errors')}")
    check("state remains 'stopped' after the second stop()",
          srv.get_state() == STATE_STOPPED, srv.get_state())

    try:
        srv.stop()
        srv.stop()
        check("further stop() calls remain safe", True)
    except Exception as exc:
        check("further stop() calls remain safe", False, str(exc))


def test_11_immediate_restart():
    case("11", "Immediate restart")
    q   = queue.Queue(maxsize=200)
    srv = new_server(q=q)

    check("initial start() succeeds", srv.start(), srv.get_state())
    srv.stop()

    # No sleep between stop() and start(): an immediate rebind is exactly the
    # scenario that fails when the socket was not properly closed.
    restarted = srv.start()
    check("start() immediately after stop() succeeds (same object)",
          restarted, f"state={srv.get_state()} err={srv.get_bind_error()}")
    check("state == 'running' after restart",
          srv.get_state() == STATE_RUNNING, srv.get_state())

    status, body = http_get("/health")
    check("restarted server answers /health with 200", status == 200, f"got {status}")

    new_id = None
    try:
        new_id = json.loads(body).get("instance_id")
    except Exception:
        pass
    check("restarted instance still reports its identifier", bool(new_id),
          f"instance_id={new_id}")

    srv.stop()

    # A brand-new object must also be able to take the port straight away.
    srv2 = new_server()
    check("a FRESH LocalServer object can bind the port immediately",
          srv2.start(), f"state={srv2.get_state()} err={srv2.get_bind_error()}")
    srv2.stop()


def test_12_occupied_port():
    case("12", "Occupied-port failure")
    print(f"         (using port {BUSY_PORT} so the main test port is untouched)")

    listener = None
    try:
        listener = RealListener(HOST, BUSY_PORT)
    except OSError as exc:
        check("test precondition: a real listener could be started", False, str(exc))
        return

    try:
        check("precondition: the port really is occupied by a live listener",
              probe_listener(HOST, BUSY_PORT),
              "TCP connect to the listener succeeded")

        srv = new_server(port=BUSY_PORT)
        started = srv.start()

        check("start() returns False when the port is occupied",
              started is False,
              f"returned {started}; state={srv.get_state()}")
        check("state == 'bind_failed'",
              srv.get_state() == STATE_BIND_FAILED, srv.get_state())
        check("a specific bind-failure reason was recorded",
              bool(srv.get_bind_error()), srv.get_bind_error())
        check("no serving thread was started", srv.is_thread_alive() is False)
        check("the port was NOT silently changed",
              srv.port == BUSY_PORT, f"port={srv.port}")

        # The decisive Windows check: the original listener must still be the
        # one answering.  If SO_REUSEADDR had allowed a double bind, ownership
        # would be indeterminate and this invariant is what we would lose.
        check("the original listener still owns the port",
              probe_listener(HOST, BUSY_PORT))

        stop_report = srv.stop()
        check("stop() on a never-started server is safe",
              isinstance(stop_report, dict) and not stop_report.get("errors"),
              str(stop_report.get("errors")))
    finally:
        if listener:
            listener.close()

    # Once the foreign listener is gone the port must become usable.
    deadline = time.monotonic() + 3.0
    bound = False
    while time.monotonic() < deadline:
        srv3 = new_server(port=BUSY_PORT)
        if srv3.start():
            bound = True
            srv3.stop()
            break
        time.sleep(0.1)
    check("the port becomes bindable after the foreign listener closes", bound)


def test_13_repeated_cycles(cycles=5):
    case("13", "Repeated startup/shutdown cycles")
    failures = []
    ids = set()

    for i in range(1, cycles + 1):
        q   = queue.Queue(maxsize=200)
        srv = new_server(q=q)
        ids.add(srv.instance_id)

        if not srv.start():
            failures.append(f"cycle {i}: start() failed ({srv.get_bind_error()})")
            continue

        status, _ = http_get("/health")
        if status != 200:
            failures.append(f"cycle {i}: /health returned {status}")

        report  = srv.stop()
        verdict = srv.verify_stopped()

        if not report.get("thread_joined"):
            failures.append(f"cycle {i}: serving thread did not exit")
        if not report.get("socket_closed"):
            failures.append(f"cycle {i}: server_close() did not run")
        if not verdict.get("clean"):
            failures.append(f"cycle {i}: not clean -> {verdict}")

    check(f"{cycles} consecutive start/stop cycles all succeeded",
          not failures,
          "; ".join(failures) if failures else f"{cycles}/{cycles} clean")
    check("every cycle used a distinct instance_id",
          len(ids) == cycles, f"{len(ids)} unique ids across {cycles} cycles")

    ok, err = bind_like_server(HOST, PORT)
    check("port is still free after all cycles (no socket leak)", ok, err)


# =============================================================================
# Runner
# =============================================================================

def main():
    print("=" * 66)
    print("  SafeIM LocalServer -- Infrastructure Lifecycle Test Suite")
    print("=" * 66)
    print(f"  Platform    : {platform.system()} {platform.release()} "
          f"({os.name}), Python {platform.python_version()}")
    print(f"  Test host   : {HOST}")
    print(f"  Test ports  : {PORT} (main), {BUSY_PORT} (occupied-port case)")
    print("  Network     : loopback only -- no external hosts contacted")
    print("  Browser     : not launched")
    print("  Purchases   : none (simulation-only dummy payloads, stub DB)")
    if os.name != "nt":
        print("  NOTE        : case 12 is most meaningful on Windows, where")
        print("                SO_REUSEADDR would otherwise allow a silent")
        print("                double-bind over a live listener.")
    print("=" * 66)

    # Pre-flight: the main test port must be free or nothing below is valid.
    if probe_listener(HOST, PORT):
        print(f"\n  {FAIL} Port {PORT} is already in use. "
              f"Close that process or set SAFEIM_TEST_PORT.\n")
        sys.exit(1)

    srv = None
    try:
        srv, q = test_01_startup()
        health = test_02_health(srv)
        test_03_instance_identification(srv, health)
        test_04_valid_request(srv, q)
        test_05_invalid_request(srv)
        test_06_queue_delivery(srv, q)
        test_07_08_09_10_stop(srv)
        test_11_immediate_restart()
        test_12_occupied_port()
        test_13_repeated_cycles()
    except Exception as exc:
        import traceback
        print("\n  Unhandled exception in the test run:")
        traceback.print_exc()
        results.append(("--", f"suite completed without crashing ({exc})", False))
    finally:
        # Never leave a listener behind, whatever happened above.
        try:
            if srv is not None:
                srv.stop()
        except Exception:
            pass

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 66)
    print("  RESULTS BY CASE")
    print("=" * 66)

    case_titles = {
        "01": "Server startup",
        "02": "Health endpoint",
        "03": "Server instance identification",
        "04": "Valid request handling",
        "05": "Invalid request handling",
        "06": "Queue event delivery",
        "07": "Clean stop",
        "08": "Idempotent stop",
        "09": "Server thread termination",
        "10": "Socket release",
        "11": "Immediate restart",
        "12": "Occupied-port failure",
        "13": "Repeated startup/shutdown cycles",
    }

    for cid in sorted(case_titles):
        rows = [r for r in results if r[0] == cid]
        if not rows:
            print(f"  [{'SKIP'}] {cid}  {case_titles[cid]}  (not executed)")
            continue
        passed = sum(1 for _, _, ok in rows if ok)
        verdict = PASS if passed == len(rows) else FAIL
        print(f"  [{verdict}] {cid}  {case_titles[cid]}  ({passed}/{len(rows)} checks)")
        for _, label, ok in rows:
            if not ok:
                print(f"           - failed: {label}")

    total  = len(results)
    passed = sum(1 for _, _, ok in results if ok)
    print("-" * 66)
    print(f"  {passed}/{total} individual checks passed")
    print("=" * 66 + "\n")
    sys.exit(0 if passed == total and total > 0 else 1)


if __name__ == "__main__":
    main()
