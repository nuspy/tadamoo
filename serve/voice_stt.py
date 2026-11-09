"""The voice service's speech to text, for the Telegram bot: ask whether it is ready, send it one audio file, get the words.

It never loads or installs anything: `check` reads the service's own state of the `stt` function and refuses, with the service's reason, when the
model is not installed, not loaded or the service is not running. `transcribe` posts the file to the service's `/transcribe` (the same decoder
and engine as callAPIcall's route) on loopback with the service token, which goes nowhere else."""
from __future__ import annotations

import http.client
import json
import re
import secrets
from pathlib import Path

from serve import voice_service
from tadamoo.telegram import SpeechError

READY_TIMEOUT_S = 5.0
TRANSCRIBE_TIMEOUT_S = 300.0
LANG = re.compile(r"[a-z]{2,3}")


def _reason(body: dict | None, fallback: str) -> str:
    if isinstance(body, dict):
        for key in ("message", "error", "reason"):
            if isinstance(body.get(key), str) and body[key].strip():
                return " ".join(body[key].split())
    return fallback


class VoiceSpeech:
    """What `TelegramBot` calls: `check()` and `transcribe(path, lang)`, both raising `SpeechError`."""

    def check(self) -> None:
        try:
            status, body = voice_service.call("GET", "/functions", timeout=READY_TIMEOUT_S)
        except voice_service.ServiceDown as exc:
            raise SpeechError("unavailable", str(exc)) from None
        stt = ((body or {}).get("functions") or {}).get("stt") if status == 200 else None
        if not isinstance(stt, dict):
            raise SpeechError("unavailable", f"the voice service did not say whether speech to text is ready (answer {status})")
        if stt.get("installed") is False:
            raise SpeechError("unavailable", _reason(stt, "the speech to text model is not installed"))
        if stt.get("state") != "loaded":
            why = f"the speech to text model is not loaded (it is {stt.get('state') or 'unknown'}): load it from the Voice functions dialog"
            raise SpeechError("unavailable", _reason({"message": stt.get("error")}, why))

    def transcribe(self, path: Path, lang: str = "") -> str:
        try:
            host, port, token = voice_service.endpoint()
        except voice_service.ServiceDown as exc:
            raise SpeechError("unavailable", str(exc)) from None
        boundary = "tadamoo" + secrets.token_hex(12)
        head = f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="voice.ogg"\r\nContent-Type: audio/ogg\r\n\r\n'.encode()
        body = head + Path(path).read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        target = "/transcribe" + (f"?lang={lang}" if LANG.fullmatch(lang or "") else "")
        conn = http.client.HTTPConnection(host, port, timeout=TRANSCRIBE_TIMEOUT_S)
        try:
            conn.request("POST", target, body=body, headers={"Authorization": "Bearer " + token, "Content-Type": f"multipart/form-data; boundary={boundary}"})
            resp = conn.getresponse()
            raw = resp.read(1 << 20)
        except (OSError, http.client.HTTPException) as exc:
            raise SpeechError("unavailable", f"the voice service does not answer ({exc.__class__.__name__})") from None
        finally:
            conn.close()
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if resp.status == 200 and isinstance(data, dict) and isinstance(data.get("text"), str):
            return data["text"]
        why = _reason(data, f"the voice service answered {resp.status}").replace(token, "[hidden]")
        if resp.status == 409:
            raise SpeechError("unavailable", why)
        raise SpeechError("undecodable" if resp.status == 400 else "failed", why)
