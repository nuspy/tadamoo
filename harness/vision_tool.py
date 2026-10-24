"""`analyze_image`: looks at an image with whatever the person chose for visual analysis.

The choice lives in the capability settings (`harness/capabilities.py`): the model's own vision projector, a skill or an MCP
server found on this PC and approved, or nothing. With the model's own vision the image is cropped and enlarged on request
(fine detail is read better from a close-up), limited in size, and sent to the loaded model. When vision is off the tool says so
and what to do; it never pretends to have seen the image.
"""
from __future__ import annotations

import base64
import io
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence

from harness.capabilities import CapabilityIndex

MAX_BYTES = 12_000_000
LONG_EDGE = 1568                      # larger images are reduced: more pixels cost tokens without showing more
MIN_EDGE = 640                        # a small crop is enlarged to this so the detail is readable
FORMATS = {"PNG", "JPEG", "WEBP", "GIF", "BMP"}


def vision_loaded(endpoint: str, api_key: Optional[str], timeout: float = 5.0) -> bool:
    """Whether the loaded model takes images (`/v1/models` lists its input modalities)."""
    req = urllib.request.Request(endpoint.rstrip("/") + "/v1/models", headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return any("image" in ((m.get("architecture") or {}).get("input_modalities") or []) for m in data.get("data", []))
    except (urllib.error.URLError, OSError, ValueError):
        return False


def prepare_image(raw: bytes, crop: Optional[Sequence[float]] = None) -> str:
    """The image as a data URL: opened and checked, cropped to a region (fractions of the width and height), kept within the
    long edge and enlarged when the region is small."""
    from PIL import Image
    if len(raw) > MAX_BYTES:
        raise ValueError(f"The file is {len(raw) // 1_000_000} MB; the most is {MAX_BYTES // 1_000_000} MB.")
    try:
        img = Image.open(io.BytesIO(raw))
    except Exception:
        raise ValueError("That is not an image I can read (PNG, JPEG, WEBP, GIF or BMP).") from None
    if (img.format or "") not in FORMATS:
        raise ValueError("That is not an image I can read (PNG, JPEG, WEBP, GIF or BMP).")
    img.load()
    img = img.convert("RGB")
    if crop is not None:
        if len(crop) != 4 or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in crop):
            raise ValueError("crop is four numbers from 0 to 1: left, top, right, bottom.")
        x0, y0, x1, y1 = (min(1.0, max(0.0, float(v))) for v in crop)
        if x1 - x0 < 0.02 or y1 - y0 < 0.02:
            raise ValueError("The crop is too small or inverted: left < right and top < bottom.")
        w, h = img.size
        img = img.crop((int(x0 * w), int(y0 * h), max(int(x0 * w) + 1, int(x1 * w)), max(int(y0 * h) + 1, int(y1 * h))))
    long_edge = max(img.size)
    scale = LONG_EDGE / long_edge if long_edge > LONG_EDGE else (MIN_EDGE / long_edge if crop is not None and long_edge < MIN_EDGE else 1.0)
    if scale != 1.0:
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def register_vision_tool(registry: Any, index: CapabilityIndex, read_path: Callable[[str], Path], endpoint: str, api_key: Optional[str],
                         client: Any, vision_check: Optional[Callable[[], bool]] = None) -> None:
    """Adds `analyze_image`. `read_path` turns the model's path into one inside the workspace (and refuses an escape)."""
    from harness.tools.registry import EffectClass
    check = vision_check or (lambda: vision_loaded(endpoint, api_key))

    def analyze_image(path: str, question: str, crop: Optional[List[float]] = None) -> str:
        choice = index.settings()["visual"]
        if choice == "none":
            return "Visual analysis is not set up. Ask the person to choose one in Settings, under Capabilities."
        if choice != "builtin":
            entry = index.get(choice)
            if entry is None or entry["state"] != "approved":
                return f"The visual analysis the person chose ({choice}) is not available or not approved: Settings, under Capabilities."
            if entry["kind"] == "skill":
                try:
                    return f"Use this skill to look at the image {path} and answer: {question}\n\n{index.body(choice)}"
                except (KeyError, ValueError, PermissionError) as exc:
                    return str(exc.args[0] if exc.args else exc)
            return (f"Use call_capability with id '{choice}': leave tool empty to list its tools, then call the one that analyses an image, "
                    f"giving it the path {path}. The question is: {question}")
        try:
            raw = read_path(path).read_bytes()
            url = prepare_image(raw, crop)
        except (OSError, PermissionError) as exc:
            return f"Could not read the image: {exc}"
        except ValueError as exc:
            return str(exc)
        except Exception as exc:                                  # a damaged file raises many kinds of error
            return f"That file could not be opened as an image ({type(exc).__name__})."
        if not check():
            return ("The loaded model cannot see images: its vision is off. Tell the person; they can turn it on in the model's settings "
                    "(vision: dynamic, then restart the server), or choose another visual analysis in Settings.")
        try:
            reply = client.chat_completion(
                messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}, {"type": "text", "text": str(question)[:2000]}]}],
                temperature=0.0, max_tokens=900, timeout=180.0, extra={"reasoning_effort": "none"})
            return (reply["choices"][0]["message"].get("content") or "").strip() or "The model returned no description."
        except Exception as exc:
            return f"The model could not look at the image: {type(exc).__name__}: {exc}"

    registry.register(
        "analyze_image", "Looks at an image file in the workspace and answers a question about it. For fine detail pass `crop` "
                         "[left, top, right, bottom] as fractions of the image (0 to 1): the region is enlarged.",
        {"type": "object", "properties": {"path": {"type": "string"}, "question": {"type": "string"},
                                          "crop": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4}},
         "required": ["path", "question"]}, EffectClass.PURE, analyze_image, untrusted=True)
