#!/usr/bin/env python3
"""Acquire pinned upstream repositories without modifying existing checkouts."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", choices=("AOrchestra", "Meta-Team", "Magentic-One"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    rows = json.loads((ROOT / "configs/baselines/upstream.json").read_text())["dependencies"]
    unavailable = 0
    for row in (r for r in rows if args.name is None or r["name"] == args.name):
        target = (ROOT / row["path"]).resolve()
        if not target.is_relative_to(ROOT / ".cache/baselines"):
            raise ValueError("Dependency path escapes its declared group")
        revision, origin = row.get("revision"), row.get("origin")
        if target.exists():
            if revision:
                actual = subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True).strip()
                if actual != revision:
                    raise ValueError(f"Revision mismatch: {row['path']}; existing checkout was not modified")
                print("PRESENT", row["path"], revision[:12])
            else:
                print("LOCAL  ", row["path"], "(unversioned acquired data; verify separately)")
            continue
        if not origin or not origin.startswith("https://") or not re.fullmatch(r"[a-f0-9]{40}", revision or ""):
            print("MANUAL ", row["path"], "requires an acquired asset copy")
            unavailable += 1
            continue
        if args.dry_run:
            print("FETCH  ", row["path"], origin, revision)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".fetch-", dir=target.parent) as temporary:
            work = Path(temporary) / "repo"
            subprocess.run(["git", "init", "-q", str(work)], check=True)
            subprocess.run(["git", "-C", str(work), "remote", "add", "origin", origin], check=True)
            subprocess.run(["git", "-C", str(work), "fetch", "--depth", "1", "origin", revision], check=True)
            subprocess.run(["git", "-C", str(work), "checkout", "--detach", "FETCH_HEAD"], check=True)
            actual = subprocess.check_output(["git", "-C", str(work), "rev-parse", "HEAD"], text=True).strip()
            if actual != revision:
                raise ValueError("Fetched revision mismatch")
            work.rename(target)
        print("FETCHED", row["path"], revision[:12])
    return 1 if unavailable else 0


if __name__ == "__main__":
    raise SystemExit(main())
