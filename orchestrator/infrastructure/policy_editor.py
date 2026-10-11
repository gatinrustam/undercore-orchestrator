"""Atomic settings edit with optimistic revision; active processes retain their policy."""

import hashlib
import json
import os
import tempfile
import stat
from pathlib import Path
from orchestrator.domain.models import OrchestratorError


class PolicyFile:
    def __init__(self, path, validate):
        self.path, self.validate = Path(path), validate

    def contents(self):
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_mode & 0o077:
                raise ValueError("private_settings_required")
            raw = os.read(fd, 1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("settings_too_large")
        finally:
            os.close(fd)
        return raw, self.validate(raw)

    def read(self) -> dict:
        raw, settings = self.contents()
        return {
            "revision": hashlib.sha256(raw).hexdigest(),
            "policy": settings.policy.model_dump(),
            "activation": "restart_required",
        }

    def save(self, expected_revision: str, policy: dict) -> dict:
        raw, _ = self.contents()
        if expected_revision != hashlib.sha256(raw).hexdigest():
            raise OrchestratorError("policy_revision_conflict", 409)
        value = json.loads(raw)
        value["policy"] = policy
        updated = (json.dumps(value, indent=2) + "\n").encode()
        self.validate(updated)
        fd, name = tempfile.mkstemp(prefix=".policy-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(updated)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            parent = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            Path(name).unlink(missing_ok=True)
        return {"revision": hashlib.sha256(updated).hexdigest(), "status": "restart_required"}
