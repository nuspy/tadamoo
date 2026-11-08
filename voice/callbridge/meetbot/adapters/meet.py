"""Adapter Google Meet (web).

Selettori centralizzati in costanti: Meet cambia spesso il markup ma le
aria-label restano piu' stabili. Coperte le label IT e EN.
"""
from __future__ import annotations

import logging
import re

from .base import JoinResult, MeetingAdapter, SpeakerInfo

log = logging.getLogger("callbridge.meetbot.meet")


class MeetAdapter(MeetingAdapter):
    platform = "meet"

    # --- selettori (IT/EN) --------------------------------------------------
    SEL_NAME_INPUT = 'input[placeholder*="nome" i], input[placeholder*="name" i]'
    SEL_MIC_OFF = ('[aria-label*="Disattiva il microfono" i], '
                   '[aria-label*="Turn off microphone" i], '
                   '[aria-label*="microphone" i][data-is-muted="false"]')
    SEL_CAM_OFF = ('[aria-label*="Disattiva la videocamera" i], '
                   '[aria-label*="Turn off camera" i], '
                   '[aria-label*="camera" i][data-is-muted="false"]')
    SEL_JOIN_BTN = ('button:has-text("Partecipa"), button:has-text("Chiedi di partecipare"), '
                    'button:has-text("Join now"), button:has-text("Ask to join")')
    SEL_LEAVE_BTN = ('[aria-label*="Abbandona la chiamata" i], '
                     '[aria-label*="Leave call" i]')
    SEL_WAITING = (':text("Chiedi di partecipare"), :text("in attesa"), '
                   ':text("Asking to join"), :text("waiting")')
    # Riquadri partecipanti: il bordo "attivo" segue chi parla.
    SEL_PARTICIPANT = "[data-participant-id]"

    @staticmethod
    def matches(url: str) -> bool:
        return bool(re.search(r"meet\.google\.com/", url))

    def join(self, page, display_name: str) -> JoinResult:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            # Nome (solo se ospite senza account)
            try:
                name_box = page.locator(self.SEL_NAME_INPUT).first
                if name_box.is_visible(timeout=5000):
                    name_box.fill(display_name)
            except Exception:  # noqa: BLE001 - loggato in account: niente campo
                pass
            # Cam/mic off (best-effort: a volte gia' off da permessi negati)
            for sel in (self.SEL_MIC_OFF, self.SEL_CAM_OFF):
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=2000):
                        btn.click()
                except Exception:  # noqa: BLE001
                    pass
            page.locator(self.SEL_JOIN_BTN).first.click(timeout=15000)
            # Esito: dentro subito o in sala d'attesa
            try:
                page.locator(self.SEL_LEAVE_BTN).first.wait_for(
                    state="visible", timeout=20000)
                return JoinResult(True, "in_call")
            except Exception:  # noqa: BLE001
                return JoinResult(True, "waiting_admit",
                                  "in attesa che un organizzatore ammetta il bot")
        except Exception as e:  # noqa: BLE001
            log.error("Meet join fallito: %s", e)
            return JoinResult(False, "error", str(e))

    def in_call(self, page) -> bool:
        try:
            return page.locator(self.SEL_LEAVE_BTN).first.is_visible(timeout=1500)
        except Exception:  # noqa: BLE001
            return False

    def speakers(self, page) -> SpeakerInfo:
        """Partecipanti dai tile; parlante attivo dal tile con il bordo attivo
        (classe instabile: best-effort, fallback None)."""
        info = SpeakerInfo()
        try:
            tiles = page.locator(self.SEL_PARTICIPANT)
            n = min(tiles.count(), 30)
            for i in range(n):
                t = tiles.nth(i)
                name = (t.get_attribute("aria-label") or "").strip()
                if name:
                    name = name.split(",")[0].strip()
                    if name and name not in info.participants:
                        info.participants.append(name)
                cls = t.get_attribute("class") or ""
                if info.active is None and ("speaking" in cls or "active" in cls):
                    info.active = name or None
        except Exception:  # noqa: BLE001 - DOM cambiato: pazienza
            pass
        return info

    def leave(self, page) -> None:
        try:
            page.locator(self.SEL_LEAVE_BTN).first.click(timeout=5000)
        except Exception:  # noqa: BLE001
            pass
