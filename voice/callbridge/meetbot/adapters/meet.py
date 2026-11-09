"""Google Meet (web), joining as a guest. Selectors are in class constants, English and Italian labels; not proven against a live meeting."""
from __future__ import annotations

import logging
import re

from .base import JoinResult, MeetingAdapter, SpeakerInfo

log = logging.getLogger("callbridge.meetbot.meet")


class MeetAdapter(MeetingAdapter):
    platform = "meet"

    SEL_NAME_INPUT = 'input[placeholder*="nome" i], input[placeholder*="name" i]'
    SEL_MIC_OFF = ('[aria-label*="Disattiva il microfono" i], [aria-label*="Turn off microphone" i], '
                   '[aria-label*="microphone" i][data-is-muted="false"]')
    SEL_CAM_OFF = ('[aria-label*="Disattiva la videocamera" i], [aria-label*="Turn off camera" i], '
                   '[aria-label*="camera" i][data-is-muted="false"]')
    SEL_JOIN_BTN = ('button:has-text("Partecipa"), button:has-text("Chiedi di partecipare"), '
                    'button:has-text("Join now"), button:has-text("Ask to join")')
    SEL_LEAVE_BTN = '[aria-label*="Abbandona la chiamata" i], [aria-label*="Leave call" i]'
    SEL_PARTICIPANT = "[data-participant-id]"
    SEL_CHAT_BTN = '[aria-label*="Chat with everyone" i], [aria-label*="Chatta con tutti" i]'
    SEL_CHAT_INPUT = 'textarea[aria-label*="Send a message" i], textarea[aria-label*="Invia un messaggio" i]'
    SEL_CHAT_SEND = 'button[aria-label*="Send a message" i], button[aria-label*="Invia un messaggio" i]'
    SEL_CHAT_MSG = "[data-message-id]"
    REMOVED_TEXTS = ("you've been removed", "you have been removed", "sei stato rimosso", "è stato rimosso dalla riunione")
    ENDED_TEXTS = ("the meeting has ended", "you left the meeting", "la riunione è terminata", "hai abbandonato la riunione", "return to home screen")

    @staticmethod
    def matches(url: str) -> bool:
        return bool(re.search(r"meet\.google\.com/", url))

    def join(self, page, display_name: str, mic: bool = False, cam: bool = False) -> JoinResult:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            try:
                name_box = page.locator(self.SEL_NAME_INPUT).first
                if name_box.is_visible(timeout=5000):
                    name_box.fill(display_name)
            except Exception:  # noqa: BLE001 (signed in: no name field)
                pass
            for wanted, sel in ((mic, self.SEL_MIC_OFF), (cam, self.SEL_CAM_OFF)):
                if wanted:
                    continue
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=2000):
                        btn.click()
                except Exception:  # noqa: BLE001 (already off)
                    pass
            page.locator(self.SEL_JOIN_BTN).first.click(timeout=15000)
            try:
                page.locator(self.SEL_LEAVE_BTN).first.wait_for(state="visible", timeout=20000)
                return JoinResult(True, "in_call")
            except Exception:  # noqa: BLE001
                return JoinResult(True, "waiting_admit", "waiting for the host to let the bot in")
        except Exception as e:  # noqa: BLE001
            log.error("Meet join failed: %s", e)
            return JoinResult(False, "error", str(e))

    def in_call(self, page) -> bool:
        try:
            return page.locator(self.SEL_LEAVE_BTN).first.is_visible(timeout=1500)
        except Exception:  # noqa: BLE001
            return False

    def speakers(self, page) -> SpeakerInfo:
        """The participants from their tiles; the speaker from the tile with the active border (an unstable class: best effort)."""
        info = SpeakerInfo()
        try:
            tiles = page.locator(self.SEL_PARTICIPANT)
            for i in range(min(tiles.count(), 30)):
                tile = tiles.nth(i)
                name = (tile.get_attribute("aria-label") or "").split(",")[0].strip()
                if name and name not in info.participants:
                    info.participants.append(name)
                cls = tile.get_attribute("class") or ""
                if info.active is None and ("speaking" in cls or "active" in cls):
                    info.active = name or None
        except Exception:  # noqa: BLE001
            pass
        return info

    def leave(self, page) -> None:
        try:
            page.locator(self.SEL_LEAVE_BTN).first.click(timeout=5000)
        except Exception:  # noqa: BLE001
            pass
