"""E8 fixture: twenty-nine requests for Optimize prompt, each with something in it a rewrite must keep (a file name, a number, a quotation, a code
block, a path, a link) and checks that decide the task afterwards: twenty short ones, three vague one-liners, four long ones (150 to 400 words, with
lists, code and links to protect) and two that mix Italian and English. No row asks a question; the person is not asked anything.

`checks` decide the task; `solution` is a result that passes them (a file with None is deleted). What a rewrite must keep is read from the text itself
by `tadamoo.optimize.protected`, so the fixture and the guard can never disagree about it.
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

LONG1 = """I need you to prepare the weekly status file for the platform team, and I want it done exactly the way I describe, because the file is read by a script afterwards. Create the file status/week_44.md in the workspace. It must start with the heading line `# Week 44 status` and then contain, in this order, the following six sections, each as a second level heading:

1. Summary
2. Completed work
3. Open issues
4. Risks
5. Next week
6. Links

Under "Links" put the two addresses https://wiki.example.com/platform/runbook and https://dash.example.com/team/44 on separate lines, copied exactly. Under "Completed work" write the three lines `- shipped build 1187`, `- closed 12 tickets` and `- migrated 3 services`. Under "Open issues" write the single line `- 5 tickets still open`. The other sections can hold the single line `- none` each. Please do not add any other section, do not change the heading names, and do not translate anything: the script that reads the file looks for these exact words. If the directory status does not exist yet, create it. When you finish, tell me in one sentence which file you wrote."""

LONG2 = """Please set up the environment script for the staging deploy. Create the file deploy/env.sh with exactly the following content, nothing before it and nothing after it:

```sh
export APP_ENV=staging
export APP_PORT=8443
export API_URL=https://api.staging.example.com/v2
export WORKERS=6
```

After that, create the file deploy/README.txt that explains in plain words, one line each, what the four variables are for. Mention every variable name by its exact name. The first line of the README must be the sentence "Staging environment variables". I know the port looks unusual, but 8443 is the one the load balancer forwards, so do not replace it with 443 or 8080. The worker count of 6 comes from the capacity test we ran last month and must stay as it is. If a file with the same name already exists, overwrite it. Do not touch anything else in the workspace, in particular leave the file deploy/old_env.sh alone, because another team still uses it. Once both files exist, answer with the two paths and nothing more. Thanks, this is blocking the release so please be careful with the values."""

LONG3 = """Ciao, ho bisogno che tu prepari alcuni file per la consegna al cliente e che tu li prepari proprio come li descrivo, perché poi li controlla un collega a mano. Nella cartella consegna/ crea questi tre file: 1. consegna/indice.txt con la prima riga "Indice della consegna" e sotto un elenco di due righe, `documento_a.txt` e `documento_b.txt`; 2. consegna/documento_a.txt con la frase "Il preventivo è di 1250 euro" e la data 2026-11-15 su una riga a parte; 3. consegna/documento_b.txt con la frase "Il pagamento avviene entro 30 giorni" e, sotto, l'indirizzo https://pagamenti.example.it/cliente/77 copiato identico. Ti prego di non cambiare le cifre, di non arrotondare 1250 e di non riscrivere le date in un altro formato, perché il cliente le cerca con un programma. Non aggiungere intestazioni, commenti o righe vuote in più. Se la cartella consegna non esiste, creala tu. Non modificare il file consegna/vecchio.txt, che deve restare com'è. Alla fine rispondimi con una sola frase che dica quali file hai creato, senza elencare il contenuto."""

LONG4 = """Hi, hope you are well. Sorry for the long message, I am writing from the train and my thoughts are all over the place. Last week we talked about the invoices again, and honestly I lost track of which version is the right one, but the short story is this. Marco sent me three drafts, the second had a mistake in the VAT, the third fixed it, and then Elena said that the very first one was actually fine for the old customers. I do not care about the history. What I need now is one small thing in the workspace: open the file invoices.csv, which has one amount per line, add up all the amounts, and write only the total into the file answer.txt. Nothing else in that file, no currency symbol, no text. Then, in a second file called quote.txt, copy exactly this sentence, with the quotation marks removed: "The total was checked twice by Anna". I will forward both files to accounting this afternoon, and they are strict about it, so please do not paraphrase the sentence and do not round the total. By the way, the amounts in the file are in euro and some have two decimals, so be careful with the sum. If something is unclear, make the most sensible choice and tell me what you chose in your last message. Thanks a lot, and sorry again for the rambling. Talk soon."""

