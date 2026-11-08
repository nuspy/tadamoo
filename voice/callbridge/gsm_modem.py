"""Driver AT per il modem GSM/GPRS USB (SIMCom SIM800C su COM6 via CH340).

Strato di CONTROLLO della chiamata cellulare diretta dal PC, in alternativa al
giro Bluetooth-HFP via Android. Funziona su questa scheda (SIM800C) per:
  - chiamate vocali (segnalazione): ATD<num>; / ATA / ATH  + URC RING/+CLIP
  - SMS testo: invio/ricezione (+CMTI)
  - stato: segnale (CSQ), registrazione (CREG), SIM (CSMINS/CPIN), operatore (COPS)
  - DTMF: AT+VTS

Lo STESSO strato AT controlla anche i moduli 4G (Quectel EC25 / SIMCom SIM7600);
su quelli l'audio della chiamata arriva come scheda audio USB. Su questo SIM800C
l'audio è solo analogico (pin non esposti): l'unica via per la voce è **a file,
turn-based** tramite i primitivi qui sotto (verificato il file system; da
validare con la SIM il fatto che CMEDPLAY raggiunga l'uplink e CREC il downlink):
  - file system flash: FSCREATE / FSWRITE / FSREAD / FSFLSIZE / FSDEL
  - riproduci file nella chiamata (TTS->interlocutore): AT+CMEDPLAY
  - registra la chiamata su file (interlocutore->STT): AT+CREC

Dipendenza: pyserial.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

import serial  # pyserial

from .util import is_valid_phone_number

log = logging.getLogger("callbridge.gsm")

# Risposte non sollecitate (URC) che possono arrivare in qualsiasi momento.
_TERMINATORS = ("OK", "ERROR", "NO CARRIER", "BUSY", "NO ANSWER", "NO DIALTONE")
_CME = ("+CME ERROR", "+CMS ERROR")


@dataclass
class CmdResult:
    cmd: str
    final: str | None          # OK / ERROR / NO CARRIER / None(timeout)
    lines: list[str]           # righe di contenuto (senza il terminatore)
    ok: bool

    def value(self, prefix: str) -> str | None:
        """Ritorna la parte dopo `prefix:` della prima riga che lo contiene."""
        for ln in self.lines:
            if ln.startswith(prefix):
                return ln[len(prefix):].strip().lstrip(":").strip()
        return None


class GsmModem:
    """Connessione AT a un modem SIM800C (o compatibile) su porta seriale."""

    def __init__(
        self,
        port: str = "COM6",
        baud: int = 115200,
        on_incoming_call: Callable[[str | None], None] | None = None,
        on_call_ended: Callable[[], None] | None = None,
        on_sms: Callable[[int], None] | None = None,
    ) -> None:
        self.port = port
        self.baud = baud
        self.on_incoming_call = on_incoming_call
        self.on_call_ended = on_call_ended
        self.on_sms = on_sms

        self._ser: serial.Serial | None = None
        self._lock = threading.Lock()          # un comando per volta
        self._running = False
        self._reader: threading.Thread | None = None

        # Sincronizzazione comando<->reader
        self._resp_lines: list[str] = []
        self._final: str | None = None
        self._resp_event = threading.Event()

        # Pausa del reader per I/O binario grezzo (FSWRITE/FSREAD)
        self._pause = threading.Event()
        self._pause_ack = threading.Event()

        self._ringing = False
        # Notifica chiamata in arrivo: il +CLIP (caller-ID) puo' non arrivare
        # (numero nascosto o rete che non lo manda). Un timer armato al primo
        # RING notifica comunque con numero None se il CLIP tarda.
        self._incoming_signaled = False
        self._ring_timer: threading.Timer | None = None

    # --- ciclo di vita ----------------------------------------------------

    # baud rapido per i trasferimenti file (FSWRITE/FSREAD); 0 = non alzare
    fast_baud: int = 460800

    def open(self) -> None:
        self._start_reader(self.baud)
        ok = self._cmd("AT").ok
        # Il modulo potrebbe gia' essere a fast_baud (sessione precedente senza
        # power-cycle): se non risponde al baud iniziale, riprova a fast_baud.
        if not ok and self.fast_baud:
            self._start_reader(self.fast_baud)
            ok = self._cmd("AT").ok
        if not ok:
            # Senza questa guardia init() spende ~1 min in 7 comandi da 8 s di
            # timeout e poi dichiara "modem aperto" con successo su una porta muta.
            raise RuntimeError(
                f"modem GSM non risponde su {self.port} "
                f"(baud {self.baud}/{self.fast_baud})")
        self.init()
        self._raise_baud()
        log.info("GSM modem aperto su %s @ %d", self.port, self._ser.baudrate)

    def _start_reader(self, baud: int) -> None:
        """(Ri)apre la seriale al baud dato e fa ripartire il thread reader."""
        self._running = False
        if self._reader is not None:
            self._reader.join(timeout=1.0)
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:  # noqa: BLE001
                pass
        self._ser = serial.Serial(self.port, baud, timeout=0.15)
        self._running = True
        self._reader = threading.Thread(target=self._read_loop, name="gsm-reader", daemon=True)
        self._reader.start()

    def _raise_baud(self) -> None:
        """Alza il baud UART CH340<->SIM800C per accelerare i file (~4x)."""
        if not self.fast_baud or self._ser.baudrate >= self.fast_baud:
            return
        if not self._cmd(f"AT+IPR={self.fast_baud}").ok:
            return
        self._start_reader(self.fast_baud)
        if not self._cmd("AT").ok:                 # fast_baud instabile: ripristina
            # Il modulo ORA ascolta a fast_baud: mandagli il ripristino a
            # fast_baud, POI riapri la seriale a baud base. (Prima si riapriva a
            # base e si inviava il comando che il modulo, a fast_baud, ignorava,
            # lasciando i due lati disallineati.)
            self._cmd(f"AT+IPR={self.baud}")
            self._start_reader(self.baud)

    def init(self) -> None:
        """Configurazione iniziale del modem."""
        self._cmd("AT")             # sveglia / autobaud
        self._cmd("ATE0")           # echo off (parsing pulito)
        self._cmd("AT+CMEE=2")      # errori verbosi (+CME ERROR: <testo>)
        self._cmd("AT+CMGF=1")      # SMS in modalità testo
        self._cmd("AT+CSCS=\"GSM\"")
        self._cmd("AT+CLIP=1")      # caller-ID sulle chiamate in arrivo
        self._cmd("AT+CNMI=2,1")    # nuovo SMS -> URC +CMTI (non dump automatico)

    def close(self) -> None:
        self._running = False
        if self._ring_timer is not None:
            self._ring_timer.cancel()
            self._ring_timer = None
        if self._reader is not None:
            self._reader.join(timeout=1.0)
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:  # noqa: BLE001
                pass
            self._ser = None

    # --- reader / dispatch URC -------------------------------------------

    def _read_loop(self) -> None:
        assert self._ser is not None
        buf = b""
        while self._running:
            if self._pause.is_set():
                self._pause_ack.set()
                time.sleep(0.02)
                continue
            try:
                data = self._ser.read(256)
            except Exception:  # noqa: BLE001 - porta chiusa/strappata
                time.sleep(0.1)
                continue
            if not data:
                continue
            buf += data
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("ascii", "replace").strip("\r ").strip()
                if line:
                    self._handle_line(line)

    def _notify_ring_no_clip(self) -> None:
        """Timer post-RING: se il +CLIP non e' arrivato, notifica con None."""
        self._ring_timer = None
        if (self._ringing and not self._incoming_signaled
                and self.on_incoming_call):
            self._incoming_signaled = True
            self.on_incoming_call(None)

    def _handle_line(self, s: str) -> None:
        # --- URC asincroni ---
        if s == "RING":
            self._ringing = True
            if (self.on_incoming_call and not self._incoming_signaled
                    and self._ring_timer is None):
                self._ring_timer = threading.Timer(1.2, self._notify_ring_no_clip)
                self._ring_timer.start()
            return
        if s.startswith("+CLIP:"):
            num = self._parse_quoted(s)
            if (self._ringing and self.on_incoming_call
                    and not self._incoming_signaled):
                self._incoming_signaled = True
                self.on_incoming_call(num)
                self._ringing = False
            return
        if s.startswith("+CMTI:"):
            idx = s.rsplit(",", 1)[-1].strip()
            if self.on_sms:
                try:
                    self.on_sms(int(idx))
                except ValueError:
                    pass
            return
        if s in ("NO CARRIER", "BUSY", "NO ANSWER", "NO DIALTONE"):
            self._ringing = False
            self._incoming_signaled = False
            if self._ring_timer is not None:
                self._ring_timer.cancel()
                self._ring_timer = None
            if self.on_call_ended:
                self.on_call_ended()
            # è anche il terminatore di ATD/ATH in corso
            self._final = s
            self._resp_event.set()
            return
        # --- terminatori di comando ---
        if s == "OK" or s == "ERROR" or s.startswith(_CME):
            if s.startswith(_CME):
                self._resp_lines.append(s)
            self._final = s
            self._resp_event.set()
            return
        # --- contenuto di risposta ---
        self._resp_lines.append(s)

    @staticmethod
    def _parse_quoted(s: str) -> str | None:
        a = s.find('"')
        if a < 0:
            return None
        b = s.find('"', a + 1)
        return s[a + 1:b] if b > a else None

    # --- comando sincrono -------------------------------------------------

    def _cmd(self, cmd: str, timeout: float = 8.0) -> CmdResult:
        assert self._ser is not None
        with self._lock:
            # Scarta i byte pendenti (es. la risposta tardiva di un comando
            # precedente andato in timeout) prima di inviare: altrimenti quel
            # residuo verrebbe attribuito a QUESTO comando.
            try:
                self._ser.reset_input_buffer()
            except Exception:  # noqa: BLE001
                pass
            self._resp_lines = []
            self._final = None
            self._resp_event.clear()
            self._ser.write((cmd + "\r").encode())
            got = self._resp_event.wait(timeout)
            final = self._final if got else None
            lines = list(self._resp_lines)
        ok = final == "OK"
        if not ok and final not in ("NO CARRIER",):
            log.debug("AT %s -> %s %s", cmd, final, lines)
        return CmdResult(cmd, final, lines, ok)

    # --- chiamate ---------------------------------------------------------

    def dial(self, number: str) -> bool:
        """Chiamata vocale in uscita (il ';' la rende voce, non dati)."""
        # Guardia difensiva: `number` finisce testualmente in ATD<number>; un
        # `;`+CR qui dentro inietterebbe comandi AT arbitrari sulla seriale.
        if not is_valid_phone_number(number):
            log.warning("GSM: numero rifiutato (non componibile): %r", number)
            return False
        return self._cmd(f"ATD{number};", timeout=20.0).ok

    def answer(self) -> bool:
        return self._cmd("ATA", timeout=20.0).ok

    def hangup(self) -> bool:
        return self._cmd("ATH").ok

    def call_state(self) -> str:
        """Stato chiamata via AT+CLCC (best-effort)."""
        r = self._cmd("AT+CLCC")
        return (r.value("+CLCC") or "idle")

    def send_dtmf(self, digits: str) -> bool:
        ok = True
        for d in digits:
            ok = self._cmd(f"AT+VTS={d}").ok and ok
        return ok

    # --- stato rete / SIM -------------------------------------------------

    def status(self) -> dict:
        csq = self._cmd("AT+CSQ").value("+CSQ")
        rssi = None
        if csq:
            try:
                rssi = int(csq.split(",")[0])
            except ValueError:
                pass
        creg = self._cmd("AT+CREG?").value("+CREG")
        cpin = self._cmd("AT+CPIN?").value("+CPIN")
        cops = self._cmd("AT+COPS?").value("+COPS")
        csmins = self._cmd("AT+CSMINS?").value("+CSMINS")
        sim_in = None
        if csmins:
            try:
                sim_in = csmins.split(",")[-1].strip() == "1"
            except Exception:  # noqa: BLE001
                pass
        registered = bool(creg and creg.split(",")[-1].strip() in ("1", "5"))
        return {
            "signal_rssi": rssi,              # 0-31 (>=10 usabile), 99=sconosciuto
            "registered": registered,
            "sim_inserted": sim_in,
            "pin": cpin,
            "operator": cops,
            "raw": {"csq": csq, "creg": creg, "cpin": cpin, "cops": cops, "csmins": csmins},
        }

    # --- SMS --------------------------------------------------------------

    def send_sms(self, number: str, text: str, timeout: float = 20.0) -> bool:
        assert self._ser is not None
        with self._lock:
            self._pause_reader()
            try:
                self._ser.reset_input_buffer()
                self._ser.write(f'AT+CMGS="{number}"\r'.encode())
                if not self._wait_raw(b">", 3.0):
                    # Annulla la CMGS in sospeso: senza ESC il modem resta in
                    # attesa del corpo e interpreterebbe il prossimo AT come testo.
                    self._ser.write(bytes([0x1B]))
                    return False
                self._ser.write(text.encode("ascii", "replace"))
                self._ser.write(bytes([0x1A]))   # Ctrl-Z = invia
                return self._wait_raw(b"OK", timeout)
            finally:
                self._resume_reader()

    def read_sms(self, index: int) -> str | None:
        r = self._cmd(f"AT+CMGR={index}")
        # le righe dopo +CMGR sono il corpo del messaggio
        body = [ln for ln in r.lines if not ln.startswith("+CMGR")]
        return "\n".join(body) if body else None

    def delete_sms(self, index: int) -> bool:
        return self._cmd(f"AT+CMGD={index}").ok

    # --- file system (per l'audio a file) ---------------------------------

    def fs_size(self, filename: str) -> int | None:
        v = self._cmd(f"AT+FSFLSIZE={filename}").value("+FSFLSIZE")
        try:
            return int(v) if v is not None else None
        except ValueError:
            return None

    def fs_write(self, filename: str, data: bytes, timeout: float = 30.0) -> bool:
        """Carica `data` come file nella flash del modulo."""
        assert self._ser is not None
        self._cmd(f"AT+FSCREATE={filename}")
        with self._lock:
            self._pause_reader()
            try:
                self._ser.reset_input_buffer()
                self._ser.write(f"AT+FSWRITE={filename},0,{len(data)},20\r".encode())
                if not self._wait_raw(b">", 3.0):
                    # ESC: annulla la FSWRITE in attesa dei byte del payload.
                    self._ser.write(bytes([0x1B]))
                    return False
                self._ser.write(data)
                return self._wait_raw(b"OK", timeout)
            finally:
                self._resume_reader()

    def fs_read(self, filename: str, timeout: float = 30.0,
                chunk: int = 4096) -> bytes | None:
        """Scarica un file dalla flash del modulo (es. registrazione CREC).

        Il SIM800 limita la dimensione per singola FSREAD: chiedere tutto il
        file in un colpo da `+CME ERROR`. Si legge quindi a blocchi (`chunk`
        byte) avanzando il `location`.
        """
        assert self._ser is not None
        size = self.fs_size(filename)
        if not size:
            return None
        out = b""
        with self._lock:
            self._pause_reader()
            try:
                while len(out) < size:
                    want = min(chunk, size - len(out))
                    self._ser.reset_input_buffer()
                    self._ser.write(
                        f"AT+FSREAD={filename},1,{want},{len(out)}\r".encode())
                    deadline = time.time() + timeout
                    buf = b""
                    # Formato (echo off): "\r\n<DATA(want)>\r\nOK\r\n".
                    # Si legge esattamente il payload + il terminatore invece di
                    # fermarsi al primo "OK": i byte 0x4F4B ("OK") compaiono
                    # normalmente nell'audio binario e troncherebbero il chunk.
                    need = want + 8  # "\r\n" + payload(want) + "\r\nOK\r\n"
                    while time.time() < deadline:
                        c = self._ser.read(max(1, need - len(buf)))
                        if c:
                            buf += c
                        # Errore del modem: NON e' audio, e' testo -> fallimento.
                        if b"+CME ERROR" in buf or b"+CMS ERROR" in buf:
                            log.warning("GSM fs_read: errore modem: %r", buf)
                            self._reroute_urcs(buf)
                            return out or None
                        # Chiamata caduta durante la lettura: esci e reinstrada.
                        if (b"NO CARRIER" in buf or b"BUSY" in buf
                                or b"NO ANSWER" in buf):
                            self._reroute_urcs(buf)
                            return out or None
                        start = 2 if buf[:2] == b"\r\n" else 0
                        if len(buf) >= start + want and b"OK" in buf[start + want:]:
                            break
                        if not c:
                            time.sleep(0.01)
                    start = 2 if buf[:2] == b"\r\n" else 0
                    data = buf[start:start + want]
                    if len(data) < want:
                        # Blocco incompleto (timeout): non troncare in silenzio.
                        log.warning("GSM fs_read: blocco incompleto %d/%d byte",
                                    len(data), want)
                        return out or None
                    out += data
                return out or None
            finally:
                self._resume_reader()

    def fs_delete(self, filename: str) -> bool:
        return self._cmd(f"AT+FSDEL={filename}").ok

    # --- audio nella chiamata (DA VALIDARE CON LA SIM) --------------------

    def play_media(self, filename: str, path: int = 3, volume: int = 80) -> bool:
        """Riproduce un file audio. `path` 0-3 seleziona la destinazione
        (da validare quale invii l'audio all'UPLINK/interlocutore)."""
        return self._cmd(f'AT+CMEDPLAY=1,"{filename}",{path},{volume}',
                         timeout=10.0).ok

    def stop_media(self) -> bool:
        return self._cmd("AT+CMEDPLAY=0").ok

    @staticmethod
    def record_path(file_id: int = 1) -> str:
        r"""Percorso del file prodotto da CREC: C:\REC\<id>.DVI (DVI/IMA ADPCM)."""
        return f"C:\\REC\\{file_id}.DVI"

    def record_start(self, file_id: int = 1, channel: int = 2) -> bool:
        """Avvia la registrazione della chiamata su file (id 1-99).

        Sintassi SIM800: `AT+CREC=<mode>,<index>,<channel>` (3 parametri:
        con 2 da `+CME ERROR: Param count not enough`). `channel`: 1=locale,
        2=remoto (voce dell'interlocutore), 3=entrambi. Il file viene scritto
        in C:\\REC\\<id>.DVI; se esiste gia' CREC fallisce, quindi lo si elimina.
        """
        self._cmd(f"AT+FSDEL={self.record_path(file_id)}")
        return self._cmd(f"AT+CREC=1,{file_id},{channel}").ok

    def record_stop(self) -> bool:
        return self._cmd("AT+CREC=2").ok

    # --- helper I/O grezzo ------------------------------------------------

    def _pause_reader(self) -> None:
        self._pause_ack.clear()
        self._pause.set()
        self._pause_ack.wait(1.0)

    def _resume_reader(self) -> None:
        self._pause.clear()

    def _reroute_urcs(self, buf: bytes) -> None:
        """Reinstrada a `_handle_line` gli URC trovati in un buffer di I/O grezzo.

        Durante FSREAD/FSWRITE/CMGS il reader e' in pausa e i byte non passano
        da `_handle_line`: un `NO CARRIER` (caduta chiamata) arrivato in quel
        momento andrebbe perso e `on_call_ended` non scatterebbe mai (la
        conversazione GSM continuerebbe a registrare/parlare su linea morta).
        Qui si estraggono le sole righe URC ben delimitate e le si reinstrada.
        """
        try:
            text = buf.decode("ascii", "replace")
        except Exception:  # noqa: BLE001
            return
        for line in text.replace("\r", "\n").split("\n"):
            line = line.strip()
            if not line:
                continue
            if line in ("NO CARRIER", "BUSY", "NO ANSWER", "NO DIALTONE",
                        "RING") or line.startswith(("+CLIP:", "+CMTI:")):
                self._handle_line(line)

    def _wait_raw(self, token: bytes, timeout: float) -> bool:
        assert self._ser is not None
        deadline = time.time() + timeout
        buf = b""
        found = False
        while time.time() < deadline:
            chunk = self._ser.read(64)
            if chunk:
                buf += chunk
                if token in buf:
                    found = True
                    break
                if b"ERROR" in buf:
                    break
            else:
                time.sleep(0.02)
        # Reinstrada eventuali URC (es. NO CARRIER) letti col reader in pausa.
        self._reroute_urcs(buf)
        return found


