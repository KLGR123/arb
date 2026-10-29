"""SQLite durable queue. Transactions atomically publish stages and replay events."""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .contracts import CreateBacktest, now

LEASE_SECONDS = 180  # Engine subprocess has a shorter hard timeout.


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, request TEXT NOT NULL, dataset TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    run_id TEXT NOT NULL REFERENCES runs(id), engine TEXT NOT NULL,
                    status TEXT NOT NULL, result TEXT, error TEXT,
                    lease_token TEXT, lease_until REAL, PRIMARY KEY(run_id, engine)
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    body TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id, seq);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _event(self, db, run_id, kind, engine=None, **payload):
        db.execute("INSERT INTO events(run_id, body) VALUES (?,?)", (run_id, json.dumps({
            "run_id": run_id, "type": kind, "engine": engine, "recorded_at": now(),
            **payload,
        }, allow_nan=False)))

    def create(self, request: CreateBacktest, dataset: dict) -> str:
        run_id = str(uuid4())
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES (?,?,?,?)", (
                run_id, request.model_dump_json(), json.dumps(dataset), now(),
            ))
            engines = ["fast"] if request.engine == "fast" else ["fast", "realistic"]
            for engine in engines:
                db.execute("INSERT INTO jobs(run_id,engine,status) VALUES (?,?,'queued')", (run_id, engine))
                self._event(db, run_id, "stage_queued", engine)
        return run_id

    def promote(self, run_id: str):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                raise KeyError(run_id)
            inserted = db.execute(
                "INSERT OR IGNORE INTO jobs(run_id,engine,status) VALUES (?,'realistic','queued')",
                (run_id,),
            ).rowcount
            if inserted:
                self._event(db, run_id, "stage_queued", "realistic")

    def claim(self) -> dict | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            expired = db.execute(
                "SELECT run_id,engine FROM jobs WHERE status='running' AND lease_until<?",
                (time.time(),),
            ).fetchall()
            for job in expired:
                db.execute("UPDATE jobs SET status='failed',error='worker_lease_expired' "
                           "WHERE run_id=? AND engine=?", tuple(job))
                self._event(db, job["run_id"], "stage_failed", job["engine"], error="worker_lease_expired")
            job = db.execute("""
                SELECT j.run_id,j.engine,r.request,r.dataset FROM jobs j JOIN runs r ON r.id=j.run_id
                WHERE j.status='queued' AND (j.engine='fast' OR NOT EXISTS (
                    SELECT 1 FROM jobs f WHERE f.run_id=j.run_id AND f.engine='fast'
                    AND f.status IN ('queued','running')))
                ORDER BY r.created_at, j.engine LIMIT 1
            """).fetchone()
            if not job:
                return None
            token = str(uuid4())
            db.execute("UPDATE jobs SET status='running',lease_token=?,lease_until=? "
                       "WHERE run_id=? AND engine=?",
                       (token, time.time() + LEASE_SECONDS, job["run_id"], job["engine"]))
            self._event(db, job["run_id"], "stage_running", job["engine"])
            return {**dict(job), "lease_token": token,
                    "request": json.loads(job["request"]), "dataset": json.loads(job["dataset"])}

    def finish(self, job: dict, result: dict | None, error: str | None = None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE jobs SET status=?,result=?,error=?,lease_until=NULL "
                "WHERE run_id=? AND engine=? AND status='running' AND lease_token=?",
                ("failed" if error else "succeeded",
                 json.dumps(result, allow_nan=False) if result else None, error,
                 job["run_id"], job["engine"], job["lease_token"]),
            ).rowcount
            if not changed:
                return
            if error:
                self._event(db, job["run_id"], "stage_failed", job["engine"], error=error)
            else:
                provenance = {k: result[k] for k in ("source", "as_of", "computed_at")}
                for kind in ("fills", "equity"):
                    for value in result[kind]:
                        self._event(db, job["run_id"], "fill" if kind == "fills" else "equity",
                                    job["engine"], **provenance, data=value)
                self._event(db, job["run_id"], "result_available", job["engine"],
                            **provenance, phase=result["phase"])

    def get(self, run_id: str) -> dict:
        with self.connect() as db:
            # One read transaction: never mix jobs from two different revisions.
            db.execute("BEGIN")
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise KeyError(run_id)
            jobs = db.execute("SELECT * FROM jobs WHERE run_id=? ORDER BY engine", (run_id,)).fetchall()
        dataset = json.loads(run["dataset"])
        stages = {j["engine"]: {"status": j["status"], "error": j["error"],
                               "result": json.loads(j["result"]) if j["result"] else None} for j in jobs}
        selected = next((stages[e]["result"] for e in ("realistic", "fast")
                         if e in stages and stages[e]["result"]), None)
        states = [j["status"] for j in jobs]
        if "running" in states:
            status = "running"
        elif "queued" in states:
            status = "draft" if selected else "queued"
        elif all(s == "succeeded" for s in states):
            status = "succeeded"
        else:
            status = "partial" if selected else "failed"
        return {
            "run_id": run_id, "status": status, "created_at": run["created_at"],
            "request": json.loads(run["request"]),
            "requested_engine": "realistic" if "realistic" in stages else "fast",
            "dataset": {k: v for k, v in dataset.items() if k not in ("bars", "file")},
            "source": selected["source"] if selected else "arb.backtests",
            "as_of": dataset["as_of"], "phase": selected["phase"] if selected else None,
            "metrics": selected["metrics"] if selected else {},
            "equity": selected["equity"] if selected else [],
            "fills": selected["fills"] if selected else [], "stages": stages,
        }

    def bars(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT dataset FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        dataset = json.loads(row["dataset"])
        return {"run_id": run_id, "source": dataset["source"], "as_of": dataset["as_of"],
                "sha256": dataset["sha256"], "bars": dataset["bars"]}

    def events(self, run_id, after, limit):
        run = self.get(run_id)  # consistent 404 contract
        with self.connect() as db:
            rows = db.execute("SELECT seq,body FROM events WHERE run_id=? AND seq>? "
                              "ORDER BY seq LIMIT ?", (run_id, after, limit)).fetchall()
        events = [{"seq": r["seq"], **json.loads(r["body"])} for r in rows]
        return {"run_id": run_id, "source": "arb.backtests", "as_of": run["as_of"], "events": events,
                "next_cursor": events[-1]["seq"] if events else after}
