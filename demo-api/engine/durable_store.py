"""Transactional snapshot ledger. No raw rejected payloads are persisted."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timezone


SCHEMA_VERSION = "1"


class InvariantError(RuntimeError):
    pass


def canonical_json(snapshot):
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def fingerprint(snapshot):
    return hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()


def _timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise InvariantError("normalizer must return a UTC timestamp")
    return parsed


class Store:
    def __init__(self, db_path, normalizer, policy_hash, *, fault_hook=None):
        self.path = Path(db_path).resolve()
        self.error_path = self.path.with_suffix(self.path.suffix + ".errors.jsonl")
        self.normalizer = normalizer
        self.fault_hook = fault_hook
        self.connection = None
        try:
            if not isinstance(policy_hash, str) or not policy_hash:
                raise ValueError("policy_hash must be nonempty")
            self.connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("BEGIN IMMEDIATE")
            tables = {r[0] for r in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if not tables:
                for statement in _SCHEMA:
                    self.connection.execute(statement)
                self.connection.executemany("INSERT INTO metadata(key,value) VALUES (?,?)",
                    [("schema_version", SCHEMA_VERSION), ("policy_hash", policy_hash)])
            else:
                if tables != {"metadata", "accepted", "current", "attempts", "quarantine"}:
                    raise InvariantError("unrecognized database schema; explicit migration required")
                metadata = dict(self.connection.execute("SELECT key,value FROM metadata"))
                if metadata != {"schema_version": SCHEMA_VERSION, "policy_hash": policy_hash}:
                    raise InvariantError("schema/policy mismatch; use explicit migration or a new database")
            self.connection.commit()
            self.verify()
        except Exception as error:
            self._rollback()
            self._log_failure("open", error)
            self.close()
            raise

    def _log_failure(self, operation, error):
        # Exception messages may contain provider values, SQL or paths: log type only.
        record = {"at": datetime.now(timezone.utc).isoformat(), "severity": "FAIL",
                  "operation": operation, "exception_type": type(error).__name__}
        try:
            with self.error_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except Exception as logging_error:
            print(json.dumps({**record, "logging_exception_type": type(logging_error).__name__}),
                  file=sys.stderr)

    def _rollback(self):
        if self.connection is not None and self.connection.in_transaction:
            try:
                self.connection.rollback()
            except Exception as error:
                self._log_failure("rollback", error)

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def ingest(self, raw: bytes, source: str):
        try:
            if not isinstance(raw, bytes) or not isinstance(source, str):
                raise TypeError("ingest expects bytes and source reference string")
            raw_hash = hashlib.sha256(raw).hexdigest()
            snapshot = None
            code = path = None
            try:
                snapshot = self.normalizer(raw)
            except Exception as error:
                if not (isinstance(getattr(error, "code", None), str) and
                        isinstance(getattr(error, "path", None), str)):
                    raise
                code, path = error.code, error.path
            if snapshot is not None:
                serialized = canonical_json(snapshot)
                digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
                identity = tuple(snapshot[k] for k in ("broker", "account", "snapshot_id"))
                if any(not isinstance(v, str) or not v for v in identity):
                    raise InvariantError("normalizer returned invalid identity")
                timestamp = _timestamp(snapshot["as_of"])
            elif code is None:
                raise InvariantError("normalizer returned no snapshot")
            db = self.connection
            db.execute("BEGIN IMMEDIATE")
            status = "QUARANTINED"
            if snapshot is not None:
                previous = db.execute("SELECT fingerprint FROM accepted WHERE broker=? AND account=? AND snapshot_id=?", identity).fetchone()
                active = db.execute("SELECT as_of FROM current WHERE broker=? AND account=?", identity[:2]).fetchone()
                if previous:
                    if previous["fingerprint"] == digest:
                        status = "REPLAY"
                    else:
                        code, path = "CONFLICTING_REPLAY", "$.snapshot_id"
                elif active and timestamp <= _timestamp(active["as_of"]):
                    code, path = "STALE_SNAPSHOT", "$.as_of"
                else:
                    status = "ACCEPTED"
                    values = (*identity, snapshot["as_of"], digest, serialized)
                    db.execute("INSERT INTO accepted(broker,account,snapshot_id,as_of,fingerprint,snapshot_json) VALUES (?,?,?,?,?,?)", values)
                    db.execute("""INSERT INTO current(broker,account,snapshot_id,as_of,fingerprint,snapshot_json)
                        VALUES (?,?,?,?,?,?) ON CONFLICT(broker,account) DO UPDATE SET
                        snapshot_id=excluded.snapshot_id,as_of=excluded.as_of,
                        fingerprint=excluded.fingerprint,snapshot_json=excluded.snapshot_json""", values)
            event = {"status": status, "source": source, "raw_sha256": raw_hash,
                     "code": code, "path": path}
            # Quarantine/audit metadata must never contain malformed payload values.
            if status != "QUARANTINED":
                event.update(dict(zip(("broker", "account", "snapshot_id"), identity)))
                event["fingerprint"] = digest
            attempt = db.execute("INSERT INTO attempts(status,event_json) VALUES (?,?)",
                                 (status, canonical_json(event)))
            event["attempt_id"] = attempt.lastrowid
            if status == "QUARANTINED":
                db.execute("INSERT INTO quarantine(attempt_id,sha256,source,code,path) VALUES (?,?,?,?,?)",
                           (attempt.lastrowid, raw_hash, source, code, path))
            if self.fault_hook is not None:
                self.fault_hook("before_commit")
            db.commit()
            return event
        except Exception as error:
            self._rollback()
            self._log_failure("ingest", error)
            raise

    def report(self):
        try:
            db = self.connection
            db.execute("BEGIN")
            counts = {"accepted": 0, "replay": 0, "quarantined": 0}
            for row in db.execute("SELECT status,COUNT(*) AS n FROM attempts GROUP BY status"):
                counts[row["status"].lower()] = row["n"]
            counts["attempts"] = sum(counts.values())
            counts["accepted_snapshots"] = db.execute("SELECT COUNT(*) FROM accepted").fetchone()[0]
            current = [json.loads(r[0]) for r in db.execute("SELECT snapshot_json FROM current ORDER BY broker,account")]
            counts["current_accounts"] = len(current)
            queue = [dict(r) for r in db.execute("SELECT * FROM quarantine ORDER BY attempt_id")]
            db.commit()
            return {"current": current, "counts": counts, "quarantine_queue": queue}
        except Exception as error:
            self._rollback()
            self._log_failure("report", error)
            raise

    def verify(self):
        try:
            db = self.connection
            db.execute("BEGIN")
            if [r[0] for r in db.execute("PRAGMA integrity_check")] != ["ok"]:
                raise InvariantError("SQLite integrity check failed")
            if list(db.execute("PRAGMA foreign_key_check")):
                raise InvariantError("foreign-key coverage failed")
            accepted = {}
            for row in db.execute("SELECT * FROM accepted"):
                snapshot = json.loads(row["snapshot_json"])
                key = tuple(row[k] for k in ("broker", "account", "snapshot_id"))
                if (fingerprint(snapshot) != row["fingerprint"] or
                    tuple(snapshot[k] for k in ("broker", "account", "snapshot_id")) != key or
                    snapshot["as_of"] != row["as_of"]):
                    raise InvariantError("accepted snapshot hash/identity mismatch")
                _timestamp(row["as_of"])
                accepted[key] = dict(row)
            current = list(db.execute("SELECT * FROM current"))
            for row in current:
                key = tuple(row[k] for k in ("broker", "account", "snapshot_id"))
                if key not in accepted or dict(row) != accepted[key]:
                    raise InvariantError("current snapshot missing/mismatched accepted coverage")
                if any(_timestamp(a["as_of"]) > _timestamp(row["as_of"])
                       for k, a in accepted.items() if k[:2] == key[:2]):
                    raise InvariantError("current snapshot is not latest accepted")
            if {k[:2] for k in accepted} != {(r["broker"], r["account"]) for r in current}:
                raise InvariantError("accepted account missing from current")
            accepted_events = []
            quarantine_ids = set()
            attempts = list(db.execute("SELECT * FROM attempts"))
            for row in attempts:
                event = json.loads(row["event_json"])
                if event["status"] != row["status"]:
                    raise InvariantError("attempt status mismatch")
                if event["status"] == "QUARANTINED":
                    quarantine_ids.add(row["attempt_id"])
                    q = db.execute("SELECT * FROM quarantine WHERE attempt_id=?", (row["attempt_id"],)).fetchone()
                    if q is None or any(q[k] != event[e] for k, e in
                        (("sha256", "raw_sha256"), ("source", "source"), ("code", "code"), ("path", "path"))):
                        raise InvariantError("quarantine audit coverage mismatch")
                else:
                    key = tuple(event[k] for k in ("broker", "account", "snapshot_id"))
                    if key not in accepted or event["fingerprint"] != accepted[key]["fingerprint"]:
                        raise InvariantError("attempt accepted coverage mismatch")
                    if event["status"] == "ACCEPTED":
                        accepted_events.append(key)
            if len(accepted_events) != len(accepted) or set(accepted_events) != set(accepted):
                raise InvariantError("accepted audit coverage mismatch")
            if quarantine_ids != {r[0] for r in db.execute("SELECT attempt_id FROM quarantine")}:
                raise InvariantError("quarantine queue coverage mismatch")
            db.commit()
            return {"ok": True, "accepted_snapshots": len(accepted),
                    "current_accounts": len(current), "attempts": len(attempts),
                    "quarantined": len(quarantine_ids)}
        except Exception as error:
            self._rollback()
            self._log_failure("verify", error)
            raise


_SCHEMA = [
    "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    """CREATE TABLE accepted(broker TEXT NOT NULL,account TEXT NOT NULL,snapshot_id TEXT NOT NULL,
        as_of TEXT NOT NULL,fingerprint TEXT NOT NULL,snapshot_json TEXT NOT NULL,
        PRIMARY KEY(broker,account,snapshot_id))""",
    """CREATE TABLE current(broker TEXT NOT NULL,account TEXT NOT NULL,snapshot_id TEXT NOT NULL,
        as_of TEXT NOT NULL,fingerprint TEXT NOT NULL,snapshot_json TEXT NOT NULL,
        PRIMARY KEY(broker,account), FOREIGN KEY(broker,account,snapshot_id)
        REFERENCES accepted(broker,account,snapshot_id))""",
    """CREATE TABLE attempts(attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
        status TEXT NOT NULL CHECK(status IN ('ACCEPTED','REPLAY','QUARANTINED')),event_json TEXT NOT NULL)""",
    """CREATE TABLE quarantine(attempt_id INTEGER PRIMARY KEY REFERENCES attempts(attempt_id),
        sha256 TEXT NOT NULL,source TEXT NOT NULL,code TEXT NOT NULL,path TEXT NOT NULL)""",
]
