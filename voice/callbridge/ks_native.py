"""Streaming audio nativo via Windows Kernel Streaming (WDM-KS).

Serve per l'endpoint Bluetooth HFP del telefono, che su Windows esiste SOLO come
filtro Kernel Streaming (driver ``bthhfenum``): non c'e' endpoint Core Audio, e
PortAudio non riesce ad aprirlo. Qui si apre il filtro KS direttamente, si crea
il pin con ``KsCreatePin`` e si fa I/O con ``IOCTL_KS_READ_STREAM`` /
``IOCTL_KS_WRITE_STREAM``.

Punti imparati a caro prezzo (vedi memoria telefonia-android-controller):

- I valori IOCTL veri (da ks.h) sono ``Function 0x005/0x004`` con ``METHOD_NEITHER``:
  READ = 0x2F4017, WRITE = 0x2F8013. (Non 0x100/METHOD_OUT_DIRECT.)
- Il pin va portato ACQUIRE -> PAUSE -> RUN; il RUN da' GLE=1 finche' il canale
  SCO Bluetooth non e' pronto, quindi si ritenta.
- Per un ritmo real-time servono PIU' pacchetti in volo (overlapped) e non un
  blocco bloccante alla volta: altrimenti l'audio si spezza (underrun).
"""
from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes

log = logging.getLogger("callbridge.ks")

setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ksuser = ctypes.WinDLL("ksuser", use_last_error=True)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, s: str) -> None:
        super().__init__()
        p = s.strip("{}").split("-")
        self.Data1 = int(p[0], 16)
        self.Data2 = int(p[1], 16)
        self.Data3 = int(p[2], 16)
        rest = bytes.fromhex(p[3] + p[4])
        for i in range(8):
            self.Data4[i] = rest[i]


KSCATEGORY_CAPTURE = GUID("{65E8773D-8F56-11D0-A3B9-00A0C9223196}")
KSCATEGORY_RENDER = GUID("{65E8773E-8F56-11D0-A3B9-00A0C9223196}")
KSINTERFACESETID_Standard = GUID("{1A8766A0-62CE-11CF-A5D6-28DB04C10000}")
KSMEDIUMSETID_Standard = GUID("{4747B320-62CE-11CF-A5D6-28DB04C10000}")
KSDATAFORMAT_TYPE_AUDIO = GUID("{73647561-0000-0010-8000-00AA00389B71}")
KSDATAFORMAT_SUBTYPE_PCM = GUID("{00000001-0000-0010-8000-00AA00389B71}")
KSDATAFORMAT_SPECIFIER_WAVEFORMATEX = GUID("{05589F81-C356-11CE-BF01-00AA0055595A}")
KSPROPSETID_Connection = GUID("{1D58C920-AC9B-11CF-A5D6-28DB04C10000}")
KSPROPSETID_Pin = GUID("{8C134960-51AD-11CF-878A-94F801C10000}")
KSDATAFORMAT_TYPE_AUDIO_PROBE = GUID("{73647561-0000-0010-8000-00AA00389B71}")
KSPROPERTY_TYPE_GET = 0x00000001
KSPROPERTY_PIN_CTYPES = 1
KSPROPERTY_PIN_DATAFLOW = 2      # 1 = IN (render/uplink), 2 = OUT (capture/downlink)
KSPROPERTY_PIN_DATARANGES = 3
KSPROPERTY_PIN_COMMUNICATION = 7  # 1 = SINK, 3 = BOTH: pin di streaming validi

DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10
SPDRP_FRIENDLYNAME = 0x0C
SPDRP_DEVICEDESC = 0x00
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_RW = 0x03
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
IS64 = ctypes.sizeof(ctypes.c_void_p) == 8

IOCTL_KS_PROPERTY = 0x002F0003
IOCTL_KS_READ_STREAM = 0x002F4017   # CTL_CODE(0x2f, 0x005, METHOD_NEITHER, READ)
IOCTL_KS_WRITE_STREAM = 0x002F8013  # CTL_CODE(0x2f, 0x004, METHOD_NEITHER, WRITE)
KSPROPERTY_TYPE_SET = 0x00000002
KSPROPERTY_CONNECTION_STATE = 0
KSSTATE = {"STOP": 0, "ACQUIRE": 1, "PAUSE": 2, "RUN": 3}
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
ERROR_IO_PENDING = 997


