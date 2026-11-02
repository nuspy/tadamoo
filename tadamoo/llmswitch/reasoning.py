"""Quanto ragiona un modello, e come glielo si dice: il **punto unico** in cui un livello diventa parametri.

Derivato dal modulo dei fornitori di TAROTS (`tarot_core/llm/ragionamento.py`), dove è nato da guasti veri: un
compito che doveva rispondere in fretta ragionava per migliaia di token perché il fornitore aveva il ragionamento
acceso, e un altro mandava insieme `reasoning_effort` ed `enable_thinking`, con il server che ne rispettava uno a caso.

**I livelli** sono quelli che Nebius Token Factory documenta per `reasoning_effort`: `none`, `minimal`, `low`,
`medium`, `high`, `xhigh`, `max`. In più `inherit`: per una richiesta vuol dire «quello del fornitore», per un
fornitore «non dire niente al server» (decide il suo predefinito).

**Il dialetto** dipende dal tipo di fornitore, non dal modello:

- `effort` — OpenAI e gli OpenAI-compatibili (Nebius, LM Studio, vLLM, llama.cpp, DeepSeek, Gemini sul suo endpoint
  OpenAI…): `reasoning_effort: <livello>`. LM Studio rispetta solo `none` (verificato a ottobre 2026): con gli altri
  livelli ragiona come sa;
- `thinking` — NVIDIA NIM: `chat_template_kwargs.enable_thinking` e un `reasoning_budget` proporzionale al livello;
- `anthropic` — Claude: `thinking: {type: enabled, budget_tokens: N}`, proporzionale al livello; con `none` niente;
- `none` — chi non ha parametri per il ragionamento (i CLI ad abbonamento, Cloud Code): non si manda niente.

**Mai parametri in conflitto.** `params_for` toglie sempre tutte le chiavi del ragionamento che i parametri di base
avevano (`reasoning_effort`, `reasoning_budget`, `chat_template_kwargs.enable_thinking`, `thinking`) e scrive solo
quelle del dialetto.

**Il tetto.** Il ragionamento consuma lo stesso `max_tokens` della risposta: con un livello acceso il tetto da
mandare è quello visibile più un margine (`max_tokens_for`), altrimenti il modello pensa fino al tetto e la risposta
resta vuota.

**Dal vecchio booleano**: una voce del registro con `ragiona` / `reasoning` / `thinking` booleano (prima dei livelli)
diventa `medium` (vero) o `none` (falso) — `from_bool`, applicato da `Registry.load`.
"""
from __future__ import annotations

from typing import Any

INHERIT = "inherit"
LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
#: Le scelte di un menu: «eredita» e i sette livelli, in ordine.
CHOICES = (INHERIT, *LEVELS)

#: Come si presentano in un menu. Le app con le loro lingue possono usare solo `value`.
LABELS: dict[str, dict[str, str]] = {
    "it": {
        INHERIT: "Eredita dal fornitore", "none": "Nessuno", "minimal": "Minimo", "low": "Basso",
        "medium": "Medio", "high": "Alto", "xhigh": "Molto alto", "max": "Massimo",
    },
    "en": {
        INHERIT: "Inherit from provider", "none": "None", "minimal": "Minimal", "low": "Low",
        "medium": "Medium", "high": "High", "xhigh": "Extra high", "max": "Max",
    },
}

#: Il budget per i dialetti che vogliono un numero (NIM, Anthropic): raddoppia a ogni livello, da 512 a 32 000.
BUDGET: dict[str, int] = {
    "none": 0, "minimal": 512, "low": 1024, "medium": 4096, "high": 8192, "xhigh": 16384, "max": 32000,
}
#: Il budget minimo che Anthropic accetta per il thinking.
BUDGET_MIN_ANTHROPIC = 1024

#: Il margine da aggiungere al tetto visibile quando il livello è acceso (`max_tokens_for`). Misurato su Nebius
#: (Nemotron 3.5 Lightning): fino a ~8 000 token di ragionamento su risposte brevi e ~14 000 su una lunga.
MARGIN: dict[str, int] = {
    "none": 0, "minimal": 2048, "low": 4096, "medium": 8192, "high": 16384, "xhigh": 24576, "max": 32768,
}

EFFORT, THINKING, ANTHROPIC, NO_DIALECT = "effort", "thinking", "anthropic", "none"

#: I tipi che vogliono `enable_thinking` + `reasoning_budget` invece di `reasoning_effort`.
THINKING_TYPES = frozenset({"nvidia"})
#: Le API senza parametri per il ragionamento.
NO_REASONING_APIS = frozenset({"cli", "cloudcode"})

