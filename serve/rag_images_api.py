"""Where the images of the documents are kept, from the Knowledge settings.

    GET  /api/rag/images                  the store in use, the folder and how many images it holds, and what MinIO there is on this PC with the choices to offer
    POST /api/rag/images/folder           {path}: move the images to another folder (copied, checked, then the setting changes; the old folder is left)
    POST /api/rag/images/use-folder       {}: keep the images as files in a folder again
    POST /api/rag/images/minio/existing   {endpoint, access_key, secret_key, bucket?}: use a MinIO that is already here (its secret key goes to the vault)
    POST /api/rag/images/minio/install    {}: install a separate MinIO of our own (its own folder, data and port), start it and use it
    POST /api/rag/images/minio/start|stop our own copy

Nothing is downloaded except by a POST the person made. Our own copy, once installed, starts itself the first time something actually reads or writes an image
through it and stops itself after IDLE_S (10 minutes) with no such use; `wire()` ties a RagStore to this, `stop_all()` is called when the server ends."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from tadamoo import minio_setup
from tadamoo.rag import RagError, RagStore
from tadamoo.rag.s3store import S3Error, S3ImageStore

OWN = "own.json"
SECRET_ENTRY = "images-s3"
IDLE_S = 600.0             # our own MinIO stops after this long with no real use of the image store
SWEEP_S = 30.0


def _put_secret(vault_dir: Any, entry: str, secret: str) -> None:
    from tadamoo.vault import Vault
    Vault(vault_dir).put(entry, kind="secret", label="Secret key of the image store (MinIO)", fields={"value": secret})


_SHARED: Dict[str, minio_setup.MinioService] = {}        # our own MinIO of each programs folder: the API is made again at each request, the process it started is not
_LAST_USED: Dict[str, float] = {}
_sweeper_started = False
_sweeper_lock = threading.Lock()


def stop_all() -> None:
    """Stops the MinIO processes this server started (called when the server ends)."""
    for service in list(_SHARED.values()):
        try:
            service.stop()
        except Exception:                                   # a process that is already gone is stopped
            pass
    _SHARED.clear()


def _touch(key: str, clock: Callable[[], float] = time.time) -> None:
    _LAST_USED[key] = clock()


def sweep(clock: Callable[[], float] = time.time) -> None:
    """Stops a running copy of ours that went unused for `IDLE_S`: called by the background sweeper, and directly by tests."""
    now = clock()
    for key, service in list(_SHARED.items()):
        try:
            if service.running() and now - _LAST_USED.get(key, now) > IDLE_S:
                service.stop()
        except Exception:                                    # a process already gone, or one that refuses to stop, is not this sweep's problem
            pass


def _ensure_sweeper() -> None:
    global _sweeper_started
    with _sweeper_lock:
        if _sweeper_started:
            return
        _sweeper_started = True

        def loop() -> None:
            while True:
                time.sleep(SWEEP_S)
                sweep()
        threading.Thread(target=loop, daemon=True, name="minio-idle").start()


class RagImagesApi:
    def __init__(self, store: RagStore, vault_dir: Any = None, tools_dir: Any = None, detect: Callable[[], Dict[str, Any]] = minio_setup.detect,
                 fetch: Optional[Callable[[str, Path], None]] = None, launcher: Optional[Callable[[List[str], Dict[str, str]], Any]] = None,
                 s3_factory: Callable[..., Any] = S3ImageStore, put_secret: Optional[Callable[[str, str], None]] = None, get_secret: Optional[Callable[[str], str]] = None) -> None:
        self.store, self.vault_dir, self.tools = store, vault_dir, Path(tools_dir) if tools_dir else None
        self._detect, self._fetch, self._launcher, self._s3 = detect, fetch, launcher, s3_factory
        self._put = put_secret or (lambda entry, secret: _put_secret(vault_dir, entry, secret))
        self._get = get_secret or self._vault_secret

    @property
    def _service(self) -> Optional[minio_setup.MinioService]:
        return _SHARED.get(str(self.tools))

    @_service.setter
    def _service(self, value: Optional[minio_setup.MinioService]) -> None:
        if value is None:
            _SHARED.pop(str(self.tools), None)
        else:
            _SHARED[str(self.tools)] = value

    def autostart(self) -> bool:
        """Starts our own MinIO when the images are kept in it and it is not running: called whenever something actually asks for the image store (`wire()`), so a copy the idle
        sweep stopped comes back on its own, with nothing to click. A failure is left for the page to show (the images page says the store does not answer); it never stops the server."""
        if self.store.settings.read().get("images_store") != "s3" or not self._own() or (self._service and self._service.running()):
            return False
        try:
            self.start_own()
            return True
        except Exception:
            return False

    def wire(self) -> None:
        """Ties this API to its store: every real use of the image store touches our own MinIO as just used (so the idle sweep leaves it alone) and starts it first if it had
        gone idle. Safe to call again on every request: it only replaces the same callback."""
        key = str(self.tools)

        def used() -> None:
            _touch(key)
            _ensure_sweeper()
            self.autostart()
        self.store.on_image_store_use = used

    def _vault_secret(self, entry: str) -> str:
        from tadamoo.rag.backends import vault_password
        return vault_password(self.vault_dir)(entry)

    # -- reading -----------------------------------------------------------------------------------

    def _exe(self) -> Optional[Path]:
        """MinIO ready to run: bundled with this copy of TaDaMoo (run from where it is, nothing to install) first, else one this PC downloaded itself."""
        bundled = minio_setup.bundled_program()
        if bundled is not None:
            return bundled
        return next((Path(self.tools) / n for n in minio_setup.NAMES if self.tools and (Path(self.tools) / n).is_file()), None)

    def _own_file(self) -> Optional[Path]:
        return (self.tools / OWN) if self.tools else None

    def _own(self) -> Dict[str, Any]:
        f = self._own_file()
        try:
            return json.loads(f.read_text(encoding="utf-8")) if f and f.is_file() else {}
        except (OSError, ValueError):
            return {}

    def overview(self) -> Dict[str, Any]:
        cfg = self.store.settings.read()
        folder = Path(cfg.get("images_dir") or (self.store.root / "images"))
        try:
            count = sum(1 for _ in folder.glob("??/??/*.webp")) if folder.is_dir() else 0
        except OSError:
            count = 0
        own = self._own()
        found = self._detect()
        exe = self._exe()
        return {"store": cfg.get("images_store", "folder"), "folder": str(folder), "count": count,
                "s3": {"endpoint": cfg.get("images_s3_endpoint", ""), "bucket": cfg.get("images_s3_bucket", ""), "access_key": cfg.get("images_s3_access_key", ""),
                       "secure": bool(cfg.get("images_s3_secure"))},
                "minio": {**minio_setup.plan(found), "release": {"tag": minio_setup.RELEASE["tag"], "bytes": minio_setup.RELEASE["bytes"], "url": minio_setup.RELEASE["url"]},
                          "own": {"installed": exe is not None, "running": bool(self._service and self._service.running()), "endpoint": own.get("endpoint", "")}}}

    # -- the person's choices ----------------------------------------------------------------------

    def route_get(self, parts: Sequence[str], query: Optional[Dict[str, List[str]]] = None) -> Optional[Dict[str, Any]]:
        return self.overview() if parts == ["images"] else None

    def route_post(self, parts: Sequence[str], req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if parts[:1] != ["images"]:
            return None
        if parts == ["images", "folder"]:
            return self.move_folder(req.get("path"))
        if parts == ["images", "use-folder"]:
            self.store.settings.write({"images_store": "folder"})
            return self.overview()
        if parts == ["images", "minio", "existing"]:
            return self.use_existing(req)
        if parts == ["images", "minio", "install"]:
            return self.install_separate()
        if parts == ["images", "minio", "start"]:
            return self.start_own()
        if parts == ["images", "minio", "stop"]:
            if self._service:
                self._service.stop()
            return self.overview()
        raise RagError("not found", "not_found")

    def move_folder(self, path: Any) -> Dict[str, Any]:
        if not isinstance(path, str) or not path.strip() or len(path) > 500 or "\x00" in path:
            raise RagError("path: the folder to move the images to")
        target = Path(path.strip())
        if self.store.settings.read().get("images_store") == "s3":
            raise RagError("the images are in MinIO, not in a folder: choose the folder store first")
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise RagError(f"that folder cannot be made: {e}") from None
        moved = self.store.move_images(target)
        return {**self.overview(), "moved": moved}

    def _check(self, endpoint: str, bucket: str, access: str, secret: str, secure: bool) -> None:
        try:
            self._s3(endpoint, bucket, access, secret, secure).ensure_bucket()
        except S3Error as e:
            raise RagError(f"MinIO did not accept it: {e}") from None
        except ValueError as e:
            raise RagError(str(e)) from None

    def use_existing(self, req: Dict[str, Any]) -> Dict[str, Any]:
        endpoint, access, secret = (str(req.get(k) or "").strip() for k in ("endpoint", "access_key", "secret_key"))
        bucket = str(req.get("bucket") or minio_setup.LOCAL_BUCKET).strip()
        if not endpoint or not access or not secret:
            raise RagError("endpoint, access_key and secret_key are needed")
        secure = bool(req.get("secure"))
        self._check(endpoint, bucket, access, secret, secure)
        minio_setup.provision(self.store, self._put, endpoint, access, secret, bucket, SECRET_ENTRY)
        self.store.settings.write({"images_s3_secure": secure})
        return self.overview()

    def install_separate(self) -> Dict[str, Any]:
        if self.tools is None:
            raise RagError("there is no folder for the programs of this app")
        folder = self.tools
        bundled = minio_setup.bundled_program()
        program = bundled if bundled is not None else minio_setup.install_release(folder, fetch=self._fetch)
        own = self._own()
        port = int(own.get("port") or minio_setup.free_port())
        keys = {"access_key": own.get("access_key"), "secret_key": ""}
        if not keys["access_key"]:
            keys = minio_setup.new_keys()
        else:
            keys["secret_key"] = self._get(SECRET_ENTRY)
        self._service = minio_setup.MinioService(program, folder / "data", port, keys["access_key"], keys["secret_key"], self._launcher)
        endpoint = self._service.start()
        _touch(str(self.tools))
        self._check(endpoint, minio_setup.LOCAL_BUCKET, keys["access_key"], keys["secret_key"], False)
        minio_setup.provision(self.store, self._put, endpoint, keys["access_key"], keys["secret_key"], minio_setup.LOCAL_BUCKET, SECRET_ENTRY)
        f = self._own_file()
        f.write_text(json.dumps({"port": port, "access_key": keys["access_key"], "endpoint": endpoint}), encoding="utf-8")
        return self.overview()

    def start_own(self) -> Dict[str, Any]:
        own = self._own()
        exe = self._exe()
        if not own or exe is None:
            raise RagError("there is no MinIO of our own installed: install it first")
        secret = self._get(SECRET_ENTRY)
        if self._service and self._service.running():
            return self.overview()
        self._service = minio_setup.MinioService(exe, Path(self.tools) / "data", int(own["port"]), own["access_key"], secret, self._launcher)
        self._service.start()
        _touch(str(self.tools))
        return self.overview()
