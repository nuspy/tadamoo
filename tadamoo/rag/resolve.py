"""Which knowledge archives apply to a function, in one place.

`resolve(store, context)` answers for every function that may draw on archives (the agent, a meeting, the copilot, a call, the avatar, a plan
step, a routine): the archives it may use, each with its name, and the archives left out with the reason. It never takes the caller's word for where
the model runs or who listens: a model call that leaves this PC is worked out from the providers registry (the caller may only make it stricter), and
a function whose output other people perceive (meeting, call) always has an audience of others; the private copilot never does.

The context is a dict: `function`, the ids it needs (`session`, `plan` and `node`, `routine`, `meeting`), `project` (a project key), `attached`
(archive ids), `routed_to_cloud`, `audience` (`me` or `others`), `granted` (archive ids the person agreed just now to have said to others)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

from tadamoo.rag.errors import RagError
from tadamoo.rag.project import is_key, project_key
from tadamoo.rag.store import RagStore, others_of

FUNCTIONS = ("agent", "meeting", "copilot", "call", "avatar", "plan_node", "routine")
ROUTE_OF = {"agent": "executor", "plan_node": "executor", "routine": "executor"}     # the llm function whose route the model call takes; the others use the loaded model
ALWAYS_OTHERS = ("meeting", "call")                                                   # what these produce is perceived by other people whatever the caller says
NEVER_OTHERS = ("copilot",)                                                           # shown only to the person
ALLOW, DENY, ASK_DIALOG, ASK_VOICE = "allow", "deny", "ask_dialog", "ask_voice"
WHY = {"cloud": "not cleared for cloud models", "others": "not cleared to be said, written or shown to other people", "missing": "no longer exists"}

Source = Callable[[Dict[str, Any]], Any]


@dataclass
class Resolution:
    """The answer: `archives` (each `{id, name, project, project_name, description, documents, cloud_ok, others_ok, verdict, via}`; the verdict is `allow`,
    or `ask_dialog` / `ask_voice` when the person must agree before it is said, written or shown to others, and in which way to ask) and `blocked`
    (`{id, name, project_name, codes, reason, verdict: "deny"}`: left out because of the cloud rule or because the archive says no to others)."""
    function: str
    project: str = ""
    routed_to_cloud: bool = False
    audience: str = "me"
    archives: List[Dict[str, Any]] = field(default_factory=list)
    blocked: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def ids(self) -> List[str]:
        """The archives to use now."""
        return [a["id"] for a in self.archives if a["verdict"] == ALLOW]

    @property
    def ask(self) -> List[str]:
        """The archives that wait for the person's yes (either way of asking)."""
        return [a["id"] for a in self.archives if a["verdict"] != ALLOW]

    @property
    def ask_dialog(self) -> List[str]:
        return [a["id"] for a in self.archives if a["verdict"] == ASK_DIALOG]

    @property
    def ask_voice(self) -> List[str]:
        return [a["id"] for a in self.archives if a["verdict"] == ASK_VOICE]

    def names(self, verdict: str = ALLOW) -> List[str]:
        return [a["name"] for a in self.archives if a["verdict"] == verdict]

    def to_dict(self) -> Dict[str, Any]:
        return {"function": self.function, "project": self.project, "routed_to_cloud": self.routed_to_cloud, "audience": self.audience,
                "archives": self.archives, "blocked": self.blocked, "ids": self.ids, "ask_dialog": self.ask_dialog, "ask_voice": self.ask_voice}


def routes_outside(function: str, registry: Any = None) -> bool:
    """Whether the model calls of `function` may leave the loaded model of this server. An error reading the registry counts as leaving."""
    route = ROUTE_OF.get(function)
    if route is None:
        return False
    try:
        from tadamoo import llm
        from tadamoo.llmswitch.providers import TYPES
        reg = registry or llm.registry()
        data = reg.load()
        return any(pid and not TYPES.get((reg._find(data, pid) or {}).get("type") or "", {}).get("this_server") for pid in reg.fallback_chain(route))
    except Exception:
        return True


