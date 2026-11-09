"""The browser the bot joins with: a Chromium driven by Playwright, visible on the desktop, as a guest.

Playwright's sync API belongs to the thread that starts it, so the session opens, uses and closes the browser in one thread. The camera and
the microphone are denied unless the bot is allowed to speak. Nothing is installed here: when Playwright or its Chromium is missing, `open`
raises BrowserMissing with the plain reason."""
from __future__ import annotations

import logging

log = logging.getLogger("callbridge.meetbot.browser")

INSTALL_HINT = "install Playwright and its Chromium first (pip install playwright; python -m playwright install chromium)"


class BrowserMissing(RuntimeError):
    pass


class PlaywrightBrowser:
    def __init__(self, media: bool = False, locale: str = "en-US") -> None:
        self.media, self.locale = media, locale
        self._pw = self._browser = None

    def open(self, url: str):
        """Start the browser and load `url`; returns the page."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise BrowserMissing("Playwright is not installed: " + INSTALL_HINT) from None
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=False, args=["--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required"])
        except Exception as e:  # noqa: BLE001 (no Chromium downloaded)
            self.close()
            raise BrowserMissing(f"Chromium cannot start ({e.__class__.__name__}): " + INSTALL_HINT) from None
        context = self._browser.new_context(permissions=["microphone", "camera"] if self.media else [], locale=self.locale)
        page = context.new_page()
        page.goto(url, timeout=60000)
        return page

    def close(self) -> None:
        for closer in (getattr(self._browser, "close", None), getattr(self._pw, "stop", None)):
            if closer is not None:
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass
        self._pw = self._browser = None
