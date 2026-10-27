"""Browse exported public task descriptions without runtime assets or providers."""
import argparse
import json
from mac_bench.runtime.paths import repository_root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List public task metadata")
    listing.add_argument("--split", choices=("dev", "test"))
    listing.add_argument("--category")
    listing.add_argument("--json", action="store_true")
    show = commands.add_parser("show", help="Read one task description")
    show.add_argument("task_id")
    args = parser.parse_args(argv)
    rows = json.loads((repository_root() / "data/public_tasks.json").read_text())["tasks"]
    if args.command == "show":
        found = [r for r in rows if r["task_id"] == args.task_id]
        if len(found) != 1:
            parser.error("Unknown task ID. Use `mac-bench tasks list` to list available tasks.")
        print(found[0]["instruction"])
        return 0
    rows = [r for r in rows if all(getattr(args, k) in (None, r[k])
                                  for k in ("split", "category"))]
    fields = ("task_id", "split", "category", "world_revision")
    if args.json:
        print(json.dumps([{k: r[k] for k in fields} for r in rows], indent=2))
    else:
        print(f'{"Task":42} {"Split":5} Category')
        for r in rows:
            print(f'{r["task_id"]:42} {r["split"]:5} {r["category"]}')
        print(f"\n{len(rows)} tasks. ")
    return 0