def _ids(value: Any) -> List[str]:
    return [i for i in dict.fromkeys(value) if isinstance(i, str)] if isinstance(value, (list, tuple)) else []


def resolve(store: RagStore, context: Mapping[str, Any], sources: Optional[Mapping[str, Source]] = None,
            leaves_pc: Callable[[str], bool] = routes_outside) -> Resolution:
    """The archives that apply to `context` and those left out. `sources` maps what the store cannot know to a function of the context:
    `session` -> the session's record (its `workspace` and `knowledge`), `plan_node` -> the archive ids declared for a plan step, `routine` and `meeting`
    -> the ids kept on that record. `leaves_pc(function)` says whether the function's model calls leave this PC (the registry by default)."""
    sources = sources or {}
    function = context.get("function")
    if function not in FUNCTIONS:
        raise RagError("function: one of " + ", ".join(FUNCTIONS))
    project = context.get("project") if is_key(context.get("project")) else ""
    attached = _ids(context.get("attached"))
    record = sources["session"](dict(context)) if context.get("session") and "session" in sources else None
    if isinstance(record, dict):                                        # the server's record decides, not what the caller says about it
        project = project_key(record["workspace"]) if record.get("workspace") else project
        attached = _ids(record.get("knowledge"))
    cloud = bool(context.get("routed_to_cloud")) or bool(leaves_pc(function))
    audience = "others" if function in ALWAYS_OTHERS or (context.get("audience") == "others" and function not in NEVER_OTHERS) else "me"
    granted = set(_ids(context.get("granted")))
    out = Resolution(function, project, cloud, audience)

    wanted: Dict[str, str] = {}                                         # id -> where it came from
    if project:
        for aid in store.ids(project):
            wanted.setdefault(aid, "project")
    for aid in attached:
        wanted.setdefault(aid, "attached")
    declared = sources.get(function)
    if declared is not None and function != "agent":
        for aid in _ids(declared(dict(context))):
            wanted.setdefault(aid, function)

    for aid, via in wanted.items():
        try:
            meta = store.meta(aid)
        except RagError:
            out.blocked.append({"id": aid, "name": aid, "project": "", "project_name": "", "codes": ["missing"], "reason": WHY["missing"], "verdict": DENY})
            continue
        row = {"id": aid, "name": meta["name"], "project": meta.get("project", ""), "project_name": meta.get("project_name", ""), "description": meta.get("description", ""),
               "documents": len(meta["documents"]), "cloud_ok": bool(meta.get("cloud_ok")), "others_ok": others_of(meta), "via": via}
        codes = []
        if cloud and not row["cloud_ok"]:
            codes.append("cloud")
        if audience == "others" and row["others_ok"] == DENY:
            codes.append("others")
        if codes:
            out.blocked.append({k: row[k] for k in ("id", "name", "project", "project_name")} | {"codes": codes, "reason": "; ".join(WHY[c] for c in codes), "verdict": DENY})
        else:
            ask = audience == "others" and row["others_ok"] in (ASK_DIALOG, ASK_VOICE) and aid not in granted
            out.archives.append({**row, "verdict": row["others_ok"] if ask else ALLOW})
    return out


def describe(res: Resolution) -> str:
    """The archives of a resolution in a few words for the model or the person: names first (with the project when it is not the function's own), then those
    left out and why. Ids never appear."""
    def label(a: Dict[str, Any]) -> str:
        where = a.get("project_name") or ""
        return a["name"] + (f" ({where})" if where and a.get("via") != "project" else "")
    parts = [label(a) for a in res.archives]
    left = [f"{b['name']} ({b['reason']})" for b in res.blocked if "missing" not in b["codes"]]
    return "; ".join(parts) + (("; " if parts else "") + "not available now: " + "; ".join(left) if left else "")