PROMPTS += [
    # vague: one line, the substance is in a file of the workspace
    row("vague1", "vague", "do what notes.txt says about the config", [has("config.json", '"debug": false'), has("config.json", '"port": 80')],
        {"config.json": '{"debug": false, "port": 80}\n'}, files={"notes.txt": "In config.json set debug to false and leave the rest.\n", "config.json": '{"debug": true, "port": 80}\n'}),
    row("vague2", "vague", "clean up the tmp_ files but keep keep.txt", [{"type": "no_file", "path": "tmp_a.txt"}, {"type": "no_file", "path": "tmp_b.txt"}, {"type": "file_unchanged", "path": "keep.txt"}],
        {"tmp_a.txt": None, "tmp_b.txt": None}, files={"tmp_a.txt": "a\n", "tmp_b.txt": "b\n", "keep.txt": "keep\n"}),
    row("vague3", "vague", "metti in summary.txt il totale di sales.csv", [only("summary.txt", r"\b60\b")], {"summary.txt": "60\n"}, files={"sales.csv": "q1,10\nq2,20\nq3,30\n"}),
    # long: 150 to 400 words with lists, code, links, quotations and numbers to protect
    row("long1", "long", LONG1, [has("status/week_44.md", "# Week 44 status"), has("status/week_44.md", "## Open issues"), has("status/week_44.md", "## Links"),
        has("status/week_44.md", "https://wiki.example.com/platform/runbook"), has("status/week_44.md", "https://dash.example.com/team/44"), has("status/week_44.md", "- shipped build 1187"),
        has("status/week_44.md", "- closed 12 tickets"), has("status/week_44.md", "- migrated 3 services"), has("status/week_44.md", "- 5 tickets still open")],
        {"status/week_44.md": "# Week 44 status\n\n## Summary\n- none\n\n## Completed work\n- shipped build 1187\n- closed 12 tickets\n- migrated 3 services\n\n## Open issues\n"
         "- 5 tickets still open\n\n## Risks\n- none\n\n## Next week\n- none\n\n## Links\nhttps://wiki.example.com/platform/runbook\nhttps://dash.example.com/team/44\n"}),
    row("long2", "long", LONG2, [has("deploy/env.sh", "export APP_ENV=staging"), has("deploy/env.sh", "export APP_PORT=8443"), has("deploy/env.sh", "export API_URL=https://api.staging.example.com/v2"),
        has("deploy/env.sh", "export WORKERS=6"), has("deploy/README.txt", "Staging environment variables"), has("deploy/README.txt", "APP_ENV"), has("deploy/README.txt", "APP_PORT"),
        has("deploy/README.txt", "API_URL"), has("deploy/README.txt", "WORKERS"), {"type": "file_unchanged", "path": "deploy/old_env.sh"}],
        {"deploy/env.sh": "export APP_ENV=staging\nexport APP_PORT=8443\nexport API_URL=https://api.staging.example.com/v2\nexport WORKERS=6\n",
         "deploy/README.txt": "Staging environment variables\nAPP_ENV: the environment name\nAPP_PORT: the port\nAPI_URL: the API address\nWORKERS: the worker count\n"},
        files={"deploy/old_env.sh": "export OLD=1\n"}),
    row("long3", "long", LONG3, [has("consegna/indice.txt", "Indice della consegna"), has("consegna/indice.txt", "documento_a.txt"), has("consegna/indice.txt", "documento_b.txt"),
        has("consegna/documento_a.txt", "Il preventivo è di 1250 euro"), has("consegna/documento_a.txt", "2026-11-15"), has("consegna/documento_b.txt", "Il pagamento avviene entro 30 giorni"),
        has("consegna/documento_b.txt", "https://pagamenti.example.it/cliente/77"), {"type": "file_unchanged", "path": "consegna/vecchio.txt"}],
        {"consegna/indice.txt": "Indice della consegna\ndocumento_a.txt\ndocumento_b.txt\n", "consegna/documento_a.txt": "Il preventivo è di 1250 euro\n2026-11-15\n",
         "consegna/documento_b.txt": "Il pagamento avviene entro 30 giorni\nhttps://pagamenti.example.it/cliente/77\n"}, files={"consegna/vecchio.txt": "da non toccare\n"}),
    row("long4", "long", LONG4, [only("answer.txt", r"^\s*400(\.0+)?\s*$"), has("quote.txt", "The total was checked twice by Anna")],
        {"answer.txt": "400\n", "quote.txt": "The total was checked twice by Anna\n"}, files={"invoices.csv": "100.50\n200.25\n99.25\n"}),
    # Italian and English in one request
    row("mix1", "mixed", 'crea README.md con il titolo "Project Atlas" e una riga che dice: build with `make all`', [has("README.md", "Project Atlas"), has("README.md", "make all")],
        {"README.md": "# Project Atlas\nbuild with make all\n"}),
    row("mix2", "mixed", "please scrivi in plan.txt tre step: 1) read docs/intro.md 2) fix bug 42 3) deploy to https://staging.example.com/app",
        [has("plan.txt", "docs/intro.md"), has("plan.txt", "42"), has("plan.txt", "https://staging.example.com/app")],
        {"plan.txt": "1) read docs/intro.md\n2) fix bug 42\n3) deploy to https://staging.example.com/app\n"}),
]


def prompts() -> List[Dict[str, Any]]:
    return PROMPTS
