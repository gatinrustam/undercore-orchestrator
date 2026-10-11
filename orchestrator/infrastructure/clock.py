"""One clock epoch per OS boot; conservative per-process fallback off Linux."""

from pathlib import Path
import uuid

try:
    BOOT_ID = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
except OSError:
    BOOT_ID = uuid.uuid4().hex
