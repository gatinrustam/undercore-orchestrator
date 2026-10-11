"""Private credential files injected into inventory management."""

import os
import uuid
from pathlib import Path
from orchestrator.infrastructure.secrets import read_secret
from orchestrator.domain.inventory import NodeSettings
from orchestrator.domain.models import Node


class CredentialFiles:
    def __init__(self, directory):
        self.directory = Path(directory)

    def resolve(self, config: NodeSettings) -> Node:
        return config.node(self.read(config.api_key_file))

    def read(self, path: str) -> str:
        return read_secret(path).decode()

    def allocate(self) -> str:
        return str(self.directory / "node-secrets" / (uuid.uuid4().hex + ".token"))

    def write(self, path: str, key: str) -> None:
        file = Path(path)
        file.parent.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())

    def discard(self, path: str) -> None:
        Path(path).unlink(missing_ok=True)
