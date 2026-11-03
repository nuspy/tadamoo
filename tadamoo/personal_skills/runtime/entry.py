"""Runs a personal skill's script: `python -I entry.py <script> <context.json>`.

The context holds the parameters. The harness is on the other end of the pipes: see tadamoo_skill.py."""
import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tadamoo_skill as sdk  # noqa: E402


class _Printer:
    """What the script prints goes to the harness as output, not into the pipe the calls use."""

    def __init__(self):
        self.buffer = ""

    def write(self, text):
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            sdk._send({"type": "print", "text": line})
        return len(text)

    def flush(self):
        if self.buffer:
            sdk._send({"type": "print", "text": self.buffer})
            self.buffer = ""


def _text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)


def main():
    script, context_path = sys.argv[1], sys.argv[2]
    with open(context_path, encoding="utf-8") as f:
        context = json.load(f)
    out, inp = sys.stdout, sys.stdin
    sdk._init(out, inp, context.get("params") or {})
    printer = _Printer()
    sys.stdout = printer
    try:
        with open(script, encoding="utf-8") as f:
            code = compile(f.read(), os.path.basename(script), "exec")
        scope = {"__name__": "skill"}
        exec(code, scope)
        result = scope["main"](sdk.Params(context.get("params") or {}))
        printer.flush()
        sdk._send({"type": "result", "value": _text(result)})
        return 0
    except SystemExit:
        raise
    except BaseException as exc:                                   # the script's own error ends the run with what was wrong
        printer.flush()
        tb = traceback.extract_tb(exc.__traceback__)
        mine = [f for f in tb if f.filename == os.path.basename(script)]
        where = f" (line {mine[-1].lineno})" if mine else ""
        sdk._send({"type": "error", "message": f"{type(exc).__name__}: {exc}{where}"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
