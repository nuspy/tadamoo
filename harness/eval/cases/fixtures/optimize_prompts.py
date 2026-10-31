"""E8 fixture: twenty short requests for Optimize prompt, each with something in it a rewrite must keep (a file name, a number, a quotation, a code
block, a path, a link) and checks that decide the task afterwards. No row asks a question; the person is not asked anything.

`checks` decide the task; `solution` is a result that passes them (a file with None is deleted). What a rewrite must keep is read from the text itself
by `harness.optimize.protected`, so the fixture and the guard can never disagree about it.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

DOUBLE_OK = "{python} -c \"import mathx; assert mathx.double(4) == 8\""
TRIPLE_OK = "{python} -c \"import mathx; assert mathx.triple(2) == 6\""


def row(rid: str, category: str, text: str, checks: List[Dict[str, Any]], solution: Dict[str, Optional[str]], *, files: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    return {"id": rid, "category": category, "text": text, "files": files or {}, "checks": checks, "solution": solution}


def has(path: str, text: str) -> Dict[str, Any]:
    return {"type": "file_contains", "path": path, "text": text}


def only(path: str, pattern: str) -> Dict[str, Any]:
    return {"type": "file_regex", "path": path, "pattern": pattern}


PROMPTS: List[Dict[str, Any]] = [
    # terse: little said, files named
    row("terse1", "terse", "make todo.txt with 3 lines: wash car, call Anna, pay rent", [has("todo.txt", "wash car"), has("todo.txt", "call Anna"), has("todo.txt", "pay rent")],
        {"todo.txt": "wash car\ncall Anna\npay rent\n"}),
    row("terse2", "terse", "add double(x) to mathx.py", [{"type": "command", "cmd": DOUBLE_OK}], {"mathx.py": "def triple(x):\n    return x * 3\n\n\ndef double(x):\n    return x * 2\n"},
        files={"mathx.py": "def triple(x):\n    return x * 3\n"}),
    row("terse3", "terse", "put the line count of data.csv in count.txt", [only("count.txt", r"^\s*7\s*$")], {"count.txt": "7\n"},
        files={"data.csv": "a,1\nb,2\nc,3\nd,4\ne,5\nf,6\ng,7\n"}),
    row("terse4", "terse", "uppercase names.txt into upper.txt", [{"type": "file_equals", "path": "upper.txt", "content": "ANN\nBOB\n"}], {"upper.txt": "ANN\nBOB\n"},
        files={"names.txt": "ann\nbob\n"}),
    row("terse5", "terse", "rename old.txt to new.txt", [{"type": "no_file", "path": "old.txt"}, has("new.txt", "payload")], {"old.txt": None, "new.txt": "payload\n"},
        files={"old.txt": "payload\n"}),
    # numbers that must not change
    row("num1", "numbers", "Write port.txt containing exactly the number 8080", [only("port.txt", r"^\s*8080\s*$")], {"port.txt": "8080\n"}),
    row("num2", "numbers", "Create sizes.txt with the numbers 10, 20 and 30, one per line", [only("sizes.txt", r"(?s)^10\s+20\s+30\s*$")], {"sizes.txt": "10\n20\n30\n"}),
    row("num3", "numbers", "In config.ini change timeout from 30 to 45 seconds and leave the rest", [has("config.ini", "timeout=45"), has("config.ini", "retries=3")],
        {"config.ini": "timeout=45\nretries=3\n"}, files={"config.ini": "timeout=30\nretries=3\n"}),
    row("num4", "numbers", "Scrivi in totale.txt la somma di 12 e 30", [only("totale.txt", r"\b42\b")], {"totale.txt": "42\n"}),
    # quotations that must stay word for word
    row("quote1", "quoted", "Create greeting.txt that contains the exact text \"Hello, brave new world\"", [has("greeting.txt", "Hello, brave new world")],
        {"greeting.txt": "Hello, brave new world\n"}),
    row("quote2", "quoted", "Scrivi in nota.txt la frase \"Non dimenticare il latte\" senza cambiarla", [has("nota.txt", "Non dimenticare il latte")],
        {"nota.txt": "Non dimenticare il latte\n"}),
    row("quote3", "quoted", "Replace the word \"draft\" with \"final\" in memo.txt", [has("memo.txt", "final memo"), {"type": "file_not_contains", "path": "memo.txt", "text": "draft"}],
        {"memo.txt": "This is a final memo.\n"}, files={"memo.txt": "This is a draft memo.\n"}),
    # code that must stay as written
    row("code1", "code", "Save this as hello.py:\n```python\nprint('hi from hello')\n```", [has("hello.py", "print('hi from hello')")], {"hello.py": "print('hi from hello')\n"}),
    row("code2", "code", "Append the line `export MODE=prod` to env.sh", [has("env.sh", "export MODE=prod"), has("env.sh", "export A=1")],
        {"env.sh": "export A=1\nexport MODE=prod\n"}, files={"env.sh": "export A=1\n"}),
    # paths and links
    row("path1", "paths", "Copy src/app/main.txt to build/out/main.txt", [has("build/out/main.txt", "payload")], {"build/out/main.txt": "payload\n"},
        files={"src/app/main.txt": "payload\n"}),
    row("path2", "paths", "Write the URL https://example.com/docs/start into link.txt", [has("link.txt", "https://example.com/docs/start")], {"link.txt": "https://example.com/docs/start\n"}),
    row("path3", "paths", "Create reports/2026/summary.md with the heading # Summary", [has("reports/2026/summary.md", "# Summary")], {"reports/2026/summary.md": "# Summary\n"}),
    # Italian
    row("it1", "italian", "crea saluto.txt con la riga: ciao a tutti", [has("saluto.txt", "ciao a tutti")], {"saluto.txt": "ciao a tutti\n"}),
    row("it2", "italian", "elimina il file temp.log e tieni notes.txt", [{"type": "no_file", "path": "temp.log"}, {"type": "file_unchanged", "path": "notes.txt"}], {"temp.log": None},
        files={"temp.log": "old\n", "notes.txt": "keep\n"}),
    row("it3", "italian", "aggiungi la funzione triple(x) che moltiplica per 3 a mathx.py", [{"type": "command", "cmd": TRIPLE_OK}],
        {"mathx.py": "def double(x):\n    return x * 2\n\n\ndef triple(x):\n    return x * 3\n"}, files={"mathx.py": "def double(x):\n    return x * 2\n"}),
]


def prompts() -> List[Dict[str, Any]]:
    return PROMPTS
