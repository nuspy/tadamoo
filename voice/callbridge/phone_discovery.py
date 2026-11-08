"""Ricerca automatica del telefono gateway sulla rete.

Il telefono non ha un indirizzo stabile: cambia a ogni riconnessione al Wi-Fi,
e un telefono che si riavvia da solo lo cambia di continuo. Configurarlo a mano
nel config e' quindi una configurazione statica per una cosa che statica non e'.

Qui il telefono viene cercato da una cascata di strategie, dalla piu' affidabile
alla piu' generica; la prima che risponde vince:

    0. cache      l'ultimo indirizzo che ha funzionato (un /health e via)
    1. usb        adb forward: il PC parla con 127.0.0.1 e l'IP non conta piu'
    2. mdns       il servizio _callapicall._tcp annunciato dall'app
    3. broadcast  probe UDP, per le reti dove il multicast e' filtrato
    4. scan       scansione della /24, ultima spiaggia

Oltre all'indirizzo, la ricerca restituisce *chi* e' il telefono trovato: in
particolare il suo nome Bluetooth, con cui `ks_native` individua l'endpoint
audio HFP corrispondente senza liste di modelli cablate nel codice.
"""

from __future__ import annotations

import json
import logging
import shutil
import socket
import subprocess
import threading
from dataclasses import dataclass, asdict
from pathlib import Path

import requests

log = logging.getLogger("callbridge.discovery")

CACHE_FILE = Path(__file__).resolve().parent.parent / ".phone_cache.json"

# Devono combaciare con DiscoveryResponder / ControlService lato Android.
MDNS_TYPE = "_callapicall._tcp.local."
UDP_PORT = 8079
UDP_PROBE = b"CALLAPICALL-DISCOVERY?"


@dataclass
class PhoneEndpoint:
    """Un telefono raggiungibile, con la sua carta d'identita'."""

    host: str
    port: int = 8080
    id: str = ""
    model: str = ""
    bt_name: str = ""
    via: str = ""
    serial: str = ""          # seriale adb, valorizzato solo per la via USB

    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def describe(self) -> str:
        nome = self.model or "telefono"
        bt = f", BT '{self.bt_name}'" if self.bt_name else ""
        return f"{nome} su {self.host}:{self.port} (via {self.via}{bt})"


# --- interrogazione di un candidato ------------------------------------------


def _identify(host: str, port: int, token: str, timeout: float = 2.0) -> dict | None:
    """Chiede la carta d'identita'. None se non risponde o non e' la nostra app.

    Su una app vecchia /identify non esiste: in quel caso ripieghiamo su
    /health, cosi' un bridge aggiornato continua a funzionare con un telefono
    non ancora aggiornato.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    base = f"http://{host}:{port}"
    try:
        resp = requests.get(f"{base}/identify", headers=headers, timeout=timeout)
        if resp.ok:
            data = resp.json()
            if isinstance(data, dict) and "id" in data:
                return data
        if resp.status_code == 404:
            # App non ancora aggiornata: niente /identify. Per non scambiare un
            # servizio qualsiasi che risponde "ok" per il nostro telefono, si
            # pretende anche uno /status con la forma giusta.
            health = requests.get(f"{base}/health", headers=headers, timeout=timeout)
            if not (health.ok and health.json().get("status") == "ok"):
                return None
            stato = requests.get(f"{base}/status", headers=headers, timeout=timeout)
            if stato.ok and "state" in stato.json():
                log.info("%s:%s risponde ma non conosce /identify (app da aggiornare)",
                         host, port)
                return {"id": "", "model": "", "btname": ""}
    except (requests.RequestException, ValueError):
        pass
    return None


def _endpoint_from(host: str, port: int, card: dict, via: str,
                   serial: str = "") -> PhoneEndpoint:
    # `port` e' la porta a cui abbiamo parlato davvero, che non sempre e' quella
    # che il telefono dichiara: via adb forward il telefono dice "8080" ma noi
    # passiamo da una porta locale qualsiasi su 127.0.0.1.
    return PhoneEndpoint(
        host=host,
        port=port,
        id=str(card.get("id") or ""),
        model=str(card.get("model") or ""),
        bt_name=str(card.get("btname") or "").strip(),
        via=via,
        serial=serial,
    )


# --- 0. cache ----------------------------------------------------------------


def _load_cache() -> dict | None:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_cache(ep: PhoneEndpoint) -> None:
    """Ricorda l'ultimo telefono trovato, cosi' la volta dopo si parte da li'."""
    try:
        CACHE_FILE.write_text(json.dumps(asdict(ep), indent=2), encoding="utf-8")
    except OSError as e:
        log.debug("cache non scrivibile: %s", e)


