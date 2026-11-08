"""Bridge media per l'app Android (Task #21).

Piccolo server HTTP che serve all'app i file generati da Hermes (immagini/video)
tramite URL, invece di mandarli inline sul WebSocket (molto meglio per i video,
e necessario per il salvataggio locale + lo streaming con seek).

Flusso:
  - Hermes include nel testo di risposta un tag `[[media:image:SRC]]` o
    `[[media:video:SRC]]` (SRC = path locale o URL). Vedi voice_gateway.py.
  - Il gateway registra il file qui (`register_file`) e manda all'app
    `{type:"media", kind, token, port}`. L'app scarica da
    `http://<host>:<port>/media/<token>`.
  - L'app puo' anche inviare un'immagine: viene salvata con `save_incoming_image`
    e il path passato a Hermes nell'enunciato successivo.
  - `GET /poster/<persona>` serve l'immagine fissa dell'avatar: l'app la mostra
    quando il video e' acceso ma l'audio no, invece di tenere aperto un flusso
    WebRTC per una faccia immobile. Sta qui e non sul control server (:8770)
    perche' :8772 e' la porta che il telefono gia' raggiunge.
"""
from __future__ import annotations

import base64
import json
import shutil
import logging
import mimetypes
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

log = logging.getLogger("callbridge.media")

# token -> (path, scadenza). Con TTL + limite duro: senza, su un servizio
# long-running il registry crescerebbe per sempre (token mai rimossi).
_REGISTRY: dict[str, tuple[str, float]] = {}
_LOCK = threading.Lock()
_TTL_S = 3600.0
_MAX_ENTRIES = 500
_INCOMING_DIR = Path(os.environ.get("TEMP", "/tmp")) / "callapicall_media"

#: Cartella delle persone, per `/poster/<nome>`. La passa `start_media_server`
#: invece di rileggere la configurazione da disco a ogni richiesta.
_PERSONE_DIR: Path | None = None
#: Porta del control server, per risolvere la persona ATTIVA quando l'app non
#: ne indica una (e' il caso normale: nelle impostazioni il campo e' vuoto e
#: significa "quella gia' in uso").
_CONTROL_PORT: int = 8770


#: Il build web della app, servito su /myAi. Cartella SEPARATA da `dist`, che
#: e' quella dell'app installata: le due build non sono interscambiabili,
#: perche' la versione web ha i percorsi assoluti sotto /myAi/ e dentro
#: l'APK quei percorsi non esistono. Si costruisce con `npm run build:web`.
_WEBAPP_DIR = str(Path(__file__).resolve().parents[2] / "app" / "dist-web")


def _persona_attiva() -> str:
    """Nome della persona attualmente caricata nel worker avatar, o ""."""
    import json as _json
    import urllib.request
    try:
        url = f"http://127.0.0.1:{_CONTROL_PORT}/avatar/state"
        with urllib.request.urlopen(url, timeout=3) as r:
            return str(_json.loads(r.read()).get("persona") or "")
    except Exception as e:  # noqa: BLE001 - il bridge puo' essere spento
        log.debug("persona attiva non risolta: %s", e)
        return ""


def _to_local_path(src: str) -> str | None:
    """Risolve SRC in un path leggibile dall'orchestratore Windows.

    Accetta path Windows (E:\\...), path WSL (/home/...) mappati su UNC
    \\\\wsl.localhost\\Ubuntu\\..., o file:// .
    """
    if src.startswith("file://"):
        src = src[7:]
    p = Path(src)
    if p.is_file():
        return str(p)
    if src.startswith("/"):  # path WSL -> UNC
        for distro in ("Ubuntu", "Ubuntu-22.04", "Ubuntu-24.04"):
            unc = Path(rf"\\wsl.localhost\{distro}") / src.lstrip("/")
            if unc.is_file():
                return str(unc)
            unc2 = Path(rf"\\wsl$\{distro}") / src.lstrip("/")
            if unc2.is_file():
                return str(unc2)
    return None


