"""What holds the GPU for the local model, and how to make it let go for a while and take it back: the engines a heavy job can ask to step aside.

    free(reason)   "" when the VRAM is given back, else why not
    restore()      the model comes back

* Basalt-Plus and Strata: the engine sleeps (`SLEEP`: its VRAM is free, its experts stay in RAM) and wakes (`WAKE`) in seconds; nothing else may wake it meanwhile.
* llama.cpp started from TaDaMoo: the server is stopped (the model is unloaded) and started again with the same settings.
* LM Studio: its loaded models are unloaded (`lms unload`) and loaded again (`lms load`).

vLLM, and a llama-server that was not started here, have no way to be asked from here: they are not holders. Each holder says whether it is up now (`active`), so a
cloud service answering, or a model that is not loaded, gives none."""
from __future__ import annotations

import subprocess
import time
from typing import Any, Callable, List

SLEEP_TRIES = 60                                   # one try a second: a request the engine still runs has to finish first
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class BasaltHolder:
    name = "Basalt/Strata"

    def __init__(self, service: Any, pause: Callable[[float], None] = time.sleep, tries: int = SLEEP_TRIES) -> None:
        self.service, self.pause, self.tries = service, pause, tries

    def active(self) -> bool:
        engine = getattr(self.service, "engine", None)
        return bool(self.service.loaded()) and hasattr(engine, "sleep") and hasattr(engine, "can_sleep")

    def free(self, reason: str) -> str:
        self.service.heavy_hold = reason
        last = ""
        for _ in range(max(1, self.tries)):
            last = self.service.sleep()
            if last in ("asleep", "not loaded"):
                return ""
            if last != "busy":
                break
            self.pause(1.0)
        self.service.heavy_hold = None
        return "a request is still running" if last == "busy" else last

    def restore(self) -> None:
        self.service.heavy_hold = None
        self.service.wake_now()


class LlamaCppHolder:
    name = "llama.cpp"

    def __init__(self, get: Callable[[], Any]) -> None:
        self.get, self.config = get, {}

    def active(self) -> bool:
        return self.get().state()[0] in ("ready", "starting") and bool(self.get().proc is not None)

    def free(self, reason: str) -> str:
        server = self.get()
        self.config = dict(server.running_cfg or server.settings())
        try:
            server.stop()
        except Exception as e:  # noqa: BLE001 - the reason goes to the person
            return f"llama-server could not be stopped ({e})"
        return ""

    def restore(self) -> None:
        if self.config:
            self.get().start(self.config)


class LmStudioHolder:
    name = "LM Studio"

    def __init__(self, local: Any) -> None:
        self.local, self.models = local, []

    def active(self) -> bool:
        try:
            return bool(self.local.lmstudio_up() and self.local.loaded())
        except Exception:  # noqa: BLE001 - not reachable is not active
            return False

    def free(self, reason: str) -> str:
        self.models = list(self.local.loaded() or [])
        ok, message = self.local.unload()
        return "" if ok else f"LM Studio could not unload its models ({message})"

    def restore(self) -> None:
        cli = self.local.lms_cli()
        for model in self.models:
            if cli:
                subprocess.run([cli, "load", model, "--yes"], capture_output=True, text=True, timeout=float(self.local.lm.load_timeout_s), creationflags=NO_WINDOW)


def default_holders(service: Any) -> List[Any]:
    """The holders that exist on this PC; each says whether it is up now."""
    from serve import llamacpp
    from tadamoo.llmswitch.local import LocalEngines
    return [BasaltHolder(service), LlamaCppHolder(llamacpp.get), LmStudioHolder(LocalEngines())]
