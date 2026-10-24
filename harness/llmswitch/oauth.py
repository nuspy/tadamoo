"""Abbonamenti: usare il piano che si paga già, invece di una chiave a consumo.

Chi ha ChatGPT Plus, Google AI Pro, SuperGrok o un piano Qwen sta già pagando delle
richieste. Gli strumenti ufficiali di quei servizi — `codex`, `gemini`, `grok`, `qwen` —
tengono su disco le credenziali del login, e da lì si possono riusare senza chiedere
all'utente di autenticarsi una seconda volta per la stessa cosa.

La cascata è questa, e vale per ogni abbonamento:

1. le credenziali dello strumento ufficiale ci sono e sono ancora buone → si usano;
2. ci sono ma sono scadute → si rinnovano con il `refresh_token`;
3. non ci sono, o il rinnovo è stato rifiutato → si fa il login da qui, dove è possibile;
4. dove non è possibile, si dice quale comando lanciare.

**Il file dello strumento ufficiale viene riscritto** dopo un rinnovo. Non è una comodità:
molti servizi ruotano anche il `refresh_token`, invalidando quello vecchio. Se tenessimo il
nuovo solo per noi, il `gemini` o il `codex` dell'utente si ritroverebbe con una credenziale
morta e un errore incomprensibile alla prima esecuzione. La riscrittura è atomica e la prima
volta lascia una copia di sicurezza accanto all'originale.

Qui dentro non si scrive mai un token in un log, né in un messaggio d'errore.

**I `client_id` e `client_secret` degli strumenti ufficiali non stanno nel codice.** Si
leggono dalla macchina — variabili d'ambiente o `oauth_clients.json` accanto al registro,
vedi ``cliente`` — così un repository pubblico non porta credenziali di nessuno.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import shutil
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("llmswitch.oauth")

#: Ci si presenta con il proprio nome. Fingersi il client ufficiale di qualcun altro per
#: aggirarne i controlli è ciò che fa sospendere gli account, e non si fa.
USER_AGENT = "llmswitch/0.1.0"

#: Quanto prima della scadenza si rinnova. Un token che scade durante una richiesta lunga
#: la fa fallire a metà, e su un lavoro di ore è il modo peggiore di scoprirlo.
MARGINE_DEFAULT_S = 120.0


@dataclass(frozen=True)
class Abbonamento:
    """Come si leggono, si rinnovano e — dove si può — si ottengono le credenziali."""

    id: str
    label: str
    #: File dello strumento ufficiale, con ``~`` da espandere.
    file: str
    #: Percorso dei campi dentro il JSON: ``()`` se stanno in cima, ``("tokens",)`` se annidati.
    dentro: tuple[str, ...] = ()
    token_url: str = ""
    #: Vuoti negli abbonamenti di serie: si leggono dalla configurazione locale, vedi
    #: ``cliente``. Qui restano per chi costruisce un ``Abbonamento`` da sé.
    client_id: str = ""
    client_secret: str = ""
    #: Endpoint con cui si parla ai modelli una volta autenticati.
    inference_base_url: str = ""
    scope: str = ""
    #: Presente solo dove il login si può fare da qui, senza indovinare parametri.
    device_code_url: str = ""
    #: Comando dello strumento ufficiale, per capire se è installato e cosa suggerire.
    comando: str = ""
    login_manuale: str = ""
    #: Intestazione aggiuntiva che il servizio pretende (Codex vuole l'id dell'account).
    header_account: str = ""
    margine_s: float = MARGINE_DEFAULT_S
    #: Perché, pur con credenziali buone, il servizio non è utilizzabile. Vuoto = nessun
    #: impedimento noto. Un token valido non basta se è il fornitore a chiudere la porta, e
    #: mostrare "pronto" a chi poi riceve un rifiuto è il modo peggiore di dirglielo.
    impedimento: str = ""


ABBONAMENTI: dict[str, Abbonamento] = {
    "codex_oauth": Abbonamento(
        id="codex_oauth",
        label="ChatGPT (piano Codex)",
        file="~/.codex/auth.json",
        dentro=("tokens",),
        token_url="https://auth.openai.com/oauth/token",
        inference_base_url="https://chatgpt.com/backend-api/codex",
        comando="codex",
        login_manuale="codex login",
        header_account="ChatGPT-Account-Id",
    ),
    "gemini_oauth": Abbonamento(
        id="gemini_oauth",
        label="Gemini (piano Google AI Pro)",
        file="~/.gemini/oauth_creds.json",
        token_url="https://oauth2.googleapis.com/token",
        inference_base_url="https://cloudcode-pa.googleapis.com",
        scope=("openid https://www.googleapis.com/auth/cloud-platform "
               "https://www.googleapis.com/auth/userinfo.profile "
               "https://www.googleapis.com/auth/userinfo.email"),
        comando="gemini",
        login_manuale="gemini",
        # Misurato il 15 agosto 2026: le credenziali si rinnovano e `loadCodeAssist` risponde
        # 200, ma dentro dice che il piano dei privati non è più ammesso — reasonCode
        # UNSUPPORTED_CLIENT, «migrate to the Antigravity suite of products». Il token è buono,
        # è il servizio a rifiutare: senza questo avviso lo stato direbbe "pronto" a chi poi si
        # prende un errore che non sa spiegarsi.
        impedimento=("Google non ammette più i piani individuali su Cloud Code "
                     "(UNSUPPORTED_CLIENT). Usa l'agente «antigravity», dove Google stessa "
                     "rimanda, oppure il fornitore «gemini» con una chiave a consumo."),
    ),
    "qwen_oauth": Abbonamento(
        id="qwen_oauth",
        label="Qwen (abbonamento)",
        file="~/.qwen/oauth_creds.json",
        token_url="https://chat.qwen.ai/api/v1/oauth2/token",
        inference_base_url="https://portal.qwen.ai/v1",
        device_code_url="https://chat.qwen.ai/api/v1/oauth2/device/code",
        comando="qwen",
        login_manuale="qwen",
    ),
    "grok_oauth": Abbonamento(
        id="grok_oauth",
        label="Grok (SuperGrok / X Premium+)",
        file="~/.grok/auth.json",
        token_url="https://auth.x.ai/oauth2/token",
        inference_base_url="https://api.x.ai/v1",
        scope="openid profile email offline_access grok-cli:access api:access",
        device_code_url="https://auth.x.ai/oauth2/device/code",
        comando="grok",
        login_manuale="grok",
        # I token di xAI durano circa sei ore. Con un margine di due minuti, un lavoro che
        # tocca il provider ogni mezz'ora si ritrova regolarmente con la credenziale spirata:
        # si rinnova con un'ora di anticipo e resta sempre caldo.
        margine_s=3600.0,
    ),
}


class OAuthError(RuntimeError):
    """Non è stato possibile ottenere una credenziale valida per l'abbonamento."""


