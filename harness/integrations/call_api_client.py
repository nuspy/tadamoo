"""ACP Client for telephony, Whisper STT and Kokoro TTS (callAPIcall module).

Allows the Basalt Agent to perform real-world voice calls via GSM SIM800C modem
or Android Bluetooth Hands-Free Profile (HFP).
"""
from __future__ import annotations

import json
import urllib.request
import urllib.error
from typing import Any, Dict, Optional


class CallAPICallClient:
    def __init__(self, endpoint: str = "http://127.0.0.1:8790", timeout: float = 5.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    def is_available(self) -> bool:
        """Checks if the callAPIcall server is running."""
        try:
            req = urllib.request.Request(f"{self.endpoint}/health", method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    def make_phone_call(
        self,
        phone_number: str,
        goal: str,
        persona_prompt: str = "",
        mode: str = "gsm_modem",
    ) -> Dict[str, Any]:
        """Initiates an autonomous phone call using Whisper STT and Kokoro TTS."""
        payload = {
            "phone_number": phone_number,
            "goal": goal,
            "persona_prompt": persona_prompt,
            "mode": mode,
        }
        try:
            req = urllib.request.Request(
                f"{self.endpoint}/api/calls",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            return {
                "status": "failed",
                "error": f"callAPIcall service unavailable: {str(exc)}",
                "phone_number": phone_number,
            }

    def get_call_transcript(self, call_id: str) -> Optional[str]:
        """Retrieves real-time Whisper transcription of an ongoing or completed call."""
        try:
            req = urllib.request.Request(f"{self.endpoint}/api/calls/{call_id}/transcript", method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("transcript")
        except Exception:
            return None

    def hangup(self, call_id: str) -> bool:
        """Terminates an active voice call."""
        try:
            req = urllib.request.Request(f"{self.endpoint}/api/calls/{call_id}/hangup", method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status == 200
        except Exception:
            return False
