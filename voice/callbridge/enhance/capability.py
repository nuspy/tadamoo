"""What this PC can do for the avatar's acceleration, and the plain reason for everything it cannot.

`read_gpu()` is the one function that touches the machine (NVML, else nvidia-smi); everything else takes a `GpuInfo` and is a pure function, so
a test fakes the GPU by passing one. The report lists, per stage, every provider with `available` and, when not, a `code` and a `reason` a
person can act on. A provider that is not available is shown, never silently missing.

Compute capability by generation: RTX 20 = 7.5, RTX 30 = 8.6, RTX 40 = 8.9, RTX 50 = 12.0."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

from .settings import PROVIDER_SCALES

RTX_MIN_CAPABILITY = 7.5                  # RTX class: tensor cores (RTX 20 and later)
BLACKWELL_CAPABILITY = 12.0
BLACKWELL_MIN_DRIVER = 570                # the first driver branch that supports the GeForce RTX 50 series
RTX_VIDEO_SDK_URL = "https://developer.nvidia.com/rtx-video-sdk"
INSTALL_HINT = ("download the NVIDIA RTX Video SDK from " + RTX_VIDEO_SDK_URL + " under NVIDIA's terms (TaDaMoo never downloads or bundles it), "
                "then put the full path of its DLL in Settings > Voice > Avatar acceleration")
_FAMILY = (("rtx 50", 12.0), ("rtx 40", 8.9), ("rtx 30", 8.6), ("rtx 20", 7.5), ("gtx 16", 7.5))      # when the driver gives no capability
SMI_FIELDS = "name,compute_cap,driver_version,memory.total,memory.used"


@dataclass(frozen=True)
class GpuInfo:
    vendor: str = "none"                  # nvidia | none (no NVIDIA GPU could be read)
    name: str = ""
    compute_cap: float | None = None
    driver: str = ""
    memory_total_mb: int | None = None
    memory_used_mb: int | None = None
    index: int = 0
    note: str = ""                        # why nothing could be read, or where the capability came from

    @property
    def driver_major(self) -> int | None:
        head = self.driver.split(".")[0].strip()
        return int(head) if head.isdigit() else None

    def as_dict(self) -> dict:
        return {"vendor": self.vendor, "name": self.name, "compute_cap": self.compute_cap, "driver": self.driver, "index": self.index,
                "memory_total_mb": self.memory_total_mb, "memory_used_mb": self.memory_used_mb, "note": self.note}


def _capability_from_name(name: str) -> float | None:
    low = name.lower()
    return next((cap for key, cap in _FAMILY if key in low), None)


def _megabytes(value: str) -> int | None:
    try:
        return int(float(value))
    except ValueError:
        return None


def parse_smi(text: str, index: int) -> GpuInfo:
    """The GPU `index` of `nvidia-smi --query-gpu=<SMI_FIELDS> --format=csv,noheader,nounits`."""
    rows = [[c.strip() for c in line.split(",")] for line in text.strip().splitlines() if line.strip()]
    if index >= len(rows) or len(rows[index]) < 5:
        return GpuInfo(note="nvidia-smi did not list that GPU")
    name, cap, driver, total, used = rows[index][:5]
    note = ""
    try:
        cc = float(cap)
    except ValueError:
        cc = _capability_from_name(name)
        note = "the compute capability comes from the card's name: the driver did not report it" if cc is not None else "the driver did not report the compute capability"
    return GpuInfo("nvidia", name, cc, driver, _megabytes(total), _megabytes(used), index, note)


def _read_nvml(index: int) -> GpuInfo | None:
    try:
        import pynvml
        pynvml.nvmlInit()
    except Exception:                      # noqa: BLE001 (no NVML here: nvidia-smi is tried)
        return None
    try:
        h = pynvml.nvmlDeviceGetHandleByIndex(index)
        name = pynvml.nvmlDeviceGetName(h)
        name = name.decode() if isinstance(name, bytes) else str(name)
        major, minor = pynvml.nvmlDeviceGetCudaComputeCapability(h)
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        driver = pynvml.nvmlSystemGetDriverVersion()
        driver = driver.decode() if isinstance(driver, bytes) else str(driver)
        return GpuInfo("nvidia", name, float(f"{major}.{minor}"), driver, mem.total >> 20, mem.used >> 20, index)
    except Exception:                      # noqa: BLE001
        return None
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:                  # noqa: BLE001
            pass


def _read_smi(index: int) -> GpuInfo:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return GpuInfo(note="neither NVML nor nvidia-smi is available")
    try:
        out = subprocess.run([exe, f"--query-gpu={SMI_FIELDS}", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return GpuInfo(note="nvidia-smi did not answer")
    if out.returncode != 0:
        return GpuInfo(note="nvidia-smi failed: " + (out.stderr.strip() or out.stdout.strip())[:120])
    return parse_smi(out.stdout, index)


def read_gpu(index: int = 0) -> GpuInfo:
    """The GPU the avatar runs on, read from NVML, else nvidia-smi. Replace this function in tests."""
    return _read_nvml(index) or _read_smi(index)


class _Cache:
    """`read_gpu` can start a process: its answer is kept for a few seconds so a page that polls does not run it every time."""
    ttl = 10.0

    def __init__(self) -> None:
        self.at, self.key, self.value = 0.0, None, None

    def get(self, index: int, reader, clock=time.monotonic) -> GpuInfo:
        now = clock()
        if self.value is None or self.key != (index, reader) or now - self.at > self.ttl:
            self.value, self.key, self.at = reader(index), (index, reader), now
        return self.value


_cache = _Cache()


def gpu(index: int = 0, reader=None) -> GpuInfo:
    return _cache.get(index, reader or read_gpu)


def _provider(available: bool, code: str = "", reason: str = "", **extra) -> dict:
    return {"available": available, "code": code, "reason": reason, **extra}


def rtx_video(info: GpuInfo, platform: str, dll_path: str, file_exists=os.path.isfile) -> dict:
    """Whether NVIDIA's RTX Video SDK can be used for upscaling; the first thing that is missing is the reason.

    Even with everything present this build cannot run it: `rtx_video.py` has no binding to the SDK's C API (its function signatures are not
    verified), so the last check always names that."""
    extra = {"scales": list(PROVIDER_SCALES["rtx_video"])}
    if platform != "win32":
        return _provider(False, "not_windows", "NVIDIA's RTX Video SDK is for Windows only.", **extra)
    if info.vendor != "nvidia":
        return _provider(False, "not_nvidia", "It needs an NVIDIA RTX GPU: " + (info.note or "none was found") + ".", **extra)
    if info.compute_cap is None:
        return _provider(False, "unknown_gpu", f"The GPU's generation could not be read ({info.name or 'unnamed'}).", **extra)
    if info.compute_cap < RTX_MIN_CAPABILITY:
        return _provider(False, "gpu_too_old", f"{info.name} has no RTX tensor cores (compute capability {info.compute_cap}, RTX needs {RTX_MIN_CAPABILITY} or more).", **extra)
    major = info.driver_major
    if info.compute_cap >= BLACKWELL_CAPABILITY and (major is None or major < BLACKWELL_MIN_DRIVER):
        return _provider(False, "driver_too_old", f"{info.name} needs NVIDIA driver {BLACKWELL_MIN_DRIVER} or newer (installed: {info.driver or 'unknown'}).", **extra)
    if not dll_path:
        return _provider(False, "needs_sdk", "The RTX Video SDK is not installed: " + INSTALL_HINT + ".", **extra)
    if not file_exists(dll_path):
        return _provider(False, "sdk_not_found", f"The RTX Video SDK file is not there: {dll_path}. Check the path, or " + INSTALL_HINT + ".", **extra)
    return _provider(False, "binding_missing", "The SDK is found, but this build has no binding to its C API yet (see docs/voice/acceleration.md).", **extra)


def basic_available(importable=None) -> tuple[bool, str]:
    """The CPU resize and blend, and the frame conversions around them, need numpy and OpenCV."""
    def have(name: str) -> bool:
        if importable is not None:
            return importable(name)
        try:
            __import__(name)
            return True
        except ImportError:
            return False
    if not have("numpy"):
        return False, "numpy is not installed in the voice environment"
    if not have("cv2"):
        return False, "OpenCV (cv2) is not installed in the voice environment"
    return True, ""


def report(info: GpuInfo, platform: str = sys.platform, dll_path: str = "", importable=None, file_exists=os.path.isfile) -> dict:
    """Per stage, every provider with whether it can be used now and why not."""
    ok, why = basic_available(importable)
    code = "" if ok else "missing_library"
    rtx = rtx_video(info, platform, dll_path, file_exists)
    rtx.update(label="NVIDIA RTX Video SDK", neural=True)
    return {"gpu": info.as_dict(), "platform": platform,
            "upscale": {"none": _provider(True, label="Off"),
                        "basic": _provider(ok, code, why, label="Basic (CPU resize, not neural)", neural=False, scales=list(PROVIDER_SCALES["basic"])),
                        "rtx_video": rtx},
            "interpolate": {"none": _provider(True, label="Off"),
                            "basic": _provider(ok, code, why, label="Basic (CPU blend of two frames, not neural)", neural=False)}}


def usable(rep: dict, stage: str, name: str) -> bool:
    return bool(rep.get(stage, {}).get(name, {}).get("available"))
