"""This PC's processor and GPU as the Loader's computed safe values need them, read when asked (nothing is assumed
about the machine):

  cpu()          logical and physical cores, and on hybrid processors the performance and efficiency cores
                 (Windows: GetLogicalProcessorInformationEx's EfficiencyClass; Linux: /sys/devices/cpu_core|cpu_atom)
  gpu_list()     the NVIDIA GPUs found, [{"index", "name"}] (nvidia-smi's numbering), read once a minute at most
  gpu(index)     name, CUDA compute capability (the engine's kernels are built for Blackwell: 10.x and 12.x), VRAM,
                 and the other processes on the card (NVML; their memory is unknown under Windows' WDDM)

  machine()      the compute adapters and their families (nvidia_blackwell / ada / ampere / turing / other, amd, intel,
                 apple) and whether the memory is one pool shared by CPU and GPU (True, False, or None when unknown)
  displays()     the video adapters and which of them drive a monitor: the primary display (the desktop's main
                 monitor) or only secondary ones (Windows: EnumDisplayDevices; elsewhere unknown)
  monitor_on(g)  where the compute GPU stands: "primary" (it drives the main monitor), "secondary" (another monitor,
                 on or connected but off: NVML's display_active), "none", or "unknown"
  page_file()    the page file's size and whether Windows manages it (then it may grow)

The mock server reads BASALT_FAKE_CPU / BASALT_FAKE_GPU / BASALT_FAKE_DISPLAYS / BASALT_FAKE_PAGEFILE (JSON) like
BASALT_FAKE_SYSTEM, only with BASALT_FAKE_SYSTEM_OK=1. BASALT_FAKE_ACCEL (a JSON object or list of {"vendor", "name",
"unified_memory"}) stands for the non-NVIDIA adapters; BASALT_FAKE_GPU may carry "unified_memory" too."""
from __future__ import annotations

import ctypes
import json
import os
import re
import time

BLACKWELL_MAJORS = (10, 12)             # sm_100 (data centre) and sm_120 (GeForce RTX 50, RTX PRO)


def _fake(name: str):
    if os.environ.get("BASALT_FAKE_SYSTEM_OK") == "1" and os.environ.get(name):
        try:
            return json.loads(os.environ[name])
        except ValueError:
            return None
    return None


# ------------------------------------------------------------------------------------------------ CPU
def _windows_cores() -> tuple[int, int, int] | None:
    """(physical cores, performance cores, efficiency cores) from GetLogicalProcessorInformationEx."""
    k32 = ctypes.windll.kernel32
    size = ctypes.c_ulong(0)
    k32.GetLogicalProcessorInformationEx(0, None, ctypes.byref(size))      # RelationProcessorCore
    if not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if not k32.GetLogicalProcessorInformationEx(0, buf, ctypes.byref(size)):
        return None
    raw, off, classes = buf.raw, 0, []
    while off + 10 <= size.value:
        rel, rec = int.from_bytes(raw[off:off + 4], "little"), int.from_bytes(raw[off + 4:off + 8], "little")
        if rec <= 0:
            break
        if rel == 0:
            classes.append(raw[off + 9])                                   # PROCESSOR_RELATIONSHIP.EfficiencyClass
        off += rec
    if not classes:
        return None
    top = max(classes)
    p = sum(1 for c in classes if c == top)
    return len(classes), (p if p < len(classes) else 0), (len(classes) - p if p < len(classes) else 0)


def _linux_list(path: str) -> int:
    try:
        text = open(path).read().strip()
    except OSError:
        return 0
    n = 0
    for part in filter(None, text.split(",")):
        a, _, b = part.partition("-")
        n += int(b) - int(a) + 1 if b else 1
    return n