#: Le vecchie chiavi booleane di una voce del registro, prima dei livelli.
LEGACY_BOOL_KEYS = ("ragiona", "reasoning", "thinking")


def valid(level: Any, *, allow_inherit: bool = True) -> bool:
    """Se `level` è una scelta ammessa (con o senza `inherit`)."""
    return isinstance(level, str) and (level in LEVELS or (allow_inherit and level == INHERIT))


def normalize(level: Any) -> str:
    """Il livello pulito: una scelta ammessa, oppure `inherit` (vuoto, `None`, sconosciuto)."""
    if isinstance(level, str):
        level = level.strip().lower()
    return level if valid(level) else INHERIT


def from_bool(on: bool) -> str:
    """Il vecchio booleano «ragiona»: acceso → `medium`, spento → `none`."""
    return "medium" if on else "none"


def is_on(level: str | None) -> bool:
    return bool(level) and level not in ("none", INHERIT)


def dialect(provider_type: str, api: str = "") -> str:
    """Il dialetto del ragionamento per un tipo di fornitore (`TYPES[tipo]["api"]` come `api`)."""
    if api in NO_REASONING_APIS:
        return NO_DIALECT
    if api == "anthropic" or provider_type == "claude":
        return ANTHROPIC
    if provider_type in THINKING_TYPES:
        return THINKING
    return EFFORT


def resolve(provider_level: Any, request_level: Any = INHERIT) -> str | None:
    """Il livello che vale: quello della richiesta, se non eredita; altrimenti quello del fornitore; `None` se anche
    il fornitore eredita (non si manda nulla: decide il server)."""
    for level in (normalize(request_level), normalize(provider_level)):
        if level != INHERIT:
            return level
    return None


_REASONING_KEYS = ("reasoning_effort", "reasoning_budget", "thinking")


def clean(params: dict | None) -> dict:
    """I parametri senza nessuna chiave del ragionamento (gli altri restano)."""
    out = {k: v for k, v in (params or {}).items() if k not in _REASONING_KEYS}
    kwargs = out.get("chat_template_kwargs")
    if isinstance(kwargs, dict):
        rest = {k: v for k, v in kwargs.items() if k != "enable_thinking"}
        if rest:
            out["chat_template_kwargs"] = rest
        else:
            out.pop("chat_template_kwargs")
    return out


def params_for(level: str | None, dialect_: str, base: dict | None = None) -> dict:
    """I parametri della richiesta con il ragionamento al `level` scelto, nel dialetto giusto e senza conflitti.

    `level=None` (tutto eredita): i parametri di base restano come sono — non si aggiunge e non si toglie niente.
    """
    if level is None:
        return dict(base or {})
    if not valid(level, allow_inherit=False):
        raise ValueError(f"livello di ragionamento sconosciuto: {level!r}")
    out = clean(base)
    if dialect_ == EFFORT:
        out["reasoning_effort"] = level
    elif dialect_ == THINKING:
        on = is_on(level)
        kwargs = dict(out.get("chat_template_kwargs") or {})
        out["chat_template_kwargs"] = {**kwargs, "enable_thinking": on}
        if on:
            out["reasoning_budget"] = BUDGET[level]
    elif dialect_ == ANTHROPIC:
        if is_on(level):
            out["thinking"] = {"type": "enabled", "budget_tokens": max(BUDGET_MIN_ANTHROPIC, BUDGET[level])}
    return out


def max_tokens_for(visible: int, level: str | None, params: dict | None = None,
                   margins: dict[str, int] | None = None) -> int:
    """Il `max_tokens` da mandare: il tetto visibile più il margine del livello (almeno il budget dichiarato nei
    parametri, perché il pensiero chiesto ci stia tutto). Livello spento o assente: il tetto visibile."""
    if not is_on(level):
        return visible
    margin = (margins or {}).get(level, MARGIN[level])
    params = params or {}
    budget = params.get("reasoning_budget")
    thinking = params.get("thinking")
    if isinstance(thinking, dict) and isinstance(thinking.get("budget_tokens"), int):
        budget = thinking["budget_tokens"]
    if isinstance(budget, int) and not isinstance(budget, bool) and budget > margin:
        margin = budget
    return visible + margin


def choices(lang: str = "it", *, inherit: bool = True) -> list[dict[str, str]]:
    """Le scelte per un menu a tendina: `[{value, label}, …]`, con o senza «eredita»."""
    labels = LABELS.get(lang) or LABELS["en"]
    return [{"value": v, "label": labels[v]} for v in (CHOICES if inherit else LEVELS)]
