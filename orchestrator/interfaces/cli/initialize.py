"""Generate an empty private configuration; never overwrite an installation."""

import os
import secrets
from pathlib import Path
from orchestrator.config.settings import Settings


def initialize(directory, state_directory=None):
    directory = Path(directory).absolute()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    state = Path(state_directory).absolute() if state_directory else directory / "state"
    token = directory / "backend.token"
    fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(secrets.token_urlsafe(48))
    config = Settings(state_directory=str(state), backend_token_file=str(token))
    path = directory / "settings.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(config.model_dump_json(indent=2, exclude={"admin_token_file", "nodes"}) + "\n")
    return {"settings": str(path), "status": "initialized", "nodes": 0}
