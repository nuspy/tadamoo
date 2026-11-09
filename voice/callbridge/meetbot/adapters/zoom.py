"""Zoom (web client), joining as a guest. Selectors are in class constants, English and Italian labels; not proven against a live meeting."""
from __future__ import annotations

import logging
import re

from .base import JoinResult, MeetingAdapter, SpeakerInfo

log = logging.getLogger("callbridge.meetbot.zoom")


class ZoomAdapter(MeetingAdapter):
    platform = "zoom"

    SEL_NAME_INPUT = '#input-for-name, input[placeholder*="Name" i], input[placeholder*="nome" i]'
    SEL_JOIN_BTN = 'button:has-text("Join"), button:has-text("Entra"), button:has-text("Partecipa")'
    SEL_LEAVE_BTN = 'button:has-text("Leave"), [aria-label*="Leave" i], button:has-text("Esci"), [aria-label*="Esci" i]'
    SEL_LEAVE_CONFIRM = 'button:has-text("Leave Meeting"), button:has-text("Esci dalla riunione")'
    SEL_PARTICIPANT = '.participants-item__display-name, [class*="participants-item"]'
    SEL_CHAT_BTN = '[aria-label*="chat" i]'
    SEL_CHAT_INPUT = '.chat-box__chat-textarea, textarea[placeholder*="message" i], textarea[placeholder*="messaggio" i]'
    SEL_CHAT_MSG = ".chat-message__container"
    REMOVED_TEXTS = ("you have been removed", "you were removed", "sei stato rimosso", "rimosso dalla riunione")
    ENDED_TEXTS = ("this meeting has been ended", "the host has ended", "la riunione è stata terminata", "l'host ha terminato")

    @staticmethod
    def matches(url: str) -> bool:
        return bool(re.search(r"zoom\.us/(j|wc|s)/", url))

    @staticmethod
    def web_client(url: str) -> str:
        """The address of the browser client for a /j/<id> link (the launch page is skipped)."""
        m = re.search(r"zoom\.us/j/(\d+)(?:\?pwd=([\w.]+))?", url)
        if not m:
            return url
        return f"https://app.zoom.us/wc/{m.group(1)}/join" + (f"?pwd={m.group(2)}" if m.group(2) else "")

    def join(self, page, display_name: str, mic: bool = False, cam: bool = False) -> JoinResult:
        try:
            page.goto(self.web_client(page.url), timeout=60000)
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            try:
                name_box = page.locator(self.SEL_NAME_INPUT).first
                name_box.wait_for(state="visible", timeout=20000)
                name_box.fill(display_name)
            except Exception:  # noqa: BLE001
                pass
            page.locator(self.SEL_JOIN_BTN).first.click(timeout=15000)
            try:
                page.locator(self.SEL_LEAVE_BTN).first.wait_for(state="visible", timeout=30000)
                return JoinResult(True, "in_call")
            except Exception:  # noqa: BLE001
                return JoinResult(True, "waiting_admit", "in the waiting room: the host must let the bot in")
        except Exception as e:  # noqa: BLE001
            log.error("Zoom join failed: %s", e)
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
            for i in range(min(tiles.count(), 30)):
                name = (tiles.nth(i).inner_text(timeout=500) or "").strip()
                if name and name not in info.participants:
                    info.participants.append(name)
        except Exception:  # noqa: BLE001
            pass
        return info

    def leave(self, page) -> None:
        try:
            page.locator(self.SEL_LEAVE_BTN).first.click(timeout=5000)
            page.locator(self.SEL_LEAVE_CONFIRM).first.click(timeout=3000)
        except Exception:  # noqa: BLE001
            pass
