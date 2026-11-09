"""What each kind of PC can do for the acceleration, with fake GPU readings: no real NVML, nvidia-smi or DLL is touched."""
import pytest

from callbridge.enhance import capability as cap
from callbridge.enhance.capability import GpuInfo

DLL = "C:\\NVIDIA\\RTXVideo\\rtxvideo.dll"


def nv(name, cc, driver="581.42"):
    return GpuInfo("nvidia", name, cc, driver, 32768, 1024)


def rtx(info, platform="win32", path=DLL, exists=True):
    return cap.rtx_video(info, platform, path, lambda p: exists)


@pytest.mark.parametrize("name,cc", [("NVIDIA GeForce RTX 2080", 7.5), ("NVIDIA GeForce RTX 3080", 8.6), ("NVIDIA GeForce RTX 4090", 8.9), ("NVIDIA GeForce RTX 5090", 12.0)])
def test_every_rtx_generation_passes_the_gpu_checks_and_stops_only_at_the_missing_binding(name, cc):
    got = rtx(nv(name, cc))
    assert got["available"] is False and got["code"] == "binding_missing"
    assert "no binding" in got["reason"]


def test_a_card_without_tensor_cores_is_named_as_too_old():
    got = rtx(nv("NVIDIA GeForce GTX 1080", 6.1))
    assert got["code"] == "gpu_too_old" and "GTX 1080" in got["reason"]


def test_a_blackwell_card_needs_driver_570():
    assert rtx(nv("NVIDIA GeForce RTX 5090", 12.0, "560.94"))["code"] == "driver_too_old"
    assert rtx(nv("NVIDIA GeForce RTX 5090", 12.0, ""))["code"] == "driver_too_old"
    assert rtx(nv("NVIDIA GeForce RTX 5090", 12.0, "570.00"))["code"] == "binding_missing"
    assert rtx(nv("NVIDIA GeForce RTX 4090", 8.9, "531.00"))["code"] == "binding_missing"          # the 570 rule is for RTX 50 only


def test_a_pc_without_an_nvidia_gpu_says_so_with_the_reading_note():
    got = rtx(GpuInfo(note="neither NVML nor nvidia-smi is available"))
    assert got["code"] == "not_nvidia" and "neither NVML nor nvidia-smi" in got["reason"]


def test_linux_is_not_windows():
    got = rtx(nv("NVIDIA GeForce RTX 4090", 8.9), platform="linux")
    assert got["code"] == "not_windows" and "Windows only" in got["reason"]


def test_a_missing_sdk_says_where_to_get_it_and_that_it_is_never_downloaded():
    got = rtx(nv("NVIDIA GeForce RTX 4090", 8.9), path="")
    assert got["code"] == "needs_sdk"
    assert cap.RTX_VIDEO_SDK_URL in got["reason"] and "never downloads" in got["reason"]


def test_a_path_to_nothing_is_reported_with_the_path():
    got = rtx(nv("NVIDIA GeForce RTX 4090", 8.9), exists=False)
    assert got["code"] == "sdk_not_found" and DLL in got["reason"]


def test_an_unknown_generation_is_not_guessed():
    assert rtx(GpuInfo("nvidia", "Mystery", None, "581.0"))["code"] == "unknown_gpu"


def test_the_report_lists_every_provider_with_a_reason_when_it_is_not_usable():
    rep = cap.report(GpuInfo(note="none"), platform="linux", importable=lambda n: True)
    assert set(rep["upscale"]) == {"none", "basic", "rtx_video"} and set(rep["interpolate"]) == {"none", "basic"}
    assert cap.usable(rep, "upscale", "none") and cap.usable(rep, "upscale", "basic") and cap.usable(rep, "interpolate", "basic")
    assert not cap.usable(rep, "upscale", "rtx_video") and rep["upscale"]["rtx_video"]["reason"]
    assert rep["upscale"]["basic"]["neural"] is False and rep["upscale"]["rtx_video"]["neural"] is True


def test_the_basic_providers_need_numpy_and_opencv_and_say_which_is_missing():
    rep = cap.report(nv("NVIDIA GeForce RTX 4090", 8.9), importable=lambda n: n != "cv2")
    assert not cap.usable(rep, "upscale", "basic") and "OpenCV" in rep["upscale"]["basic"]["reason"]
    assert rep["interpolate"]["basic"]["code"] == "missing_library"
    rep = cap.report(nv("NVIDIA GeForce RTX 4090", 8.9), importable=lambda n: n != "numpy")
    assert "numpy" in rep["interpolate"]["basic"]["reason"]


def test_nvidia_smi_output_is_parsed_and_a_missing_capability_comes_from_the_name():
    text = "NVIDIA GeForce RTX 5090, 12.0, 581.42, 32607, 2204\nNVIDIA GeForce RTX 4090, [N/A], 581.42, 24564, 100\n"
    first = cap.parse_smi(text, 0)
    assert (first.name, first.compute_cap, first.driver, first.memory_total_mb, first.memory_used_mb) == ("NVIDIA GeForce RTX 5090", 12.0, "581.42", 32607, 2204)
    second = cap.parse_smi(text, 1)
    assert second.compute_cap == 8.9 and "name" in second.note
    assert cap.parse_smi(text, 5).vendor == "none"
    assert cap.parse_smi("", 0).vendor == "none"


def test_the_gpu_reading_is_kept_for_a_few_seconds():
    calls = []
    cache = cap._Cache()
    now = [0.0]

    def reader(i):
        calls.append(i)
        return nv("X", 8.9)
    for _ in range(3):
        cache.get(0, reader, clock=lambda: now[0])
    assert calls == [0]
    now[0] = 11.0
    cache.get(0, reader, clock=lambda: now[0])
    assert calls == [0, 0]


def test_without_nvml_or_nvidia_smi_the_reading_is_a_note_not_an_error(monkeypatch):
    monkeypatch.setattr(cap, "_read_nvml", lambda i: None)
    monkeypatch.setattr(cap.shutil, "which", lambda n: None)
    got = cap.read_gpu(0)
    assert got.vendor == "none" and "nvidia-smi" in got.note
