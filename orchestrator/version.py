"""Version independent of installation layout."""

from pathlib import Path
from importlib.metadata import PackageNotFoundError, version


def current_version():
    source = Path(__file__).resolve().parent.parent / "VERSION"
    if source.is_file():
        return source.read_text().strip()
    try:
        return version("undercore-orchestrator")
    except PackageNotFoundError:
        return "unknown"
