#!/usr/bin/env python3
"""Root-only immutable release update. Credentials, nodes and live journal stay put.

No git pull in runtime, no database restore on rollback, no node API mutations.
Schema compatibility is checked before cutover; migrations are rehearsed on snapshots.
"""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shutil
import signal
import sqlite3
import subprocess
import tarfile
import time
import urllib.request

ROOT = Path("/opt/vpn-orchestrator")
SETTINGS = Path("/etc/vpn-orchestrator/settings.json")
UNITS_DIR = Path("/etc/systemd/system")
BACKUPS = Path("/var/backups")
CLI = Path("/usr/local/bin/orchestratorctl")
UNITS = (
    "vpn-orchestrator.service",
    "vpn-orchestrator-recovery.service",
    "vpn-orchestrator-recovery.timer",
    "vpn-orchestrator-leases.service",
    "vpn-orchestrator-leases.timer",
)
MAX_BYTES = 8 * 1024 * 1024


def storage_contract(info):
    # The old journal_schema field described lease support, not PRAGMA user_version.
    # Schema 1 retains those tables/columns; old source releases ignore user_version.
    value = info.get("storage", {"min_read": 0, "max_read": 1, "write": 0})
    if not isinstance(value, dict) or set(value) != {"min_read", "max_read", "write"}:
        raise ValueError("release_storage_invalid")
    if (
        any(type(v) is not int for v in value.values())
        or not 0 <= value["min_read"] <= value["write"] <= value["max_read"]
    ):
        raise ValueError("release_storage_invalid")
    return value


def check_storage(journal, incoming, previous):
    target, rollback = storage_contract(incoming), storage_contract(previous)
    with sqlite3.connect(journal.as_uri() + "?mode=ro", uri=True) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
    if not target["min_read"] <= version <= target["max_read"]:
        raise ValueError("journal_schema_unsupported")
    # A failed health check must be able to run old code against the new journal.
    resulting = max(version, target["write"])
    if not rollback["min_read"] <= resulting <= rollback["max_read"]:
        raise ValueError("rollback_schema_incompatible")


def inspect_archive(path, digest):
    if path.stat().st_size > MAX_BYTES or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError("archive_checksum_invalid")
    with tarfile.open(path) as tar:
        result = {}
        size = 0
        for item in tar:
            name = PurePosixPath(item.name)
            if (
                not item.isfile()
                or name.is_absolute()
                or ".." in name.parts
                or str(name) != item.name
                or item.name in result
                or item.size < 0
            ):
                raise ValueError("archive_entry_invalid")
            size += item.size
            if size > MAX_BYTES:
                raise ValueError("archive_too_large")
            result[item.name] = tar.extractfile(item).read()
    info = json.loads(result.pop("release.json"))
    if (
        not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", info["version"])
        or not re.fullmatch(r"[a-f0-9]{40}", info["commit"])
        or info["journal_schema"] not in (1, 2)
    ):
        raise ValueError("release_metadata_invalid")
    storage_contract(info)
    if (
        "storage" in info
        and json.loads(result.get("contracts/storage.json", b"null")) != info["storage"]
    ):
        raise ValueError("release_storage_invalid")
    if info["files"] != {k: hashlib.sha256(v).hexdigest() for k, v in result.items()}:
        raise ValueError("release_manifest_invalid")
    if result["VERSION"].decode().strip() != info["version"]:
        raise ValueError("release_version_invalid")
    required = {
        "requirements.lock",
        "orchestrator/runtime.py",
        "orchestrator/settings.py",
        "orchestrator/cli.py",
        "deploy/orchestratorctl",
        *(f"deploy/{u}" for u in UNITS),
    }
    if not required <= result.keys():
        raise ValueError("release_incomplete")
    for name in result:
        if not (
            name
            in ("VERSION", "requirements.lock", "pyproject.toml", "README.md", "LICENSE", "NOTICE")
            or name.startswith(
                ("orchestrator/", "deploy/", "scripts/", "contracts/", "config/", "docs/public/")
            )
        ):
            raise ValueError("release_path_invalid")
        if name == "config/settings.json" or name.endswith((".token", ".key", ".db", ".sqlite3")):
            raise ValueError("runtime_file_in_release")
    return info, result


def run(args, *, cwd=None, timeout=120):
    # No inherited proxy/pip credentials or arbitrary Python paths in a root update.
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "PIP_CONFIG_FILE": "/dev/null",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "ORCHESTRATOR_SETTINGS": str(SETTINGS),
    }
    value = subprocess.run(args, cwd=cwd, env=env, capture_output=True, timeout=timeout)
    if value.returncode:
        raise RuntimeError("command_failed:" + Path(args[0]).name)
    return value.stdout