@dataclass
class Credenziali:
    access_token: str
    refresh_token: str = ""
    #: Momento della scadenza, in secondi dall'epoca. ``0`` = non dichiarata.
    scade_a: float = 0.0
    account_id: str = ""
    #: Il documento originale, per riscriverlo senza perdere i campi che non ci riguardano.
    grezzo: dict[str, Any] = field(default_factory=dict)

    def valide(self, margine_s: float = MARGINE_DEFAULT_S) -> bool:
        if not self.access_token:
            return False
        if not self.scade_a:
            return True  # nessuna scadenza dichiarata: si prova, e il servizio dirà la sua
        return self.scade_a - time.time() > margine_s

    def scade_fra(self) -> float:
        """Ore alla scadenza; negativo se è già passata, ``inf`` se non è dichiarata."""
        return float("inf") if not self.scade_a else (self.scade_a - time.time()) / 3600


# ---- lettura -----------------------------------------------------------------------


def percorso(a: Abbonamento) -> Path:
    return Path(os.path.expanduser(a.file))


def _annidato(dati: dict, dentro: tuple[str, ...]) -> dict:
    for passo in dentro:
        dati = dati.get(passo) or {}
        if not isinstance(dati, dict):
            return {}
    return dati


def _exp_da_jwt(token: str) -> float:
    """Scadenza letta dal corpo del JWT. ``0`` se non è un JWT o non la dichiara.

    Serve dove il file non porta una scadenza esplicita: Codex salva solo l'ora dell'ultimo
    rinnovo, e senza guardare dentro al token non si saprebbe se è ancora buono. La firma non
    si verifica — non stiamo autenticando nessuno, stiamo leggendo una data.
    """
    parti = token.split(".")
    if len(parti) != 3:
        return 0.0
    corpo = parti[1] + "=" * (-len(parti[1]) % 4)
    try:
        dati = json.loads(base64.urlsafe_b64decode(corpo))
    except (ValueError, binascii.Error):
        return 0.0
    exp = dati.get("exp")
    return float(exp) if isinstance(exp, (int, float)) else 0.0


