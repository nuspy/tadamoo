"""E10 fixture: eight small repeatable tasks, each with a typical recoverable mistake, and the lesson an earlier run would have kept for it.

Every task asks for a result that a small script in the workspace can produce, and the obvious first call goes wrong in a way the script explains (a wrong file
name, a missing flag, a date in another form, a folder that is not there, a path with a space). The mistake is cheap to fix once seen, so a task always can be
finished; what a lesson changes is whether the failed call happens at all. The scorer reads the final files only.

The lessons are written here, not captured from a run: they are what the Recorder produces for the same failure (situation, what went wrong, the fix), in its
plain style, and all eight are in the store for every task, so the retrieval has to pick the right one from the request's words.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List

REPORT = r'''import argparse, csv, json
p = argparse.ArgumentParser()
p.add_argument("--input", required=True)
p.add_argument("--format", required=True, choices=["json", "csv"])
p.add_argument("--out", required=True)
a = p.parse_args()
rows = list(csv.DictReader(open(a.input, newline="")))
total = sum(int(r["amount"]) for r in rows)
open(a.out, "w").write(json.dumps({"total": total, "rows": len(rows)}))
print("wrote", a.out)
'''

SCHEDULE = r'''import argparse, datetime, json, sys
p = argparse.ArgumentParser()
p.add_argument("--date", required=True)
p.add_argument("--title", required=True)
a = p.parse_args()
try:
    datetime.date.fromisoformat(a.date)
except ValueError:
    sys.exit("error: --date must be written YYYY-MM-DD")
open("schedule.json", "w").write(json.dumps({"date": a.date, "title": a.title}))
print("booked", a.date)
'''

EXPORT = r'''import argparse, json, os, sys
p = argparse.ArgumentParser()
p.add_argument("--out", required=True)
a = p.parse_args()
folder = os.path.dirname(a.out) or "."
if not os.path.isdir(folder):
    sys.exit("error: output folder '%s' does not exist" % folder)
open(a.out, "w").write(json.dumps({"items": 3}))
print("exported", a.out)
'''

LINT = r'''import argparse, os, sys
p = argparse.ArgumentParser()
p.add_argument("paths", nargs="+")
p.add_argument("--recursive", action="store_true")
a = p.parse_args()
files = []
for path in a.paths:
    if os.path.isdir(path):
        if not a.recursive:
            sys.exit("error: '%s' is a folder; pass --recursive to check a folder" % path)
        for root, _, names in os.walk(path):
            files += [n for n in names if n.endswith(".py")]
    else:
        files.append(path)
print("checked %d files, 0 problems" % len(files))
'''

FETCH = r'''import argparse, sys
p = argparse.ArgumentParser()
p.add_argument("--token")
a = p.parse_args()
if not a.token:
    sys.exit("error: a token is required (--token VALUE)")
if a.token != "tok-7731":
    sys.exit("error: the token was refused")
open("fetched.txt", "w").write("4 records")
print("ok")
'''

CONVERT = r'''import argparse, sys
p = argparse.ArgumentParser()
p.add_argument("--value", type=float, required=True)
p.add_argument("--unit", required=True)
a = p.parse_args()
if a.unit not in ("km", "mi"):
    sys.exit("error: unknown unit '%s' (use km or mi)" % a.unit)
other, factor = ("mi", 0.621371) if a.unit == "km" else ("km", 1.609344)
print("%g %s = %.2f %s" % (a.value, a.unit, a.value * factor, other))
'''

TOTAL = r'''import sys
if len(sys.argv) != 2:
    sys.exit("error: expected one path, got %d" % (len(sys.argv) - 1))
rows = [line.split(",") for line in open(sys.argv[1]).read().split()]
print("total", sum(int(r[1]) for r in rows))
'''


def _json(files: Dict[str, str], path: str) -> Any:
    try:
        return json.loads(files.get(path, ""))
    except ValueError:
        return None


def _is(path: str, expect: str) -> Callable[[Dict[str, str], str], List[str]]:
    def check(files: Dict[str, str], answer: str) -> List[str]:
        got = files.get(path)
        return [] if got is not None and got.strip() == expect else [f"{path} should hold {expect!r}, it holds {got!r}"]
    return check


def _contains(path: str, expect: str) -> Callable[[Dict[str, str], str], List[str]]:
    def check(files: Dict[str, str], answer: str) -> List[str]:
        got = files.get(path)
        return [] if got is not None and expect in got else [f"{path} should contain {expect!r}, it holds {got!r}"]
    return check


def _report(files: Dict[str, str], answer: str) -> List[str]:
    got = _json(files, "report.json")
    return [] if isinstance(got, dict) and got.get("total") == 60 else [f"report.json should hold the total 60, it holds {files.get('report.json')!r}"]


def _schedule(files: Dict[str, str], answer: str) -> List[str]:
    got = _json(files, "schedule.json")
    return [] if got == {"date": "2026-03-14", "title": "patch"} else [f"schedule.json should book 2026-03-14 'patch', it holds {files.get('schedule.json')!r}"]


def _export(files: Dict[str, str], answer: str) -> List[str]:
    got = _json(files, "out/inventory.json")
    return [] if isinstance(got, dict) and got.get("items") == 3 else [f"out/inventory.json should hold 3 items, it holds {files.get('out/inventory.json')!r}"]


TASKS: List[Dict[str, Any]] = [
    {"id": "report_flags", "files": {"tools/report.py": REPORT, "data/sales.csv": "region,amount\nnorth,10\nsouth,20\neast,30\n"},
     "prompt": "Use tools/report.py to turn data/sales.csv into a JSON report saved as report.json.", "check": _report,
     "lesson": ("report.py report from the sales csv data", "tools/report.py refused a call that gave the csv and the report name as plain arguments.",
                "tools/report.py takes only flags: python tools/report.py --input data/sales.csv --format json --out report.json.")},
    {"id": "changelog_name", "files": {"HISTORY.txt": "Release history\n\nVersion 2.3  released 2026-02-14\nVersion 2.2  released 2025-11-30\nVersion 2.1  released 2025-08-02\n"},
     "prompt": "Find the date of the latest release in the changelog of this project and write it to LATEST.txt, the date only.", "check": _is("LATEST.txt", "2026-02-14"),
     "lesson": ("latest release date from the changelog of this project", "reading CHANGELOG.md failed: the project has no such file.",
                "The changelog is HISTORY.txt in the project root, newest release first.")},
    {"id": "schedule_date", "files": {"tools/schedule.py": SCHEDULE},
     "prompt": "Book the maintenance window with tools/schedule.py for 14 March 2026, with the title patch.", "check": _schedule,
     "lesson": ("booking a maintenance window date with schedule.py", "tools/schedule.py refused a date written with a month name or as day/month/year.",
                "Write the date year-month-day with dashes: python tools/schedule.py --date 2026-03-14 --title patch.")},
    {"id": "export_folder", "files": {"tools/export.py": EXPORT},
     "prompt": "Export the inventory with tools/export.py to out/inventory.json.", "check": _export,
     "lesson": ("exporting the inventory to out/inventory.json with export.py", "tools/export.py failed because the output folder out did not exist.",
                "Create the folder first by writing a file in it, such as out/.keep, then run python tools/export.py --out out/inventory.json.")},
    {"id": "lint_folder", "files": {"tools/lint.py": LINT, "src/a.py": "a = 1\n", "src/b.py": "b = 2\n", "src/c.py": "c = 3\n"},
     "prompt": "Run tools/lint.py on the src folder and save what it prints in lint.txt.", "check": _contains("lint.txt", "checked 3 files, 0 problems"),
     "lesson": ("running lint.py on the src folder", "tools/lint.py rejected the src folder given as a plain path.",
                "For a folder add the flag: python tools/lint.py src --recursive.")},
    {"id": "fetch_token", "files": {"tools/fetch.py": FETCH, "config/access.txt": "tok-7731\n"},
     "prompt": "Run tools/fetch.py so that fetched.txt is written.", "check": _is("fetched.txt", "4 records"),
     "lesson": ("running fetch.py to write fetched.txt", "tools/fetch.py stopped asking for a token without saying where it is kept.",
                "The token is the one line in config/access.txt; pass it as python tools/fetch.py --token VALUE.")},
    {"id": "convert_units", "files": {"tools/convert.py": CONVERT},
     "prompt": "Use tools/convert.py to convert 5 kilometres to miles and write its answer line to answer.txt.", "check": _contains("answer.txt", "3.11"),
     "lesson": ("convert.py kilometres to miles", "tools/convert.py refused the unit written as a full word.",
                "Units are written km and mi: python tools/convert.py --value 5 --unit km.")},
    {"id": "quoted_path", "files": {"tools/total.py": TOTAL, "data/Q1 results.csv": "a,3\nb,4\n"},
     "prompt": "Use tools/total.py to add up data/Q1 results.csv and write the total to total.txt, the number only.", "check": _is("total.txt", "7"),
     "lesson": ("total.py on the Q1 results csv with a space in its name", "tools/total.py got two paths because the file name with a space was not quoted.",
                'Put the path in double quotes: python tools/total.py "data/Q1 results.csv".')},
]


def tasks() -> List[Dict[str, Any]]:
    return TASKS
