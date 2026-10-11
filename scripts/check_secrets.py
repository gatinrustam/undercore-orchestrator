#!/usr/bin/env python3
"""Offline scan of tracked source; print locations only, never candidate secrets."""

import argparse
import json
import subprocess
from pathlib import Path
from detect_secrets import SecretsCollection
from detect_secrets.settings import default_settings

ROOT = Path(__file__).resolve().parents[1]


def findings(paths):
    secrets = SecretsCollection()
    with default_settings():
        for path in paths:
            if Path(path).is_file() and not Path(path).is_symlink():
                secrets.scan_file(path)
    return [
        {"file": filename, "type": secret.type, "hash": secret.secret_hash}
        for filename, secret in secrets
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--paths-file", type=Path, help="Local staged review only; CI scans Git tracked files"
    )
    args = parser.parse_args()
    paths = (
        args.paths_file.read_text().splitlines()
        if args.paths_file
        else subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    )
    found = findings(sorted(set(paths) - {"contracts/secret-baseline.json"}))
    baseline = json.loads((ROOT / "contracts/secret-baseline.json").read_text())
    approved = {
        (entry["file"], entry["type"], entry["hash"]) for entry in baseline if entry.get("reason")
    }
    new = [
        entry for entry in found if (entry["file"], entry["type"], entry["hash"]) not in approved
    ]
    if new:
        for entry in new:
            print(json.dumps(entry))
        raise SystemExit("Unreviewed secret candidates; inspect locally without printing values")
    print(f"Offline secret scan passed: {len(paths)} paths; {len(found)} reviewed candidates")


if __name__ == "__main__":
    main()
