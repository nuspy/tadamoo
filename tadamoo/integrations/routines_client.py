"""Client for the server's routines routes (/api/routines): what the cron_* tools use.

The routines live in the server (one scheduler for the Cron tab and every conversation's tools). The agent process talks to them
over the loopback address, with the API key when there is one.
"""
from __future__ import annotations

import urllib.parse
from typing import Any, Dict, List, Optional

from ._http import ServiceError, request_json
from tadamoo import control_secret


class RoutinesClient:
    def __init__(self, endpoint: str, api_key: Optional[str] = None, timeout: float = 10.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _call(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        headers.update(control_secret.header_for(self.endpoint))
        return request_json(method, self.endpoint + path, payload, headers, self.timeout)

    @staticmethod
    def _id(routine_id: str) -> str:
        if not isinstance(routine_id, str) or not routine_id:
            raise ValueError("name the routine")
        return urllib.parse.quote(routine_id)

    def list(self) -> List[Dict[str, Any]]:
        data = self._call("GET", "/api/routines")
        return data.get("routines", []) if isinstance(data, dict) else []

    def create(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        return self._call("POST", "/api/routines", fields)

    def act(self, routine_id: str, action: str, fields: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._call("POST", f"/api/routines/{self._id(routine_id)}/{action}", fields or {})


__all__ = ["RoutinesClient", "ServiceError"]
