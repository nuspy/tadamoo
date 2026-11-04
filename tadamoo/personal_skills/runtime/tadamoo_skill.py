"""What a personal skill's script imports: `from tadamoo_skill import tool, ai, ask, say`.

The script runs in a process of its own; each of these calls is a line of JSON to the harness, which does the work (through the permissions,
the hooks and the audit record) and answers. `print` is kept too: what it writes is shown as the skill's output."""
import datetime as _dt
import json
import sys

PREFIX = "@@tadamoo@@ "
_out = None
_in = None
_n = 0
_values = {}


class SkillError(Exception):
    """Something the skill asked for did not happen; the message says why."""


class ToolError(SkillError):
    """A tool was refused (by the person, a rule or a hook) or it failed."""


def _init(out, inp, params):
    global _out, _in, _values
    _out, _in, _values = out, inp, params


def _send(message):
    _out.write(PREFIX + json.dumps(message, ensure_ascii=False) + "\n")
    _out.flush()


def _call(kind, **payload):
    global _n
    _n += 1
    _send({"id": _n, "type": kind, **payload})
    line = _in.readline()
    if not line:
        sys.exit(3)                                # the harness is gone: nothing is left to do
    reply = json.loads(line)
    if reply.get("id") != _n:
        raise SkillError("the harness answered another call")
    if not reply.get("ok"):
        raise (ToolError if kind == "tool" else SkillError)(reply.get("error") or "it did not work")
    return reply.get("value")


def tool(name, /, **arguments):
    """Calls one of the agent's tools and returns its answer as text. It raises ToolError when the tool is refused or fails."""
    return _call("tool", name=name, arguments=arguments)


def ai(prompt, /, *, cache=True, **inputs):
    """Asks the model; use it only where the language has to be understood. The prompt may use {name} for each input. The answer is the
    same for the same prompt and inputs (temperature 0, and with cache=True it is kept and not asked again)."""
    return _call("ai", prompt=prompt, inputs={k: v if isinstance(v, (str, int, float, bool, type(None), list, dict)) else str(v) for k, v in inputs.items()}, cache=bool(cache))


def ask(question, options=None):
    """Asks the person and returns what they chose (one of the options) or, with no options, what they wrote."""
    return _call("ask", question=str(question), options=[str(o) for o in (options or [])])


def say(text):
    """Tells the person how it is going, in the conversation."""
    _send({"type": "say", "text": str(text)})


def _convert(kind, value):
    if value is None:
        return None
    if kind == "date":
        return _dt.date.fromisoformat(value)
    if kind == "datetime":
        return _dt.datetime.fromisoformat(value)
    return value


class Params:
    """The values the skill was started with, by name: params.city or params["city"]. Dates are datetime.date, moments datetime.datetime,
    numbers int or float, lists lists."""

    def __init__(self, values):
        self._v = {name: _convert(item["type"], item["value"]) for name, item in values.items()}

    def __getattr__(self, name):
        try:
            return self._v[name]
        except KeyError:
            raise AttributeError(f"this skill has no parameter '{name}'") from None

    def __getitem__(self, name):
        return self._v[name]

    def __contains__(self, name):
        return name in self._v

    def __iter__(self):
        return iter(self._v)

    def get(self, name, default=None):
        return self._v.get(name, default)

    def as_dict(self):
        return dict(self._v)