# --- CLI di test -----------------------------------------------------------

def _main() -> None:
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Driver/test modem GSM SIM800C")
    ap.add_argument("--port", default="COM6")
    ap.add_argument("--baud", type=int, default=115200)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("fstest")
    sub.add_parser("monitor")
    pc = sub.add_parser("call"); pc.add_argument("number")
    ps = sub.add_parser("sms"); ps.add_argument("number"); ps.add_argument("text")
    args = ap.parse_args()

    def on_call(num): print(f"\n>>> CHIAMATA IN ARRIVO da {num or 'sconosciuto'}")
    def on_end():     print("\n>>> chiamata terminata")
    def on_sms(i):    print(f"\n>>> nuovo SMS index {i}")

    m = GsmModem(args.port, args.baud, on_call, on_end, on_sms)
    m.open()
    try:
        if args.cmd == "status":
            st = m.status()
            for k, v in st.items():
                print(f"  {k}: {v}")
        elif args.cmd == "fstest":
            ok = m.fs_write("C:\\fstest.bin", b"HELLO-GSM")
            rd = m.fs_read("C:\\fstest.bin")
            m.fs_delete("C:\\fstest.bin")
            print(f"  write={ok} read={rd!r}")
        elif args.cmd == "call":
            print(f"  dial {args.number}:", m.dial(args.number))
            print("  (Ctrl+C per riagganciare)")
            try:
                while True: time.sleep(1)
            except KeyboardInterrupt:
                m.hangup()
        elif args.cmd == "sms":
            print("  send_sms:", m.send_sms(args.number, args.text))
        elif args.cmd == "monitor":
            print("  In ascolto di chiamate/SMS in arrivo. Ctrl+C per uscire.")
            try:
                while True: time.sleep(1)
            except KeyboardInterrupt:
                pass
    finally:
        m.close()


if __name__ == "__main__":
    _main()
