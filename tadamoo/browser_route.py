"""The browser tools when agent-browser drives the pages but the person chose that the vault's steps (a value from the vault typed, a sign-in, a card in a payment frame) belong to TaDaMoo's own driver.

The person's setting, "for the vault, sign-ins and payments": `browser` (the same driver as the rest) or `tadamoo` (the default: the own driver, until a payment provider's test mode has been tried for real).
With `tadamoo` the model uses the ordinary `browser_*` tools; the first step that would type a value from the vault is not done by agent-browser. The page is **moved** to the own driver instead:
the address and the cookies are carried over, agent-browser's browser is closed (a kept profile cannot be open in two browsers), the own driver opens the address, and the step is refused with the page as
the own driver reads it, so the model asks again with its element numbers. From then on the own driver does everything in the conversation. What is not carried over: what was typed or chosen on the page,
the page's own scripts' state, local storage, and any window the page had opened. docs/browser-backends.md has the table.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from tadamoo.browser import DEFAULT_VAULT_BACKEND, Browser, BrowserError, describe


def cookie_for_cdp(c: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A cookie as agent-browser lists it, in the form Network.setCookies takes; None when it has no name or domain."""
    if not c.get("name") or not c.get("domain"):
        return None
    out: Dict[str, Any] = {"name": str(c["name"]), "value": str(c.get("value", "")), "domain": str(c["domain"]), "path": str(c.get("path") or "/"),
                           "secure": bool(c.get("secure")), "httpOnly": bool(c.get("httpOnly"))}
    expires = c.get("expires")
    if isinstance(expires, (int, float)) and expires > 0 and not c.get("session"):
        out["expires"] = float(expires)
    if c.get("sameSite") in ("Strict", "Lax", "None"):
        out["sameSite"] = c["sameSite"]
    return out


class RoutedBrowser:
    """What `register_browser_tools` takes as `browser`: agent-browser first, the own driver from the first vault step on."""

    def __init__(self, primary: Any, own: Browser, route: str = DEFAULT_VAULT_BACKEND) -> None:
        self.primary, self.own, self.route = primary, own, route
        self.active: Any = primary
        self.moved_at: Optional[str] = None             # the address the page was moved from, once it was

    # -- what the tool layer reads and sets ---------------------------------------------------------------------------------

    @property
    def mask(self) -> Callable[[str], str]:
        return self.active.mask

    @mask.setter
    def mask(self, value: Callable[[str], str]) -> None:
        self.primary.mask = self.own.mask = value

    @property
    def hidden(self) -> set:
        return self.active.hidden

    @property
    def notice(self) -> str:
        return self.active.notice

    @notice.setter
    def notice(self, value: str) -> None:
        self.active.notice = value

    @property
    def page_url(self) -> str:
        return self.active.page_url

    def host_for(self, ref: int) -> str:
        return self.active.host_for(ref)

    def insecure_public(self, host: str) -> bool:
        return self.active.insecure_public(host)

    def insecure_ok(self, host: str) -> bool:
        return self.active.insecure_ok(host)

    # -- the tools ------------------------------------------------------------------------------------------------------------

    def open(self, url: str) -> Dict[str, Any]:
        return self.active.open(url)

    def snapshot(self) -> Dict[str, Any]:
        return self.active.snapshot()

    def click(self, ref: int) -> Dict[str, Any]:
        return self.active.click(ref)

    def select(self, ref: int, value: str) -> Dict[str, Any]:
        return self.active.select(ref, value)

    def press(self, name: str) -> Dict[str, Any]:
        return self.active.press(name)

    def back(self) -> Dict[str, Any]:
        return self.active.back()

    def screenshot(self, full_page: bool = False) -> bytes:
        return self.active.screenshot(full_page)

    def type(self, ref: int, text: str, submit: bool = False, clear: bool = True, secret: bool = False) -> Dict[str, Any]:
        if secret and self.active is self.primary and self.route == "tadamoo":
            raise BrowserError(self._move())                      # nothing was typed: the value is not given to agent-browser
        return self.active.type(ref, text, submit, clear, secret)

    def close(self) -> None:
        for browser in (self.primary, self.own):
            try:
                browser.close()
            except Exception:
                pass

    # -- the move ----------------------------------------------------------------------------------------------------------------

    def _move(self) -> str:
        """Carries the page to the own driver and says so, with the page as that driver reads it. The address is taken before anything is closed."""
        url = self.primary.page_url
        cookies: List[Dict[str, Any]] = []
        try:
            cookies = [c for c in (cookie_for_cdp(c) for c in (self.primary.cli.run(["cookies", "get"]).get("cookies") or [])) if c]
        except BrowserError:
            pass                                                   # a page without its cookies is still moved: it may ask to sign in again
        self.own.hidden.update(self.primary.hidden)
        self.primary.close()                                       # a kept profile cannot be open in two browsers
        self.own.start()
        if cookies:
            self.own.cdp.call("Network.setCookies", {"cookies": cookies})
        self.active, self.moved_at = self.own, url
        page = self.own.open(url) if url.startswith(("http://", "https://")) else self.own.snapshot()
        shown = self.own.mask(describe(page, self.own.hidden))
        return ("Nothing was typed. This step uses the vault, and the person chose that the vault's steps are done by TaDaMoo's own browser, so the page was moved there (its address and its cookies "
                "were kept; what was typed or chosen on it was not). The elements have new numbers: here is the page; ask again with them. The rest of this conversation uses that browser.\n" + shown)
