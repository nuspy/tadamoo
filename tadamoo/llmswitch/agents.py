"""CLI-agenti: far scrivere un testo allo strumento ufficiale di un abbonamento.

Claude Code, Codex e Antigravity non espongono un endpoint: si lanciano come programmi e si
legge quello che stampano. È l'unico modo conforme di far pagare le chiamate a un abbonamento —
il client ufficiale è autorizzato, un'applicazione terza con le stesse credenziali no.

Vanno usati sapendo cosa sono. **Non sono modelli, sono agenti**: ragionano, usano strumenti,
fanno più giri prima di rispondere. Misurato su questa macchina, con «rispondi solo ok»:

    claude -p            4,2 s     risposta pulita
    agy -p               10,1 s    risposta pulita
    codex exec           16,3 s    14.237 token bruciati per due lettere

Per una domanda ogni tanto vanno benissimo. Per un lavoro che ne fa centinaia — tradurre un
libro, per dire — quell'attesa fissa si moltiplica e diventa ore prima ancora di generare una
parola: lì conviene un modello vero, locale o a consumo.

Ogni agente gira in una **cartella vuota**, non in quella del progetto: leggono i file che si
trovano intorno per farsi un'idea del contesto, e per scrivere un testo non serve — che vedano
il codice di chi li chiama è un effetto collaterale, non una funzione.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("llmswitch.agents")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # niente console su Windows

#: Attesa massima per una risposta. Sono agenti: dieci secondi sono la norma, non l'eccezione.
TIMEOUT_DEFAULT_S = 300.0

#: Id che `modelli()` restituisce quando un provider non ha un modello scelto. Non è un modello
#: reale: non va mai passato come `--model`, o l'agente lo rifiuta.
MODELLO_SEGNAPOSTO = "default"


class AgentError(RuntimeError):
    """L'agente non ha prodotto una risposta utilizzabile."""


@dataclass(frozen=True)
class Agente:
    id: str
    label: str
    #: Eseguibile e argomenti fissi.
    comando: tuple[str, ...]
    #: Come riceve il testo: da ``stdin`` o come ultimo ``argomento``.
    prompt_come: str = "stdin"
    #: Dove lascia la risposta: su ``stdout`` o in un ``file`` indicato da ``opzione_uscita``.
    risposta_da: str = "stdout"
    opzione_uscita: str = ""
    opzione_modello: str = ""
    elenco_modelli: tuple[str, ...] = ()
    creds: str = ""
    note: str = ""


AGENTI: dict[str, Agente] = {
    "claude_cli": Agente(
        id="claude_cli",
        label="Claude via Claude Code (abbonamento)",
        comando=("claude", "-p"),
        prompt_come="stdin",
        opzione_modello="--model",
        creds="~/.claude/.credentials.json",
        note="Il più rapido dei tre: circa quattro secondi a risposta.",
    ),
    "codex_cli": Agente(
        id="codex_cli",
        label="OpenAI via Codex CLI (abbonamento)",
        comando=("codex", "exec", "--skip-git-repo-check"),
        prompt_come="stdin",
        # Su stdout stampa anche ganci, contatori e righe di servizio: l'ultimo messaggio
        # scritto su file è la risposta e basta, senza doverla ritagliare da un registro.
        risposta_da="file",
        opzione_uscita="-o",
        opzione_modello="--model",
        creds="~/.codex/auth.json",
        note="Il più lento e il più costoso in token: consuma migliaia di token anche per "
             "una risposta di due parole.",
    ),
    "antigravity": Agente(
        id="antigravity",
        label="Gemini via Antigravity CLI (abbonamento)",
        # `-p` vuole il prompt come ARGOMENTO, e su Windows la command line di CreateProcess ha un
        # tetto (~32 KB): una trascrizione d'intervista lo supera e il lancio muore con
        # [WinError 206] «The filename or extension is too long». La modalità stream-json legge
        # invece un messaggio NDJSON da stdin, che non ha limiti di lunghezza, e restituisce la
        # risposta nell'evento `result`.
        comando=("agy", "--input-format", "stream-json", "--output-format", "stream-json"),
        prompt_come="stream-json",
        opzione_modello="--model",
        elenco_modelli=("agy", "models"),
        note="L'unica via rimasta per usare un abbonamento Google: dal 2026 Cloud Code non "
             "ammette più i piani individuali. Dà anche modelli non-Gemini.",
    ),
}


def disponibile(nome: str) -> bool:
    """Vero se lo strumento è installato e raggiungibile dal PATH."""
    a = AGENTI.get(nome)
    return bool(a and shutil.which(a.comando[0]))


def installati() -> list[str]:
    return [nome for nome in AGENTI if disponibile(nome)]


