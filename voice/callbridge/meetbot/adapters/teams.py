"""Adapter Microsoft Teams (client web, join come ospite).

Flusso: il link meetup-join apre una pagina che propone app/web -> si forza
il client web, si inserisce il nome ospite, cam/mic off, "Partecipa ora".
Selettori centralizzati (IT/EN); il markup Teams cambia spesso: in caso di
rottura partire da qui.
"""
from __future__ import annotations

import logging
import re

from .base import JoinResult, MeetingAdapter, SpeakerInfo

log = logging.getLogger("callbridge.meetbot.teams")


class TeamsAdapter(MeetingAdapter):
    platform = "teams"

    SEL_USE_WEB = ('button:has-text("Continua in questo browser"), '
                   'button:has-text("Continue on this browser"), '
                   'a:has-text("Usa l\'app Web"), a:has-text("Use the web app")')
    SEL_NAME_INPUT = ('input[data-tid="prejoin-display-name-input"], '
                      'input[placeholder*="nome" i], input[placeholder*="name" i]')
    SEL_MIC_TOGGLE = ('[data-tid="toggle-mute"], '
                      '[aria-label*="microfono" i], [aria-label*="microphone" i]')
    SEL_CAM_TOGGLE = ('[data-tid="toggle-video"], '
                      '[aria-label*="videocamera" i], [aria-label*="camera" i]')
    SEL_JOIN_BTN = ('button[data-tid="prejoin-join-button"], '
                    'button:has-text("Partecipa ora"), button:has-text("Join now")')
    SEL_LEAVE_BTN = ('button[data-tid="hangup-main-btn"], '
                     '[aria-label*="Abbandona" i], [aria-label*="Leave" i]')
    SEL_WAITING = (':text("in attesa"), :text("waiting for"), '
                   ':text("Ti faremo entrare"), :text("someone will let you in")')
    SEL_ROSTER = '[data-tid="participantsInCall"] [data-tid*="participant"]'

    @staticmethod
    def matches(url: str) -> bool:
        return bool(re.search(r"teams\.(microsoft|live)\.com/", url))

    def join(self, page, display_name: str) -> JoinResult:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            try:
                btn = page.locator(self.SEL_USE_WEB).first
                if btn.is_visible(timeout=8000):
                    btn.click()
            except Exception:  # noqa: BLE001 - gia' nel web client
                pass
            try:
                name_box = page.locator(self.SEL_NAME_INPUT).first
                name_box.wait_for(state="visible", timeout=20000)
                name_box.fill(display_name)
            except Exception:  # noqa: BLE001 - loggato con account
                pass
            for sel in (self.SEL_MIC_TOGGLE, self.SEL_CAM_TOGGLE):
                try:
                    t = page.locator(sel).first
                    if t.is_visible(timeout=2000) and \
                            (t.get_attribute("aria-pressed") or "true") == "true":
                        t.click()
                except Exception:  # noqa: BLE001
                    pass
            page.locator(self.SEL_JOIN_BTN).first.click(timeout=15000)
            try:
                page.locator(self.SEL_LEAVE_BTN).first.wait_for(
                    state="visible", timeout=20000)
                return JoinResult(True, "in_call")
            except Exception:  # noqa: BLE001
                return JoinResult(True, "waiting_admit",
                                  "in lobby: un organizzatore deve ammettere il bot")
        except Exception as e:  # noqa: BLE001
            log.error("Teams join fallito: %s", e)
            return JoinResult(False, "error", str(e))

    def in_call(self, page) -> bool:
        try:
            return page.locator(self.SEL_LEAVE_BTN).first.is_visible(timeout=1500)
        except Exception:  # noqa: BLE001
            return False

    def speakers(self, page) -> SpeakerInfo:
        info = SpeakerInfo()
        try:
            tiles = page.locator(self.SEL_ROSTER)
            n = min(tiles.count(), 30)
            for i in range(n):
                name = (tiles.nth(i).get_attribute("aria-label") or "").strip()
                if name:
                    name = name.split(",")[0].strip()
                    if name and name not in info.participants:
                        info.participants.append(name)
        except Exception:  # noqa: BLE001
            pass
        return info

    def leave(self, page) -> None:
        try:
            page.locator(self.SEL_LEAVE_BTN).first.click(timeout=5000)
        except Exception:  # noqa: BLE001
            pass
