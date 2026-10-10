#!/usr/bin/env python3
"""Generate or check the offline API artifact without loading production settings."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator.interfaces.http.specification import document  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    path = ROOT / "contracts/openapi.json"
    content = json.dumps(document(), indent=2, ensure_ascii=False) + "\n"
    if args.check:
        if not path.exists() or path.read_text() != content:
            raise SystemExit("OpenAPI is stale: run python scripts/export_openapi.py")
        print("Offline OpenAPI verified")
    else:
        path.write_text(content)
        print("Generated contracts/openapi.json")


if __name__ == "__main__":
    main()
