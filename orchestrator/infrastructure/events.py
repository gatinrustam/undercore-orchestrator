"""Bounded, best-effort process-shared event tail. Not an audit ledger."""

import fcntl
import os
import stat
from pathlib import Path
from contextlib import contextmanager

from orchestrator.application.telemetry import Stage, Reason, Action
from orchestrator.domain.panel import PanelEvent

MAX_BYTES = 256 * 1024


def checked_event(value):
    event = PanelEvent.model_validate(value)
    Stage(event.stage), Reason(event.reason), Action(event.action)
    return event


class EventTail:
    def __init__(self, directory):
        self.directory = Path(directory)

    @contextmanager
    def locked(self, write=False):
        fd = os.open(
            self.directory / "events.lock",
            (os.O_RDWR | os.O_CREAT if write else os.O_RDONLY) | os.O_NOFOLLOW,
            0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_mode & 0o077:
                raise ValueError("private_event_file_required")
            fcntl.flock(fd, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def append(self, value):
        # Never wait behind another process or make a VPN operation fail for the panel.
        try:
            data = checked_event(value).model_dump_json(exclude_none=True).encode() + b"\n"
            with self.locked(write=True):
                current = self.directory / "events.jsonl"
                fd = os.open(current, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                        return
                    if info.st_size + len(data) <= MAX_BYTES:
                        os.write(fd, data)
                        return
                finally:
                    os.close(fd)
                current.replace(self.directory / "events.previous.jsonl")
                fd = os.open(current, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
        except (OSError, ValueError):
            pass

    def recent(self):
        try:
            values = []
            with self.locked():
                for name in ("events.previous.jsonl", "events.jsonl"):
                    path = self.directory / name
                    try:
                        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    except FileNotFoundError:
                        continue
                    with os.fdopen(fd, "rb") as stream:
                        info = os.fstat(stream.fileno())
                        if (
                            not stat.S_ISREG(info.st_mode)
                            or info.st_mode & 0o077
                            or info.st_size > MAX_BYTES
                        ):
                            return [], False
                        for line in stream.read(MAX_BYTES + 1).splitlines():
                            try:
                                import json

                                values.append(checked_event(json.loads(line)))
                            except (ValueError, TypeError):
                                continue
            return sorted(values, key=lambda e: e.timestamp, reverse=True)[:100], True
        except (OSError, ValueError):
            return [], False
