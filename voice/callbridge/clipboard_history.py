"""Cronologia degli appunti con Ctrl+Alt+frecce (componente senza GUI ne' parametri).

  Ctrl+Alt+↑  copia la selezione dell'app in primo piano (anche nei terminali)
  Ctrl+Alt+←  elemento precedente (piu' vecchio) della cronologia
  Ctrl+Alt+→  elemento successivo (piu' recente); agli estremi resta sull'ultimo
  Ctrl+Alt+↓  incolla l'elemento selezionato

Ogni tasto mostra una finestrella col contenuto selezionato (overlay, stesso
stile delle icone "In ascolto"/"Elaboro…"). Scorrere mette subito l'elemento
negli appunti del sistema, quindi anche un Ctrl+V normale incolla quello.

Memoria: tutto cio' che passa dagli appunti (anche un Ctrl+C qualunque) entra
in cima; un contenuto gia' presente risale in cima invece di duplicarsi.
Restano gli ultimi 50 testi e le ultime 10 immagini, su disco in
%LOCALAPPDATA%\\callAPIcall\\clipboard su Windows, ~/.local/share/callAPIcall/clipboard
su Linux (sopravvivono al riavvio). Cio' che e'
marcato "da non registrare" (le password dei password manager, i passaggi
temporanei della dettatura) non entra.

Copia e incolla partono quando si lasciano Ctrl e Alt: inviare Ctrl+C con Alt
ancora premuto arriverebbe all'app come Ctrl+Alt+C.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

from .desktop import Binding, Desktop, DesktopUnsupported, KeyHook, platform

log = logging.getLogger("callbridge.clipboard")

MAX_TEXTS = 50
MAX_IMAGES = 10
_MAX_TEXT_CHARS = 2_000_000       # oltre (dump enormi) non si registra
_POLL_S = 0.4                     # controllo del numero di sequenza degli appunti
_PREVIEW_CHARS = 320
_PREVIEW_LINES = 6
_THUMB = (352, 200)


def _default_dir() -> Path:
    base = (os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
            or str(Path.home() / ".local" / "share"))
    return Path(base) / "callAPIcall" / "clipboard"


class ClipStore:
    """Elenco dal piu' recente al piu' vecchio: testi in JSON, immagini in PNG."""

    def __init__(self, folder: Path | None = None) -> None:
        self.dir = folder or _default_dir()
        self.img_dir = self.dir / "images"
        self.index_file = self.dir / "history.json"
        self.entries: list[dict] = []

    def load(self) -> None:
        self.img_dir.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(self.index_file.read_text(encoding="utf-8"))
            self.entries = [e for e in data.get("entries", [])
                            if e.get("kind") == "text"
                            or (self.img_dir / e.get("file", "")).is_file()]
        except FileNotFoundError:
            self.entries = []
        except Exception as e:  # noqa: BLE001 - file rovinato: si riparte
            log.warning("Appunti: cronologia illeggibile (%s), riparto da vuota", e)
            self.entries = []

    def _save(self) -> None:
        tmp = self.index_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"entries": self.entries}, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, self.index_file)

    def _push(self, entry: dict) -> dict:
        """In cima; se c'era gia' (stesso hash) si sposta invece di duplicarsi."""
        for i, e in enumerate(self.entries):
            if e["hash"] == entry["hash"]:
                old = self.entries.pop(i)
                old["ts"] = entry["ts"]
                if entry.get("file") and entry["file"] != old.get("file"):
                    (self.img_dir / entry["file"]).unlink(missing_ok=True)
                self.entries.insert(0, old)
                self._save()
                return old
        self.entries.insert(0, entry)
        self._trim()
        self._save()
        return entry

    def _trim(self) -> None:
        texts = images = 0
        keep: list[dict] = []
        for e in self.entries:
            if e["kind"] == "text":
                texts += 1
                if texts > MAX_TEXTS:
                    continue
            else:
                images += 1
                if images > MAX_IMAGES:
                    (self.img_dir / e["file"]).unlink(missing_ok=True)
                    continue
            keep.append(e)
        self.entries = keep

    def add_text(self, text: str) -> dict | None:
        if not text or not text.strip() or len(text) > _MAX_TEXT_CHARS:
            return None
        h = "t:" + hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()
        return self._push({"id": uuid.uuid4().hex, "kind": "text", "ts": time.time(),
                           "hash": h, "text": text})

    def add_image(self, img) -> dict | None:
        h = "i:" + hashlib.sha1(
            f"{img.mode}{img.size}".encode() + img.tobytes()).hexdigest()
        for e in self.entries:              # gia' presente: niente PNG nuovo
            if e["hash"] == h:
                return self._push(dict(e, ts=time.time()))
        entry = {"id": uuid.uuid4().hex, "kind": "image", "ts": time.time(),
                 "hash": h, "w": img.size[0], "h": img.size[1]}
        entry["file"] = entry["id"] + ".png"
        img.save(self.img_dir / entry["file"], "PNG")
        return self._push(entry)

    def image(self, entry: dict):
        from PIL import Image
        img = Image.open(self.img_dir / entry["file"])
        img.load()
        return img


class ClipboardHistory:
    """Hotkey Ctrl+Alt+frecce + monitor degli appunti + finestrella overlay."""

    def __init__(self, overlay=None, store: ClipStore | None = None,
                 desktop: Desktop | None = None) -> None:
        self._overlay = overlay
        self._desk = desktop or platform()
        self._clip = self._desk.clipboard
        self.store = store or ClipStore()
        self._index = 0                    # elemento selezionato (0 = piu' recente)
        self._lock = threading.RLock()
        self._seen = 0                     # numero di sequenza gia' esaminato
        self._hook: KeyHook | None = None
        self._stop = threading.Event()
        self._thumbs: dict[str, bytes] = {}

    # --- ciclo di vita ---------------------------------------------------

    def start(self) -> bool:
        try:
            self.store.load()
        except Exception as e:  # noqa: BLE001 - cartella non creabile
            log.error("Appunti: cronologia non disponibile: %s", e)
            return False
        ctrl_alt = frozenset({"ctrl", "alt"})
        try:
            self._hook = self._desk.key_hook("appunti", [
                Binding(ctrl_alt, "up", self._copy, exact=True),
                Binding(ctrl_alt, "down", self._paste, exact=True),
                Binding(ctrl_alt, "left", lambda: self._nav(+1), exact=True, repeat=True),
                Binding(ctrl_alt, "right", lambda: self._nav(-1), exact=True, repeat=True),
            ])
        except DesktopUnsupported as e:
            log.error("Appunti: cronologia non disponibile: %s", e)
            return False
        if not self._hook.start():
            self._hook = None
            return False
        self._capture()                    # cio' che c'e' negli appunti adesso
        threading.Thread(target=self._monitor, name="clipboard-monitor",
                         daemon=True).start()
        log.info("Cronologia appunti attiva: Ctrl+Alt+↑ copia, ←/→ scorre, ↓ incolla "
                 "(%d elementi in memoria)", len(self.store.entries))
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._hook is not None:
            self._hook.stop()
            self._hook = None

    # --- registrazione ---------------------------------------------------

    def _monitor(self) -> None:
        while not self._stop.wait(_POLL_S):
            if self._clip.sequence() != self._seen:
                try:
                    self._capture()
                except Exception as e:  # noqa: BLE001 - il monitor non deve morire
                    log.warning("Appunti: lettura fallita: %s", e)

    def _capture(self) -> dict | None:
        """Registra il contenuto attuale degli appunti, se nuovo."""
        with self._lock:
            seq = self._clip.sequence()
            if seq == self._seen:
                return None
            content = self._clip.read()
            # Il numero si rilegge DOPO: se l'app stava ancora scrivendo, cio'
            # che abbiamo letto e' gia' lo stato finale.
            self._seen = self._clip.sequence()
            if content is None or content[0] == "excluded":
                return None
            kind, value = content
            entry = (self.store.add_text(value) if kind == "text"
                     else self.store.add_image(value))
            if entry is not None:
                self._index = 0
            return entry

    def _select_into_clipboard(self, entry: dict) -> None:
        """Mette l'elemento negli appunti, marcato "da non registrare": non va a
        ricadere in cima alla cronologia ne' nella Win+V di Windows."""
        if entry["kind"] == "text":
            self._clip.write_text(entry["text"], exclude=True)
        else:
            self._clip.write_image(self.store.image(entry), exclude=True)
        with self._lock:
            self._seen = self._clip.sequence()

    # --- azioni (worker dell'hook, in ordine) -----------------------------

    def _wait_mods_released(self, timeout: float = 3.0) -> None:
        t_end = time.monotonic() + timeout
        while self._clip.modifiers_down() and time.monotonic() < t_end:
            time.sleep(0.02)

    def _copy(self) -> None:
        self._wait_mods_released()
        prima = self._clip.sequence()
        self._clip.send_copy()
        t_end = time.monotonic() + 0.8
        while self._clip.sequence() == prima and time.monotonic() < t_end:
            time.sleep(0.02)
        if self._clip.sequence() == prima:
            self._show("empty", "Nessuna selezione da copiare")
            return
        time.sleep(0.05)               # l'app finisce di scrivere gli altri formati
        entry = self._capture()
        with self._lock:
            entries = self.store.entries
            if entry is None and entries:
                entry = entries[0]     # gia' registrato dal monitor nel frattempo
            self._index = 0
        if entry is None:
            self._show("empty", "Copiato, ma non e' testo ne' immagine")
            return
        self._show("copy", "Copiato", entry)

    def _nav(self, delta: int) -> None:
        with self._lock:
            entries = self.store.entries
            if not entries:
                self._show("empty", "Cronologia vuota")
                return
            nuovo = max(0, min(len(entries) - 1, self._index + delta))
            bordo = nuovo == self._index
            self._index = nuovo
            entry = entries[nuovo]
        if not bordo:
            self._select_into_clipboard(entry)
        if delta > 0:
            self._show("prev", "Il più vecchio" if bordo else "Precedente", entry)
        else:
            self._show("next", "Il più recente" if bordo else "Successivo", entry)

    def _paste(self) -> None:
        with self._lock:
            entries = self.store.entries
            entry = entries[self._index] if entries else None
        if entry is None:
            self._show("empty", "Cronologia vuota")
            return
        self._wait_mods_released()
        # Sempre riscritto: negli appunti potrebbe esserci altro (es. una
        # password marcata "da non registrare", copiata dopo l'ultima voce).
        self._select_into_clipboard(entry)
        time.sleep(0.03)
        self._clip.send_paste()
        self._show("paste", "Incollato", entry)

    # --- overlay -----------------------------------------------------------

    def _show(self, action: str, title: str, entry: dict | None = None) -> None:
        if self._overlay is None or not hasattr(self._overlay, "show_clipboard"):
            return
        card: dict = {"action": action, "title": title}
        if entry is not None:
            with self._lock:
                entries = self.store.entries
                pos = next((i for i, e in enumerate(entries) if e is entry), self._index)
                card["pos"] = f"{pos + 1}/{len(entries)}"
            if entry["kind"] == "text":
                card["kind"] = "text"
                card["text"] = _preview(entry["text"])
                card["size"] = f"{len(entry['text'])} caratteri"
            else:
                card["kind"] = "image"
                card["png"] = self._thumb(entry)
                card["size"] = f"{entry.get('w', '?')}×{entry.get('h', '?')}"
        try:
            self._overlay.show_clipboard(card)
        except Exception as e:  # noqa: BLE001 - l'overlay non deve bloccare le azioni
            log.debug("Appunti: overlay non aggiornato: %s", e)

    def _thumb(self, entry: dict) -> bytes | None:
        cached = self._thumbs.get(entry["id"])
        if cached is not None:
            return cached
        try:
            img = self.store.image(entry).convert("RGB")
            img.thumbnail(_THUMB)
            buf = io.BytesIO()
            img.save(buf, "PNG")
            png = buf.getvalue()
        except Exception as e:  # noqa: BLE001
            log.debug("Appunti: miniatura non generata: %s", e)
            return None
        if len(self._thumbs) > 2 * MAX_IMAGES:
            self._thumbs.clear()
        self._thumbs[entry["id"]] = png
        return png


def _preview(text: str) -> str:
    """Prime righe del testo, accorciate per la finestrella."""
    righe = [r.rstrip() for r in text.strip().splitlines()]
    corto = "\n".join(righe[:_PREVIEW_LINES])
    if len(corto) > _PREVIEW_CHARS:
        corto = corto[:_PREVIEW_CHARS].rstrip() + "…"
    elif len(righe) > _PREVIEW_LINES:
        corto += "\n…"
    return corto
