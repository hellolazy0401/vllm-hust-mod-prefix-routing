"""Read-only inspection commands. Never apply a host patch implicitly."""
import argparse
import json

from .adapters.core.contract import check_host
from .plugin import descriptor
from .runtime import runtime_status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("describe")
    sub.add_parser("status", help="This process only, not a running server's counters")
    host = sub.add_parser("check-host")
    host.add_argument("root")
    host.add_argument("--stage", choices=["base", "patched"], default="patched")
    args = parser.parse_args()
    if args.command == "describe":
        result = descriptor()
    elif args.command == "status":
        result = runtime_status()
    else:
        result = check_host(args.root, stage=args.stage)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("compatible") is False:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