def from_cache(token: str, want_id: str = "") -> PhoneEndpoint | None:
    data = _load_cache()
    if not data:
        return None
    host, port = data.get("host", ""), int(data.get("port") or 8080)
    if not host:
        return None
    card = _identify(host, port, token, timeout=1.5)
    if card is None:
        return None
    ep = _endpoint_from(host, port, card, "cache", data.get("serial", ""))
    # Il nome BT non viaggia mai in chiaro: se la carta non lo porta, riusiamo
    # quello memorizzato invece di perderlo.
    if not ep.bt_name:
        ep.bt_name = str(data.get("bt_name") or "")
    if want_id and ep.id and ep.id != want_id:
        return None
    return ep


# --- 1. USB (adb) ------------------------------------------------------------


def _adb_exe(configured: str = "") -> str | None:
    if configured and Path(configured).exists():
        return configured
    found = shutil.which("adb")
    if found:
        return found
    for p in (
        Path.home() / "AppData/Local/Android/Sdk/platform-tools/adb.exe",
        Path("C:/Program Files (x86)/Android/android-sdk/platform-tools/adb.exe"),
    ):
        if p.exists():
            return str(p)
    return None


def _adb(exe: str, *args: str, timeout: float = 10.0) -> str:
    out = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    return out.stdout.strip()


def via_usb(token: str, adb_path: str = "", want_id: str = "") -> PhoneEndpoint | None:
    """Telefono collegato via cavo: `adb forward` e l'indirizzo non conta piu'.

    E' la via piu' solida in assoluto — sopravvive ai cambi di IP e alle
    riaccensioni del telefono — e in piu' il cavo lo tiene sotto carica.
    """
    exe = _adb_exe(adb_path)
    if not exe:
        return None
    try:
        righe = _adb(exe, "devices").splitlines()[1:]
        seriali = [r.split()[0] for r in righe if r.strip().endswith("device")]
        # Forward gia' attivi: riusarli evita di accumulare una porta nuova a
        # ogni ricerca (il telefono che si riavvia ne provoca parecchie).
        attivi: dict[str, str] = {}
        for riga in _adb(exe, "forward", "--list").splitlines():
            campi = riga.split()
            if len(campi) >= 3 and campi[2] == "tcp:8080":
                attivi[campi[0]] = campi[1].removeprefix("tcp:")

        for seriale in seriali:
            porta = attivi.get(seriale, "")
            if not porta:
                # tcp:0 = "scegli tu una porta libera", e adb la stampa.
                porta = _adb(exe, "-s", seriale, "forward", "tcp:0", "tcp:8080")
            if not porta.isdigit():
                continue
            card = _identify("127.0.0.1", int(porta), token, timeout=3.0)
            if card is None:
                # Il forward non porta a nulla (app ferma, telefono spento):
                # si butta, cosi' la volta dopo se ne crea uno pulito.
                _adb(exe, "-s", seriale, "forward", "--remove", f"tcp:{porta}")
                continue
            if not card.get("model"):
                card["model"] = _adb(exe, "-s", seriale, "shell",
                                     "getprop", "ro.product.model")
            if not card.get("btname"):
                card["btname"] = _adb(exe, "-s", seriale, "shell",
                                      "settings", "get", "secure", "bluetooth_name")
            ep = _endpoint_from("127.0.0.1", int(porta), card, "usb", seriale)
            if want_id and ep.id and ep.id != want_id:
                continue
            return ep
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("via USB non disponibile: %s", e)
    return None


# --- 2. mDNS -----------------------------------------------------------------


def via_mdns(token: str, timeout: float = 3.0, want_id: str = "") -> PhoneEndpoint | None:
    """Il servizio annunciato dall'app: funziona con qualunque IP e con piu' telefoni."""
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except ImportError:
        log.debug("zeroconf non installato: salto mDNS")
        return None

    trovati: list[tuple[str, int]] = []
    evento = threading.Event()

    class _Listener(ServiceListener):
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name, timeout=int(timeout * 1000))
            if not info:
                return
            for indirizzo in info.parsed_addresses():
                trovati.append((indirizzo, info.port))
            if trovati:
                evento.set()

        def update_service(self, zc, type_, name):
            pass

        def remove_service(self, zc, type_, name):
            pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, MDNS_TYPE, _Listener())
        evento.wait(timeout)
        for host, port in trovati:
            card = _identify(host, port, token, timeout=2.0)
            if card is None:
                continue
            ep = _endpoint_from(host, port, card, "mdns")
            if want_id and ep.id and ep.id != want_id:
                continue
            return ep
    finally:
        zc.close()
    return None


