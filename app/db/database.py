"""
app/db/database.py
==================
Thread-safe SQLite database layer for SafeIM Lead Monitor V2.

Design:
  - WAL (Write-Ahead Logging) journal mode for concurrent read/write performance.
  - A threading.Lock() guards every write operation to prevent data corruption.
  - Thread-local connections so each thread has its own SQLite connection handle.
  - ON CONFLICT IGNORE on the lead_hash UNIQUE index provides automatic duplicate prevention.
"""

import sqlite3
import threading
import hashlib
import logging
from datetime import date
from pathlib import Path
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


class LeadDatabase:
    """
    Thread-safe SQLite manager for lead storage and purchase tracking.

    Tables:
        leads            — All discovered/purchased lead records.
        daily_purchases  — Running tally of real purchases per calendar day.
    """

    def __init__(self, db_path: Path):
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._local = threading.local()   # Thread-local connection storage

        # Registry of EVERY connection handed out, across all threads, so that
        # shutdown can close them all rather than only the caller's.
        self._all_conns = []
        self._conn_registry_lock = threading.Lock()

        logger.info(f"LeadDatabase initializing at: {db_path}")

    # =========================================================================
    # Connection Management
    # =========================================================================

    def _get_conn(self) -> sqlite3.Connection:
        """
        Return (or lazily create) the thread-local SQLite connection.
        Each thread gets its own connection; SQLite allows multiple readers
        and one writer simultaneously under WAL mode.

        Every connection created here is also registered in _all_conns so that
        close_all() can release connections belonging to threads other than the
        caller's.  Without that registry, connections opened on the monitor
        thread and the HTTP serving thread were never closed -- they were only
        reclaimed when the process exited, leaving -wal/-shm files behind.
        """
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
            with self._conn_registry_lock:
                self._all_conns.append(conn)
        return self._local.conn

    # =========================================================================
    # Initialization
    # =========================================================================

    def initialize(self):
        """Create all database tables and indexes on first run."""
        with self._lock:
            conn = self._get_conn()
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS leads (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    lead_hash       TEXT    UNIQUE NOT NULL,
                    title           TEXT    NOT NULL,
                    location        TEXT    DEFAULT '',
                    country         TEXT    DEFAULT '',
                    quantity        TEXT    DEFAULT '',
                    customer_name   TEXT    DEFAULT '',
                    contact_info    TEXT    DEFAULT '',
                    purchased       INTEGER DEFAULT 0,
                    simulated       INTEGER DEFAULT 0,
                    filtered_reason TEXT    DEFAULT '',
                    timestamp       TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS daily_purchases (
                    purchase_date   TEXT PRIMARY KEY,
                    count           INTEGER DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_lead_hash  ON leads (lead_hash);
                CREATE INDEX IF NOT EXISTS idx_timestamp  ON leads (timestamp);
                CREATE INDEX IF NOT EXISTS idx_purchased  ON leads (purchased);
            """)
            conn.commit()
        logger.info("Database tables ready.")

    # =========================================================================
    # Duplicate Detection
    # =========================================================================

    @staticmethod
    def compute_hash(title: str, location: str, quantity: str) -> str:
        """
        Build a SHA-256 fingerprint for a lead.
        Used to detect exact duplicates across polling cycles.
        """
        raw = f"{title.strip().lower()}|{location.strip().lower()}|{quantity.strip().lower()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def lead_exists(self, lead_hash: str) -> bool:
        """Return True if a lead with this hash is already in the database."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM leads WHERE lead_hash = ? LIMIT 1", (lead_hash,)
        ).fetchone()
        return row is not None

    # =========================================================================
    # Write Operations
    # =========================================================================

    def add_lead(self, lead_data: Dict[str, Any]) -> bool:
        """
        Insert a lead record.  Returns True if inserted, False if it was a duplicate.
        Uses ON CONFLICT IGNORE so duplicate hashes are silently skipped.
        """
        with self._lock:
            try:
                conn = self._get_conn()
                data = dict(lead_data)
                data["customer_name"] = data.get("display_name", data.get("customer_name", ""))
                conn.execute(
                    """
                    INSERT OR IGNORE INTO leads
                        (lead_hash, title, location, country, quantity,
                         customer_name, contact_info, purchased, simulated,
                         filtered_reason, timestamp)
                    VALUES
                        (:lead_hash, :title, :location, :country, :quantity,
                         :customer_name, :contact_info, :purchased, :simulated,
                         :filtered_reason, :timestamp)
                    """,
                    data,
                )
                rows_changed = conn.execute("SELECT changes()").fetchone()[0]
                conn.commit()
                return rows_changed > 0
            except sqlite3.Error as exc:
                logger.error(f"DB add_lead error: {exc}")
                return False

    def increment_today_count(self):
        """
        Atomically bump today's purchase counter.
        Uses UPSERT so the row is created on the first purchase of the day.
        """
        with self._lock:
            today = date.today().isoformat()
            conn = self._get_conn()
            conn.execute(
                """
                INSERT INTO daily_purchases (purchase_date, count) VALUES (?, 1)
                ON CONFLICT(purchase_date) DO UPDATE SET count = count + 1
                """,
                (today,),
            )
            conn.commit()

    # =========================================================================
    # Read Operations
    # =========================================================================

    def get_today_purchase_count(self) -> int:
        """Return the number of real (non-simulated) purchases made today."""
        today = date.today().isoformat()
        conn = self._get_conn()
        row = conn.execute(
            "SELECT count FROM daily_purchases WHERE purchase_date = ?", (today,)
        ).fetchone()
        return int(row["count"]) if row else 0

    def get_all_leads(self) -> List[Dict[str, Any]]:
        """Return all lead records sorted newest-first."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM leads ORDER BY timestamp DESC"
        ).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            d["display_name"] = d.pop("customer_name", "")
            results.append(d)
        return results

    def search_leads(self, query: str) -> List[Dict[str, Any]]:
        """Full-text search across title, contact_info, country, and customer_name."""
        pattern = f"%{query}%"
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT * FROM leads
            WHERE  title         LIKE ?
               OR  contact_info  LIKE ?
               OR  country       LIKE ?
               OR  customer_name LIKE ?
            ORDER BY timestamp DESC
            """,
            (pattern, pattern, pattern, pattern),
        ).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            d["display_name"] = d.pop("customer_name", "")
            results.append(d)
        return results

    # =========================================================================
    # Cleanup
    # =========================================================================

    def close(self):
        """Close the thread-local connection if open."""
        if hasattr(self._local, "conn") and self._local.conn:
            conn = self._local.conn
            conn.close()
            self._local.conn = None
            with self._conn_registry_lock:
                if conn in self._all_conns:
                    self._all_conns.remove(conn)

    def close_all(self) -> int:
        """
        Close every connection created by any thread.  Returns the count closed.

        Call this exactly once, during application shutdown, after the threads
        that use the database have been stopped.  Closing a connection while
        another thread is mid-query would raise there, so ordering matters:
        stop the monitor thread first, then call close_all().
        """
        with self._conn_registry_lock:
            conns = list(self._all_conns)
            self._all_conns.clear()

        closed = 0
        for conn in conns:
            try:
                conn.close()
                closed += 1
            except Exception as exc:
                logger.warning("Error closing a SQLite connection: %s", exc)

        if hasattr(self._local, "conn"):
            self._local.conn = None

        logger.info("LeadDatabase closed %d connection(s).", closed)
        return closed
