#!/usr/bin/env python3
"""First Linux/systemd installation of a checksum-verified source release.

Does not add VPN nodes, contact agents, overwrite settings, or upgrade an existing
installation. Use update.py for upgrades. Partial failures are left for inspection.
"""

import argparse
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys

from update import inspect_archive, ROOT, SETTINGS, UNITS_DIR, UNITS

STATE = Path("/var/lib/vpn-orchestrator")


def run(args, cwd=None):
    subprocess.run(
        args,
        cwd=cwd,
        check=True,
        timeout=600,
        env={
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "PIP_CONFIG_FILE": "/dev/null",
            "ORCHESTRATOR_SETTINGS": str(SETTINGS),
        },
    )


def install(archive, digest):
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ValueError("Linux root installation required")
    info, files = inspect_archive(archive, digest)
    for path in (ROOT, SETTINGS.parent, STATE):
        if path.exists() or path.is_symlink():
            raise ValueError(
                "Existing installation path: use documented upgrade/recovery procedure"
            )
    for unit in UNITS:
        if (UNITS_DIR / unit).exists():
            raise ValueError("Existing service unit")
    for name in ("orchestratorctl", "undercore-orchestrator"):
        if (Path("/usr/local/bin") / name).exists():
            raise ValueError("Existing executable")
    try:
        owner = pwd.getpwnam("vpn-orchestrator")
    except KeyError:
        run(
            [
                "useradd",
                "--system",
                "--user-group",
                "--home-dir",
                str(STATE),
                "--shell",
                "/usr/sbin/nologin",
                "vpn-orchestrator",
            ]
        )
        owner = pwd.getpwnam("vpn-orchestrator")
    release = ROOT / "releases" / ("v" + info["version"] + "-" + info["commit"][:12])
    release.mkdir(parents=True, mode=0o755)
    for name, data in files.items():
        path = release / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o644)
    import json

    (release / "release.json").write_text(json.dumps(info, indent=2) + "\n")
    run(["/usr/bin/python3", "-m", "venv", str(release / ".venv")])
    python = str(release / ".venv/bin/python")
    run(
        [
            python,
            "-m",
            "pip",
            "install",
            "--only-binary=:all:",
            "--index-url",
            "https://pypi.org/simple",
            "-r",
            str(release / "requirements.lock"),
        ]
    )
    run(
        [
            python,
            "-m",
            "orchestrator",
            "init",
            "--directory",
            str(SETTINGS.parent),
            "--state-directory",
            str(STATE),
        ],
        cwd=release,
    )
    STATE.mkdir(mode=0o700)
    for path in (STATE, SETTINGS.parent, *SETTINGS.parent.iterdir()):
        os.chown(path, owner.pw_uid, owner.pw_gid)
    (ROOT / "current").symlink_to(release)
    for unit in UNITS:
        shutil.copyfile(release / "deploy" / unit, UNITS_DIR / unit)
    wrapper = Path("/usr/local/bin/orchestratorctl")
    shutil.copyfile(release / "deploy/orchestratorctl", wrapper)
    wrapper.chmod(0o755)
    Path("/usr/local/bin/undercore-orchestrator").symlink_to(wrapper)
    run(
        ["runuser", "-u", owner.pw_name, "--", python, "-m", "orchestrator", "config", "validate"],
        cwd=release,
    )
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", UNITS[0], UNITS[2], UNITS[4]])
    print("Installed. No nodes enrolled. Run undercore-orchestrator start when ready.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    install(args.archive, args.sha256)


if __name__ == "__main__":
    main()