# --- 3. broadcast UDP --------------------------------------------------------


def _local_ipv4() -> list[str]:
    indirizzi = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in indirizzi:
                indirizzi.append(ip)
    except OSError:
        pass
    return indirizzi


def via_broadcast(token: str, timeout: float = 2.0,
                  want_id: str = "") -> PhoneEndpoint | None:
    """Probe UDP: passa anche dove il multicast (quindi mDNS) e' filtrato."""
    destinazioni = {"255.255.255.255"}
    for ip in _local_ipv4():
        destinazioni.add(".".join(ip.split(".")[:3]) + ".255")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)
    try:
        for dest in sorted(destinazioni):
            try:
                sock.sendto(UDP_PROBE, (dest, UDP_PORT))
            except OSError:
                continue
        while True:
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                return None
            try:
                card = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            host = addr[0]
            # Qui invece la porta dichiarata serve: il telefono ci dice su
            # quale porta ascolta il suo server HTTP.
            porta = int(card.get("port") or 8080)
            # La risposta UDP non e' autenticata: la carta completa (col nome
            # Bluetooth) la chiediamo comunque via HTTP col token.
            completa = _identify(host, porta, token, timeout=2.0) or card
            ep = _endpoint_from(host, porta, completa, "broadcast")
            if want_id and ep.id and ep.id != want_id:
                continue
            return ep
    finally:
        sock.close()


# --- 4. scansione della rete locale ------------------------------------------


def via_scan(token: str, port: int = 8080, timeout: float = 4.0,
             want_id: str = "") -> PhoneEndpoint | None:
    """Ultima spiaggia: bussa alla porta su tutta la /24, in parallelo."""
    reti = []
    for ip in _local_ipv4():
        prefisso = ".".join(ip.split(".")[:3])
        if prefisso not in reti:
            reti.append(prefisso)

    for prefisso in reti:
        candidati: list[str] = []
        lock = threading.Lock()

        def bussa(host: str) -> None:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.6)
            try:
                if s.connect_ex((host, port)) == 0:
                    with lock:
                        candidati.append(host)
            except OSError:
                pass
            finally:
                s.close()

        threads = [threading.Thread(target=bussa, args=(f"{prefisso}.{i}",), daemon=True)
                   for i in range(1, 255)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout)

        for host in candidati:
            card = _identify(host, port, token, timeout=1.5)
            if card is None:
                continue
            ep = _endpoint_from(host, port, card, "scan")
            if want_id and ep.id and ep.id != want_id:
                continue
            return ep
    return None


# --- cascata -----------------------------------------------------------------


def discover(token: str, adb_path: str = "", want_id: str = "",
             use_cache: bool = True) -> PhoneEndpoint | None:
    """Cerca il telefono provando le strategie in ordine di affidabilita'.

    `want_id` limita la ricerca a un telefono preciso (utile con piu' telefoni
    accesi); senza, vince il primo che risponde.
    """
    strategie: list[tuple[str, object]] = []
    if use_cache:
        strategie.append(("cache", lambda: from_cache(token, want_id)))
    strategie += [
        ("usb", lambda: via_usb(token, adb_path, want_id)),
        ("mdns", lambda: via_mdns(token, 3.0, want_id)),
        ("broadcast", lambda: via_broadcast(token, 2.0, want_id)),
        ("scan", lambda: via_scan(token, 8080, 4.0, want_id)),
    ]

    for nome, strategia in strategie:
        try:
            ep = strategia()
        except Exception as e:  # una via che esplode non deve fermare le altre
            log.debug("strategia %s fallita: %s", nome, e)
            continue
        if ep:
            log.info("telefono trovato: %s", ep.describe())
            save_cache(ep)
            return ep
        log.debug("strategia %s: nessun telefono", nome)

    log.warning("nessun telefono trovato (provate: %s)",
                ", ".join(n for n, _ in strategie))
    return None


if __name__ == "__main__":
    # Diagnostica: "dove sta il telefono adesso?"
    #   python -m callbridge.phone_discovery [token]
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        from .config import Config
        cfg = Config.load()
        token, adb, want = cfg.auth_token, cfg.adb_exe, cfg.phone_id
    except Exception:
        token = sys.argv[1] if len(sys.argv) > 1 else ""
        adb, want = "", ""

    trovato = discover(token=token, adb_path=adb, want_id=want)
    print(trovato.describe() if trovato else "nessun telefono trovato")
