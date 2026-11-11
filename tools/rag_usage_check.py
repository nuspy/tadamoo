"""Replays a request against the retrieval a conversation gets from an archive and says, for each fact the answer ought to hold, whether the pages that carry it
reach the model: through the passages put in front of the request when the turn starts (as it was: the whole request as one query; and as it is: the request asked
as several questions), and through `knowledge_search` calls. No language model is called.

  python tools/rag_usage_check.py --archive <id> --prompt prompt.txt --facts facts.json [--plans plans.json] [--rag-dir DIR] [--url http://127.0.0.1:7232] [--json out.json]

facts.json: a list of {"id", "doc", "true_pages": [pages], "fact"}; a document `doc` is a name or the start of one (`US123` for `US123.pdf`), a fact is reached when
a passage of that document starts on one of its true pages. plans.json (optional): {"questions": [the queries a technical session would write for the
request], "searches": [the `knowledge_search` queries a model could plausibly make], "oracle": {"<fact id>": "a query worded on the fact itself"}}; each part
that is missing is left out of the table. The server at --url lends the GPU to the embedder and the reranker, as the program does; the report says whether the
reranker ran."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tadamoo.rag import lookup  # noqa: E402
from tadamoo.rag.recall import ProjectRecall  # noqa: E402
from tadamoo.rag.store import RagStore  # noqa: E402

HEAD = re.compile(r"^\[[^/\]]+ / ([^,\]]+)(?:, pp?\. (\d+)(?:-(\d+))?)?", re.MULTILINE)


def reached(facts: Sequence[Dict[str, Any]], seen: Set[Tuple[str, int]]) -> Dict[str, bool]:
    """For each fact whether a seen (document, page) is on one of its true pages of its document."""
    out = {}
    for f in facts:
        pages = {int(p) for p in f.get("true_pages", [])}
        out[f["id"]] = any(doc.lower().startswith(str(f["doc"]).lower()) and page in pages for doc, page in seen)
    return out


def injected(block: Optional[str]) -> Tuple[Set[Tuple[str, int]], int]:
    """The (document, first page) of each passage of a turn-start block, and how many passages it holds."""
    seen: Set[Tuple[str, int]] = set()
    count = 0
    for m in HEAD.finditer(block or ""):
        count += 1
        if m.group(2):
            seen.add((m.group(1), int(m.group(2))))
    return seen, count


def searched(store: RagStore, recall: ProjectRecall, queries: Sequence[str]) -> Tuple[Set[Tuple[str, int]], int]:
    """What `knowledge_search` gives for each query on its own, together."""
    seen: Set[Tuple[str, int]] = set()
    count = 0
    for q in queries:
        for p in lookup.search(store, recall, q).get("passages", []):
            count += 1
            if p.get("page"):
                seen.add((p["document"], int(p["page"])))
    return seen, count


def evaluate(store: RagStore, aid: str, project: str, prompt: str, facts: Sequence[Dict[str, Any]], plans: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """`{methods: {name: {passages, chars?, facts: {id: bool}}}, reranked}`."""
    plans = plans or {}
    methods: Dict[str, Dict[str, Any]] = {}

    def fresh() -> ProjectRecall:
        return ProjectRecall(store, project, attached=[aid])

    old = fresh()
    old.queries = lambda goal, ids: [goal.strip()]                          # the whole request as one query, as before
    block = old.message(prompt)
    seen, n = injected(block)
    methods["turn start, whole request"] = {"passages": n, "chars": len(block or ""), "facts": reached(facts, seen)}
    split = fresh()
    block = split.message(prompt)
    seen, n = injected(block)
    methods["turn start, request split by code"] = {"passages": n, "chars": len(block or ""), "facts": reached(facts, seen), "questions": split.queries(prompt, [aid])[:-1]}
    if plans.get("questions"):
        planned = fresh()
        planned.plan = lambda goal, documents: list(plans["questions"])
        block = planned.message(prompt)
        seen, n = injected(block)
        methods["turn start, questions from the session"] = {"passages": n, "chars": len(block or ""), "facts": reached(facts, seen), "questions": planned.queries(prompt, [aid])[:-1]}
    if plans.get("searches"):
        seen, n = searched(store, fresh(), plans["searches"])
        methods["knowledge_search, aspects"] = {"passages": n, "facts": reached(facts, seen)}
    if plans.get("oracle"):
        seen, n = searched(store, fresh(), list(plans["oracle"].values()))
        methods["knowledge_search, worded on the fact"] = {"passages": n, "facts": reached(facts, seen)}
    probe = store.search([aid], "x" if not prompt.strip() else prompt[:200], top_k=1, strict=False)
    return {"methods": methods, "reranked": bool(probe.get("reranked")), "mode": probe.get("mode"), "note": probe.get("note", "")}


def table(report: Dict[str, Any], facts: Sequence[Dict[str, Any]]) -> str:
    names = list(report["methods"])
    lines = [f"search mode {report['mode']}, reranker {'on' if report['reranked'] else 'off'}" + (f" ({report['note']})" if report["note"] else "")]
    lines.append(f"{'fact':<6}" + "".join(f"{i + 1:>4}" for i in range(len(names))))
    for f in facts:
        lines.append(f"{f['id']:<6}" + "".join(f"{'yes' if report['methods'][n]['facts'].get(f['id']) else '-':>4}" for n in names))
    lines.append(f"{'found':<6}" + "".join(f"{sum(report['methods'][n]['facts'].values()):>4}" for n in names) + f"  of {len(facts)}")
    for i, n in enumerate(names):
        m = report["methods"][n]
        lines.append(f"  {i + 1}. {n}: {m['passages']} passages" + (f", {m['chars']} characters" if "chars" in m else ""))
        for q in m.get("questions", []):
            lines.append(f"       - {q}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--archive", required=True)
    ap.add_argument("--prompt", required=True, type=Path)
    ap.add_argument("--facts", required=True, type=Path)
    ap.add_argument("--plans", type=Path)
    ap.add_argument("--rag-dir", type=Path)
    ap.add_argument("--url", default="http://127.0.0.1:7232", help="the server that holds the embedder")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args(argv)
    rag_dir = args.rag_dir
    if rag_dir is None:
        from serve import userdata
        rag_dir = userdata.path("rag")
    store = RagStore(rag_dir)
    from tadamoo.rag.remote import server_search
    store.remote = server_search(args.url)
    project = store.load(args.archive).meta.get("project", "")
    facts = json.loads(args.facts.read_text(encoding="utf-8"))
    plans = json.loads(args.plans.read_text(encoding="utf-8")) if args.plans else None
    report = evaluate(store, args.archive, project, args.prompt.read_text(encoding="utf-8").strip(), facts, plans)
    print(table(report, facts))
    if args.json:
        args.json.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