def _prune_locked(now: float) -> None:
    """Rimuove i token scaduti e applica il limite duro (chiamare sotto _LOCK)."""
    for k in [k for k, (_, exp) in _REGISTRY.items() if exp < now]:
        _REGISTRY.pop(k, None)
    if len(_REGISTRY) > _MAX_ENTRIES:
        oldest = sorted(_REGISTRY.items(), key=lambda kv: kv[1][1])
        for k, _ in oldest[: len(_REGISTRY) - _MAX_ENTRIES]:
            _REGISTRY.pop(k, None)


def register_file(src: str) -> str | None:
    """Registra un file (path locale/WSL/URL già risolto) e ritorna un token."""
    path = _to_local_path(src)
    if not path:
        return None
    token = uuid.uuid4().hex
    now = time.time()
    with _LOCK:
        _prune_locked(now)
        _REGISTRY[token] = (path, now + _TTL_S)
    return token


def get_path(token: str) -> str | None:
    with _LOCK:
        entry = _REGISTRY.get(token)
        if entry is None:
            return None
        path, expiry = entry
        if expiry < time.time():
            _REGISTRY.pop(token, None)
            return None
        return path


def save_incoming_image(data_b64: str, mime: str) -> str:
    """Salva un'immagine ricevuta dall'app, ritorna il path locale."""
    _INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    ext = mimetypes.guess_extension(mime or "image/jpeg") or ".jpg"
    fn = _INCOMING_DIR / f"in_{uuid.uuid4().hex}{ext}"
    fn.write_bytes(base64.b64decode(data_b64))
    return str(fn)


#: Tetto per un file in arrivo dall'app. Non e' un limite di protocollo: e'
#: il punto oltre il quale un errore (o qualcuno che si e' sbagliato) riempie
#: il disco del PC senza che nessuno se ne accorga.
_MAX_UPLOAD = 256 * 1024 * 1024

#: APK servito su /app. Si aggiorna a ogni build: si tiene il PERCORSO, non una
#: copia, cosi' non esiste il caso in cui si distribuisce una versione vecchia
#: perche' qualcuno ha dimenticato di ricopiarla.
_APK_PATH = os.environ.get("CALLAPICALL_APK") or str(
    Path(__file__).resolve().parents[2]
    / "app/android/app/build/outputs/apk/debug/app-debug.apk")


def _nome_sicuro(nome: str) -> str:
    """Nome di file ripulito, mai un percorso.

    Il nome arriva dal telefono, quindi e' dato non fidato: "../../..\ntldr"
    e' un nome di file perfettamente valido per chi lo scrive. Si tiene solo
    l'ultimo segmento e si tolgono i caratteri che su Windows non possono
    stare in un nome.
    """
    nome = (nome or "").replace("\\", "/").split("/")[-1].strip()
    nome = "".join(c for c in nome if c not in '<>:"|?*' and ord(c) >= 32)
    nome = nome.strip(". ")          # Windows non tollera punti/spazi finali
    return nome[:120] or "allegato"


def save_incoming_file(dati: bytes, nome: str) -> str:
    """Salva un file ricevuto dall'app e ne ritorna il percorso locale.

    Il nome originale si conserva — un allegato che l'agente deve leggere e'
    molto piu' utile se si chiama `bilancio-2026.pdf` invece di un esadecimale
    — ma dentro una cartella per invio, cosi' due file con lo stesso nome non
    si sovrascrivono e nessuno puo' scegliere dove scrivere.
    """
    cartella = _INCOMING_DIR / uuid.uuid4().hex
    cartella.mkdir(parents=True, exist_ok=True)
    fn = cartella / _nome_sicuro(nome)
    fn.write_bytes(dati)
    return str(fn)


