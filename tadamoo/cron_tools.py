"""cron_list, cron_create, cron_update, cron_pause, cron_resume, cron_delete and cron_fire_now: a conversation's own schedules
(tadamoo/scheduler.py, through the server's /api/routines). A schedule fires in Automatic, on its own: `cron_create` and the tools
that change a schedule already in place follow the same permission as any other change the conversation makes (asked below Auto,
allowed at it); what a fire itself delivers is a cron: message, read as data unless the receiving conversation was told this one
may give it orders (tadamoo/provenance.py).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from tadamoo.integrations._http import ServiceError

NAME_MAX = 80


def _failure(exc: Exception) -> str:
    if isinstance(exc, ServiceError):
        return f"Could not reach the schedule service ({exc.kind}): {exc.detail or exc}"
    return f"Error: {exc}"


def _line(r: Dict[str, Any]) -> str:
    deliver = r.get("deliver_to") or {"kind": "new_session", "ref": ""}
    where = "a new conversation" if deliver["kind"] == "new_session" else f"{deliver['kind']} '{deliver['ref']}'"
    state = "on" if r.get("enabled") else "off"
    return f"{r['id']} \"{r['name']}\" [{state}] {r['schedule']} -> {where}"


def register_cron_tools(registry: Any, client: Any) -> None:
    from tadamoo.tools.registry import EffectClass

    def cron_list() -> str:
        try:
            routines = client.list()
        except Exception as exc:
            return _failure(exc)
        return "\n".join(_line(r) for r in routines) if routines else "There are no schedules."

    def cron_create(name: str, schedule: str, workspace: str, prompt: str = "", deliver_kind: str = "new_session", deliver_ref: str = "",
                    message: str = "", ttl_min: Optional[int] = None, dedup_key: str = "") -> str:
        if not isinstance(name, str) or not name.strip() or len(name) > NAME_MAX:
            return f"Give the schedule a name of at most {NAME_MAX} characters."
        if deliver_kind != "new_session" and not (isinstance(deliver_ref, str) and deliver_ref.strip()):
            return f"Name the conversation this schedule delivers to (deliver_ref), or use deliver_kind=new_session."
        if not prompt and not message:
            return "Write what it should do (prompt), or what it sends (message)."
        fields = {"name": name, "schedule": schedule, "workspace": workspace, "prompt": prompt or name,
                  "deliver_to": {"kind": deliver_kind, "ref": deliver_ref}, "message": message}
        if ttl_min is not None:
            fields["ttl_min"] = ttl_min
        if dedup_key:
            fields["dedup_key"] = dedup_key
        try:
            made = client.create(fields)
        except Exception as exc:
            return _failure(exc)
        return f"Created {made['routine']['id']}: {_line(made['routine'])}"

    def cron_update(id: str, **fields: Any) -> str:
        try:
            changed = client.act(id, "update", fields)
        except Exception as exc:
            return _failure(exc)
        return _line(changed["routine"])

    def cron_pause(id: str) -> str:
        try:
            return _line(client.act(id, "disable")["routine"])
        except Exception as exc:
            return _failure(exc)

    def cron_resume(id: str) -> str:
        try:
            return _line(client.act(id, "enable")["routine"])
        except Exception as exc:
            return _failure(exc)

    def cron_delete(id: str) -> str:
        try:
            client.act(id, "remove")
        except Exception as exc:
            return _failure(exc)
        return f"Removed {id}."

    def cron_fire_now(id: str) -> str:
        try:
            return f"Queued {id}: {_line(client.act(id, 'run')['routine'])}"
        except Exception as exc:
            return _failure(exc)

    obj = lambda props, req: {"type": "object", "properties": props, "required": req}
    registry.register("cron_list", "Lists this PC's schedules: id, name, on or off, when it fires, and what it fires into.",
                      obj({}, []), EffectClass.PURE, cron_list)
    registry.register("cron_create", "Makes a new schedule. 'schedule' is 'every 2h', 'daily 08:30', a cron expression ('cron 50 14 * * 1-5') "
                       "or a single moment ('at 2026-12-24 18:00'). deliver_kind is 'new_session' (default: starts a conversation and runs "
                       "'prompt' in it) or 'session' (sends 'message', or 'prompt' if 'message' is empty, to the conversation named by "
                       "deliver_ref). ttl_min: a fire later than this many minutes is skipped instead of run late.",
                      obj({"name": {"type": "string"}, "schedule": {"type": "string"}, "workspace": {"type": "string"},
                           "prompt": {"type": "string", "description": "What a new conversation does, or a label when delivering a message"},
                           "deliver_kind": {"type": "string", "enum": ["new_session", "session"]},
                           "deliver_ref": {"type": "string", "description": "The conversation's name or id, with deliver_kind=session"},
                           "message": {"type": "string", "description": "What is sent, with deliver_kind=session; may use {date} and other names"},
                           "ttl_min": {"type": "integer"}, "dedup_key": {"type": "string"}},
                          ["name", "schedule", "workspace"]), EffectClass.REVERSIBLE, cron_create)
    registry.register("cron_update", "Changes a schedule's fields (by id); only the ones given are changed.",
                      obj({"id": {"type": "string"}}, ["id"]), EffectClass.REVERSIBLE, cron_update)
    registry.register("cron_pause", "Turns a schedule off: it stops firing on its own until cron_resume.",
                      obj({"id": {"type": "string"}}, ["id"]), EffectClass.REVERSIBLE, cron_pause)
    registry.register("cron_resume", "Turns a schedule back on.", obj({"id": {"type": "string"}}, ["id"]), EffectClass.REVERSIBLE, cron_resume)
    registry.register("cron_delete", "Removes a schedule for good.", obj({"id": {"type": "string"}}, ["id"]), EffectClass.REVERSIBLE, cron_delete)
    registry.register("cron_fire_now", "Fires a schedule at once, whether or not it is due.", obj({"id": {"type": "string"}}, ["id"]),
                      EffectClass.REVERSIBLE, cron_fire_now)