# --- strutture SetupAPI -----------------------------------------------------
class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", wintypes.DWORD), ("Reserved", ctypes.POINTER(ctypes.c_ulong))]


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", GUID),
                ("DevInst", wintypes.DWORD), ("Reserved", ctypes.POINTER(ctypes.c_ulong))]


class SP_DEVICE_INTERFACE_DETAIL_DATA_W(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("DevicePath", wintypes.WCHAR * 1024)]


# --- strutture KS -----------------------------------------------------------
class KSIDENTIFIER(ctypes.Structure):
    _fields_ = [("Set", GUID), ("Id", wintypes.DWORD), ("Flags", wintypes.DWORD)]


class KSPROPERTY(ctypes.Structure):
    _fields_ = [("Set", GUID), ("Id", wintypes.DWORD), ("Flags", wintypes.DWORD)]


class KSP_PIN(ctypes.Structure):
    _fields_ = [("Property", KSPROPERTY), ("PinId", wintypes.DWORD),
                ("Reserved", wintypes.DWORD)]


class KSPRIORITY(ctypes.Structure):
    _fields_ = [("Priority", wintypes.DWORD), ("SubPriority", wintypes.DWORD)]


class KSPIN_CONNECT(ctypes.Structure):
    _fields_ = [("Interface", KSIDENTIFIER), ("Medium", KSIDENTIFIER),
                ("PinId", wintypes.DWORD), ("PinToHandle", ctypes.c_void_p),
                ("Priority", KSPRIORITY)]


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
                ("nSamplesPerSec", wintypes.DWORD), ("nAvgBytesPerSec", wintypes.DWORD),
                ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD),
                ("cbSize", wintypes.WORD)]


class KSDATAFORMAT(ctypes.Structure):
    _fields_ = [("FormatSize", wintypes.DWORD), ("Flags", wintypes.DWORD),
                ("SampleSize", wintypes.DWORD), ("Reserved", wintypes.DWORD),
                ("MajorFormat", GUID), ("SubFormat", GUID), ("Specifier", GUID)]


class KSTIME(ctypes.Structure):
    _fields_ = [("Time", ctypes.c_longlong), ("Numerator", wintypes.DWORD),
                ("Denominator", wintypes.DWORD)]


class KSSTREAM_HEADER(ctypes.Structure):
    _fields_ = [("Size", wintypes.DWORD), ("TypeSpecificFlags", wintypes.DWORD),
                ("PresentationTime", KSTIME), ("Duration", ctypes.c_longlong),
                ("FrameExtent", wintypes.DWORD), ("DataUsed", wintypes.DWORD),
                ("Data", ctypes.c_void_p), ("OptionsFlags", wintypes.DWORD),
                ("Reserved", wintypes.DWORD)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE)]


# --- prototipi --------------------------------------------------------------
setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR,
                                          wintypes.HWND, wintypes.DWORD]
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID), wintypes.DWORD,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
    ctypes.POINTER(SP_DEVICE_INTERFACE_DETAIL_DATA_W), wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(SP_DEVINFO_DATA)]
setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD)]
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
k32.DeviceIoControl.restype = wintypes.BOOL
k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
k32.CreateEventW.restype = wintypes.HANDLE
k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
k32.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.c_void_p,
                                       wintypes.BOOL, wintypes.DWORD]
k32.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED),
                                    ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
k32.VirtualAlloc.restype = ctypes.c_void_p
k32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
ksuser.KsCreatePin.restype = wintypes.DWORD
ksuser.KsCreatePin.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                               ctypes.POINTER(wintypes.HANDLE)]

# Modelli di TELEFONO (non gli auricolari Buds): l'endpoint HFP che serve e'
# quello del telefono in chiamata.
PHONE_HINTS = ("s25", "s21", "ultra", "galaxy")
HFP_HINTS = ("hands-free", "hands free", "headset")


