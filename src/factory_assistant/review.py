"""Human review of schema changes, kept as a queue so one pending file never blocks a build.

When a stock file has columns the approved stock schema does not define, the build
files a proposal here and holds that file's records. A reviewer approves, edits,
rejects or defers it; the next build (a replay, so no model calls) publishes the
records under the approved definitions. Approval changes the schema, not the truth of
any count, and every decision is kept.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from factory_assistant.models import FieldDefinition, ReviewDecision, StockSchema, initial_schema


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(proposal: dict) -> str:
    """Changes whenever the proposal shown to a reviewer changes, so stale forms fail."""
    body = {k: proposal[k] for k in ("id", "fields", "revision", "status")}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class SchemaReview:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS approved_fields (
                    name TEXT PRIMARY KEY, body TEXT NOT NULL, version INTEGER NOT NULL,
                    proposal_id TEXT NOT NULL, approved_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_path TEXT NOT NULL,
                    source_sha TEXT NOT NULL, evidence TEXT NOT NULL, fields TEXT NOT NULL,
                    status TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, action TEXT NOT NULL,
                    reviewer TEXT NOT NULL, note TEXT NOT NULL, fields TEXT,
                    created_at TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def schema(self) -> StockSchema:
        base = initial_schema()
        with self.connect() as db:
            rows = db.execute("SELECT body, version FROM approved_fields ORDER BY version, name")
            rows = rows.fetchall()
        fields = base.fields + [FieldDefinition.model_validate_json(r["body"]) for r in rows]
        return StockSchema(
            version=max([base.version, *(r["version"] for r in rows)]), fields=fields
        )

    def submit(self, source, fields: list[FieldDefinition], evidence: str) -> dict:
        """File (or find) the proposal for these new columns of this exact file version."""
        names = sorted(f.name for f in fields)
        identity = hashlib.sha256(json.dumps([source.sha256, names]).encode()).hexdigest()[:16]
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO proposals VALUES (?, ?, ?, ?, ?, ?, 'pending', 1, ?)",
                (
                    identity,
                    source.source_id,
                    source.path,
                    source.sha256,
                    evidence,
                    json.dumps([f.model_dump() for f in fields]),
                    now(),
                ),
            )
        return self.proposal(identity)

    def proposal(self, identity: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM proposals WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("Unknown proposal.")
        proposal = {**dict(row), "fields": json.loads(row["fields"])}
        if proposal["status"] in {"pending", "deferred"}:
            approved = set(self.schema().by_name())
            if {f["name"] for f in proposal["fields"]} <= approved:
                # Another file's approval already defines these columns.
                proposal["status"] = "covered"
        return {**proposal, "fingerprint": fingerprint(proposal)}

    def proposals(self) -> list[dict]:
        with self.connect() as db:
            ids = [r["id"] for r in db.execute("SELECT id FROM proposals ORDER BY updated_at DESC")]
        return [self.proposal(i) for i in ids]

    def history(self, identity: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM decisions WHERE proposal_id=? ORDER BY created_at, rowid",
                (identity,),
            ).fetchall()
        return [dict(r) for r in rows]

    def decide(self, identity: str, decision: ReviewDecision) -> dict:
        proposal = self.proposal(identity)
        if decision.proposal_fingerprint != proposal["fingerprint"]:
            raise ValueError("This review form is stale. Reload the latest proposal.")
        if proposal["status"] in {"approved", "rejected", "covered"}:
            raise ValueError("This proposal has already been decided.")
        fields = [FieldDefinition.model_validate(f) for f in proposal["fields"]]
        status, revision = proposal["status"], proposal["revision"]
        if decision.action == "edit":
            edited = decision.edited_fields or []
            if sorted(f.name for f in edited) != sorted(f.name for f in fields):
                raise ValueError("An edit must define exactly the proposed columns.")
            # An edit is a new proposal for review, never an implicit approval.
            fields, status, revision = edited, "pending", revision + 1
        elif decision.action == "defer":
            status = "deferred"
        elif decision.action == "reject":
            status = "rejected"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if decision.action == "approve":
                version = db.execute("SELECT MAX(version) FROM approved_fields").fetchone()[0]
                version = (version or initial_schema().version) + 1
                for field in fields:
                    existing = db.execute(
                        "SELECT body FROM approved_fields WHERE name=?", (field.name,)
                    ).fetchone()
                    if existing and FieldDefinition.model_validate_json(existing["body"]) != field:
                        raise ValueError(f"{field.name} is already approved with another meaning.")
                    db.execute(
                        "INSERT OR IGNORE INTO approved_fields VALUES (?, ?, ?, ?, ?)",
                        (field.name, field.model_dump_json(), version, identity, now()),
                    )
                status = "approved"
            db.execute(
                "UPDATE proposals SET fields=?, status=?, revision=?, updated_at=? WHERE id=?",
                (json.dumps([f.model_dump() for f in fields]), status, revision, now(), identity),
            )
            db.execute(
                "INSERT OR IGNORE INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    decision.event_id,
                    identity,
                    decision.action,
                    decision.reviewer,
                    decision.note,
                    json.dumps([f.model_dump() for f in decision.edited_fields or []]),
                    now(),
                ),
            )
        return self.proposal(identity)