def _scadenza(campi: dict) -> float:
    """Scadenza in secondi dall'epoca, comunque il servizio abbia deciso di scriverla."""
    for chiave in ("expiry_date", "expires_at", "expiry"):
        v = campi.get(chiave)
        if isinstance(v, (int, float)) and v > 0:
            return float(v) / 1000 if v > 1e11 else float(v)  # certi la scrivono in millisecondi
    return _exp_da_jwt(str(campi.get("access_token") or ""))


def leggi(a: Abbonamento) -> Credenziali | None:
    """Le credenziali dello strumento ufficiale, o ``None`` se non ce ne sono di leggibili."""
    p = percorso(a)
    try:
        grezzo = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(grezzo, dict):
        return None
    campi = _annidato(grezzo, a.dentro)
    token = str(campi.get("access_token") or "")
    if not token:
        return None
    return Credenziali(
        access_token=token,
        refresh_token=str(campi.get("refresh_token") or ""),
        scade_a=_scadenza(campi),
        account_id=str(campi.get("account_id") or ""),
        grezzo=grezzo,
    )


def percorso_clienti() -> Path:
    """``oauth_clients.json``, nella cartella di configurazione di llmswitch.

    Forma: ``{"gemini_oauth": {"client_id": "…", "client_secret": "…"}, …}``. Sta accanto
    al registro dei provider, fuori da ogni progetto e quindi fuori dai commit.
    """
    from .providers import default_registry_path

    return default_registry_path().parent / "oauth_clients.json"


