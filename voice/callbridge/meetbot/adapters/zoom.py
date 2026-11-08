"""Adapter Zoom (web client, join come ospite).

I link /j/<id> aprono la pagina di lancio: si forza il web client
(wc/join), nome ospite, mic/cam off, Join. Selettori centralizzati.
"""
from __future__ import annotations

import logging
import re

from .base import JoinResult, MeetingAdapter, SpeakerInfo

log = logging.getLogger("callbridge.meetbot.zoom")


class ZoomAdapter(MeetingAdapter):
    platform = "zoom"

    SEL_USE_WEB = ('a:has-text("Join from your browser"), '
                   'a:has-text("Partecipa dal browser"), '
                   'a[href*="/wc/"]')
    SEL_NAME_INPUT = '#input-for-name, input[placeholder*="Name" i], input[placeholder*="nome" i]'
    SEL_JOIN_BTN = ('button:has-text("Join"), button:has-text("Entra"), '
                    'button:has-text("Partecipa")')
    SEL_MIC_BTN = ('[aria-label*="mute" i], [aria-label*="microfono" i]')
    SEL_LEAVE_BTN = ('button:has-text("Leave"), [aria-label*="Leave" i], '
                     'button:has-text("Esci"), [aria-label*="Esci" i]')
    SEL_PARTICIPANT = '.participants-item__display-name, [class*="participants-item"]'

    @staticmethod
    def matches(url: str) -> bool:
        return bool(re.search(r"zoom\.us/(j|wc|s)/", url))

    def _to_web_client(self, url: str) -> str:
        m = re.search(r"zoom\.us/j/(\d+)(?:\?pwd=([\w.]+))?", url)
        if not m:
            return url
        wc = f"https://app.zoom.us/wc/{m.group(1)}/join"
        if m.group(2):
            wc += f"?pwd={m.group(2)}"
        return wc

    def join(self, page, display_name: str) -> JoinResult:
        try:
            # Vai dritto al web client (salta la pagina di lancio app).
            page.goto(self._to_web_client(page.url), timeout=60000)
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            try:
                name_box = page.locator(self.SEL_NAME_INPUT).first
                name_box.wait_for(state="visible", timeout=20000)
                name_box.fill(display_name)
            except Exception:  # noqa: BLE001
                pass
            page.locator(self.SEL_JOIN_BTN).first.click(timeout=15000)
            try:
                page.locator(self.SEL_LEAVE_BTN).first.wait_for(
                    state="visible", timeout=30000)
                return JoinResult(True, "in_call")
            except Exception:  # noqa: BLE001
                return JoinResult(True, "waiting_admit",
                                  "in sala d'attesa: l'host deve ammettere il bot")
        except Exception as e:  # noqa: BLE001
            log.error("Zoom join fallito: %s", e)
            return JoinResult(False, "error", str(e))

    def in_call(self, page) -> bool:
        try:
            return page.locator(self.SEL_LEAVE_BTN).first.is_visible(timeout=1500)
        except Exception:  # noqa: BLE001
            return False

    def speakers(self, page) -> SpeakerInfo:
        info = SpeakerInfo()
        try:
            tiles = page.locator(self.SEL_PARTICIPANT)
            n = min(tiles.count(), 30)
            for i in range(n):
                name = (tiles.nth(i).inner_text(timeout=500) or "").strip()
                if name and name not in info.participants:
                    info.participants.append(name)
        except Exception:  # noqa: BLE001
            pass
        return info

    def leave(self, page) -> None:
        try:
            page.locator(self.SEL_LEAVE_BTN).first.click(timeout=5000)
            # conferma "Leave meeting"
            page.locator('button:has-text("Leave Meeting"), '
                         'button:has-text("Esci dalla riunione")').first.click(
                timeout=3000)
        except Exception:  # noqa: BLE001
            pass