def _reg_prop(hdev, devinfo, prop) -> str:
    buf = ctypes.create_unicode_buffer(512)
    ok = setupapi.SetupDiGetDeviceRegistryPropertyW(
        hdev, ctypes.byref(devinfo), prop, None,
        ctypes.cast(buf, ctypes.c_void_p), ctypes.sizeof(buf), None)
    return buf.value if ok else ""


def _enum(cat: GUID) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    hdev = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(cat), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if not hdev or hdev == INVALID_HANDLE_VALUE:
        return out
    try:
        idx = 0
        while True:
            ifd = SP_DEVICE_INTERFACE_DATA()
            ifd.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                    hdev, None, ctypes.byref(cat), idx, ctypes.byref(ifd)):
                break
            idx += 1
            detail = SP_DEVICE_INTERFACE_DETAIL_DATA_W()
            detail.cbSize = 8 if IS64 else 6
            devinfo = SP_DEVINFO_DATA()
            devinfo.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                    hdev, ctypes.byref(ifd), ctypes.byref(detail),
                    ctypes.sizeof(detail), None, ctypes.byref(devinfo)):
                continue
            name = (_reg_prop(hdev, devinfo, SPDRP_FRIENDLYNAME)
                    or _reg_prop(hdev, devinfo, SPDRP_DEVICEDESC) or "")
            out.append((name, detail.DevicePath))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return out


def find_hfp_paths(hint: str | None = None) -> tuple[str, str]:
    """Ritorna (capture_path, render_path) del filtro HFP del telefono.

    `hint` dovrebbe essere il nome Bluetooth del telefono (es. "Andrea's S21
    Ultra"), che il telefono stesso dichiara via GET /identify: Windows chiama
    i suoi endpoint "<nome BT> Hands-Free HF Audio", quindi il match e' esatto
    e funziona con qualunque modello, anche con piu' telefoni appaiati.

    Senza `hint` si ripiega su PHONE_HINTS (modelli noti) e infine su un HFP
    generico: euristiche che reggono i casi semplici ma non sanno distinguere
    due telefoni. Solleva RuntimeError se non trova una coppia."""
    pulito = (hint or "").strip().lower()

    def pick(cat):
        devs = _enum(cat)
        candidati = [(n, p) for n, p in devs
                     if "hands-free" in n.lower() or "hands free" in n.lower()]
        # 1) il telefono chiesto per nome: prima il prefisso esatto, che e' la
        #    forma con cui Windows costruisce il nome dell'endpoint.
        if pulito:
            for name, path in candidati:
                if name.lower().startswith(pulito):
                    return name, path
            for name, path in candidati:
                if pulito in name.lower():
                    return name, path
        # 2) euristiche: modelli noti, poi un HFP qualsiasi.
        for hints in (PHONE_HINTS, HFP_HINTS):
            for name, path in candidati:
                if any(h in name.lower() for h in hints):
                    return name, path
        return None, None

    cap_name, cap = pick(KSCATEGORY_CAPTURE)
    ren_name, ren = pick(KSCATEGORY_RENDER)
    if not cap or not ren:
        raise RuntimeError(
            "Endpoint HFP non trovato fra i device Kernel Streaming. Il telefono "
            "e' connesso via Bluetooth con 'Calls' attivo?")
    log.info("HFP KS: capture=%s | render=%s", cap_name, ren_name)
    return cap, ren


def _format_blob(rate: int, ch: int, bits: int = 16) -> bytes:
    w = WAVEFORMATEX()
    w.wFormatTag = 1
    w.nChannels = ch
    w.nSamplesPerSec = rate
    w.wBitsPerSample = bits
    w.nBlockAlign = ch * bits // 8
    w.nAvgBytesPerSec = rate * w.nBlockAlign
    w.cbSize = 0
    fmt = KSDATAFORMAT()
    fmt.FormatSize = ctypes.sizeof(KSDATAFORMAT) + ctypes.sizeof(WAVEFORMATEX)
    fmt.SampleSize = w.nBlockAlign
    fmt.MajorFormat = KSDATAFORMAT_TYPE_AUDIO
    fmt.SubFormat = KSDATAFORMAT_SUBTYPE_PCM
    fmt.Specifier = KSDATAFORMAT_SPECIFIER_WAVEFORMATEX
    return bytes(fmt) + bytes(w)


