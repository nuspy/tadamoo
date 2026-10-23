"""Phone tools for the agent, on top of the callAPIcall client.

Placing a call always needs the user's confirmation, every time (the permission manager lists
`phone_call` as always-ask). Once a call is approved and running, saying a line, listening and
hanging up on that call need no new approval; they end with the call. What the other person says is
untrusted input: it is fenced as data and taints the session.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Set

from harness.permissions import PermissionManager
from harness.tools.registry import EffectClass, ToolRegistry

from .call_api_client import CallAPICallClient, build_greeting

FOLLOW_UPS = ("phone_say", "phone_listen", "phone_hangup")
MAX_PURPOSE = 300
MAX_LINE = 500


def register_phone_tools(
    registry: ToolRegistry,
    client: CallAPICallClient,
    permissions: Optional[PermissionManager] = None,
    on_behalf_of: str = "",
) -> None:
    active: Set[str] = set()
    heard_index: Dict[str, int] = {}

    def open_follow_ups() -> None:
        if permissions:
            for name in FOLLOW_UPS:
                if not any(r.pattern == name and r.action == "allow" for r in permissions.rules):
                    permissions.add_rule(name, "allow")

    def close_follow_ups() -> None:
        if permissions and not active:
            for name in FOLLOW_UPS:
                permissions.remove_rule(name, "allow")

    def known(call_id: str) -> str:
        if call_id not in active:
            raise ValueError(f"No active call '{call_id}' was placed in this session")
        return call_id

    def phone_call(number: str, purpose: str) -> Dict[str, Any]:
        if not purpose.strip() or len(purpose) > MAX_PURPOSE:
            raise ValueError(f"purpose must be 1 to {MAX_PURPOSE} characters")
        res = client.place_call(number, build_greeting(on_behalf_of, purpose))
        if res.get("status") != "calling" or not res.get("call_id"):
            raise RuntimeError(f"The call was not placed: {res.get('error') or res.get('status')}")
        active.add(res["call_id"])
        heard_index[res["call_id"]] = -1
        open_follow_ups()
        return {"call_id": res["call_id"], "status": "calling"}

    def phone_say(call_id: str, text: str) -> str:
        if not text.strip() or len(text) > MAX_LINE:
            raise ValueError(f"text must be 1 to {MAX_LINE} characters")
        if not client.say(known(call_id), text):
            raise RuntimeError("The line could not be queued; the call may have ended")
        return "queued"

    def phone_listen(call_id: str, timeout_s: float = 15.0) -> Dict[str, Any]:
        heard = client.heard(known(call_id), after=heard_index[call_id], timeout_s=min(max(timeout_s, 1.0), 45.0))
        if heard is None:
            raise RuntimeError("Nothing could be read from the call; it may have ended")
        heard_index[call_id] = int(heard.get("index", heard_index[call_id]))
        return {"text": heard.get("text"), "status": heard.get("status")}

    def phone_hangup(call_id: str) -> Dict[str, Any]:
        ok = client.hangup(known(call_id))
        transcript = client.get_call_transcript(call_id)
        active.discard(call_id)
        close_follow_ups()
        if not ok:
            raise RuntimeError("The hang-up request failed; check the call state")
        return {"status": "ended", "transcript": transcript}

    def spec(**props: Dict[str, Any]) -> Dict[str, Any]:
        return {"type": "object", "properties": props, "required": [k for k, v in props.items() if "default" not in v]}

    registry.register(
        "phone_call",
        "Places a real phone call. The call opens by saying it is an AI assistant calling on behalf of the user. "
        "Every call needs the user's confirmation.",
        spec(number={"type": "string", "description": "Phone number with country code"},
             purpose={"type": "string", "description": "Why you are calling, in one or two sentences"}),
        EffectClass.IRREVERSIBLE, phone_call)
    registry.register("phone_say", "Says one line to the person on an active call.",
                      spec(call_id={"type": "string"}, text={"type": "string"}), EffectClass.IRREVERSIBLE, phone_say)
    registry.register("phone_listen", "Waits for the next thing the other person says on an active call.",
                      spec(call_id={"type": "string"}, timeout_s={"type": "number", "default": 15.0}),
                      EffectClass.IRREVERSIBLE, phone_listen, untrusted=True)
    registry.register("phone_hangup", "Ends an active call and returns its transcript.",
                      spec(call_id={"type": "string"}), EffectClass.IRREVERSIBLE, phone_hangup)
