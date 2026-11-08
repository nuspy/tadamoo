"""Hook di tastiera low-level (WH_KEYBOARD_LL) con soppressione pulita.

Estratto dalla dettatura perche' serve anche alla cronologia degli appunti
(Ctrl+Alt+frecce). Ogni gestore installa il PROPRIO hook, in un thread suo:
fermarne uno (es. la dettatura dal tab Componenti) non tocca gli altri.

Perche' un hook proprio e non la libreria `keyboard`: per sopprimere i combo
`keyboard` (add_hotkey suppress=True) inietta un 'up' sintetico del tasto, che
falsava GetAsyncKeyState (il tasto risultava "su" pur essendo premuto) e
chiudeva la dettatura dopo 0.13s. Qui la soppressione e' un `return 1` dal
proc, senza iniettare nulla, e il rilascio si rileva dal KEYUP HARDWARE reale
(l'auto-repeat genera solo KEYDOWN ripetuti, mai KEYUP).
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass

log = logging.getLogger("callbridge.keyhook")

# --- costanti Win32 ----------------------------------------------------------
_WH_KEYBOARD_LL = 13
_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_SYSKEYDOWN = 0x0104
_WM_SYSKEYUP = 0x0105
_WM_QUIT = 0x0012
_LLKHF_INJECTED = 0x10
_HC_ACTION = 0

VK_CTRL, VK_ALT, VK_SHIFT, VK_WIN = 0x11, 0x12, 0x10, 0x5B
VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN = 0x25, 0x26, 0x27, 0x28

# Modificatori (vk generico).
_MOD_VK = {"alt": VK_ALT, "ctrl": VK_CTRL, "control": VK_CTRL,
           "shift": VK_SHIFT, "win": VK_WIN, "super": VK_WIN, "meta": VK_WIN}
# vk (anche L/R specifici degli eventi hook) -> vk generico del modificatore.
_GENERIC_MOD = {
    0x12: 0x12, 0xA4: 0x12, 0xA5: 0x12,   # Alt / LMenu / RMenu
    0x11: 0x11, 0xA2: 0x11, 0xA3: 0x11,   # Ctrl / LCtrl / RCtrl
    0x10: 0x10, 0xA0: 0x10, 0xA1: 0x10,   # Shift / LShift / RShift
    0x5B: 0x5B, 0x5C: 0x5B,               # LWin / RWin
}
# vk L/R da interrogare per sapere se un modificatore generico e' giu'.
_MOD_SIDES = {0x12: (0xA4, 0xA5), 0x11: (0xA2, 0xA3),
              0x10: (0xA0, 0xA1), 0x5B: (0x5B, 0x5C)}
# Tasti principali di uso comune.
_KEY_VK = {"space": 0x20, "spazio": 0x20, "tab": 0x09, "enter": 0x0D,
           "esc": 0x1B, "escape": 0x1B, "left": VK_LEFT, "up": VK_UP,
           "right": VK_RIGHT, "down": VK_DOWN}
for _c in "abcdefghijklmnopqrstuvwxyz":
    _KEY_VK[_c] = ord(_c.upper())
for _d in "0123456789":
    _KEY_VK[_d] = ord(_d)
for _i in range(1, 13):
    _KEY_VK[f"f{_i}"] = 0x70 + (_i - 1)


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


# LRESULT/HHOOK sono pointer-sized (LONG_PTR): su 64-bit c_long troncherebbe.
_LRESULT = ctypes.c_ssize_t
_HHOOK = ctypes.c_void_p
# WINFUNCTYPE (stdcall): SetWindowsHookEx richiede una callback stdcall su Windows.
_HOOKPROC = ctypes.WINFUNCTYPE(_LRESULT, ctypes.c_int, wintypes.WPARAM,
                               wintypes.LPARAM)


def _bind_win32(user32, kernel32) -> None:
    """Dichiara i prototipi (argtypes/restype) delle funzioni usate: senza,
    ctypes usa un prototipo implicito che rifiuta la callback _HOOKPROC e
    TRONCA i valori pointer-sized su 64-bit (es. l'HMODULE di GetModuleHandleW
    -> hmod invalido all'hook -> hook che non riceve eventi)."""
    user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int, _HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = _HHOOK
    user32.CallNextHookEx.argtypes = [
        _HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = _LRESULT
    user32.UnhookWindowsHookEx.argtypes = [_HHOOK]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [
        ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = ctypes.c_int
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE


def parse_hotkey(hk: str) -> tuple[frozenset[int], int | None]:
    """"alt+space" -> ({0x12}, 0x20). None se il tasto principale e' sconosciuto."""
    mods: set[int] = set()
    main: int | None = None
    for tok in (hk or "").lower().replace(" ", "").split("+"):
        if not tok:
            continue
        if tok in _MOD_VK:
            mods.add(_MOD_VK[tok])
        else:
            main = _KEY_VK.get(tok)
    return frozenset(mods), main


_VK_MASK = 0xE8     # tasto non assegnato: nessuna app lo interpreta


def mask_menu_key() -> None:
    """Un tasto inerte tra la pressione e il rilascio di Alt (o Win).

    Windows apre il menu dell'app (o i suggerimenti della barra multifunzione,
    o il menu Start) quando Alt viene premuto e rilasciato senza nessun altro
    tasto in mezzo. Con la hotkey soppressa il tasto principale non arriva
    mai al sistema, quindi il rilascio di Alt sembra un tocco isolato. Un
    evento iniettato lo interrompe senza dare alcun comando all'app."""
    user32 = ctypes.windll.user32
    user32.keybd_event(_VK_MASK, 0, 0, 0)
    user32.keybd_event(_VK_MASK, 0, 0x0002, 0)       # KEYEVENTF_KEYUP


def needs_menu_mask(mods: frozenset[int]) -> bool:
    return VK_ALT in mods or VK_WIN in mods


@dataclass
class Binding:
    """Combinazione da intercettare (e sopprimere).

    `exact`: i modificatori premuti devono essere ESATTAMENTE `mods` (Ctrl+Alt+↑
    non scatta con Ctrl+Alt+Shift+↑, che e' di altre app). Con `exact` lo stato
    dei modificatori viene anche verificato con GetAsyncKeyState: un KEYUP perso
    (Ctrl+Alt+Canc, blocco schermo) lascerebbe un modificatore "fantasma" e le
    frecce nude verrebbero inghiottite.
    `repeat`: l'auto-repeat del tasto tenuto premuto richiama `on_press`.
    """
    mods: frozenset[int]
    vk: int
    on_press: Callable[[], None]
    on_release: Callable[[], None] | None = None
    exact: bool = False
    repeat: bool = False


class LowLevelKeyHook:
    """Un hook WH_KEYBOARD_LL con le sue combinazioni, in un thread proprio.

    I callback NON girano nel proc dell'hook (deve tornare in pochi ms o Windows
    lo disinstalla, LowLevelHooksTimeout) ma in un worker dedicato, in ordine.
    """

    def __init__(self, name: str, bindings: list[Binding]) -> None:
        self.name = name
        self._by_vk: dict[int, list[Binding]] = {}
        for b in bindings:
            self._by_vk.setdefault(b.vk, []).append(b)
        self._active: dict[int, Binding] = {}   # vk premuto -> binding scattato
        self._hook = None
        self._cb = None            # ref al CFUNCTYPE (altrimenti GC -> crash)
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        self._events: "queue.Queue[Callable[[], None] | None]" = queue.Queue()
        self._stop = threading.Event()
        self._held: set[int] = set()   # modificatori attualmente premuti (da eventi)
        self._dbg_first = True

    @property
    def valid(self) -> bool:
        return bool(self._by_vk)

    def start(self) -> bool:
        if not self.valid:
            log.error("%s: nessuna combinazione valida da intercettare", self.name)
            return False
        threading.Thread(target=self._worker, name=f"{self.name}-worker",
                         daemon=True).start()
        self._thread = threading.Thread(target=self._run, name=f"{self.name}-hook",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread_id:
            self._user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
        self._events.put(None)

    # --- worker dei callback --------------------------------------------
    def _worker(self) -> None:
        while not self._stop.is_set():
            fn = self._events.get()
            if fn is None:
                return
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - callback difensivo
                log.error("%s: callback fallito: %s", self.name, e)

    # --- hook + message loop --------------------------------------------
    def _run(self) -> None:
        self._thread_id = self._kernel32.GetCurrentThreadId()
        _bind_win32(self._user32, self._kernel32)
        self._cb = _HOOKPROC(self._proc)
        hmod = self._kernel32.GetModuleHandleW(None)
        self._hook = self._user32.SetWindowsHookExW(
            _WH_KEYBOARD_LL, self._cb, hmod, 0)
        if not self._hook:
            log.error("%s: SetWindowsHookExW fallito (err %d)", self.name,
                      ctypes.get_last_error())
            return
        msg = wintypes.MSG()
        # LL hook: il thread deve pompare messaggi; il proc viene chiamato dal
        # sistema durante l'attesa. WM_QUIT (da stop()) sblocca il loop.
        while not self._stop.is_set():
            r = self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r == 0 or r == -1:
                break
            self._user32.TranslateMessage(ctypes.byref(msg))
            self._user32.DispatchMessageW(ctypes.byref(msg))
        try:
            self._user32.UnhookWindowsHookEx(self._hook)
        except Exception:  # noqa: BLE001
            pass

    def _mod_down(self, gen: int) -> bool:
        return any(self._user32.GetAsyncKeyState(vk) & 0x8000
                   for vk in _MOD_SIDES.get(gen, (gen,)))

    def _match(self, vk: int) -> Binding | None:
        for b in self._by_vk.get(vk, ()):
            if b.exact:
                # Modificatori "fantasma" (KEYUP perso) via, prima di confrontare.
                for gen in list(self._held):
                    if not self._mod_down(gen):
                        self._held.discard(gen)
                if self._held == set(b.mods):
                    return b
            elif b.mods.issubset(self._held):
                return b
        return None

    def _proc(self, nCode, wParam, lParam):
        try:
            if nCode == _HC_ACTION:
                kb = ctypes.cast(lParam,
                                 ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                vk = kb.vkCode
                down = wParam in (_WM_KEYDOWN, _WM_SYSKEYDOWN)
                up = wParam in (_WM_KEYUP, _WM_SYSKEYUP)
                injected = bool(kb.flags & _LLKHF_INJECTED)
                if self._dbg_first:
                    log.info("%s: hook RICEVE eventi (primo: vk=0x%02X "
                             "msg=0x%04X injected=%s)", self.name, vk, wParam,
                             injected)
                    self._dbg_first = False
                if not injected:
                    gen = _GENERIC_MOD.get(vk)
                    if gen is not None:
                        # Stato dei modificatori DAGLI EVENTI (piu' affidabile
                        # di GetAsyncKeyState in questo processo).
                        if down:
                            self._held.add(gen)
                        elif up:
                            self._held.discard(gen)
                    elif vk in self._by_vk:
                        attivo = self._active.get(vk)
                        if down:
                            if attivo is None:
                                b = self._match(vk)
                                if b is not None:
                                    self._active[vk] = b
                                    if needs_menu_mask(b.mods):
                                        self._events.put(mask_menu_key)
                                    self._events.put(b.on_press)
                                    return 1  # sopprime
                                # tasto senza i modificatori giusti: passa
                            else:
                                if attivo.repeat:
                                    self._events.put(attivo.on_press)
                                return 1  # auto-repeat: swallow, resta premuto
                        elif up and attivo is not None:
                            del self._active[vk]
                            if attivo.on_release is not None:
                                self._events.put(attivo.on_release)
                            return 1  # sopprime anche l'up
        except Exception as e:  # noqa: BLE001 - MAI far fallire l'hook
            log.debug("%s: hook proc errore: %s", self.name, e)
        return self._user32.CallNextHookEx(None, nCode, wParam, lParam)
