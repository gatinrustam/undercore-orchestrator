"""Durable operator intents, separate from the VPN schema; no request bodies stored."""

import hashlib
import json
import os
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path
from orchestrator.domain.administration import AuditEntry
from orchestrator.domain.models import OrchestratorError


class OperatorAudit:
    def __init__(self, directory):
        self.path = Path(directory) / "operator.sqlite3"
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_mode & 0o077:
                raise ValueError("private_audit_required")
        finally:
            os.close(fd)
        with closing(self.connect()) as db, db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("audit_version_unsupported")
            db.execute(
                "CREATE TABLE IF NOT EXISTS commands (operation_id TEXT PRIMARY KEY, digest TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL, outcome TEXT NOT NULL, result TEXT NOT NULL)"
            )
            db.execute("PRAGMA user_version=1")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=1)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    def begin(self, command: dict, actor: str) -> dict:
        if actor not in ("cli", "panel"):
            raise ValueError("invalid_actor")
        digest = hashlib.sha256(
            json.dumps(command, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM commands WHERE operation_id=?", (command["operation_id"],)
            ).fetchone()
            if row:
                if row["digest"] != digest:
                    raise OrchestratorError("operator_operation_conflict", 409)
                return self.entry(row)
            now = time.time()
            db.execute(
                "INSERT INTO commands VALUES (?,?,?,?,?,?,NULL,'started','{}')",
                (command["operation_id"], digest, actor, command["action"], command["target"], now),
            )
            return {"new": True, "outcome": "started"}

    def finish(self, operation_id: str, result: dict, success: bool) -> None:
        # Only fixed result projections from application commands reach this ledger.
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE commands SET finished_at=?,outcome=?,result=? WHERE operation_id=? AND outcome='started'",
                (
                    time.time(),
                    "succeeded" if success else "failed",
                    json.dumps(result),
                    operation_id,
                ),
            )

    @staticmethod
    def entry(row):
        value = dict(row)
        value.pop("digest")
        value["result"] = json.loads(value["result"])
        return AuditEntry.model_validate(value).model_dump()

    def recent(self) -> list[dict]:
        with closing(self.connect()) as db:
            return [
                self.entry(row)
                for row in db.execute("SELECT * FROM commands ORDER BY started_at DESC LIMIT 100")
            ]
