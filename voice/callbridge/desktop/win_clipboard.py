"""Appunti di Windows e invio tasti: le primitive comuni a dettatura e cronologia.

- lettura/scrittura di testo e immagini negli appunti, con tentativi ripetuti
  (OpenClipboard fallisce se un'altra app li tiene aperti in quel momento);
- il marcatore standard "escludi dai monitor degli appunti", che i password
  manager mettono sulle password e che la dettatura mette sui propri passaggi
  temporanei;
- invio di Ctrl+C / Ctrl+V adatto all'app in primo piano: nei terminali Ctrl+C
  senza selezione INTERROMPE il processo in corso, quindi li' si usano
  Ctrl+Ins / Maiusc+Ins.
"""

from __future__ import annotations

import ctypes
import io
import logging
import os
import time
from ctypes import wintypes

import win32clipboard
import win32con

log = logging.getLogger("callbridge.winclip")

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                             ctypes.POINTER(wintypes.DWORD)]
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

# Formati registrati che dicono "non registrare questo contenuto": il primo e'
# quello documentato da Microsoft per i monitor degli appunti, il secondo il
# flag della cronologia di Windows (Win+V), DWORD 0 = escludi.
_FMT_EXCLUDE = win32clipboard.RegisterClipboardFormat(
    "ExcludeClipboardContentFromMonitorProcessing")
_FMT_HISTORY = win32clipboard.RegisterClipboardFormat("CanIncludeInClipboardHistory")


def sequence() -> int:
    """Contatore di Windows che cambia a ogni scrittura degli appunti."""
    return int(_user32.GetClipboardSequenceNumber())


def _open(retries: int = 10) -> bool:
    for _ in range(retries):
        try:
            win32clipboard.OpenClipboard()
            return True
        except Exception:  # noqa: BLE001 - un'altra app li tiene aperti
            time.sleep(0.03)
    return False


def _mark_excluded() -> None:
    win32clipboard.SetClipboardData(_FMT_EXCLUDE, b"\x00")
    win32clipboard.SetClipboardData(_FMT_HISTORY, b"\x00\x00\x00\x00")


def read(honor_exclude: bool = True) -> tuple[str, object] | None:
    """Contenuto attuale: ("text", str) | ("image", PIL.Image) | ("excluded", None)
    | None se vuoto, illeggibile o di un tipo che non trattiamo (es. file).

    `honor_exclude=False` legge anche cio' che e' marcato da non registrare:
    serve a chi lo deve solo rimettere al suo posto (la dettatura)."""
    if not _open():
        return None
    try:
        if honor_exclude and win32clipboard.IsClipboardFormatAvailable(_FMT_EXCLUDE):
            return ("excluded", None)
        if honor_exclude and win32clipboard.IsClipboardFormatAvailable(_FMT_HISTORY):
            try:
                if win32clipboard.GetClipboardData(_FMT_HISTORY)[:4] == b"\x00\x00\x00\x00":
                    return ("excluded", None)
            except Exception:  # noqa: BLE001
                pass
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            try:
                return ("text", win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT))
            except Exception:  # noqa: BLE001
                return None
        dib = None
        for fmt in (win32con.CF_DIB, win32con.CF_DIBV5):
            if win32clipboard.IsClipboardFormatAvailable(fmt):
                try:
                    dib = win32clipboard.GetClipboardData(fmt)
                    break
                except Exception:  # noqa: BLE001
                    dib = None
    finally:
        win32clipboard.CloseClipboard()
    if dib:
        img = _dib_to_image(dib)
        if img is not None:
            return ("image", img)
    return None


def _dib_to_image(dib: bytes):
    """CF_DIB -> PIL.Image, ricostruendo l'header BMP che negli appunti manca."""
    try:
        import struct

        from PIL import Image
        header_size = struct.unpack_from("<I", dib, 0)[0]
        bit_count = struct.unpack_from("<H", dib, 14)[0]
        compression = struct.unpack_from("<I", dib, 16)[0]
        colors = struct.unpack_from("<I", dib, 32)[0]
        if bit_count <= 8 and colors == 0:
            colors = 1 << bit_count
        offset = 14 + header_size + colors * 4
        if compression == 3 and header_size == 40:   # BI_BITFIELDS: 3 maschere
            offset += 12
        bmp = b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, offset) + dib
        img = Image.open(io.BytesIO(bmp))
        img.load()
        return img
    except Exception as e:  # noqa: BLE001
        log.debug("immagine negli appunti non decodificabile: %s", e)
        return None


