"""The one list of what is missing from the person: the questions that wait, the tasks and the decisions, from every place that asks.

Sources (all existing, read when the list is asked for, nothing here keeps a copy of them):

* `session`   a conversation of the agent: a confirmation (`ask`), a question left for later (`pending`) or a card of one question;
* `plan`      what a plan asks during a run: an approval, a value, a task (`PlanService.human_requests`);
* `proposal`  a change of a plan that waits for the person to approve or refuse it;
* `routine`   a request a scheduled routine left for the person (only to be removed from the list);
* `approval`  a call of an unattended run that waits on the board for the person's yes (security authorizations).

An entry is `{id: "<source>:<reference>", source, title, context, text, kind, options: [{value, label, label_en}], free, free_hint, created_at, notified, details, open}`.
`kind` is `confirm` (two answers), `choice`, `text`, `task`, or `card` (a card of several questions: answered in its conversation, see `open`). Only entries that
are still open are listed; one that was answered anywhere disappears. `POST answer` sends the person's answer to the source; the first answer wins, a second is refused with a
clear error. `notified` is one flag for each channel (web, desktop, android): a client that shows a notification says so with `mark_notified`, and the entry is not
announced again on that channel.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

CHANNELS = ("web", "desktop", "android")
TEXT_MAX = 4000
YES = {"value": "yes", "label": "Sì", "label_en": "Yes"}
NO = {"value": "no", "label": "No", "label_en": "No"}
APPROVE = {"value": "approve", "label": "Approva", "label_en": "Approve"}
REFUSE = {"value": "reject", "label": "Rifiuta", "label_en": "Refuse"}
ALLOW = {"value": "approve", "label": "Consenti", "label_en": "Allow"}
DENY = {"value": "deny", "label": "Nega", "label_en": "Deny"}
DONE = {"value": "done", "label": "Fatto", "label_en": "Done"}
REMOVE = {"value": "dismiss", "label": "Togli dalla lista", "label_en": "Remove from the list"}


class ActionError(ValueError):
    """An answer that cannot be taken, with a message fit for the person; `code` is `gone` for an entry that is not waiting any more (it was answered elsewhere)."""

    def __init__(self, message: str, code: str = "bad_answer") -> None:
        super().__init__(message)
        self.code = code


def _call(x: Any) -> Any:
    return x() if callable(x) else x


def _clip(text: Any, n: int = TEXT_MAX) -> str:
    return str(text or "").strip()[:n]


def _opts(rows: Any) -> List[Dict[str, str]]:
    out = []
    for o in rows or []:
        if isinstance(o, dict):
            value = o.get("value", o.get("id"))
            label = o.get("label") or o.get("title") or value
            out.append({"value": str(value), "label": str(label), "label_en": str(o.get("label_en") or label)})
        elif o not in (None, ""):
            out.append({"value": str(o), "label": str(o), "label_en": str(o)})
    return out


class Actions:
    """`sessions`, `plans`, `routines` and `approvals` are the live objects or functions that return them (or None); a source that is not there adds nothing."""

    def __init__(self, sessions: Any = None, plans: Any = None, routines: Any = None, approvals: Any = None, path: Optional[Path] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._sessions, self._plans, self._routines, self._approvals = sessions, plans, routines, approvals
        self._path, self._clock = Path(path) if path else None, clock
        self._lock = threading.Lock()
        self._notified: Dict[str, Dict[str, float]] = self._load()
        self._first_seen: Dict[str, float] = {}

    # -- what is waiting ---------------------------------------------------------------------------------

    def _session_rows(self) -> List[Dict[str, Any]]:
        mgr = _call(self._sessions)
        if mgr is None:
            return []
        rows = []
        for rec in (mgr.view().get("sessions") or []):
            if rec.get("state") == "closed":
                continue
            sid = rec["id"]
            name = rec.get("alias") or rec.get("title") or sid
            ask = rec.get("ask")
            if isinstance(ask, dict) and ask.get("id"):
                rows.append(self._from_ask(f"session:{sid}:{ask['id']}", name, ask, rec.get("updated")))
            for item in rec.get("pending") or []:
                if isinstance(item, dict) and item.get("id"):
                    rows.append(self._from_ask(f"session:{sid}:{item['id']}", name, item, item.get("at") or rec.get("updated")))
            card = rec.get("question")
            if isinstance(card, dict) and card.get("questions") and card.get("id") and not ask:
                rows.append(self._from_card(f"session:{sid}:{card['id']}", sid, name, card, rec.get("updated")))
        return rows

    @staticmethod
    def _from_ask(aid: str, name: str, ask: Dict[str, Any], at: Any) -> Dict[str, Any]:
        options = _opts(ask.get("options"))
        free = bool(ask.get("free") or ask.get("free_prefix"))
        facts = ask.get("facts") if isinstance(ask.get("facts"), dict) else {}
        details = {k: ask.get(k) for k in ("facts", "tool", "arguments", "explain", "intro", "kind") if ask.get(k) not in (None, "", {}, [])}
        if facts.get("instructions") and "instructions" not in details:
            details["instructions"] = facts["instructions"]
        text = _clip(ask.get("explain") or ask.get("intro") or "")
        kind = "text" if (free and not options) else ("confirm" if len(options) == 2 else "choice")
        return {"id": aid, "source": "session", "title": _clip(ask.get("question") or ask.get("kind") or "?", 400), "context": name, "text": text, "kind": kind, "options": options,
                "free": free, "free_hint": "code" if ask.get("free") else "text" if free else "", "free_prefix": ask.get("free_prefix") or "",
                "created_at": float(at or 0), "details": details}

    @staticmethod
    def _from_card(aid: str, sid: str, name: str, card: Dict[str, Any], at: Any) -> Dict[str, Any]:
        qs = card.get("questions") or []
        row = {"id": aid, "source": "session", "title": _clip((qs[0].get("question") if qs else "") or card.get("intro") or "?", 400), "context": name, "text": _clip(card.get("intro") or ""),
               "kind": "card", "options": [], "free": False, "free_hint": "", "free_prefix": "", "created_at": float(at or 0), "details": {}, "open": {"session": sid}}
        if len(qs) == 1 and qs[0].get("kind") in ("single", "confirm", "free"):
            q = qs[0]
            row.update({"kind": "text" if q["kind"] == "free" else "confirm" if len(q.get("options") or []) == 2 else "choice", "options": _opts(q.get("options")), "free": q["kind"] == "free",
                        "free_hint": "text", "card": {"question": q.get("id"), "single": q["kind"] != "free"}})
            row.pop("open")
        return row

    def _plan_rows(self) -> List[Dict[str, Any]]:
        service = _call(self._plans)
        if service is None:
            return []
        rows = []
        for r in service.human_requests():
            ask = r.get("ask")
            base = {"id": f"plan:{r['plan_id']}:{r['node_id']}", "source": "plan", "context": r.get("plan_title", r["plan_id"]), "created_at": float(r.get("since") or 0),
                    "title": _clip(r.get("prompt") or r.get("prompt_en") or r.get("title"), 400), "title_en": _clip(r.get("prompt_en") or "", 400), "text": _clip("\n".join(
                        x for x in (r.get("help"), r.get("content")) if x)), "free_prefix": "", "details": {"plan_id": r["plan_id"], "node_id": r["node_id"], "group": r.get("group", "")}}
            if ask == "approve":
                rows.append({**base, "kind": "confirm", "options": [YES, NO], "free": False, "free_hint": ""})
            elif ask == "task":
                rows.append({**base, "kind": "task", "options": [DONE], "free": True, "free_hint": "text"})
            else:
                kind, choices = r.get("kind"), r.get("choices") or []
                if kind == "choice":
                    rows.append({**base, "kind": "confirm" if len(choices) == 2 else "choice", "options": _opts(choices), "free": bool(r.get("other")), "free_hint": "text",
                                 "proposed": r.get("proposed")})
                else:
                    rows.append({**base, "kind": "text", "options": [], "free": True, "free_hint": "number" if kind == "number" else "text", "proposed": r.get("proposed")})
        return rows

    def _proposal_rows(self) -> List[Dict[str, Any]]:
        service = _call(self._plans)
        if service is None:
            return []
        rows = []
        for plan in service.list():
            try:
                bridge = service.bridge(plan["plan_id"])
            except Exception:
                continue
            for p in list(bridge.proposals.values()):
                if p.status != "pending":
                    continue
                d = p.to_dict()
                changes = "; ".join(f"{k}: {len(v)}" for k, v in (d.get("changes") or {}).items() if v)
                rows.append({"id": f"proposal:{plan['plan_id']}:{p.proposal_id}", "source": "proposal", "title": _clip(p.title, 400), "context": plan.get("title") or plan["plan_id"],
                             "text": _clip("\n".join(x for x in (p.rationale, changes) if x)), "kind": "confirm", "options": [APPROVE, REFUSE], "free": False, "free_hint": "", "free_prefix": "",
                             "created_at": float(p.created_at), "details": {"plan_id": plan["plan_id"], "proposal_id": p.proposal_id, "changes": d.get("changes"), "involves": p.involves}})
        return rows

    def _routine_rows(self, taken: set) -> List[Dict[str, Any]]:
        scheduler = _call(self._routines)
        if scheduler is None:
            return []
        rows = []
        for p in scheduler.pending():
            if p.get("key") and any(p["key"] == f"{sid}:{aid}" for sid, aid in taken):          # the same question is already listed from its conversation
                continue
            rows.append({"id": f"routine:{p['id']}", "source": "routine", "title": _clip(p.get("question") or p.get("kind"), 400), "context": p.get("name", ""),
                         "text": _clip(", ".join(p.get("options") or [])), "kind": "confirm", "options": [REMOVE], "free": False, "free_hint": "", "free_prefix": "",
                         "created_at": float(p.get("created") or 0), "details": {"routine": p.get("routine"), "kind": p.get("kind"), "session": p.get("session")}})
        return rows

    def _approval_rows(self) -> List[Dict[str, Any]]:
        board = _call(self._approvals)
        if board is None:
            return []
        return [{"id": f"approval:{i['id']}", "source": "approval", "title": _clip(f"{i['tool']}" + (f" ({i['effect']})" if i.get("effect") else ""), 400), "context": i.get("run", ""),
                 "text": _clip(i.get("arguments")), "kind": "confirm", "options": [ALLOW, DENY], "free": False, "free_hint": "", "free_prefix": "", "created_at": float(i.get("created") or 0),
                 "details": {"tool": i["tool"], "arguments": i.get("arguments"), "effect": i.get("effect")}} for i in board.view()["pending"]]

    def open(self) -> List[Dict[str, Any]]:
        """Every entry that is waiting now, oldest first."""
        rows: List[Dict[str, Any]] = []
        for part in (self._session_rows, self._plan_rows, self._proposal_rows, self._approval_rows):
            try:
                rows.extend(part())
            except Exception:                                   # a source that cannot be read does not hide the others
                continue
        try:
            taken = {tuple(r["id"].split(":", 2)[1:]) for r in rows if r["source"] == "session"}
            rows.extend(self._routine_rows(taken))
        except Exception:
            pass
        now = self._clock()
        with self._lock:
            for r in rows:
                if not r.get("created_at"):
                    r["created_at"] = self._first_seen.setdefault(r["id"], now)
            self._first_seen = {k: v for k, v in self._first_seen.items() if k in {r["id"] for r in rows}}
        return sorted(rows, key=lambda r: (r["created_at"], r["id"]))

    def view(self, channel: str = "") -> Dict[str, Any]:
        rows = self.open()
        marked = self._notified.get(channel, {}) if channel in CHANNELS else {}
        for r in rows:
            r["notified"] = r["id"] in marked
        return {"actions": rows, "count": len(rows), "new": sum(1 for r in rows if not r["notified"])}

    def count(self) -> int:
        return len(self.open())

    # -- announced once ----------------------------------------------------------------------------------

    def _load(self) -> Dict[str, Dict[str, float]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8")) if self._path and self._path.is_file() else {}
        except (OSError, ValueError):
            data = {}
        return {c: {str(k): float(v) for k, v in (data.get(c) or {}).items()} for c in CHANNELS}

    def mark_notified(self, ids: Any, channel: str) -> Dict[str, Any]:
        """A client has shown these entries (a notification, a bubble, a dialog): they are not announced again on that channel. Entries that are not open any more are forgotten."""
        if channel not in CHANNELS:
            raise ActionError(f"the channel is one of {', '.join(CHANNELS)}", "bad_channel")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ActionError("send the ids as a list of texts", "bad_ids")
        live = {r["id"] for r in self.open()}
        with self._lock:
            marks = self._notified.setdefault(channel, {})
            now = self._clock()
            for i in ids:
                if i in live:
                    marks.setdefault(i, now)
            for gone in [k for k in marks if k not in live]:
                marks.pop(gone, None)
            if self._path:
                try:
                    self._path.parent.mkdir(parents=True, exist_ok=True)
                    self._path.write_text(json.dumps(self._notified), encoding="utf-8")
                except OSError:
                    pass
        return {"marked": [i for i in ids if i in live]}

    # -- the answer ---------------------------------------------------------------------------------------

    def answer(self, action_id: str, value: Any = "", text: Any = "") -> Dict[str, Any]:
        """Sends the person's answer to the source of an entry that is still waiting. `value` is one of its options; `text` is what the person writes when the entry takes it."""
        entry = next((r for r in self.open() if r["id"] == action_id), None)
        if entry is None:
            raise ActionError("That item is not waiting any more: it was answered somewhere else or it has ended.", "gone")
        value = "" if value is None else str(value).strip()
        text = _clip(text)
        values = [o["value"] for o in entry["options"]]
        if entry["kind"] == "card" and entry.get("open"):
            raise ActionError("This card has several questions: answer it in its conversation.", "open_in_session")
        if value and value not in values:
            if entry["free"] and not text:
                text, value = value, ""                          # a written answer sent as the value
            else:
                raise ActionError("That is not one of the answers: " + ", ".join(values), "bad_answer")
        if not value and not text:
            raise ActionError("Choose an answer or write one.", "bad_answer")
        if text and not entry["free"]:
            raise ActionError("This item does not take a written answer.", "bad_answer")
        try:
            return {"answered": action_id, **(self._send(entry, value, text) or {})}
        except ActionError:
            raise
        except KeyError:
            raise ActionError("That item is not waiting any more: it was answered somewhere else or it has ended.", "gone") from None
        except ValueError as exc:
            code = getattr(exc, "code", "")
            gone = code in ("not_asking", "gone") or "any more" in str(exc) or "not asking" in str(exc) or "already" in str(exc)
            raise ActionError(str(exc), "gone" if gone else "bad_answer") from None

    def _send(self, entry: Dict[str, Any], value: str, text: str) -> Optional[Dict[str, Any]]:
        source, ref = entry["source"], entry["id"].split(":", 2)[1:]
        if source == "session":
            sid, aid = ref
            mgr = _call(self._sessions)
            if entry.get("card"):
                picked = [value] if value else []
                body = {"status": "submitted", "answers": {entry["card"]["question"]: {"picked": picked, "text": text}}}
                mgr.answer(sid, aid, "ask:" + json.dumps(body, ensure_ascii=False))
            else:
                sent = value or text
                if not value and entry.get("free_prefix"):
                    sent = entry["free_prefix"] + text
                elif value and text and entry.get("free_prefix") and value == entry["free_prefix"]:
                    sent = entry["free_prefix"] + text
                mgr.answer(sid, aid, sent)
            return None
        if source == "plan":
            plan_id, node_id = ref
            node = entry["details"]
            if entry["kind"] == "task":
                answer: Any = {"done": True, "note": text}
            elif entry["kind"] == "confirm" and entry["options"] == [YES, NO]:
                answer = value == "yes"
            else:
                answer = value or text
            return self._event(plan_id, {"type": "plan:answer", "node_id": node_id, "answer": answer, "actor": "user"})
        if source == "proposal":
            plan_id, proposal_id = ref
            return self._event(plan_id, {"type": "plan:approve" if value == "approve" else "plan:reject", "proposal_id": proposal_id, "actor": "user"})
        if source == "routine":
            _call(self._routines).dismiss(ref[0])
            return None
        if source == "approval":
            _call(self._approvals).decide(ref[0], value == "approve")
            return None
        raise ActionError("unknown source", "bad_answer")

    def _event(self, plan_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        result = _call(self._plans).handle_event(plan_id, event)
        if not result.get("ok"):
            message = str(result.get("error") or "the plan did not take the answer")
            raise ActionError(message, "gone" if "not asking" in message or "answered already" in message or "not pending" in message else "bad_answer")
        return {}
