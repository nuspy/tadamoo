"""This PC's processor and GPU as the Loader's computed safe values need them, read when asked (nothing is assumed
about the machine):

  cpu()          logical and physical cores, and on hybrid processors the performance and efficiency cores
                 (Windows: GetLogicalProcessorInformationEx's EfficiencyClass; Linux: /sys/devices/cpu_core|cpu_atom)
  gpu(index)     name, CUDA compute capability (the engine's kernels are built for Blackwell: 10.x and 12.x), VRAM,
                 and the other processes on the card (NVML; their memory is unknown under Windows' WDDM)

The mock server reads BASALT_FAKE_CPU / BASALT_FAKE_GPU (JSON) like BASALT_FAKE_SYSTEM, only with
BASALT_FAKE_SYSTEM_OK=1."""
from __future__ import annotations

import ctypes
import json
import os

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
    if isinstance(fake, dict):
        cc = fake.get("cc")
        return {"name": fake.get("name"), "cc": cc, "blackwell": None if not cc else int(cc[0]) in BLACKWELL_MAJORS,
                "vram_total": fake.get("vram_total"), "vram_free": fake.get("vram_free"),
                "processes": fake.get("processes", [])}
    out = {"name": None, "cc": None, "blackwell": None, "vram_total": None, "vram_free": None, "processes": []}
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
