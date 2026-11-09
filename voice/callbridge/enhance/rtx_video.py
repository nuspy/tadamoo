"""NVIDIA RTX Video SDK as an upscaling provider: a stub that says plainly what is missing.

The SDK (super resolution and artifact reduction for arbitrary video, Windows 64-bit, GeForce RTX) is an NVIDIA developer download under
NVIDIA's terms: TaDaMoo never downloads, installs or bundles it. The person installs it and puts the full path of its DLL in `voice.json`
`avatar.enhance.rtx_video_path`.

THE BINDING IS NOT WRITTEN. The functions, structs and enums of the SDK's C API (create/destroy, set the input and output surfaces, process one
frame, the quality and scale options) are not in this repository, and no signature has been verified against NVIDIA's headers, so none is
guessed here: a wrong ctypes signature crashes the process. When the headers are at hand the work is: load the DLL with `ctypes.WinDLL`,
declare `argtypes`/`restype` of each function from the header, upload the BGR frame to a surface, process, read it back, and replace the
`NeedsSdk` below with that call. `capability.rtx_video` already does every check that comes before (Windows, an RTX GPU of the right
generation, the driver, the DLL file being there); the provider is registered so the person sees it and its reason.
"""
from __future__ import annotations

from . import providers
from .capability import INSTALL_HINT


class RtxVideoUpscaler:
    name, neural = "rtx_video", True

    def __init__(self, settings) -> None:
        self.dll_path = getattr(settings, "rtx_video_path", "")

    def upscale(self, img, out_w: int, out_h: int):
        if not self.dll_path:
            raise providers.NeedsSdk("the RTX Video SDK is not installed: " + INSTALL_HINT)
        raise providers.NeedsSdk("this build has no binding to the RTX Video SDK's C API yet (docs/voice/acceleration.md)")


providers.register("upscale", "rtx_video", RtxVideoUpscaler)
