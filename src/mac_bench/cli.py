"""Browse tasks, prepare environments and evaluate agent systems."""
import argparse
import importlib

COMMANDS = {"tasks": "catalog_cli", "data": "data_cli", "environment": "environment_cli",
            "doctor": "doctor", "evaluate": "evaluation.__main__"}

def main(argv=None):
    parser = argparse.ArgumentParser(prog="mac-bench", description=__doc__)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    return importlib.import_module("mac_bench." + COMMANDS[args.command]).main(args.arguments)

if __name__ == "__main__":
    raise SystemExit(main())