def _rimpicciolisci(jpg: bytes, altezza: int) -> bytes | None:
    """JPEG riscalato a quell'altezza, o None se non si puo' fare.

    Ritorna None (e il chiamante manda l'originale) invece di sollevare: un
    poster e' un ornamento, e non deve mai essere il motivo per cui il video
    non parte.
    """
    try:
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
        if img is None or img.shape[0] <= altezza:
            return None                       # gia' piccola: non si ingrandisce
        larghezza = max(2, round(img.shape[1] * altezza / img.shape[0]))
        piccola = cv2.resize(img, (larghezza, altezza),
                             interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", piccola,
                               [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        return bytes(buf) if ok else None
    except Exception as e:  # noqa: BLE001
        log.debug("poster non ridimensionato: %s", e)
        return None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # silenzia il log di default
        pass

    def _send_headers(self, status, ctype, length, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def do_OPTIONS(self) -> None:
        # La WebView dell'app fa la richiesta preliminare CORS prima di un POST
        # con intestazioni sue (X-Nome-File): senza risposta, l'upload non parte
        # nemmeno e l'errore che si vede e' un generico "Failed to fetch".
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Nome-File")
        self.send_header("Content-Length", "0")
        self.end_headers()

    #: Endpoint del media server e del control (proxati): sotto /myAi vanno
    #: normalizzati alla radice, gli asset della web app no.
    _API_PREFIXES = ("/poster", "/models", "/token", "/personas", "/avatar",
                     "/media", "/upload", "/app")

    def _norm_path(self) -> None:
        """Toglie il prefisso /myAi dagli endpoint API (non dagli asset della
        web app), così valgono sia sotto /myAi/ sia alla radice di un dominio."""
        if self.path.startswith("/myAi/"):
            resto = "/" + self.path[len("/myAi/"):]
            b = resto.split("?")[0]
            if any(b == a or b.startswith(a + "/") for a in self._API_PREFIXES):
                self.path = resto

    def _control_path(self):
        """Se il path è una rotta del control server (:8770) — anche sotto il
        prefisso /myAi con cui è servita la web app — ritorna il path da
        inoltrare (senza /myAi); altrimenti None.

        Serve a dare UN SOLO ORIGIN al client: la web app (e l'app installata)
        parlano solo col media server, che gira /personas e /avatar al control.
        Così l'editor bocca, il wizard e il CRUD funzionano anche via https
        (myai.tadamoo.com), dove le porte 8770/8772 non sono esposte a parte.
        """
        p = self.path
        if p.startswith("/myAi/"):
            p = p[len("/myAi"):]
        base = p.split("?")[0]
        if base.startswith("/personas") or base.startswith("/avatar/"):
            return p
        return None

    def _proxy_control(self, path: str) -> None:
        import urllib.error
        import urllib.request
        url = f"http://127.0.0.1:{_CONTROL_PORT}{path}"
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else None
        req = urllib.request.Request(url, data=body, method=self.command)
        ct = self.headers.get("Content-Type")
        if ct:
            req.add_header("Content-Type", ct)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data, code = r.read(), r.status
                ctype = r.headers.get("Content-Type", "application/octet-stream")
        except urllib.error.HTTPError as e:
            data, code = e.read(), e.code
            ctype = e.headers.get("Content-Type", "application/json")
        except Exception as e:  # noqa: BLE001
            self.send_error(502, f"control non raggiungibile: {e}")
            return
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_OPTIONS(self) -> None:
        # Preflight CORS per le POST/DELETE dell'app installata (origin diverso).
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Nome-File")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self) -> None:
        self._norm_path()
        cp = self._control_path()
        if cp is not None:
            self._proxy_control(cp)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        """`POST /upload` — un file dall'app al PC.

        Corpo GREZZO, nome nell'intestazione `X-Nome-File`: niente multipart e
        niente base64. Il multipart andrebbe assemblato a mano (qui non c'e' un
        framework), e il base64 sul WebSocket costerebbe un terzo di byte in
        piu' e terrebbe l'intero file in memoria due volte — su un video da
        cinquanta megabyte si sente.
        """
        self._norm_path()
        cp = self._control_path()
        if cp is not None:                    # /personas, /avatar -> control
            self._proxy_control(cp)
            return
        if self.path.split("?")[0] != "/upload":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            self.send_error(400, "corpo vuoto")
            return
        if n > _MAX_UPLOAD:
            self.send_error(413, "file troppo grande")
            return
        nome = self.headers.get("X-Nome-File") or "allegato"
        try:
            nome = unquote(nome)          # l'app lo codifica: gli accenti
        except Exception:                 # noqa: BLE001
            pass
        try:
            dati = self.rfile.read(n)
            percorso = save_incoming_file(dati, nome)
        except Exception as e:            # noqa: BLE001
            log.warning("upload fallito: %s", e)
            self.send_error(500, "salvataggio fallito")
            return
        log.info("file ricevuto dall'app: %s (%.1f KB)", percorso, len(dati) / 1024)
        corpo = json.dumps({"path": percorso, "name": os.path.basename(percorso),
                            "size": len(dati)}).encode("utf-8")
        self._send_headers(200, "application/json", len(corpo))
        self.wfile.write(corpo)

    def _modello_onnx(self, nome: str) -> None:
        """`GET /models/<file>.onnx` — un modello per il rendering on-device."""
        nome = _nome_sicuro(nome)
        base = Path(__file__).resolve().parents[1] / "models"
        f = base / nome
        if not nome.endswith((".onnx", ".wav", ".bin")) or not f.is_file():
            self.send_error(404, "modello non disponibile")
            return
        n = f.stat().st_size
        ctype = ("audio/wav" if nome.endswith(".wav")
                 else "application/octet-stream")
        self._send_headers(200, ctype, n,
                           {"Cache-Control": "public, max-age=604800"})
        with open(f, "rb") as h:
            shutil.copyfileobj(h, self.wfile)

    def _token(self) -> None:
        """`GET /token` — il token del gateway, in JSON, per l'app."""
        from .config import Config
        tok = getattr(Config.load(), "gateway_auth_token", "") or ""
        corpo = json.dumps({"token": tok}).encode("utf-8")
        self._send_headers(200, "application/json", len(corpo),
                           {"Cache-Control": "no-store"})
        self.wfile.write(corpo)

    def _apk(self) -> None:
        """`GET /app` — l'APK dell'app Android.

        Serve a installarla sul telefono senza passare da un negozio: si apre
        questo indirizzo dal telefono e Android propone l'installazione. Il
        media server e' gia' raggiungibile dall'app (e' da qui che scarica i
        media), quindi e' l'unico punto che di sicuro il telefono vede.

        Il tipo MIME non e' un dettaglio: con `application/octet-stream`
        Android scarica un file e basta, senza offrire di installarlo.
        """
        apk = _APK_PATH
        if not apk or not os.path.isfile(apk):
            self.send_error(404, "APK non disponibile")
            return
        n = os.path.getsize(apk)
        # no-store: senza, il browser puo' servire un APK vecchio dalla cache e
        # l'app "resta identica" dopo il reinstallo. L'ora nel nome cambia il
        # file a ogni build, ma la garanzia vera e' questa intestazione.
        self._send_headers(
            200, "application/vnd.android.package-archive", n,
            {"Content-Disposition": 'attachment; filename="callapicall.apk"',
             "Cache-Control": "no-store, no-cache, must-revalidate"})
        with open(apk, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def _webapp(self, percorso: str) -> None:
        """`GET /myAi[...]` — la stessa applicazione, dentro un browser.

        E' il build web della app Android: stesso codice, stessa interfaccia.
        Cambiano solo i quattro punti che nel browser non esistono — il routing
        dell'altoparlante (lo decide il sistema), il salvataggio nei Documenti
        (diventa un download), la fotocamera (la apre il browser) e l'indirizzo
        del gateway, che qui non si configura: si deduce da dove e' arrivata
        questa pagina, che e' l'unico indirizzo che il browser vede di sicuro.

        Serve un cartellino: microfono e WebRTC funzionano SOLO in contesto
        sicuro, cioe' HTTPS oppure localhost. Su un `http://` pubblico il
        browser nega il microfono senza nemmeno chiedere, e la pagina sembra
        rotta senza dire perche'.
        """
        radice = _WEBAPP_DIR
        if not radice or not os.path.isdir(radice):
            self.send_error(404, "web app non compilata")
            return
        relativo = percorso[len("/myAi"):].lstrip("/").split("?")[0]
        candidato = os.path.normpath(os.path.join(radice, relativo))
        # La pagina e' una SPA: ogni percorso che non e' un file esistente
        # deve restituire l'index, altrimenti un ricaricamento su una rotta
        # interna da' 404.
        if (not relativo or not candidato.startswith(radice)
                or not os.path.isfile(candidato)):
            candidato = os.path.join(radice, "index.html")
        if not os.path.isfile(candidato):
            self.send_error(404, "web app non compilata")
            return
        tipi = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
                ".css": "text/css", ".json": "application/json",
                ".svg": "image/svg+xml", ".png": "image/png",
                ".jpg": "image/jpeg", ".ico": "image/x-icon",
                ".woff2": "font/woff2", ".webmanifest": "application/manifest+json"}
        ext = os.path.splitext(candidato)[1].lower()
        dati = open(candidato, "rb").read()
        extra = {"Cache-Control": "no-cache"} if ext == ".html" else None
        self._send_headers(200, tipi.get(ext, "application/octet-stream"),
                           len(dati), extra)
        self.wfile.write(dati)

    def do_GET(self) -> None:
        self._norm_path()
        cp = self._control_path()
        if cp is not None:                    # /personas, /avatar -> control
            self._proxy_control(cp)
            return
        if self.path.split("?")[0] in ("/app", "/app.apk"):
            self._apk()
            return
        # Il token del gateway, servito sulla PROPRIA origine. Cosi' l'app —
        # web o installata — non deve piu' fartelo incollare a mano: lo prende
        # da qui, dallo stesso posto da cui e' arrivata la pagina. Non e' un
        # buco: chi raggiunge questo endpoint raggiunge gia' il gateway, e su
        # `myai.tadamoo.com` la pagina (e quindi anche questo) sta dietro la
        # basic-auth del proxy. In tailnet e in LAN la rete e' gia' la barriera.
        if self.path.split("?")[0] in ("/token", "/myAi/token"):
            self._token()
            return
        # Modelli ONNX per il rendering on-device (Wav2Lip): grossi e uguali per
        # tutti, quindi serviti a parte e scaricati dal client a runtime, non
        # impacchettati nell'app.
        if self.path.startswith("/models/"):
            self._modello_onnx(self.path[len("/models/"):].split("?")[0])
            return
        if self.path == "/myAi":
            # Senza la barra finale il browser risolve i percorsi relativi
            # degli asset (`./assets/...`) contro la RADICE, e la pagina
            # resta bianca. I percorsi sono relativi apposta: la stessa build
            # viene servita qui sotto /myAi/ e da un proxy alla radice di un
            # dominio suo.
            self._send_headers(301, "text/html", 0, {"Location": "/myAi/"})
            return
        if self.path.startswith("/myAi/") or self.path.startswith("/myAi?"):
            self._webapp(self.path)
            return
        if self.path.startswith("/poster/"):
            resto = self.path[len("/poster/"):]
            nome, _, query = resto.partition("?")
            self._poster(nome, query)
            return
        if not self.path.startswith("/media/"):
            self.send_error(404)
            return
        token = self.path[len("/media/"):].split("?")[0]
        path = get_path(token)
        if not path or not os.path.isfile(path):
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        size = os.path.getsize(path)
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            spec = rng[6:].split(",")[0].strip()  # solo il primo range
            start_s, _, end_s = spec.partition("-")
            try:
                if start_s == "":
                    # suffix-range "bytes=-N": gli ULTIMI N byte (usato dai
                    # player per il moov atom MP4). Prima veniva letto come
                    # start=0,end=N-1 -> seek/streaming rotto.
                    n = int(end_s)
                    if n <= 0:
                        raise ValueError
                    start = max(0, size - n)
                    end = size - 1
                else:
                    start = int(start_s)
                    end = int(end_s) if end_s else size - 1
            except ValueError:
                self.send_error(400)  # Range malformato: niente traceback/500
                return
            if start >= size or start > end:
                self._send_headers(416, ctype, 0,
                                   {"Content-Range": f"bytes */{size}"})
                return
            end = min(end, size - 1)
            length = end - start + 1
            self._send_headers(206, ctype, length,
                               {"Content-Range": f"bytes {start}-{end}/{size}"})
            self._stream(path, start, length)
        else:
            self._send_headers(200, ctype, size)
            self._stream(path, 0, size)

    def _poster(self, nome: str, query: str = "") -> None:
        """Immagine fissa dell'avatar (vedi poster.py).

        `?h=<px>` la rimpicciolisce prima di mandarla. Il poster e' il primo
        fotogramma del clip alla sua risoluzione NATIVA, che cambia da persona
        a persona — 418x556 per Shan, 1280x720 per un'altra — mentre chi la
        mostra ha un riquadro alto 480. Senza questo, a un terminale piccolo
        si mandano centinaia di kilobyte da rimpicciolire a destinazione: sul
        telefono e' spreco, su un microcontrollore e' anche memoria che non c'e'.
        Non ingrandisce mai.
        """
        from urllib.parse import parse_qs, unquote
        from . import poster
        if _PERSONE_DIR is None:
            self.send_error(404)
            return
        # Nome vuoto = "l'avatar attivo": e' cosi' che l'app chiede quasi
        # sempre, perche' nelle sue impostazioni la persona e' vuota di
        # default e significa "quella gia' in uso sul PC".
        nome = unquote(nome).strip() or _persona_attiva()
        if not nome:
            self.send_error(404)
            return
        try:
            jpg = poster.per_nome(nome, _PERSONE_DIR)
        except Exception as e:  # noqa: BLE001 - un poster non deve mai far cadere il server
            log.warning("poster %r: %s", nome, e)
            jpg = None
        if jpg is None:
            self.send_error(404)
            return
        dati = jpg.read_bytes()
        altezza = 0
        try:
            altezza = int((parse_qs(query).get("h") or ["0"])[0])
        except ValueError:
            altezza = 0
        if altezza > 0:
            dati = _rimpicciolisci(dati, altezza) or dati
        # Il clip di una persona cambia di rado: la cache evita di rimandare
        # decine di KB a ogni accensione del video.
        self._send_headers(200, "image/jpeg", len(dati),
                           {"Cache-Control": "public, max-age=300"})
        try:
            self.wfile.write(dati)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stream(self, path, start, length):
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    block = f.read(min(65536, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
        except (BrokenPipeError, ConnectionResetError):
            pass  # l'app ha chiuso (es. seek/stop del player)


def start_media_server(host: str, port: int, avatar_dir: str | None = None,
                       control_port: int = 8770) -> None:
    """Avvia il server HTTP dei media in un thread daemon."""
    global _PERSONE_DIR, _CONTROL_PORT
    _CONTROL_PORT = control_port
    if avatar_dir:
        _PERSONE_DIR = Path(avatar_dir) / "persone"
    srv = ThreadingHTTPServer((host, port), _Handler)
    threading.Thread(target=srv.serve_forever, name="media-http", daemon=True).start()
    log.info("Media server HTTP su http://%s:%d (/media/<token>, /poster/<persona>)",
             host, port)
