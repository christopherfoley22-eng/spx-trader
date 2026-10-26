"""Independent durable OFFLINE emergency latch; no broker or execution API."""

import hashlib
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from mortificatio_store import private_database


class EmergencyError(RuntimeError):
    pass


class EmergencyLatch:
    def __init__(self, path, account):
        self.path = private_database(path)
        self.binding = hashlib.sha256(account.encode()).hexdigest()
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS emergency (singleton INTEGER PRIMARY KEY CHECK(singleton=1), account_hash TEXT NOT NULL, request_id TEXT, phase TEXT NOT NULL, evidence TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS clicks (request_id TEXT PRIMARY KEY)")
            db.execute("INSERT OR IGNORE INTO emergency VALUES (1,?,NULL,'CLEAR',NULL)", (self.binding,))
            self._validate(db)
        self.path.chmod(0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def _validate(self, db):
        row = db.execute("SELECT account_hash,request_id,phase,evidence FROM emergency WHERE singleton=1").fetchone()
        if (row is None or row[0] != self.binding or row[2] not in {"CLEAR", "LATCHED", "FLAT_CONFIRMED"}
                or row[2] != "CLEAR" and not row[1]
                or row[2] == "FLAT_CONFIRMED" and not row[3]):
            raise EmergencyError("Emergency state requires recovery")
        return row

    def status(self):
        with self._db() as db:
            _, request_id, phase, evidence = self._validate(db)
        return {"latched": phase != "CLEAR", "request_id": request_id, "phase": phase,
                "evidence": evidence, "executable": False}

    def blocked(self):
        try:
            return self.status()["latched"]
        except (EmergencyError, sqlite3.DatabaseError, OSError):
            return True

    def request(self, request_id):
        if not isinstance(request_id, str) or not 0 < len(request_id) <= 120:
            raise EmergencyError("Stable emergency request ID required")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._validate(db)
            old = db.execute("SELECT 1 FROM clicks WHERE request_id=?", (request_id,)).fetchone()
            db.execute("INSERT OR IGNORE INTO clicks VALUES (?)", (request_id,))
            if not old:
                db.execute("UPDATE emergency SET request_id=coalesce(request_id,?),phase='LATCHED',evidence=NULL WHERE singleton=1", (request_id,))
        return "DUPLICATE" if old else "LATCHED"

    def confirm_flat(self, evidence):
        """Caller must first validate fresh reconciled broker evidence."""
        if not isinstance(evidence, str) or not evidence:
            raise EmergencyError("Reconciled flat evidence required")
        with self._db() as db:
            row = self._validate(db)
            if row[2] == "CLEAR":
                raise EmergencyError("No emergency to confirm")
            db.execute("UPDATE emergency SET phase='FLAT_CONFIRMED',evidence=? WHERE singleton=1", (evidence,))

    def pending(self):
        with self._db() as db:
            self._validate(db)
            db.execute("UPDATE emergency SET phase='LATCHED',evidence=NULL WHERE singleton=1 AND phase!='CLEAR'")
