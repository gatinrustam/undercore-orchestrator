#!/usr/bin/env python3
"""Download one GitHub release on the operator's computer, then update over SSH.

No GitHub token/deploy key is copied to the server. Does not update on arbitrary push.
"""

import argparse
import re
import shlex
import subprocess
import tempfile
import uuid
from pathlib import Path
from update import inspect_archive


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--release", required=True)
    p.add_argument("--host", required=True)
    p.add_argument("--repo", default="UndercoreCo/orchestrator")
    p.add_argument("--adopt-existing", action="store_true")
    a = p.parse_args()
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", a.release):
        p.error("Use an exact release tag, e.g. v0.1.0")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", a.host):
        p.error("Invalid SSH host")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", a.repo):
        p.error("Invalid repository")
    with tempfile.TemporaryDirectory(prefix="orchestrator-release-") as folder:
        root = Path(folder)
        name = "undercore-orchestrator-" + a.release + ".tar.gz"
        subprocess.run(
            [
                "gh",
                "release",
                "download",
                a.release,
                "--repo",
                a.repo,
                "--pattern",
                name,
                "--pattern",
                "SHA256SUMS",
                "--dir",
                folder,
            ],
            check=True,
        )
        checks = (root / "SHA256SUMS").read_text().splitlines()
        match = [
            line.split() for line in checks if len(line.split()) == 2 and line.split()[1] == name
        ]
        if len(match) != 1 or not re.fullmatch(r"[a-f0-9]{64}", match[0][0]):
            raise ValueError("Invalid checksum file")
        digest = match[0][0]
        info, files = inspect_archive(root / name, digest)
        if "v" + info["version"] != a.release:
            raise ValueError("Release version mismatch")
        (root / "update.py").write_bytes(files["scripts/update.py"])
        remote = "/var/tmp/undercore-orchestrator-" + uuid.uuid4().hex
        ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", a.host]
        subprocess.run(ssh + ["install -d -m 700 " + remote], check=True)
        try:
            subprocess.run(
                [
                    "scp",
                    "-q",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    str(root / name),
                    str(root / "update.py"),
                    a.host + ":" + remote + "/",
                ],
                check=True,
            )
            command = ["python3", remote + "/update.py", remote + "/" + name, "--sha256", digest]
            if a.adopt_existing:
                command.append("--adopt-existing")
            subprocess.run(ssh + [shlex.join(command)], check=True)
        finally:
            subprocess.run(
                ssh
                + [
                    "rm -f "
                    + shlex.quote(remote + "/" + name)
                    + " "
                    + shlex.quote(remote + "/update.py")
                    + " && rmdir "
                    + shlex.quote(remote)
                ],
                check=False,
            )


if __name__ == "__main__":
    main()
