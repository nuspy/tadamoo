"""A parameter set by a small Python expression.

`now()`, `today()`, `now() - timedelta(days=1)`, `now().strftime("%Y-%m-%d %H:%M")`, `params["city"].title()`: written by the person, or turned from a description by the
model, and checked here before it is kept. The expression is read, not run freely: only the names, calls and methods below, no imports, no attribute of an object other
than the plain ones listed, no power, a limited size. What it gives is plain data (text, number, true/false, a list, a date or a moment)."""
from __future__ import annotations

import ast
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

MAX_EXPRESSION = 300
METHODS = {"isoformat", "strftime", "date", "time", "replace", "weekday", "isoweekday", "upper", "lower", "strip", "title", "capitalize", "split", "join",
           "startswith", "endswith", "zfill"}
ATTRIBUTES = {"year", "month", "day", "hour", "minute", "second"}
NAMES_HELP = ("now(), today(), date(y, m, d), datetime(...), timedelta(days=, hours=, minutes=, weeks=), str, int, float, round, len, min, max, abs, sum, sorted, "
              "params[\"name\"] for a parameter above it; methods isoformat, strftime, date, time, replace, weekday, upper, lower, strip, title, split, join; "
              "attributes year, month, day, hour, minute, second")
_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Constant, ast.Name, ast.Call, ast.Attribute, ast.Subscript, ast.Tuple,
          ast.List, ast.Dict, ast.JoinedStr, ast.FormattedValue, ast.Slice, ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.USub, ast.UAdd, ast.Not,
          ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.keyword)


class ExpressionError(ValueError):
    pass


def namespace(now: datetime) -> Dict[str, Any]:
    return {"now": lambda: now, "today": lambda: now.date(), "date": date, "datetime": datetime, "timedelta": timedelta, "str": str, "int": int, "float": float,
            "round": round, "len": len, "min": min, "max": max, "abs": abs, "sum": sum, "sorted": sorted, "True": True, "False": False, "None": None}


def _check(node: ast.AST, names: set) -> None:
    for child in ast.walk(node):
        if not isinstance(child, _NODES):
            raise ExpressionError(f"'{type(child).__name__}' is not allowed here; only {NAMES_HELP}")
        if isinstance(child, ast.Name) and child.id not in names and child.id != "params":
            raise ExpressionError(f"unknown name '{child.id}'; the names are {NAMES_HELP}")
        if isinstance(child, ast.Attribute) and child.attr not in METHODS | ATTRIBUTES:
            raise ExpressionError(f"'.{child.attr}' is not allowed; use one of {', '.join(sorted(METHODS | ATTRIBUTES))}")
        if isinstance(child, ast.Call) and not (isinstance(child.func, ast.Name) or (isinstance(child.func, ast.Attribute) and child.func.attr in METHODS)):
            raise ExpressionError("only the functions and methods listed can be called")
        if isinstance(child, ast.Constant) and isinstance(child.value, (bytes, complex)):
            raise ExpressionError("a text, a number or true/false")
        if isinstance(child, ast.BinOp) and isinstance(child.op, ast.Mult) and any(isinstance(o, (ast.Constant, ast.List, ast.Tuple, ast.JoinedStr)) and
                                                                                   not (isinstance(o, ast.Constant) and isinstance(o.value, (int, float)))
                                                                                   for o in (child.left, child.right)):
            raise ExpressionError("a text or a list cannot be repeated here")


def validate(expression: Any) -> None:
    """Reads the expression without running it: raises ExpressionError when it is not allowed."""
    if not isinstance(expression, str) or not expression.strip():
        raise ExpressionError("write the expression")
    if len(expression) > MAX_EXPRESSION:
        raise ExpressionError(f"at most {MAX_EXPRESSION} characters")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"not Python: {exc.msg}") from None
    _check(tree, set(namespace(datetime.now())))


def evaluate(expression: Any, now: Optional[datetime] = None, params: Optional[Dict[str, Any]] = None) -> Any:
    """The value of the expression at `now`, with the values of the parameters above it as `params`. Raises ExpressionError saying what is wrong."""
    if not isinstance(expression, str) or not expression.strip():
        raise ExpressionError("write the expression")
    if len(expression) > MAX_EXPRESSION:
        raise ExpressionError(f"at most {MAX_EXPRESSION} characters")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"not Python: {exc.msg}") from None
    space = namespace(now or datetime.now())
    _check(tree, set(space))
    try:
        value = eval(compile(tree, "<parameter>", "eval"), {"__builtins__": {"__import__": __import__}}, {**space, "params": dict(params or {})})   # the date methods import `time` themselves; the expression cannot name it
    except ExpressionError:
        raise
    except Exception as exc:                                   # the expression ran and failed: a wrong type, a missing parameter, a division by zero
        raise ExpressionError(f"{type(exc).__name__}: {exc}") from None
    return plain(value)


def plain(value: Any) -> Any:
    """What an expression may give: text, numbers, true/false, dates and moments, and lists of them."""
    if isinstance(value, (datetime, date)):
        return value
    if isinstance(value, (str, bool, int, float)) or value is None:
        if isinstance(value, str) and len(value) > 20000:
            raise ExpressionError("the result is too long")
        return value
    if isinstance(value, (list, tuple)) and len(value) <= 500:
        return [plain(v) for v in value]
    raise ExpressionError(f"the result is a {type(value).__name__}: it must be text, a number, true/false, a date or a list")
