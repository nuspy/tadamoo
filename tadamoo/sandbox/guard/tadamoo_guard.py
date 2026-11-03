"""Refuses, inside a Python process, what would read or change the person's secrets: the vault, the payment files, the browser's
sign-ins, the control token, the phone keys.

It is an audit hook, so it sees the path a program builds at run time, which a check of the program's text cannot. It is not a wall
against code written to defeat it: a program in another language, or one that starts a program that builds the path itself, is not
seen. The places to refuse come in TADAMOO_GUARD, separated by the path separator; children that run Python load this module again
through sitecustomize.
"""
import os
import re
import sys

# event -> the positions of its arguments that are paths
_PATH_EVENTS = {"open": (0,), "os.remove": (0,), "os.rmdir": (0,), "os.rename": (0, 1), "os.link": (0, 1), "os.symlink": (0, 1),
                "os.truncate": (0,), "os.mkdir": (0,), "os.chmod": (0,), "shutil.copyfile": (0, 1), "shutil.copytree": (0, 1),
                "shutil.move": (0, 1), "os.startfile": (0,), "sqlite3.connect": (0,)}
# event -> the positions of its arguments that make up the command
_TEXT_EVENTS = {"os.system": (0,), "os.exec": (0, 1), "os.spawn": (1, 2), "os.posix_spawn": (0, 1), "subprocess.Popen": (0, 1)}
_installed = False


def _flat(text):
    return re.sub(r"[\\/]+", "/", text).lower()


def _text(value):
    if isinstance(value, (list, tuple)):
        return " ".join(_text(v) for v in value)
    if isinstance(value, bytes):
        return os.fsdecode(value)
    return value if isinstance(value, str) else ""


def install():
    global _installed
    places = [_flat(n).rstrip("/") for n in os.environ.get("TADAMOO_GUARD", "").split(os.pathsep) if n]
    if _installed or not places:
        return
    _installed = True

    def refuse(what):
        raise PermissionError(f"Access denied: {what}. The vault, the payment files, the browser's sign-ins, the control token and the phone "
                              "keys are the person's; programs the agent runs cannot read or change them.")

    def check_path(value):
        try:
            raw = os.fsdecode(value)
            real = _flat(os.path.realpath(raw))
        except (TypeError, ValueError, OSError):
            return
        if any(real == p or real.startswith(p + "/") for p in places):
            refuse(f"'{raw}' is one of them")

    def audit(event, args):
        if event in _PATH_EVENTS:
            for i in _PATH_EVENTS[event]:
                if len(args) > i:
                    check_path(args[i])
        elif event in _TEXT_EVENTS:
            text = _flat(" ".join(_text(args[i]) for i in _TEXT_EVENTS[event] if len(args) > i))
            if any(p in text for p in places):
                refuse("the command names one of them")

    sys.addaudithook(audit)