def cliente(a: Abbonamento) -> tuple[str, str]:
    """``(client_id, client_secret)`` con cui chiedere token per conto dell'abbonamento.

    Nell'ordine: le variabili ``LLMSWITCH_<ID>_CLIENT_ID`` e ``LLMSWITCH_<ID>_CLIENT_SECRET``
    (``<ID>`` è l'id in maiuscolo, es. ``GEMINI_OAUTH``), poi ``percorso_clienti()``, poi i
    campi dell'``Abbonamento``. Un valore mancante resta ``""``: sarà chi lo usa a dirlo.
    """
    prefisso = f"LLMSWITCH_{a.id.upper()}_"
    try:
        tutti = json.loads(percorso_clienti().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        tutti = {}
    voce = tutti.get(a.id) if isinstance(tutti, dict) else None
    voce = voce if isinstance(voce, dict) else {}
    client_id = (os.environ.get(prefisso + "CLIENT_ID")
                 or str(voce.get("client_id") or "") or a.client_id)
    client_secret = (os.environ.get(prefisso + "CLIENT_SECRET")
                     or str(voce.get("client_secret") or "") or a.client_secret)
    return client_id.strip(), client_secret.strip()


def _manca_cliente(a: Abbonamento) -> OAuthError:
    return OAuthError(
        f"{a.label}: manca il client_id dello strumento ufficiale. Mettilo in "
        f"{percorso_clienti()} sotto «{a.id}», oppure in LLMSWITCH_{a.id.upper()}_CLIENT_ID."
    )


def installato(a: Abbonamento) -> bool:
    """Vero se lo strumento ufficiale è sulla macchina. Non è necessario, ma spiega molto."""
    import shutil as _sh

    return bool(a.comando and _sh.which(a.comando))


# ---- scrittura ---------------------------------------------------------------------


def _scrivi(a: Abbonamento, cred: Credenziali) -> None:
    """Riscrive il file dello strumento ufficiale con i campi rinnovati.

    Si conserva tutto il resto del documento: `~/.codex/auth.json` porta anche altro, e
    riscriverlo da zero significherebbe cancellare roba di cui non sappiamo nulla. La
    sostituzione è atomica, così un'interruzione a metà non lascia un file troncato — che qui
    vorrebbe dire perdere l'accesso allo strumento.
    """
    p = percorso(a)
    if not p.is_file():
        p.parent.mkdir(parents=True, exist_ok=True)

    backup = p.with_suffix(p.suffix + ".prima-di-llmswitch")
    if p.is_file() and not backup.exists():
        shutil.copy2(p, backup)

    documento = dict(cred.grezzo)
    campi = dict(_annidato(documento, a.dentro))
    campi["access_token"] = cred.access_token
    if cred.refresh_token:
        campi["refresh_token"] = cred.refresh_token
    if cred.scade_a:
        # Si riscrive nell'unità che il file usava già, per non confondere chi lo rilegge.
        chiave = next((k for k in ("expiry_date", "expires_at", "expiry") if k in campi), "expiry_date")
        campi[chiave] = int(cred.scade_a * 1000) if chiave == "expiry_date" else int(cred.scade_a)
    if cred.account_id:
        campi["account_id"] = cred.account_id

    if a.dentro:
        dentro = documento
        for passo in a.dentro[:-1]:
            dentro = dentro.setdefault(passo, {})
        dentro[a.dentro[-1]] = campi
    else:
        documento = campi

    temporaneo = p.with_suffix(p.suffix + ".nuovo")
    temporaneo.write_text(json.dumps(documento, indent=2), encoding="utf-8")
    os.replace(temporaneo, p)
    cred.grezzo = documento


# ---- rinnovo -----------------------------------------------------------------------


def rinnova(a: Abbonamento, cred: Credenziali) -> Credenziali:
    """Chiede un nuovo access token con il refresh token. Solleva ``OAuthError`` se rifiutato."""
    import requests

    if not cred.refresh_token:
        raise OAuthError(f"{a.label}: nessun refresh token, serve rifare l'accesso")
    if not a.token_url:
        raise OAuthError(f"{a.label}: non so a chi chiedere il rinnovo")
    client_id, client_secret = cliente(a)
    if not client_id:
        raise _manca_cliente(a)

    dati = {
        "grant_type": "refresh_token",
        "refresh_token": cred.refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        dati["client_secret"] = client_secret
    try:
        risposta = requests.post(
            a.token_url, data=dati,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
    except requests.RequestException as e:
        raise OAuthError(f"{a.label}: il servizio non risponde ({e})") from e
    if not risposta.ok:
        # Il corpo di una risposta d'errore può contenere il token che abbiamo mandato:
        # si riporta solo il codice, che è ciò che serve a capire cosa è successo.
        raise OAuthError(
            f"{a.label}: rinnovo rifiutato (HTTP {risposta.status_code}). "
            f"Rifai l'accesso con «{a.login_manuale or a.comando}»."
        )
    try:
        payload = risposta.json()
    except ValueError as e:
        raise OAuthError(f"{a.label}: risposta al rinnovo illeggibile") from e

    token = str(payload.get("access_token") or "")
    if not token:
        raise OAuthError(f"{a.label}: il rinnovo non ha restituito un token")
    durata = payload.get("expires_in")
    nuova = Credenziali(
        access_token=token,
        # Se il servizio non ne manda uno nuovo, il vecchio resta valido.
        refresh_token=str(payload.get("refresh_token") or cred.refresh_token),
        scade_a=time.time() + float(durata) if isinstance(durata, (int, float)) else _exp_da_jwt(token),
        account_id=cred.account_id or str(payload.get("account_id") or ""),
        grezzo=cred.grezzo,
    )
    _scrivi(a, nuova)
    log.info("%s: credenziale rinnovata", a.label)
    return nuova


# ---- accesso da zero ---------------------------------------------------------------


def login_device(a: Abbonamento, *, attesa_massima_s: float = 600.0,
                 apri_browser: bool = True) -> Credenziali:
    """Accesso con il flusso a codice (RFC 8628): si mostra un codice, si conferma nel browser.

    È il flusso adatto a un programma che non ha una finestra propria: nessun server locale da
    aprire, nessuna porta da negoziare. Chi non lo espone resta escluso, e per quelli si passa
    dallo strumento ufficiale — vedi ``credenziali``.
    """
    import requests

    if not a.device_code_url:
        raise OAuthError(
            f"{a.label}: non espone l'accesso a codice. Fai l'accesso una volta con "
            f"«{a.login_manuale or a.comando}»: da lì in poi la credenziale si rinnova da sola."
        )
    client_id = cliente(a)[0]
    if not client_id:
        raise _manca_cliente(a)
    richiesta = {"client_id": client_id}
    if a.scope:
        richiesta["scope"] = a.scope
    try:
        avvio = requests.post(a.device_code_url, data=richiesta,
                              headers={"User-Agent": USER_AGENT}, timeout=30)
        avvio.raise_for_status()
        dati = avvio.json()
    except (requests.RequestException, ValueError) as e:
        raise OAuthError(f"{a.label}: non è stato possibile avviare l'accesso ({e})") from e

    codice = str(dati.get("user_code") or "")
    indirizzo = str(dati.get("verification_uri_complete") or dati.get("verification_uri") or "")
    device_code = str(dati.get("device_code") or "")
    if not device_code or not indirizzo:
        raise OAuthError(f"{a.label}: risposta di avvio incompleta")

    print(f"\n  {a.label}: apri {indirizzo}")
    if codice:
        print(f"  e inserisci il codice:  {codice}\n")
    if apri_browser:
        try:
            webbrowser.open(indirizzo)
        except Exception:  # noqa: BLE001 — un browser che non si apre non è un errore fatale
            pass

    intervallo = float(dati.get("interval") or 5)
    scadenza = time.time() + min(float(dati.get("expires_in") or attesa_massima_s), attesa_massima_s)
    while time.time() < scadenza:
        time.sleep(intervallo)
        try:
            esito = requests.post(a.token_url, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id": client_id,
            }, headers={"User-Agent": USER_AGENT}, timeout=30)
            payload = esito.json()
        except (requests.RequestException, ValueError):
            continue
        errore = str(payload.get("error") or "")
        if errore == "authorization_pending":
            continue
        if errore == "slow_down":
            intervallo += 5
            continue
        if errore:
            raise OAuthError(f"{a.label}: accesso non riuscito ({errore})")
        token = str(payload.get("access_token") or "")
        if token:
            durata = payload.get("expires_in")
            cred = Credenziali(
                access_token=token,
                refresh_token=str(payload.get("refresh_token") or ""),
                scade_a=time.time() + float(durata) if isinstance(durata, (int, float)) else _exp_da_jwt(token),
                grezzo={},
            )
            _scrivi(a, cred)
            log.info("%s: accesso completato", a.label)
            return cred
    raise OAuthError(f"{a.label}: accesso non confermato entro il tempo previsto")


# ---- la cascata --------------------------------------------------------------------


def credenziali(nome: str, *, consenti_login: bool = True) -> Credenziali:
    """Una credenziale utilizzabile per l'abbonamento, prendendola dove già c'è.

    Nell'ordine: quella dello strumento ufficiale se è ancora buona, il suo rinnovo se è
    scaduta, l'accesso da qui se non c'è niente. Solleva ``OAuthError`` con l'indicazione di
    cosa fare quando nessuna delle tre strade è percorribile.
    """
    a = ABBONAMENTI.get(nome)
    if a is None:
        raise OAuthError(f"abbonamento sconosciuto: {nome}")

    cred = leggi(a)
    if cred and cred.valide(a.margine_s):
        return cred
    if cred and cred.refresh_token:
        try:
            return rinnova(a, cred)
        except OAuthError as e:
            log.info("%s: rinnovo non riuscito, si prova l'accesso (%s)", a.label, e)
    if not consenti_login:
        raise OAuthError(
            f"{a.label}: nessuna credenziale valida. Accedi con «{a.login_manuale or a.comando}»."
        )
    return login_device(a)


def access_token(nome: str, *, consenti_login: bool = True) -> str:
    return credenziali(nome, consenti_login=consenti_login).access_token


def intestazioni(nome: str, *, consenti_login: bool = True) -> dict[str, str]:
    """Intestazioni HTTP pronte per parlare col servizio, compreso ciò che pretende in più."""
    a = ABBONAMENTI[nome]
    cred = credenziali(nome, consenti_login=consenti_login)
    testa = {"Authorization": f"Bearer {cred.access_token}", "User-Agent": USER_AGENT}
    if a.header_account and cred.account_id:
        testa[a.header_account] = cred.account_id
    return testa


def stato(nome: str) -> dict[str, Any]:
    """Quadro leggibile per un pannello di impostazioni. Non contiene mai un token.

    Distingue i tre casi che l'utente deve poter capire a colpo d'occhio: collegato, collegato
    ma da rinnovare, non collegato — e in quest'ultimo caso cosa fare.
    """
    a = ABBONAMENTI.get(nome)
    if a is None:
        return {"id": nome, "esiste": False}
    cred = leggi(a)
    ore = cred.scade_fra() if cred else 0.0
    client_id = cliente(a)[0]
    return {
        "id": a.id,
        "label": a.label,
        "esiste": True,
        "strumento_installato": installato(a),
        "file": str(percorso(a)),
        "collegato": bool(cred),
        # Con un impedimento noto la credenziale resta valida, ma non porta da nessuna parte.
        "utilizzabile": bool(cred and cred.valide(a.margine_s)) and not a.impedimento,
        "impedimento": a.impedimento,
        # Senza client_id il refresh token c'è ma non si sa a nome di chi presentarlo.
        "rinnovabile": bool(cred and cred.refresh_token and client_id),
        "client_configurato": bool(client_id),
        "scade_fra_ore": None if ore == float("inf") else round(ore, 1),
        "accesso_da_qui": bool(a.device_code_url),
        "come_accedere": a.login_manuale or a.comando,
        "inference_base_url": a.inference_base_url,
    }


def panoramica() -> list[dict[str, Any]]:
    """Lo stato di tutti gli abbonamenti, per mostrarli insieme."""
    return [stato(nome) for nome in ABBONAMENTI]