def snapshot(source, target):
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as a, sqlite3.connect(target) as b:
        a.backup(b)
        if b.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("journal_invalid")
    target.chmod(0o600)


def activate(target):
    link = ROOT / "current.next"
    if link.exists() or link.is_symlink():
        raise ValueError("activation_already_pending")
    link.symlink_to(target)
    link.replace(ROOT / "current")


def update(archive, digest, adopt_existing=False):
    if os.geteuid() != 0:
        raise ValueError("root_required")
    info, files = inspect_archive(archive, digest)
    for directory in (ROOT, ROOT / "releases"):
        if (
            directory.is_symlink()
            or directory.stat().st_uid != 0
            or directory.stat().st_mode & 0o022
        ):
            raise ValueError("unsafe_installation_directory")
    previous = (ROOT / "current").resolve(strict=True)
    if previous.parent != ROOT / "releases":
        raise ValueError("unexpected_current_path")
    prior = {}
    prior_manifest = previous / "release.json"
    if prior_manifest.exists():
        prior = json.loads(prior_manifest.read_text())
        if prior["commit"] == info["commit"]:
            return {"status": "already_installed", "version": info["version"]}
        if tuple(map(int, info["version"].split("."))) <= tuple(
            map(int, prior["version"].split("."))
        ):
            raise ValueError("version_must_increase")
    elif not adopt_existing:
        raise ValueError("first_cutover_requires_adopt_existing")
    owner = pwd.getpwnam("vpn-orchestrator")
    settings_before = SETTINGS.read_bytes()
    settings = json.loads(settings_before)
    if settings["state_directory"] != "/var/lib/vpn-orchestrator":
        raise ValueError("unexpected_state_directory")
    state = Path(settings["state_directory"])
    journal = state / "assignments.sqlite3"
    check_storage(journal, info, prior)
    # Once permissions have been granted, rollback must retain their heartbeat/fencing implementation.
    with sqlite3.connect(journal.as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='control_leases'").fetchone():
            enrolled = db.execute("SELECT 1 FROM control_leases LIMIT 1").fetchone()
            if enrolled and (not prior_manifest.exists() or prior.get("journal_schema", 1) < 2):
                raise ValueError("lease_aware_rollback_required")
    release = ROOT / "releases" / ("v" + info["version"] + "-" + info["commit"][:12])
    if release.exists():
        raise ValueError("release_directory_exists_inspect_previous_attempt")
    release.mkdir(mode=0o755)
    release.chmod(0o755)
    for name, data in files.items():
        file = release / name
        file.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        file.write_bytes(data)
        file.chmod(0o644)
    (release / "release.json").write_text(json.dumps(info, indent=2) + "\n")
    (release / "release.json").chmod(0o644)
    # A version-specific venv makes dependency updates independent of all other apps.
    old_mask = os.umask(0o022)
    try:
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
            ],
            timeout=600,
        )
    finally:
        os.umask(old_mask)
    run(
        ["runuser", "-u", owner.pw_name, "--", python, "-m", "orchestrator", "check-config"],
        cwd=release,
    )
    backup = BACKUPS / ("undercore-orchestrator-" + info["commit"][:12])
    backup.mkdir(mode=0o700)
    snapshot(journal, backup / "preflight.sqlite3")
    preflight = backup / "preflight"
    preflight.mkdir(mode=0o700)
    shutil.copyfile(backup / "preflight.sqlite3", preflight / "assignments.sqlite3")
    (preflight / "assignments.sqlite3").chmod(0o600)
    # Reject any implicit schema/data migration before stopping the current writer.
    code = """import sqlite3,sys
from pathlib import Path
from orchestrator.infrastructure.sqlite.upgrade import rehearse
from orchestrator.assignments import Assignments
from orchestrator.registry import NodeRegistry
from orchestrator.settings import Settings
p=Path(sys.argv[1])
rehearse(p)
NodeRegistry(Assignments(p),Settings.model_validate_json(Path(sys.argv[2]).read_bytes()).nodes)
"""
    run([python, "-c", code, str(preflight), str(SETTINGS)], cwd=release)
    (backup / "settings.json").write_bytes(settings_before)
    (backup / "settings.json").chmod(0o600)
    (backup / "previous-release.txt").write_text(str(previous))
    existing = [unit for unit in UNITS if (UNITS_DIR / unit).exists()]
    for unit in existing:
        shutil.copyfile(UNITS_DIR / unit, backup / unit)
    cli = CLI
    if cli.exists():
        shutil.copyfile(cli, backup / "orchestratorctl")
    timers_enabled = [
        u
        for u in (UNITS[2], UNITS[4])
        if u in existing
        and subprocess.run(["systemctl", "is-enabled", "--quiet", u]).returncode == 0
    ]
    changed = False
    try:
        run(["systemctl", "stop", *[u for u in existing if u in UNITS[1:3]]], timeout=720)
        if UNITS[3] in existing:
            run(["systemctl", "stop", UNITS[4], UNITS[3]], timeout=120)
        run(["systemctl", "stop", UNITS[0]])
        snapshot(journal, backup / "assignments.sqlite3")
        # Rehearse again against the final stopped snapshot: writers may have
        # changed the journal while dependencies/preflight were being prepared.
        check_storage(journal, info, prior)
        shutil.copyfile(backup / "assignments.sqlite3", preflight / "assignments.sqlite3")
        run([python, "-c", code, str(preflight), str(SETTINGS)], cwd=release)
        identity = (journal.stat().st_dev, journal.stat().st_ino)
        changed = True
        for unit in UNITS:
            path = UNITS_DIR / unit
            shutil.copyfile(release / "deploy" / unit, path)
            path.chmod(0o644)
        activate(release)
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "start", UNITS[0]])
        token = Path(settings["backend_token_file"]).read_text().strip()
        for _ in range(30):
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:8792/v1/health", headers={"Authorization": "Bearer " + token}
                )
                with urllib.request.urlopen(req, timeout=2) as response:
                    value = json.load(response)
                if value.get("status") == "ok" and value.get("version") == info["version"]:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            raise ValueError("new_service_unhealthy")
        if (
            SETTINGS.read_bytes() != settings_before
            or (journal.stat().st_dev, journal.stat().st_ino) != identity
        ):
            raise ValueError("runtime_state_replaced")
        shutil.copyfile(release / "deploy/orchestratorctl", cli)
        cli.chmod(0o755)
        run(["systemctl", "start", UNITS[3]], timeout=120)
        run(["systemctl", "enable", "--now", UNITS[4]])
        run(["systemctl", "start", UNITS[1]], timeout=720)
        run(["systemctl", "enable", "--now", UNITS[2]])
        public_cli = cli.parent / "undercore-orchestrator"
        if not public_cli.exists() and not public_cli.is_symlink():
            public_cli.symlink_to(cli)
        result = {
            "status": "updated",
            "version": info["version"],
            "commit": info["commit"],
            "backup": str(backup),
            "settings_preserved": True,
            "journal_preserved": True,
        }
        (backup / "result.json").write_text(json.dumps(result) + "\n")
        return result
    except BaseException:
        if changed:
            run(["systemctl", "stop", *[u for u in UNITS if (UNITS_DIR / u).exists()]], timeout=720)
            for unit in UNITS:
                path = UNITS_DIR / unit
                if (backup / unit).exists():
                    shutil.copyfile(backup / unit, path)
                    path.chmod(0o644)
                else:
                    path.unlink(missing_ok=True)
            with sqlite3.connect(journal.as_uri() + "?mode=ro", uri=True) as db:
                live_version = db.execute("PRAGMA user_version").fetchone()[0]
            supported = storage_contract(prior)
            if not supported["min_read"] <= live_version <= supported["max_read"]:
                raise RuntimeError("rollback_schema_incompatible_services_stopped") from None
            activate(previous)
            if (backup / "orchestratorctl").exists():
                shutil.copyfile(backup / "orchestratorctl", cli)
                cli.chmod(0o755)
            elif cli.exists():
                cli.unlink()
            run(["systemctl", "daemon-reload"])
        run(["systemctl", "start", UNITS[0]])
        for timer in timers_enabled:
            run(["systemctl", "start", timer])
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--adopt-existing", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{64}", args.sha256):
        parser.error("Invalid SHA256")

    def interrupted(signum, frame):
        # Let the cutover's exception handler restore the previous release.
        for sig in (signal.SIGHUP, signal.SIGTERM):
            signal.signal(sig, signal.SIG_IGN)
        raise RuntimeError("update_interrupted")

    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, interrupted)
    os.umask(0o022)
    lock = os.open(
        "/run/lock/vpn-orchestrator-update.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Private backup output regardless of the subprocess/package umask.
        result = update(args.archive, args.sha256, args.adopt_existing)
        print(json.dumps(result))
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(error)
                    if type(error) in (ValueError, RuntimeError)
                    else type(error).__name__,
                }
            )
        )
        return 1
    finally:
        os.close(lock)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