def modelli(nome: str, timeout_s: float = 60.0) -> list[dict]:
    """Modelli che l'abbonamento mette a disposizione. Vuoto se l'agente non sa elencarli.

    Antigravity è l'unico dei tre a saperlo, e vale la pena chiederglielo: l'abbonamento non
    dà solo Gemini, e indovinare il nome di un modello significa sbagliarlo.
    """
    a = AGENTI.get(nome)
    if a is None or not a.elenco_modelli or not disponibile(nome):
        return []
    try:
        p = subprocess.run(list(a.elenco_modelli), capture_output=True, text=True,
                           timeout=timeout_s, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("%s: elenco modelli non riuscito (%s)", nome, e)
        return []
    fuori = []
    for riga in (p.stdout or "").splitlines():
        # "id<TAB>Etichetta"; le righe di servizio non hanno il tabulatore.
        if "\t" not in riga:
            continue
        mid, _, label = riga.partition("\t")
        if mid.strip():
            fuori.append({"id": mid.strip(), "label": label.strip() or mid.strip()})
    return fuori


def genera(nome: str, prompt: str, *, model: str = "",
           timeout_s: float = TIMEOUT_DEFAULT_S, cartella: str | Path = "") -> str:
    """Fa scrivere il testo all'agente e ne restituisce la risposta, ripulita.

    ``cartella`` è dove l'agente viene eseguito: se non si indica nulla se ne usa una vuota e
    usa-e-getta. Indicarla ha senso solo quando si vuole che l'agente veda quei file.
    """
    a = AGENTI.get(nome)
    if a is None:
        raise AgentError(f"agente sconosciuto: {nome}")
    if not disponibile(nome):
        raise AgentError(
            f"«{a.comando[0]}» non è installato, o non è nel PATH. "
            f"Serve per usare {a.label}."
        )
    if not prompt.strip():
        raise AgentError(f"{a.label}: niente da chiedere")

    with tempfile.TemporaryDirectory(prefix="llmswitch-agente-") as usaegetta:
        dove = Path(cartella) if cartella else Path(usaegetta)
        uscita = Path(usaegetta) / "risposta.txt"

        argomenti = list(a.comando)
        # `default` è il segnaposto che `modelli()` restituisce per un provider a cui nessun modello
        # è stato scelto: non è un modello reale, e passarlo come `--model` fa fallire l'agente
        # («model default is not recognized»). Senza modello — o con quel segnaposto — non si passa
        # l'opzione affatto e l'agente usa il default configurato nel proprio account.
        modello = (model or "").strip()
        if modello and modello != MODELLO_SEGNAPOSTO and a.opzione_modello:
            argomenti += [a.opzione_modello, modello]
        if a.risposta_da == "file":
            argomenti += [a.opzione_uscita, str(uscita)]
        da_scrivere = None
        if a.prompt_come == "argomento":
            argomenti.append(prompt)
        elif a.prompt_come == "stream-json":
            # Un solo messaggio NDJSON su stdin: nessun limite di lunghezza, a differenza di un
            # argomento di riga di comando.
            da_scrivere = json.dumps(
                {"event": "user",
                 "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}}
            ) + "\n"
        else:
            da_scrivere = prompt

        try:
            esito = subprocess.run(
                argomenti, input=da_scrivere, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout_s, cwd=str(dove),
                creationflags=_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as e:
            raise AgentError(
                f"{a.label}: nessuna risposta in {timeout_s:.0f}s. Sono agenti, non modelli: "
                "una richiesta lunga può volerci, ma oltre questo tempo è piantata."
            ) from e
        except OSError as e:
            raise AgentError(f"{a.label}: non si è potuto eseguire ({e})") from e

        if a.prompt_come == "stream-json":
            testo, errore = _risposta_stream_json(esito.stdout or "")
            if errore:
                raise AgentError(f"{a.label}: {errore}")
        elif a.risposta_da == "file":
            testo = uscita.read_text(encoding="utf-8") if uscita.is_file() else ""
        else:
            testo = esito.stdout or ""

    testo = testo.strip()
    if not testo:
        # Il codice di uscita da solo non spiega niente: si riporta anche cosa ha detto.
        motivo = (esito.stderr or "").strip()[:300] or f"uscita {esito.returncode}"
        raise AgentError(f"{a.label}: nessuna risposta ({motivo})")
    return testo


def _risposta_stream_json(stdout: str) -> tuple[str, str]:
    """La risposta pulita dallo stream NDJSON di Antigravity — restituisce ``(testo, errore)``.

    Lo stream è una riga JSON per evento: `init`, poi gli `step_update` con i pezzi di risposta,
    infine un `result` che è l'ultima parola. Si legge quello: porta la risposta intera in
    ``response`` e, quando un turno fallisce, il motivo in ``error`` — invece di un codice di
    uscita da interpretare. Delle due stringhe tornate una sola è piena.
    """
    risultato: dict | None = None
    for riga in stdout.splitlines():
        riga = riga.strip()
        if not riga:
            continue
        try:
            evento = json.loads(riga)
        except ValueError:
            continue  # righe non-JSON di servizio: non sono la risposta
        if isinstance(evento, dict) and evento.get("event") == "result":
            risultato = evento.get("result") or {}
    if risultato is None:
        return "", "nessun evento 'result' nello stream"
    if str(risultato.get("status", "")).upper() != "SUCCESS":
        return "", str(risultato.get("error") or f"stato {risultato.get('status')}")
    return str(risultato.get("response") or ""), ""
