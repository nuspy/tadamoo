"""Every benchmark runs only with the engine and model the person serves here: no other model, no cloud API, and no cloud judge or user simulator.
`refusal` reads a run's settings and says why they are not allowed, or returns an empty string."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping, Set
from urllib.parse import urlparse

LOOPBACK = ("127.0.0.1", "localhost", "::1")
PROVIDER_KEYS = ("provider", "api_provider", "judge_provider")
MODEL_KEYS = ("model", "judge_model", "simulator_model", "user_model", "agent_model", "model_name")
ADDRESS_KEYS = ("base_url", "endpoint", "api_base", "judge_endpoint", "api_url")
SECRET_KEYS = ("api_key", "apikey", "judge_key", "openai_api_key", "anthropic_api_key", "openrouter_api_key")
LOCAL_WORDS = ("", "local", "basalt")


@dataclass(frozen=True)
class LocalModel:
    """The served model: the one fixed endpoint every benchmark run uses."""
    name: str
    file: str
    quantisation: str
    endpoint: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def names(self) -> Set[str]:
        file_name = self.file.replace("\\", "/").rsplit("/", 1)[-1]
        return {n.lower() for n in (self.name, self.file, file_name) if n} | set(LOCAL_WORDS)


def from_info(info: Mapping[str, Any]) -> LocalModel:
    """The served model from what the model-info source returns: name, file, quantisation (or quant) and endpoint."""
    return LocalModel(str(info.get("name") or ""), str(info.get("file") or info.get("path") or ""), str(info.get("quantisation") or info.get("quant") or ""),
                      str(info.get("endpoint") or ""))


def _walk(value: Any, path: str = ""):
    if isinstance(value, Mapping):
        for k, v in value.items():
            yield from _walk(v, f"{path}.{k}" if path else str(k))
            yield str(k).lower(), path, v
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk(item, path)


def refusal(settings: Mapping[str, Any], model: LocalModel) -> str:
    """Why these settings name something other than the local model ("" when they do not)."""
    local = model.names()
    local_host = urlparse(model.endpoint).hostname or ""
    for key, where, value in _walk(settings):
        name = f"{where}.{key}" if where else key
        text = str(value).strip() if value is not None and not isinstance(value, (Mapping, list, tuple)) else ""
        if key in SECRET_KEYS and value:
            return f"'{name}' holds an API key; benchmarks run only with the local model and no cloud API"
        if key in PROVIDER_KEYS and text.lower() not in LOCAL_WORDS:
            return f"'{name}' names the provider '{text}'; benchmarks run only with the local engine"
        if key in MODEL_KEYS and text.lower() not in local:
            return f"'{name}' names the model '{text}'; benchmarks run only with the served model '{model.name or model.file}'"
        if key in ADDRESS_KEYS and text:
            host = urlparse(text if "//" in text else "//" + text).hostname or ""
            if host not in LOOPBACK and host != local_host:
                return f"'{name}' points to {host or text}; benchmarks run only against the local engine"
    return ""