def _open_filter(path: str) -> wintypes.HANDLE:
    h = k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_RW, None,
                        OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
    if not h or h == INVALID_HANDLE_VALUE:
        raise OSError(f"apertura filtro KS fallita GLE={ctypes.get_last_error()}")
    return h


def _create_pin(hfilter, pin_id: int, rate: int, ch: int) -> wintypes.HANDLE:
    conn = KSPIN_CONNECT()
    conn.Interface.Set = KSINTERFACESETID_Standard
    conn.Interface.Id = 0  # KSINTERFACE_STANDARD_STREAMING
    conn.Medium.Set = KSMEDIUMSETID_Standard
    conn.Medium.Id = 0
    conn.PinId = pin_id
    conn.PinToHandle = None
    conn.Priority.Priority = 1
    conn.Priority.SubPriority = 1
    raw = bytes(conn) + _format_blob(rate, ch)
    buf = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    hpin = wintypes.HANDLE()
    err = ksuser.KsCreatePin(hfilter, buf, GENERIC_READ | GENERIC_WRITE,
                             ctypes.byref(hpin))
    if err != 0:
        raise OSError(f"KsCreatePin pin {pin_id} err={err} (0x{err & 0xffffffff:08x})")
    return hpin


def _set_state(hpin, state: str) -> None:
    prop = KSPROPERTY()
    prop.Set = KSPROPSETID_Connection
    prop.Id = KSPROPERTY_CONNECTION_STATE
    prop.Flags = KSPROPERTY_TYPE_SET
    val = wintypes.DWORD(KSSTATE[state])
    ret = wintypes.DWORD(0)
    ok = k32.DeviceIoControl(hpin, IOCTL_KS_PROPERTY, ctypes.byref(prop),
                             ctypes.sizeof(prop), ctypes.byref(val),
                             ctypes.sizeof(val), ctypes.byref(ret), None)
    if not ok:
        raise OSError(f"set_state {state} GLE={ctypes.get_last_error()}")


def _run_pin(hpin, tries: int = 40, delay_ms: int = 100) -> None:
    """Porta il pin in RUN, ritentando: il RUN fallisce (GLE=1) finche' il SCO
    non e' pronto."""
    _set_state(hpin, "ACQUIRE")
    _set_state(hpin, "PAUSE")
    last: Exception | None = None
    for _ in range(tries):
        try:
            _set_state(hpin, "RUN")
            return
        except OSError as e:
            last = e
            time.sleep(delay_ms / 1000.0)
    raise RuntimeError(f"il pin non entra in RUN (SCO non instradato?): {last}")


# --- rilevamento automatico pin + sample rate (prodotto multi-telefono) -----
def _open_filter_ro(path: str) -> wintypes.HANDLE:
    h = k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_RW, None,
                        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if not h or h == INVALID_HANDLE_VALUE:
        raise OSError(f"apertura filtro (probe) fallita GLE={ctypes.get_last_error()}")
    return h


def _pin_dword(hfilter, pin_id: int, prop_id: int):
    ksp = KSP_PIN()
    ksp.Property.Set = KSPROPSETID_Pin
    ksp.Property.Id = prop_id
    ksp.Property.Flags = KSPROPERTY_TYPE_GET
    ksp.PinId = pin_id
    out = wintypes.DWORD(0)
    ret = wintypes.DWORD(0)
    ok = k32.DeviceIoControl(hfilter, IOCTL_KS_PROPERTY, ctypes.byref(ksp),
                             ctypes.sizeof(ksp), ctypes.byref(out),
                             ctypes.sizeof(out), ctypes.byref(ret), None)
    return out.value if ok else None


