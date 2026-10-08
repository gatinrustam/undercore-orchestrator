"""Private online snapshot, including every credential needed by its inventory."""

import json
import os
from pathlib import Path
import sqlite3

from orchestrator.config.settings import load_settings
from orchestrator.infrastructure.secrets import read_secret
from orchestrator.infrastructure.sqlite.inventory import read_inventory


def backup(settings_path, destination):
    settings = load_settings(settings_path)
    source = Path(settings.state_directory) / "assignments.sqlite3"
    destination = Path(destination).absolute()
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    target = destination / "assignments.sqlite3"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    with (
        sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original,
        sqlite3.connect(target) as snapshot,
    ):
        original.backup(snapshot)
        if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Invalid snapshot")
    nodes = read_inventory(target)
    if nodes is None:
        nodes = settings.nodes
    paths = {settings.backend_token_file, *(node.api_key_file for node in nodes)}
    mapping = {}
    for index, path in enumerate(sorted(paths)):
        filename = f"secret-{index}.token"
        fd = os.open(destination / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(read_secret(path))
        mapping[filename] = path
    for filename, value in [
        ("settings.json", settings.model_dump(exclude={"admin_token_file", "nodes"})),
        ("restore-map.json", mapping),
    ]:
        fd = os.open(destination / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
    return {"status": "backed_up", "directory": str(destination)}
