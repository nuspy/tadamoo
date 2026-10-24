"""Judges: a short typed answer to a question, from the loaded model, from another model, or from Jev.

`Judge.choose(question, options, context)` returns one of the options, or None when it cannot tell. Who answers depends on the
provider the judging function is pointed at (`harness/llm/functions.py`):

* no provider, or a provider that chats: the model is asked for one word (a prompt with the options and the context);
* **Jev** (TypeSafe AI), which does not chat but answers typed questions: a *choice* question with the options as criteria.
  Only a short excerpt of the context goes to it (never a whole document or page: it is billed by the token it reads), the
  key comes from the credential manager, the function must have the person's yes when its data is personal, and an answer is
  counted against the provider's daily cap.

A judge that fails or does not know returns None; it never raises, so what it judges falls back to its own default.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, Optional

import requests

from harness.llm import functions
from harness.llm.client import RoutedClient
from harness.llm.store import Ledger, SecureRegistry
from harness.llmswitch.providers import TYPES

EXCERPT_CHARS = 1500                                # the most of the context that goes to a service billed by what it reads
QUESTION_CHARS = 600
JEV_PATH = "/systemone"
JEV_MODEL = "jev-latest"


class Judge:
    def __init__(self, function: str, registry: SecureRegistry, local: Any, ledger: Optional[Ledger] = None,
                 session: Optional[requests.Session] = None) -> None:
        if functions.spec(function)["kind"] != "judge":
            raise ValueError(f"{function} is not a judging function")
        self.function, self.registry, self.local = function, registry, local
        self.ledger = ledger or Ledger()
        self.http = session or requests.Session()
        self.last_source = ""                        # "model", "jev" or "" (it did not answer): for the page and the audit

    def choose(self, question: str, options: Dict[str, str], context: str = "", timeout: float = 20.0) -> Optional[str]:
        """One of `options` (name -> what it means), or None."""
        self.last_source = ""
        pid = (self.registry.load().get("assignments") or {}).get(self.function) or None
        ok, _ = self.registry.check(self.function, pid)
        if not ok:
            pid = None                                # not allowed: the loaded model judges instead, and nothing leaves
        prov = self.registry._find(self.registry.load(), pid) if pid else None
        try:
            if prov and TYPES.get(prov.get("type") or "", {}).get("api") == "judge":
                answer = self._jev(prov, question, options, context, timeout)
                if answer is not None:
                    self.last_source = "jev"
                    return answer
                return self._model(self.local, question, options, context, timeout)      # Jev said nothing: the loaded model, as local or more
            return self._model(RoutedClient(self.function, self.registry, self.local, self.ledger), question, options, context, timeout)
        except Exception:
            return None

    # -- a model answers with a word ---------------------------------------------------------------------

    def _model(self, client: Any, question: str, options: Dict[str, str], context: str, timeout: float) -> Optional[str]:
        listing = "\n".join(f"{name} - {meaning}" for name, meaning in options.items())
        system = f"{question[:QUESTION_CHARS]}\nAnswer with exactly one of these words and nothing else:\n{listing}"
        try:
            reply = client.chat_completion(messages=[{"role": "system", "content": system}, {"role": "user", "content": context[:4000]}],
                                           temperature=0.0, max_tokens=8, timeout=timeout, extra={"reasoning_effort": "none"})
            word = (reply["choices"][0]["message"].get("content") or "").strip().lower()
        except Exception:
            return None
        found = re.match(r"[a-z_]+", word)
        names = {n.lower(): n for n in options}
        if found and found.group(0) in names:
            self.last_source = "model"
            return names[found.group(0)]
        return None

    # -- Jev answers a typed question --------------------------------------------------------------------

    def _jev(self, prov: Dict[str, Any], question: str, options: Dict[str, str], context: str, timeout: float) -> Optional[str]:
        capped = self.ledger.over_cap(prov)
        key = (prov.get("api_key") or "").strip()
        base = (prov.get("base_url") or TYPES["jev"]["base_url"]).rstrip("/")
        if capped or not key or not base.startswith("https://"):
            return None
        payload = {"model": prov.get("model") or JEV_MODEL,
                   "state": {"excerpt": context[:EXCERPT_CHARS]},
                   "questions": {"answer": {"type": "choice", "instructions": question[:QUESTION_CHARS], "criteria": dict(options)}}}
        try:
            resp = self.http.post(base + JEV_PATH, data=json.dumps(payload), timeout=timeout, allow_redirects=False,
                                  headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            if resp.status_code != 200:
                return None
            data = resp.json()
        except (requests.RequestException, ValueError):
            return None
        usage = data.get("usage") or {}
        self.ledger.record(prov["id"], int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0)))
        choice = ((data.get("answers") or {}).get("answer") or {}).get("choice")
        return choice if choice in options else None


def router_judge(judge: Judge) -> Callable[[str, Dict[str, Any]], Optional[str]]:
    """The judge of `harness.router.Router`: steer, queue or urgent, from the new message and what the agent is doing."""
    options = {"steer": "the message changes, corrects, adds to or asks about the task in hand",
               "queue": "a different request that can wait until the task in hand is done",
               "urgent": "a different request that must be done right now, before going on"}

    def decide(text: str, context: Dict[str, Any]) -> Optional[str]:
        brief = (f"Task in hand: {context.get('task', '')[:400]}\nWhat the assistant has done so far: {context.get('progress', '')[:400]}\n"
                 f"New message: {text[:600]}")
        return judge.choose("A new message came while an assistant works on a task. Where does it belong? When in doubt, steer.", options, brief)
    return decide