def _pin_audio_rates(hfilter, pin_id: int) -> list[int]:
    """Sample rate audio (Hz) che il pin dichiara di supportare."""
    ksp = KSP_PIN()
    ksp.Property.Set = KSPROPSETID_Pin
    ksp.Property.Id = KSPROPERTY_PIN_DATARANGES
    ksp.Property.Flags = KSPROPERTY_TYPE_GET
    ksp.PinId = pin_id
    ret = wintypes.DWORD(0)
    k32.DeviceIoControl(hfilter, IOCTL_KS_PROPERTY, ctypes.byref(ksp),
                        ctypes.sizeof(ksp), None, 0, ctypes.byref(ret), None)
    size = ret.value if ret.value >= 8 else 4096
    buf = (ctypes.c_ubyte * size)()
    ok = k32.DeviceIoControl(hfilter, IOCTL_KS_PROPERTY, ctypes.byref(ksp),
                             ctypes.sizeof(ksp), ctypes.byref(buf), size,
                             ctypes.byref(ret), None)
    if not ok:
        return []
    raw = bytes(buf[:ret.value])
    if len(raw) < 8:
        return []
    count = int.from_bytes(raw[4:8], "little")
    audio = bytes(bytearray(KSDATAFORMAT_TYPE_AUDIO))
    off, rates = 8, []
    for _ in range(count):
        if off + 84 > len(raw):
            break
        fmt_size = int.from_bytes(raw[off:off + 4], "little")
        if raw[off + 16:off + 32] == audio and fmt_size >= 84:
            rates.append(int.from_bytes(raw[off + 80:off + 84], "little"))  # MaxFreq
        off += (fmt_size + 7) & ~7 if fmt_size else 8
    return rates


def probe_pin(path: str, want_flow: int) -> tuple[int, list[int]]:
    """Trova il pin di STREAMING con il dataflow voluto (2=capture/downlink,
    1=render/uplink) e comunicazione SINK/BOTH. Ritorna (pin_id, rates)."""
    h = _open_filter_ro(path)
    try:
        n = _pin_dword(h, 0, KSPROPERTY_PIN_CTYPES) or 0
        for pid in range(n):
            if _pin_dword(h, pid, KSPROPERTY_PIN_DATAFLOW) != want_flow:
                continue
            if _pin_dword(h, pid, KSPROPERTY_PIN_COMMUNICATION) not in (1, 3):
                continue
            rates = _pin_audio_rates(h, pid)
            if rates:
                return pid, rates
        raise RuntimeError(f"nessun pin di streaming (flow={want_flow}) nel filtro HFP")
    finally:
        k32.CloseHandle(h)


def probe_hfp(hint: str | None = None, preferred: int = 16000):
    """Rileva AUTOMATICAMENTE pin e sample rate dell'HFP del telefono connesso —
    indipendente dal modello (wideband 16 kHz o narrowband 8 kHz). Ritorna
    (cap_path, cap_pin, ren_path, ren_pin, rate)."""
    cap_path, ren_path = find_hfp_paths(hint)
    cap_pin, cap_rates = probe_pin(cap_path, 2)  # OUT = downlink (telefono->PC)
    ren_pin, ren_rates = probe_pin(ren_path, 1)  # IN  = uplink   (PC->telefono)
    common = set(cap_rates) & set(ren_rates)
    rate = preferred if preferred in common else (max(common) if common else cap_rates[0])
    log.info("HFP rilevato: capture pin %d, render pin %d, %d Hz (cap=%s ren=%s)",
             cap_pin, ren_pin, rate, cap_rates, ren_rates)
    return cap_path, cap_pin, ren_path, ren_pin, rate


class _Packet:
    """Un pacchetto di streaming: header KS + data buffer + evento, in memoria
    page-aligned (VirtualAlloc)."""

    __slots__ = ("data", "hdr_mem", "hdr", "ev", "ov", "nbytes")

    def __init__(self, nbytes: int) -> None:
        self.nbytes = nbytes
        self.data = k32.VirtualAlloc(None, nbytes, 0x3000, 0x04)
        self.hdr_mem = k32.VirtualAlloc(None, ctypes.sizeof(KSSTREAM_HEADER), 0x3000, 0x04)
        self.hdr = KSSTREAM_HEADER.from_address(self.hdr_mem)
        self.hdr.Size = ctypes.sizeof(KSSTREAM_HEADER)
        self.hdr.PresentationTime.Numerator = 1
        self.hdr.PresentationTime.Denominator = 1
        self.hdr.FrameExtent = nbytes
        self.hdr.Data = self.data
        self.ev = k32.CreateEventW(None, True, False, None)
        self.ov = OVERLAPPED()
        self.ov.hEvent = self.ev