def write_text(text: str, exclude: bool = False) -> int:
    """Scrive testo negli appunti. Ritorna il numero di sequenza risultante
    (0 se non e' stato possibile)."""
    if not _open():
        log.error("appunti occupati: scrittura testo fallita")
        return 0
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
        if exclude:
            _mark_excluded()
    except Exception as e:  # noqa: BLE001
        log.error("scrittura appunti fallita: %s", e)
        return 0
    finally:
        win32clipboard.CloseClipboard()
    return sequence()


def write_image(img, exclude: bool = False) -> int:
    """Scrive un'immagine (PIL) negli appunti come CF_DIB."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "BMP")
    dib = buf.getvalue()[14:]          # via l'header di file BMP
    if not _open():
        log.error("appunti occupati: scrittura immagine fallita")
        return 0
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_DIB, dib)
        if exclude:
            _mark_excluded()
    except Exception as e:  # noqa: BLE001
        log.error("scrittura immagine negli appunti fallita: %s", e)
        return 0
    finally:
        win32clipboard.CloseClipboard()
    return sequence()


# --- app in primo piano ------------------------------------------------------

# Finestre di terminale (classe) e processi che si comportano da terminale.
_TERM_CLASSES = {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS",
                 "VirtualConsoleClass", "PuTTY", "mintty", "TMobaXtermForm"}
_TERM_EXES = {"windowsterminal.exe", "wezterm-gui.exe", "alacritty.exe",
              "tabby.exe", "hyper.exe", "putty.exe", "mintty.exe",
              "conemu64.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
              # VS Code: il terminale integrato ha la classe di Chromium e non
              # si distingue dall'editor; Ctrl+Ins/Maiusc+Ins vanno in entrambi.
              "code.exe", "cursor.exe", "windsurf.exe"}


def _foreground() -> tuple[str, str]:
    """(classe finestra, nome eseguibile minuscolo) dell'app in primo piano."""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return "", ""
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    cls = buf.value
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = ""
    h = _kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
    if h:
        try:
            size = wintypes.DWORD(1024)
            pbuf = ctypes.create_unicode_buffer(1024)
            if _kernel32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(size)):
                exe = os.path.basename(pbuf.value).lower()
        finally:
            _kernel32.CloseHandle(h)
    return cls, exe


def foreground_is_terminal() -> bool:
    cls, exe = _foreground()
    return cls in _TERM_CLASSES or exe in _TERM_EXES


# --- invio tasti ---------------------------------------------------------------

_KEYUP = 0x0002
_EXTENDED = 0x0001
_VK_CTRL, _VK_SHIFT, _VK_INSERT = 0x11, 0x10, 0x2D
_EXTENDED_VKS = {_VK_INSERT}


def _key(vk: int, up: bool = False) -> None:
    # Insert DEVE avere il flag "esteso": senza, con il NumLock acceso diventa
    # lo 0 del tastierino e nel campo finisce uno "0".
    flags = (_KEYUP if up else 0) | (_EXTENDED if vk in _EXTENDED_VKS else 0)
    _user32.keybd_event(vk, _user32.MapVirtualKeyW(vk, 0), flags, 0)


def _chord(mod: int, vk: int) -> None:
    _key(mod)
    _key(vk)
    _key(vk, up=True)
    _key(mod, up=True)


def modifiers_down() -> bool:
    """True se Ctrl, Alt, Maiusc o Win sono fisicamente premuti."""
    return any(_user32.GetAsyncKeyState(vk) & 0x8000
               for vk in (0x10, 0x11, 0x12, 0x5B, 0x5C))


def send_copy() -> None:
    """Copia la selezione dell'app in primo piano."""
    if foreground_is_terminal():
        _chord(_VK_CTRL, _VK_INSERT)
    else:
        _chord(_VK_CTRL, ord("C"))


def send_paste() -> None:
    """Incolla nell'app in primo piano."""
    if foreground_is_terminal():
        _chord(_VK_SHIFT, _VK_INSERT)
    else:
        _chord(_VK_CTRL, ord("V"))
