"""The `browser_*` tools on agent-browser (https://github.com/vercel-labs/agent-browser, third_party/agent-browser/, Apache-2.0) instead of TaDaMoo's own CDP driver.

The model sees the same tools with the same names and the same page description (numbered elements, then the text); only what drives Chrome or Edge changes. agent-browser runs as a program,
one command at a time, with a background daemon that keeps the browser open between commands; each conversation has its own session. Everything about the person's rules stays in this layer,
in Python: the permission levels and allow rules, the private-address rule, the consent banners (browser_consent.py's fixed rules, run through the page), the question before a picture after a vault value was
typed, and the vault: a `{{vault:...}}` value is resolved by the tool layer (browser.py `register_browser_tools`) against the site that receives it, and reaches agent-browser on its standard input (the
`batch` command reads its commands from there), never in the program's arguments. docs/browser-backends.md says what is the same, what is different and what cannot be carried over.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from tadamoo import browser_consent as bc
from tadamoo.browser import Browser, BrowserError, PrivateAddress, check_url
from tadamoo.sandbox import cancelled

CALL_SECONDS = 60.0
TEXT_CHARS = 60000
PROBES = 40                      # how many fields get their type read (password or not) in one look at a page
IDEMPOTENT = {"open", "snapshot", "get", "tab", "read", "dialog", "wait"}
INTERACTIVE = {"link": "a", "button": "button", "textbox": "input", "searchbox": "input", "combobox": "select", "listbox": "select", "checkbox": "input", "radio": "input", "switch": "input",
               "tab": "button", "menuitem": "button", "menuitemcheckbox": "button", "menuitemradio": "button", "slider": "input", "spinbutton": "input", "option": ""}
LINE = re.compile(r'^(?P<indent>\s*)- (?P<role>[A-Za-z]+)(?: "(?P<name>(?:[^"\\]|\\.)*)")?(?: \[(?P<attrs>[^\]]*)\])?(?: hint="[^"]*")?(?::\s?(?P<value>.*))?$')
KEYS = ("Enter", "Tab", "Escape", "Backspace", "ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Space")


class AbCli:
    """Runs agent-browser commands for one session and returns the `data` of the JSON answer (`success`, `data`, `error`, `code`), or raises BrowserError."""

    def __init__(self, command: List[str], session: str, config: Path, home: Optional[Path] = None, executable: str = "", cdp_url: str = "", headed: bool = False, profile: str = "",
                 downloads: str = "", timeout: float = CALL_SECONDS, popen: Callable[..., Any] = subprocess.Popen) -> None:
        self.command, self.session, self.config, self.home = list(command), session, Path(config), home
        self.executable, self.cdp_url, self.headed, self.profile, self.downloads = executable, cdp_url, headed, profile, downloads
        self.timeout, self.popen = timeout, popen
        self.launch_args: Optional[Callable[[], List[str]]] = None       # browser switches for the launch (read each time; they only count when the browser starts)

    def env(self) -> Dict[str, str]:
        env = dict(os.environ)
        if self.home is not None:
            self.home.mkdir(parents=True, exist_ok=True)
            env["USERPROFILE"] = env["HOME"] = str(self.home)                       # its daemon sockets and state stay in TaDaMoo's own folder
        return env

    def argv(self, args: List[str]) -> List[str]:
        if not self.config.exists():
            self.config.parent.mkdir(parents=True, exist_ok=True)
            self.config.write_text("{}", encoding="utf-8")                          # the only configuration read: not the user's, not a project's (./agent-browser.json)
        out = [*self.command, "--json", "--config", str(self.config), "--session", self.session, "--idle-timeout", "20m", "--max-output", str(TEXT_CHARS)]
        if self.cdp_url:
            out += ["--cdp", self.cdp_url]
        elif self.executable:
            out += ["--executable-path", self.executable]
        if self.headed and not self.cdp_url:
            out.append("--headed")
        if self.profile:
            out += ["--profile", self.profile]
        if self.downloads:
            out += ["--download-path", self.downloads]
        launch = self.launch_args() if self.launch_args is not None and not self.cdp_url else []
        if launch:
            out += ["--args", ",".join(launch)]
        return out + args

    def run(self, args: List[str], stdin: Optional[str] = None, timeout: Optional[float] = None) -> Any:
        """The `data` of the answer; for `batch` the list of its results. A command that may have run when the connection failed (`outcome_unknown`) is run again only if reading."""
        for attempt in (1, 2):
            try:
                return self._once(args, stdin, timeout)
            except BrowserError as e:
                if attempt == 1 and getattr(e, "code", "") == "outcome_unknown" and args[0] in IDEMPOTENT:
                    continue
                raise
        raise BrowserError("unreachable")

    def _once(self, args: List[str], stdin: Optional[str], timeout: Optional[float]) -> Any:
        flags = 0x08000000 if sys.platform == "win32" else 0
        try:
            proc = self.popen(self.argv(args), stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env(), creationflags=flags)
        except OSError as e:
            raise BrowserError(f"agent-browser could not be started: {e}") from None
        payload = stdin.encode("utf-8") if stdin is not None else None
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            try:
                out, err = proc.communicate(input=payload, timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                payload = None                                                         # the input was sent with the first try; a second try must not send it again
                if cancelled():
                    proc.kill()
                    proc.communicate()
                    raise BrowserError("stopped: the person pressed Stop") from None
                if time.monotonic() > deadline:
                    proc.kill()
                    proc.communicate()
                    raise BrowserError(f"agent-browser did not answer {args[0]} in {timeout or self.timeout:g} seconds") from None
        text = (out or b"").decode("utf-8-sig", "replace").strip()
        try:
            answer = json.loads(text)
        except ValueError:
            raise BrowserError(f"agent-browser did not answer with JSON ({(err or b'').decode('utf-8', 'replace').strip()[:150] or text[:150]!r})") from None
        if isinstance(answer, list):                                                   # the results of a batch
            return answer
        if not isinstance(answer, dict) or "success" not in answer:
            raise BrowserError("agent-browser answered something this code does not know: update TaDaMoo or install the matching agent-browser")
        if not answer.get("success"):
            raise explain(str(answer.get("error") or ""), str(answer.get("code") or ""))
        return answer.get("data") or {}


class AbError(BrowserError):
    code = ""


def explain(message: str, code: str = "") -> BrowserError:
    """What agent-browser said, with what to do about the usual ones."""
    low = message.lower()
    if "unknown ref" in low or "stale ref" in low or "element not found" in low:
        text = "there is no element with that number on this page: read the page again (the page may have changed)"
    elif "covered by" in low:
        text = f"{message}. Close or answer what covers it (a banner, a dialog), read the page again, and click again"
    elif code == "outcome_unknown":
        text = "the command reached the browser but its result was lost, so it may or may not have happened: read the page before doing it again"
    else:
        text = message[:300] or "agent-browser failed"
    err = AbError(text)
    err.code = code
    return err


def _decode(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")


def parse_snapshot(text: str) -> List[Dict[str, Any]]:
    """The lines of a snapshot as nodes: role, name, ref ('e12'), attributes, value, url, depth and the iframe refs above each (outermost first)."""
    nodes: List[Dict[str, Any]] = []
    frames: List[Tuple[int, str]] = []                                  # (depth, ref) of the iframes the line is inside
    for line in text.splitlines():
        m = LINE.match(line)
        if not m:
            continue
        depth = len(m.group("indent")) // 2
        while frames and frames[-1][0] >= depth:
            frames.pop()
        attrs = [a.strip() for a in (m.group("attrs") or "").split(",") if a.strip()]
        ref = next((a[4:] for a in attrs if a.startswith("ref=")), "")
        url = next((a[4:] for a in attrs if a.startswith("url=")), "")
        node = {"role": m.group("role"), "name": _decode(m.group("name") or ""), "ref": ref, "attrs": attrs, "value": _decode(m.group("value") or ""), "url": url, "depth": depth,
                "frames": [r for _, r in frames]}
        nodes.append(node)
        if m.group("role").lower() == "iframe" and ref:
            frames.append((depth, ref))
    return nodes


class AgentBrowser(Browser):
    """The Browser the tools use, driven through agent-browser. It keeps Browser's rules (permissions, private addresses, banners, the vault's destination) and replaces how pages are driven."""

    def __init__(self, cli: AbCli, headless: bool = False, allow_private: bool = False, mask: Optional[Callable[[str], str]] = None, persistent: bool = False,
                 consent: str = bc.DEFAULT_CONSENT, permissions: str = bc.DEFAULT_PERMISSIONS, confirm: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
                 audit: Optional[Callable[[str, Dict[str, Any]], None]] = None, auto: Optional[Callable[[], bool]] = None, rules: Optional[Callable[[], List[str]]] = None,
                 sleep: Callable[[float], None] = time.sleep, proxy: str = "") -> None:
        self.cli, self.sleep, self.proxy = cli, sleep, proxy
        self.cdp_url = cli.cdp_url
        self.headless, self.allow_private = headless, allow_private or bool(cli.cdp_url)
        self.persistent = persistent
        self.profile_dir = Path(cli.profile or tempfile.gettempdir())
        self.mask = mask or (lambda t: t)
        self.pages: List[Any] = []
        self.refs: List[Tuple[str, str]] = []                          # element number - 1 -> (ref like @e12, the site that would receive what is typed there)
        self.host = ""
        self.notice = ""
        self.hidden: set = set()
        self.page_url = ""
        self.blocked: List[str] = []
        self.consent, self.permissions = bc.clean_consent(consent), bc.clean_permissions(permissions)
        self.confirm, self.audit, self.auto, self.rules = confirm, audit, auto, rules
        self._consent_sites: set = set()
        self._consenting = False
        self._asked: set = set()
        self._private_hosts: set = set()
        self._policy_applied = ""
        self._tabs: List[str] = []
        self._downloads_seen: set = set()
        cli.launch_args = self._launch_args

    def _launch_args(self) -> List[str]:
        """What a page's request for a permission (camera, location, notifications...) meets: the browser's prompt cannot be answered from here, so it is refused; `grant` lets the browser
        give the camera and the microphone (the one thing a switch can grant). The person's `ask` and `grant-safe` choices are not carried over (docs/browser-backends.md). With `proxy` set
        (the dark web, through Tor) the browser's own DNS goes through it too, so an address never asked of this PC's resolver leaks there instead."""
        args = ["--use-fake-ui-for-media-stream"] if self._permission_policy() == "grant" else ["--deny-permission-prompts"]
        if self.proxy:
            args += [f"--proxy-server={self.proxy}", "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1"]
        return args

    def start(self) -> None:
        pass

    # -- reading ---------------------------------------------------------------------------------------------------------

    def open(self, url: str) -> Dict[str, Any]:
        try:
            url = check_url(url, self._private_free())
        except PrivateAddress as e:
            if not self._private_ok(e.host):
                raise
            url = check_url(url, True)
        self.cli.run(["open", url])
        self._follow_tabs()
        self._consent_pass()
        return self.snapshot()

    def snapshot(self) -> Dict[str, Any]:
        """The page as Browser describes it: numbered elements (those of iframes after the page's own, each in the site of its frame) and the text."""
        results = self.cli.run(["batch", "--omit-command"], stdin=json.dumps([["snapshot", "-i", "-u"], ["get", "url"], ["get", "title"], ["get", "text", "body"], ["dialog", "status"]]))
        snap, url, title, text, dialog = [self._result(r) for r in results]
        self.page_url = str(url.get("url") or "")
        self._guard_private()
        nodes = [n for n in parse_snapshot(str(snap.get("snapshot") or "")) if n["ref"]]
        page_host = urlparse(self.page_url).hostname or ""
        items, refs = self._items(nodes, page_host)
        self.refs, self.host = refs, page_host
        self._after_actions(dialog)
        return {"url": self.page_url, "host": page_host, "title": str(title.get("title") or ""), "items": items, "text": str(text.get("text") or "")}

    @staticmethod
    def _result(entry: Any) -> Dict[str, Any]:
        if not isinstance(entry, dict) or not entry.get("success", False):
            raise explain(str((entry or {}).get("error") if isinstance(entry, dict) else entry or "agent-browser failed"), str((entry or {}).get("code") or "") if isinstance(entry, dict) else "")
        return entry.get("result") or {}

    def _guard_private(self) -> None:
        if self._private_free() or not self.page_url.startswith(("http://", "https://")):
            return
        try:
            check_url(self.page_url, False)
        except PrivateAddress as e:
            if not self._private_ok(e.host):
                self.cli.run(["open", "about:blank"])
                raise
        except BrowserError:
            self.cli.run(["open", "about:blank"])
            raise

    def _items(self, nodes: List[Dict[str, Any]], page_host: str) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
        """The elements a person could use: links, buttons, fields, checkboxes, lists. A field's type (password or not) and an iframe's address are read in one more batch."""
        wanted = [n for n in nodes if n["role"].lower() in INTERACTIVE and INTERACTIVE[n["role"].lower()]]
        fields = [n for n in wanted if n["role"].lower() in ("textbox", "searchbox")][:PROBES]
        frames = {r for n in wanted for r in n["frames"]}
        probes: List[List[str]] = [["get", "attr", "@" + n["ref"], "type"] for n in fields] + [["get", "attr", "@" + r, "src"] for r in sorted(frames)]
        found: List[Dict[str, Any]] = []
        if probes:
            for entry in self.cli.run(["batch", "--omit-command"], stdin=json.dumps(probes)):
                found.append(entry.get("result") or {} if isinstance(entry, dict) else {})
        types = {n["ref"]: str(found[i].get("value") or "text").lower() for i, n in enumerate(fields) if i < len(found)}
        hosts: Dict[str, str] = {}
        for i, r in enumerate(sorted(frames)):
            src = str(found[len(fields) + i].get("value") or "") if len(fields) + i < len(found) else ""
            hosts[r] = urlparse(urljoin(self.page_url, src)).hostname or page_host if src and not src.startswith(("about:", "javascript:")) else page_host
        items: List[Dict[str, Any]] = []
        refs: List[Tuple[str, str]] = []
        options: Dict[str, List[str]] = {}
        for k, n in enumerate(nodes):                                         # the options listed under a list belong to it
            if n["role"].lower() == "option":
                owner = next((p for p in reversed(nodes[:k]) if p["depth"] < n["depth"] and p["role"].lower() in ("combobox", "listbox")), None)
                if owner is not None:
                    options.setdefault(owner["ref"], []).append(n["name"])
        for n in wanted:
            role = n["role"].lower()
            tag = INTERACTIVE[role]
            it: Dict[str, Any] = {"ref": len(items) + 1, "tag": tag, "type": "", "name": n["name"][:90]}
            if tag == "input":
                it["type"] = {"checkbox": "checkbox", "switch": "checkbox", "radio": "radio", "slider": "range", "spinbutton": "number"}.get(role) or types.get(n["ref"], "text")
                if it["type"] in ("text", "email", "search", "tel", "url", "number", "password") or role in ("textbox", "searchbox"):
                    it["value"] = "(set)" if it["type"] == "password" and n["value"] else n["value"][:60]
            if tag == "select":
                it["value"] = n["value"]
                it["options"] = [o[:30] for o in options.get(n["ref"], [])]
            if role in ("checkbox", "switch", "radio"):
                it["checked"] = any(a in ("checked=true", "checked", "selected") for a in n["attrs"])
            if tag == "a" and n["url"]:
                p = urlparse(n["url"])
                it["href"] = p.netloc + p.path[:40]
            if "disabled" in n["attrs"] or "disabled=true" in n["attrs"]:
                it["disabled"] = True
            if n["frames"]:
                site = hosts.get(n["frames"][-1], page_host)
                it["frame"] = site or "frame"
            else:
                site = page_host
            items.append(it)
            refs.append(("@" + n["ref"], site))
        return items, refs

    def _where(self, ref: int) -> Tuple[str, str]:
        if isinstance(ref, bool) or not isinstance(ref, int) or not 1 <= ref <= len(self.refs):
            raise BrowserError("there is no element with that number on this page: read the page again (the page may have changed)")
        return self.refs[ref - 1]

    def host_for(self, ref: int) -> str:
        """The site that would receive what is typed into an element: the one its iframe declares (`src`), not the page's, so a payment provider's frame is a site of its own."""
        return self._where(ref)[1]

    # -- acting ---------------------------------------------------------------------------------------------------------------

    def _settle(self) -> None:
        self.sleep(0.4)
        self._follow_tabs()

    def _follow_tabs(self) -> None:
        """A window the page opened (a payment sheet) becomes the page the agent works on, as with Browser."""
        data = self.cli.run(["tab"])
        tabs = data.get("tabs") or []
        ids = [str(t.get("tabId")) for t in tabs]
        newer = [t for t in tabs if str(t.get("tabId")) not in self._tabs and not t.get("active")]
        if self._tabs and newer:
            self.cli.run(["tab", str(newer[-1]["tabId"])])
        elif self._tabs and not any(t.get("active") for t in tabs) and tabs:
            self.cli.run(["tab", str(tabs[-1]["tabId"])])
        self._tabs = ids

    def click(self, ref: int) -> Dict[str, Any]:
        element, _ = self._where(ref)
        self.cli.run(["click", element])
        self._settle()
        return self.snapshot()

    def type(self, ref: int, text: str, submit: bool = False, clear: bool = True, secret: bool = False) -> Dict[str, Any]:
        """Types into a field. A value from the vault goes to agent-browser on its standard input (a `batch`), so it is in no list of processes."""
        element, _ = self._where(ref)
        commands: List[List[str]] = [["fill" if clear else "type", element, "--", text]]
        if submit:
            commands.append(["press", "Enter"])
        if secret:
            self._batch(commands)
        else:
            for c in commands:
                self.cli.run(c)
        self._settle()
        return self.snapshot()

    def _batch(self, commands: List[List[str]]) -> None:
        for entry in self.cli.run(["batch", "--bail", "--omit-command"], stdin=json.dumps(commands)):
            if isinstance(entry, dict) and not entry.get("success", True):
                raise explain(self.mask(str(entry.get("error") or "")), str(entry.get("code") or ""))

    def select(self, ref: int, value: str) -> Dict[str, Any]:
        element, _ = self._where(ref)
        self.cli.run(["select", element, value])
        self._settle()
        return self.snapshot()

    def press(self, name: str) -> Dict[str, Any]:
        self.cli.run(["press", name])
        self._settle()
        return self.snapshot()

    def back(self) -> Dict[str, Any]:
        self.cli.run(["back"])
        self._settle()
        return self.snapshot()

    def screenshot(self, full_page: bool = False) -> bytes:
        """A PNG of the page. Once a value from the vault has been typed in this browser the picture could show it: at the Auto level it is taken, otherwise the person is asked."""
        if self.hidden and not self._approve("browser_screenshot", {"site": self._origin(), "reason": "a value from the vault was typed in this browser and the picture could show it"}):
            raise BrowserError("no screenshot was taken: a value from the vault was typed in this browser, the picture could show it, and the person did not allow it. Read the page as text")
        folder = Path(tempfile.mkdtemp(prefix="tadamoo-ab-shot-"))
        try:
            path = folder / "page.png"
            self.cli.run(["screenshot", *(["--full"] if full_page else []), str(path)])
            return path.read_bytes()
        except OSError as e:
            raise BrowserError(f"the screenshot could not be read: {e}") from None
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def close(self) -> None:
        if self.cdp_url:
            return                                                      # a browser somebody else started is never ended from here
        try:
            self.cli.run(["close"], timeout=15)
        except BrowserError:
            pass

    # -- banners, dialogs, downloads ----------------------------------------------------------------------------------------------

    def _eval(self, script: str) -> Any:
        return self.cli.run(["eval", "--stdin"], stdin=script).get("result")

    def _detect_consent(self) -> Dict[str, Any]:
        try:
            state = json.loads(str(self._eval(bc.detect_script(frame=False)) or "{}"))
        except (BrowserError, ValueError, TypeError):
            return {"banners": [], "hint": False}
        return {"banners": [{**b, "session": None} for b in state.get("banners", [])], "hint": bool(state.get("hint"))}

    def _click_consent(self, pick: "bc.Pick") -> bool:
        """The click on a control the detection found: at the middle of it, with the mouse, only if nothing else lies over it."""
        try:
            point = self._eval(bc.CLICK_POINT_JS.replace("%I%", str(int(pick.button["i"]))))
        except BrowserError:
            return False
        if not point:
            return False
        self.cli.run(["batch", "--omit-command"], stdin=json.dumps([["mouse", "move", str(point[0]), str(point[1])], ["mouse", "down"], ["mouse", "up"]]))
        self.sleep(0.3)
        return True

    def _consent_pass(self) -> None:
        policy = self._consent_policy()
        if policy == "off" or self._consenting:
            return
        self._consenting = True
        try:
            site = str(self._eval("location.protocol + '//' + location.host") or "")
            if not site.startswith(("http://", "https://")):
                return
            result = bc.run_consent(policy, self._detect_consent, self._click_consent, bc.KNOWN_SITE_SECONDS if site in self._consent_sites else bc.LATE_SECONDS, sleep=self.sleep)
        except BrowserError:
            return
        finally:
            self._consenting = False
        if result.clicks:
            self._consent_sites.add(site)
        for note in bc.consent_notes(result, policy):
            self._say(note)
        for click in result.clicks:
            self._record("browser_consent_click", {"site": site, "label": click["label"], "role": click["role"], "banner": click["banner"], "policy": policy})

    def _after_actions(self, dialog: Dict[str, Any]) -> None:
        """A confirm or prompt dialog waits for an answer: by the same rule as Browser (allowed at Auto, by a rule, or the person's answer); a download that landed is kept or removed the same way."""
        if dialog.get("hasDialog"):
            kind = str(dialog.get("type") or "confirm")
            message = str(dialog.get("message") or "")
            if kind in ("alert", "beforeunload"):
                self.cli.run(["dialog", "accept"])
                self._say(f"{kind} dialog accepted")
            else:
                allowed = self._dialog_ok(kind, message, False)
                self.cli.run(["dialog", "accept" if allowed else "dismiss"])
                self._say(f'{kind} dialog "{bc.safe_label(message, 80)}": ' + ("accepted" if allowed else "dismissed"))
            self._record("browser_dialog", {"site": self.page_url[:100], "kind": kind})
        folder = Path(self.cli.downloads) if self.cli.downloads else None
        if folder is not None and folder.is_dir():
            for f in sorted(folder.iterdir()):
                if f.name in self._downloads_seen or f.suffix in (".crdownload", ".tmp"):
                    continue
                self._downloads_seen.add(f.name)
                kept = bool(self._download_ok(self.page_url, f.name))
                if not kept:
                    f.unlink(missing_ok=True)
                self._say(f'The page started a download of "{f.name}": ' + (f"it was kept (in {folder})." if kept else "it was not allowed, so the file was removed."))
                self._record("browser_download", {"site": self._origin(), "file": f.name[:100], "outcome": "kept" if kept else "refused"})


def session_name() -> str:
    return "tadamoo-" + uuid.uuid4().hex[:10]