def cpu() -> dict:
    """{"logical", "physical", "p_cores", "e_cores", "hybrid"}; unknown counts are None."""
    fake = _fake("BASALT_FAKE_CPU")
    if isinstance(fake, dict):
        return {"logical": fake.get("logical"), "physical": fake.get("physical"), "p_cores": fake.get("p_cores", 0),
                "e_cores": fake.get("e_cores", 0), "hybrid": bool(fake.get("e_cores"))}
    logical = os.cpu_count()
    physical, p, e = None, 0, 0
    try:
        import psutil
        physical = psutil.cpu_count(logical=False)
    except Exception:  # noqa: BLE001 - psutil is optional
        pass
    if os.name == "nt":
        try:
            got = _windows_cores()
            if got:
                physical, p, e = got
        except (AttributeError, OSError):
            pass
    else:
        p = _linux_list("/sys/devices/cpu_core/cpus")
        e = _linux_list("/sys/devices/cpu_atom/cpus")
        if p and e and physical:                                           # cpu_core lists threads: count cores
            e_threads = e
            p = max(1, physical - e_threads)
    return {"logical": logical, "physical": physical, "p_cores": p, "e_cores": e, "hybrid": bool(p and e)}


def pool_workers(c: dict | None = None) -> tuple[int, str, str]:
    """CPU workers for the experts computed on the CPU: on a hybrid processor the performance cores and half the
    efficiency cores, less one; otherwise the physical cores less one. (count, why in English, why in Italian)"""
    c = c or cpu()
    if c.get("hybrid"):
        n = max(1, c["p_cores"] + c["e_cores"] // 2 - 1)
        return (n, f"{n} CPU workers: the {c['p_cores']} performance cores and half the {c['e_cores']} efficiency "
                   "cores, less one; the rest stay for the system.",
                f"{n} thread CPU: i {c['p_cores']} core performance e metà dei {c['e_cores']} core efficienza, meno "
                "uno; il resto resta al sistema.")
    phys = c.get("physical") or max(1, (c.get("logical") or 2) // 2)
    n = max(1, phys - 1)
    return (n, f"{n} CPU workers: the {phys} physical cores less one, which stays for the system.",
            f"{n} thread CPU: i {phys} core fisici meno uno, che resta al sistema.")


# ------------------------------------------------------------------------------------------------ GPU
class _Proc(ctypes.Structure):                       # nvmlProcessInfo_t (v2/v3 layout)
    _fields_ = [("pid", ctypes.c_uint), ("used", ctypes.c_ulonglong), ("gi", ctypes.c_uint), ("ci", ctypes.c_uint)]


def _proc_name(pid: int) -> str | None:
    try:
        import psutil
        return psutil.Process(pid).name()
    except Exception:  # noqa: BLE001 - gone, or no permission
        return None


def gpu(index: int = 0) -> dict:
    """{"name", "cc", "blackwell", "vram_total", "vram_free", "processes": [{"pid", "name", "used"}]}; what cannot
    be read is None (no NVIDIA driver: {"name": None, ...})."""
    fake = _fake("BASALT_FAKE_GPU")
    if os.environ.get("BASALT_FAKE_SYSTEM_OK") == "1" and os.environ.get("BASALT_FAKE_GPU") == "null":
        fake = {"name": None}                                    # a PC without an NVIDIA GPU (no NVML)
    if isinstance(fake, dict):
        cc = fake.get("cc")
        return {"name": fake.get("name"), "cc": cc, "blackwell": None if not cc else int(cc[0]) in BLACKWELL_MAJORS,
                "vram_total": fake.get("vram_total"), "vram_free": fake.get("vram_free"),
                "processes": fake.get("processes", []), "display_active": fake.get("display_active"),
                "display_mode": fake.get("display_mode")}
    out = {"name": None, "cc": None, "blackwell": None, "vram_total": None, "vram_free": None, "processes": [],
           "display_active": None, "display_mode": None}
    try:
        from serve.util.telemetry import _Nvml
        nv = _Nvml(int(index or 0))
        if not nv.ok():
            return out
        out["name"] = nv.name()
        major, minor = ctypes.c_int(), ctypes.c_int()
        try:
            if nv.lib.nvmlDeviceGetCudaComputeCapability(nv.dev, ctypes.byref(major), ctypes.byref(minor)) == 0:
                out["cc"] = [major.value, minor.value]
                out["blackwell"] = major.value in BLACKWELL_MAJORS
        except (AttributeError, OSError):
            pass
        m = nv.Mem()
        if nv.lib.nvmlDeviceGetMemoryInfo(nv.dev, ctypes.byref(m)) == 0:
            out["vram_total"], out["vram_free"] = int(m.total), int(m.free)
        for key, fn in (("display_active", "nvmlDeviceGetDisplayActive"), ("display_mode", "nvmlDeviceGetDisplayMode")):
            flag = ctypes.c_int()                                # a display initialized / connected on this GPU
            try:
                if getattr(nv.lib, fn)(nv.dev, ctypes.byref(flag)) == 0:
                    out[key] = bool(flag.value)
            except (AttributeError, OSError):
                pass
        seen = {}
        for fn in ("nvmlDeviceGetComputeRunningProcesses_v3", "nvmlDeviceGetGraphicsRunningProcesses_v3",
                   "nvmlDeviceGetComputeRunningProcesses_v2", "nvmlDeviceGetGraphicsRunningProcesses_v2"):
            f = getattr(nv.lib, fn, None)
            if f is None or (fn.endswith("_v2") and seen):
                continue
            n = ctypes.c_uint(64)
            arr = (_Proc * 64)()
            try:
                if f(nv.dev, ctypes.byref(n), arr) != 0:
                    continue
            except OSError:
                continue
            for i in range(min(n.value, 64)):
                pid = int(arr[i].pid)
                used = int(arr[i].used)
                used = None if used >= (1 << 63) else used          # NVML_VALUE_NOT_AVAILABLE under WDDM
                if pid and pid not in seen:
                    seen[pid] = {"pid": pid, "name": _proc_name(pid), "used": used}
        out["processes"] = sorted(seen.values(), key=lambda p: (p["name"] or "").lower())
    except Exception:  # noqa: BLE001 - an unreadable GPU leaves None
        pass
    return out


GPU_LIST_MAX = 16                       # GPUs looked for
GPU_LIST_TTL = 60.0                     # seconds a reading is kept
_gpu_list_cache: dict = {}


def _read_gpu_list() -> list[dict]:
    out = []
    try:
        from serve.util.telemetry import _Nvml
        for i in range(GPU_LIST_MAX):
            nv = _Nvml(i)
            if not nv.ok():
                break
            out.append({"index": i, "name": nv.name()})
    except Exception:  # noqa: BLE001 - an unreadable driver leaves the list as far as it got
        pass
    return out


def gpu_list() -> list[dict]:
    """The NVIDIA GPUs of this PC, [{"index", "name"}] in the numbering nvidia-smi uses ([] when none can be read); the
    reading is kept for GPU_LIST_TTL seconds, so asking per request costs nothing."""
    fake = _fake("BASALT_FAKE_GPU")
    if os.environ.get("BASALT_FAKE_SYSTEM_OK") == "1" and os.environ.get("BASALT_FAKE_GPU") == "null":
        return []
    if isinstance(fake, dict):
        return [{"index": 0, "name": fake["name"]}] if fake.get("name") else []
    now = time.monotonic()
    if not _gpu_list_cache or now - _gpu_list_cache["at"] > GPU_LIST_TTL:
        _gpu_list_cache.update(at=now, gpus=_read_gpu_list())
    return [dict(g) for g in _gpu_list_cache["gpus"]]


def gpu_name(index) -> str | None:
    """The name of the GPU numbered `index` (nvidia-smi's number), None when it is not found."""
    try:
        want = int(index)
    except (TypeError, ValueError):
        return None
    return next((g["name"] for g in gpu_list() if g["index"] == want and g["name"]), None)


def gpu_warning(g: dict, lang: str = "it") -> str | None:
    """A sentence when the GPU cannot run the engine (not NVIDIA, or not Blackwell); None when it can or is unknown."""
    if g.get("name") is None and g.get("cc") is None:
        return ("Nessuna GPU NVIDIA leggibile (driver NVIDIA e NVML): il motore di Basalt gira solo su GPU NVIDIA "
                "Blackwell (RTX 50, RTX PRO 6000, B200)." if lang == "it" else
                "No NVIDIA GPU could be read (NVIDIA driver and NVML): the Basalt engine runs only on NVIDIA Blackwell "
                "GPUs (RTX 50 series, RTX PRO 6000, B200).")
    if g.get("blackwell") is False:
        cc = ".".join(str(x) for x in g["cc"])
        return (f"La GPU {g.get('name')} (compute capability {cc}) non è supportata: il motore di Basalt è compilato "
                "solo per GPU NVIDIA Blackwell (compute capability 10.x o 12.x: RTX 50, RTX PRO 6000, B200)."
                if lang == "it" else
                f"The GPU {g.get('name')} (compute capability {cc}) is not supported: the Basalt engine is built only "
                "for NVIDIA Blackwell GPUs (compute capability 10.x or 12.x: RTX 50 series, RTX PRO 6000, B200).")
    return None


# ------------------------------------------------------------------------------------------------ displays
class _DisplayDevice(ctypes.Structure):              # DISPLAY_DEVICEW
    _fields_ = [("cb", ctypes.c_ulong), ("DeviceName", ctypes.c_wchar * 32), ("DeviceString", ctypes.c_wchar * 128),
                ("StateFlags", ctypes.c_ulong), ("DeviceID", ctypes.c_wchar * 128), ("DeviceKey", ctypes.c_wchar * 128)]


ATTACHED_TO_DESKTOP, PRIMARY_DEVICE = 0x1, 0x4


def _windows_displays() -> list[dict]:
    user32 = ctypes.windll.user32
    adapters: dict[str, dict] = {}
    for i in range(64):
        d = _DisplayDevice()
        d.cb = ctypes.sizeof(d)
        if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(d), 0):
            break
        monitors = 0
        for j in range(16):
            m = _DisplayDevice()
            m.cb = ctypes.sizeof(m)
            if not user32.EnumDisplayDevicesW(d.DeviceName, j, ctypes.byref(m), 0):
                break
            monitors += 1
        key = d.DeviceID or d.DeviceString
        a = adapters.setdefault(key, {"name": d.DeviceString, "id": d.DeviceID, "primary": False, "outputs": 0,
                                      "monitors": 0})
        if d.StateFlags & ATTACHED_TO_DESKTOP:            # this output shows part of the desktop
            a["outputs"] += 1
            a["monitors"] += monitors
        if d.StateFlags & PRIMARY_DEVICE:
            a["primary"] = True
    return list(adapters.values())


def displays() -> list[dict] | None:
    """The video adapters: [{"name", "id" (PCI\\VEN_...), "primary" (it shows the main monitor), "outputs" (monitors on
    the desktop now), "monitors"}]; None when this system cannot tell."""
    fake = _fake("BASALT_FAKE_DISPLAYS")
    if isinstance(fake, list):
        return [{"name": a.get("name"), "id": a.get("id") or "", "primary": bool(a.get("primary")),
                 "outputs": int(a.get("outputs") or 0), "monitors": int(a.get("monitors") or a.get("outputs") or 0)}
                for a in fake]
    if os.name != "nt":
        return None
    try:
        return _windows_displays()
    except (AttributeError, OSError):
        return None


def _is(adapter: dict, g: dict) -> bool:
    """The adapter is the NVIDIA GPU `g`: the same name, or (unnamed) NVIDIA's PCI vendor id."""
    name = (g.get("name") or "").strip().lower()
    if name and (adapter.get("name") or "").strip().lower() == name:
        return True
    return not name and "VEN_10DE" in (adapter.get("id") or "").upper()


def monitor_on(g: dict, adapters: list[dict] | None) -> dict:
    """Where the compute GPU `g` stands with the monitors: {"where": "primary" | "secondary" | "none" | "unknown",
    "primary_adapter": the name of the adapter showing the main monitor (None when unknown), "secondary_off": a
    monitor is connected to this GPU but not on the desktop (NVML display_active without a desktop output)}."""
    active = g.get("display_active") is True
    if not adapters:
        return {"where": ("primary" if active else "unknown") if g.get("name") else "unknown",
                "primary_adapter": None, "secondary_off": False}
    primary = next((a for a in adapters if a["primary"]), None)
    mine = [a for a in adapters if _is(a, g)]
    if any(a["primary"] for a in mine):
        where = "primary"
    elif any(a["outputs"] for a in mine):
        where = "secondary"
    elif active:
        where = "secondary"                              # a monitor on this GPU, switched off or not on the desktop
    else:
        where = "none"
    return {"where": where, "primary_adapter": primary["name"] if primary else None,
            "secondary_off": where == "secondary" and not any(a["outputs"] for a in mine)}


# ------------------------------------------------------------------------------------------------ page file
def page_file() -> dict:
    """{"files": [{"name", "allocated_mib", "used_mib"}], "automatic": Windows manages its size (it can grow),
    "known": False when it cannot be read}."""
    fake = _fake("BASALT_FAKE_PAGEFILE")
    if isinstance(fake, dict):
        return {"files": fake.get("files", []), "automatic": bool(fake.get("automatic")), "known": True}
    if os.name != "nt":
        return {"files": [], "automatic": None, "known": False}
    import subprocess
    cmd = ("$c = Get-CimInstance Win32_ComputerSystem; $f = @(Get-CimInstance Win32_PageFileUsage | ForEach-Object "
           "{ @{ name = $_.Name; allocated_mib = [int]$_.AllocatedBaseSize; used_mib = [int]$_.CurrentUsage } }); "
           "@{ automatic = [bool]$c.AutomaticManagedPagefile; files = $f } | ConvertTo-Json -Depth 3 -Compress")
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True,
                             text=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        data = json.loads(out.strip() or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"files": [], "automatic": None, "known": False}
    files = data.get("files") or []
    return {"files": files if isinstance(files, list) else [files], "automatic": data.get("automatic"), "known": True}

# ------------------------------------------------------------------------------------------------ families
NVIDIA_FAMILIES = ("nvidia_blackwell", "nvidia_ada", "nvidia_ampere", "nvidia_turing", "nvidia_other")
VENDOR_FAMILIES = ("amd", "intel", "apple")


def nvidia_family(cc) -> str | None:
    """The NVIDIA architecture family of a compute capability [major, minor]: Blackwell (10.x, 12.x), Ada (8.9), Ampere
    (other 8.x), Turing (7.5), anything else "nvidia_other"; None when the capability is unknown."""
    try:
        major, minor = int(cc[0]), int(cc[1])
    except (TypeError, ValueError, IndexError):
        return None
    if major in BLACKWELL_MAJORS:
        return "nvidia_blackwell"
    if (major, minor) == (8, 9):
        return "nvidia_ada"
    if major == 8:
        return "nvidia_ampere"
    if (major, minor) == (7, 5):
        return "nvidia_turing"
    return "nvidia_other"


_AMD_UNIFIED = ("8060s", "8050s", "ryzen ai max", "mi300a")
_AMD_DISCRETE = ("radeon rx", "radeon pro", "radeon ai pro", "radeon vii", "instinct")


def classify_accel(vendor: str, name: str | None) -> tuple[str | None, bool | None]:
    """(family, unified_memory) of a non-NVIDIA adapter from its vendor ("amd", "intel", "apple") and name. The family
    is None for adapters that are not compute targets (integrated graphics, unnamed adapters); unified_memory is None
    when the name does not settle it."""
    n = (name or "").lower()
    if vendor == "apple":
        return "apple", True
    if not n:
        return None, None
    if vendor == "amd":
        if any(k in n for k in _AMD_UNIFIED):
            return "amd", True
        if any(k in n for k in _AMD_DISCRETE) or re.search(r"\bmi\d{2,3}x?\b", n):
            return "amd", False
        return None, None
    if vendor == "intel":
        if re.search(r"\barc\b.*\b[ab]\d{3}", n) or "data center gpu" in n or re.search(r"\bmax \d{4}\b", n):
            return "intel", False
        return None, None
    return None, None


def _accel_fake() -> list[dict] | None:
    fake = _fake("BASALT_FAKE_ACCEL")
    if isinstance(fake, dict):
        fake = [fake]
    return fake if isinstance(fake, list) else None


def _linux_adapters() -> list[dict]:
    """AMD and Intel adapters from /sys/class/drm (vendor ids only: the sysfs has no marketing name)."""
    out = []
    try:
        for card in sorted(os.listdir("/sys/class/drm")):
            if not re.fullmatch(r"card\d+", card):
                continue
            with open(f"/sys/class/drm/{card}/device/vendor", encoding="ascii") as f:
                vid = f.read().strip().lower()
            if vid in ("0x1002", "0x8086"):
                out.append({"vendor": "amd" if vid == "0x1002" else "intel", "name": None})
    except OSError:
        pass
    return out


def _real_accel() -> list[dict]:
    """The non-NVIDIA adapters this system shows: Apple silicon, and AMD / Intel video adapters by PCI vendor id."""
    import platform
    if platform.system() == "Darwin":
        return [{"vendor": "apple", "name": "Apple silicon"}] if platform.machine() == "arm64" else []
    adapters = displays() if os.name == "nt" else None
    if adapters is not None:
        out = []
        for a in adapters:
            ident = (a.get("id") or "").upper()
            for ven, vendor in (("VEN_1002", "amd"), ("VEN_8086", "intel")):
                if ven in ident:
                    out.append({"vendor": vendor, "name": a.get("name")})
        return out
    return _linux_adapters() if os.name == "posix" else []


def machine() -> dict:
    """The compute adapters of this PC and their families: {"gpus": [{"index", "name", "vendor", "cc", "sm", "family",
    "unified_memory"}], "families": sorted family ids (plus "unified_memory" when the memory is one pool), "unified_memory":
    True / False / None (unknown), "os", "arch"}. Adapters that are not compute targets are left out."""
    import platform
    gpus: list[dict] = []
    raw = _fake("BASALT_FAKE_GPU")
    for g in gpu_list():
        info = gpu(g["index"])
        cc = info.get("cc")
        name = info.get("name") or g.get("name")
        unified = raw.get("unified_memory") if isinstance(raw, dict) and "unified_memory" in raw else (
            True if "gb10" in (name or "").lower() else False)
        gpus.append({"index": g["index"], "name": name, "vendor": "nvidia", "cc": cc,
                     "sm": f"{cc[0]}.{cc[1]}" if cc else None, "family": nvidia_family(cc), "unified_memory": unified})
    fake_accel = _accel_fake()
    faked = fake_accel is not None or (os.environ.get("BASALT_FAKE_SYSTEM_OK") == "1"
                                       and bool(os.environ.get("BASALT_FAKE_GPU")))
    for a in (fake_accel if fake_accel is not None else ([] if faked else _real_accel())):
        vendor = str(a.get("vendor") or "").lower()
        family, unified = classify_accel(vendor, a.get("name"))
        if a.get("family") in VENDOR_FAMILIES:
            family = a["family"]
        if "unified_memory" in a:
            unified = a["unified_memory"]
        if family:
            gpus.append({"index": None, "name": a.get("name"), "vendor": vendor, "cc": None, "sm": None,
                         "family": family, "unified_memory": unified})
    flags = [g["unified_memory"] for g in gpus]
    if not gpus:
        unified_all = None
    elif any(f is False for f in flags):
        unified_all = False
    elif all(f is True for f in flags):
        unified_all = True
    else:
        unified_all = None
    fams = sorted({g["family"] for g in gpus if g["family"]})
    if unified_all:
        fams.append("unified_memory")
    return {"gpus": gpus, "families": fams, "unified_memory": unified_all, "os": platform.system().lower(),
            "arch": platform.machine().lower()}
