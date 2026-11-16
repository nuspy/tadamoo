"""python -m tadamoo.computer install | status | path [--dir FOLDER] [--tag vX.Y.Z]: agent-desktop in the tools folder (docs/computer-control.md)."""
import argparse
import json
import sys
from pathlib import Path

from tadamoo.computer import install as installer
from tadamoo.computer.backend import ComputerError


def default_dir() -> Path:
    try:
        from serve.userdata import user_data_dir
        return Path(user_data_dir()) / "tools" / "agent-desktop"
    except Exception:
        return Path.home() / ".tadamoo" / "tools" / "agent-desktop"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tadamoo.computer", description="agent-desktop for computer control: install it from its GitHub release, or see what is installed")
    parser.add_argument("action", choices=["install", "status", "path"])
    parser.add_argument("--dir", default="", help="the folder (default: <data>/tools/agent-desktop)")
    parser.add_argument("--tag", default="", help="a release, for example v1.3.1 (default: the latest)")
    args = parser.parse_args(argv)
    folder = Path(args.dir) if args.dir else default_dir()
    try:
        if args.action == "install":
            print(json.dumps(installer.install(folder, args.tag), indent=2))
        elif args.action == "path":
            info = installer.installed(folder)
            print(info["path"])
            return 0 if info["found"] else 1
        else:
            info = installer.installed(folder)
            if info["found"]:
                info["program"] = installer.program_status([info["path"]], folder / "state")
            print(json.dumps(info, indent=2))
            return 0 if info["found"] else 1
    except ComputerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