class KsStream:
    """Streaming multi-pacchetto su un pin (capture o render).

    Tiene ``npackets`` I/O in volo per non spezzare l'audio. In capture i
    pacchetti completati portano dati da leggere; in render vanno riempiti e
    ri-sottomessi."""

    def __init__(self, path: str, pin_id: int, rate: int, ch: int,
                 block_frames: int, is_capture: bool, npackets: int = 8) -> None:
        self.is_capture = is_capture
        self.rate = rate
        self.ch = ch
        self.nbytes = block_frames * ch * 2
        self.hfilter = _open_filter(path)
        self.pin = _create_pin(self.hfilter, pin_id, rate, ch)
        self.packets = [_Packet(self.nbytes) for _ in range(npackets)]
        self._evarray = (wintypes.HANDLE * npackets)(*[p.ev for p in self.packets])
        _run_pin(self.pin)
        self._ioctl = IOCTL_KS_READ_STREAM if is_capture else IOCTL_KS_WRITE_STREAM
        if is_capture:
            for p in self.packets:
                self._submit(p)

    def _submit(self, p: _Packet) -> None:
        k32.ResetEvent(p.ev)
        p.hdr.DataUsed = 0 if self.is_capture else p.nbytes
        ret = wintypes.DWORD(0)
        ok = k32.DeviceIoControl(self.pin, self._ioctl, None, 0,
                                 ctypes.c_void_p(p.hdr_mem),
                                 ctypes.sizeof(KSSTREAM_HEADER),
                                 ctypes.byref(ret), ctypes.byref(p.ov))
        if not ok and ctypes.get_last_error() != ERROR_IO_PENDING:
            raise OSError(f"submit KS GLE={ctypes.get_last_error()}")

    def read_block(self, timeout_ms: int = 500) -> bytes | None:
        """Attende il prossimo pacchetto di capture completato e lo ri-sottomette.
        Ritorna i byte letti, o None su timeout."""
        idx = k32.WaitForMultipleObjects(len(self.packets), self._evarray, False,
                                         timeout_ms)
        if idx < WAIT_OBJECT_0 or idx >= WAIT_OBJECT_0 + len(self.packets):
            return None
        p = self.packets[idx - WAIT_OBJECT_0]
        got = wintypes.DWORD(0)
        k32.GetOverlappedResult(self.pin, ctypes.byref(p.ov), ctypes.byref(got), False)
        used = p.hdr.DataUsed or got.value
        buf = ctypes.string_at(p.data, used) if used else b""
        self._submit(p)
        return buf

    def write_block(self, raw: bytes, timeout_ms: int = 500) -> bool:
        """Scrive un blocco sull'uplink: prende un pacchetto libero (completato),
        lo riempie e lo sottomette. Ritorna False su timeout (buffer pieno)."""
        idx = k32.WaitForMultipleObjects(len(self.packets), self._evarray, False,
                                         timeout_ms)
        if idx < WAIT_OBJECT_0 or idx >= WAIT_OBJECT_0 + len(self.packets):
            return False
        p = self.packets[idx - WAIT_OBJECT_0]
        got = wintypes.DWORD(0)
        k32.GetOverlappedResult(self.pin, ctypes.byref(p.ov), ctypes.byref(got), False)
        n = min(len(raw), p.nbytes)
        ctypes.memmove(p.data, raw, n)
        if n < p.nbytes:
            ctypes.memset(ctypes.c_void_p(p.data + n), 0, p.nbytes - n)
        self._submit(p)
        return True

    def prime_render(self) -> None:
        """Riempie di silenzio i pacchetti render e li sottomette, per avviare
        il flusso di uplink."""
        for p in self.packets:
            ctypes.memset(ctypes.c_void_p(p.data), 0, p.nbytes)
            self._submit(p)

    def close(self) -> None:
        try:
            k32.CancelIo(self.pin)
            _set_state(self.pin, "STOP")
        except OSError:
            pass
        k32.CloseHandle(self.pin)
        k32.CloseHandle(self.hfilter)
