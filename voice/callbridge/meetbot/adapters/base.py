"""The contract of a platform adapter (Meet, Teams, Zoom).

Each adapter holds the DOM selectors and the entry flow of its platform; every operation takes the Playwright `page`. The selectors are
class constants so that a repair after a markup change stays in one place. They are written from the platforms' public pages and are not
proven against a live meeting.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class JoinResult:
    ok: bool
    state: str               # "in_call" | "waiting_admit" | "error"
    message: str | None = None


@dataclass
class SpeakerInfo:
    """A snapshot of who is in the meeting and who is speaking."""
    active: str | None = None
    participants: list[str] = field(default_factory=list)


@dataclass
class ChatMessage:
    sender: str
    text: str


class MeetingAdapter:
    platform = "generic"
    # Texts the page shows when the bot was taken out or the meeting is over (lowercase, any language the adapter knows).
    REMOVED_TEXTS: tuple[str, ...] = ()
    ENDED_TEXTS: tuple[str, ...] = ()
    # The chat: the button that opens it, the box to type in, the send button, a message and the attribute that names its sender.
    SEL_CHAT_BTN = SEL_CHAT_INPUT = SEL_CHAT_SEND = SEL_CHAT_MSG = ""
    CHAT_SENDER_ATTR = "data-sender-name"

    def __init__(self) -> None:
        self._chat_seen = 0
        self._chat_ready = False

    @staticmethod
    def matches(url: str) -> bool:
        raise NotImplementedError

    def join(self, page, display_name: str, mic: bool = False, cam: bool = False) -> JoinResult:
        """Enter as a guest under `display_name`; the microphone and the camera stay off unless asked. Returns in_call or waiting_admit."""
        raise NotImplementedError

    def in_call(self, page) -> bool:
        raise NotImplementedError

    def speakers(self, page) -> SpeakerInfo:
        """The active speaker and the participants (best effort; empty when the page does not say)."""
        return SpeakerInfo()

    def _ensure_chat(self, page) -> bool:
        if not self.SEL_CHAT_INPUT:
            return False
        if self._chat_ready:
            return True
        try:
            box = page.locator(self.SEL_CHAT_INPUT).first
            if not box.is_visible(timeout=800):
                page.locator(self.SEL_CHAT_BTN).first.click(timeout=3000)
                box.wait_for(state="visible", timeout=4000)
            self._chat_ready = True
        except Exception:  # noqa: BLE001 (the markup changed, the chat is off in this meeting)
            return False
        return True

    def send_chat(self, page, text: str) -> bool:
        """Post `text` in the meeting chat. False when it could not be done."""
        if not self._ensure_chat(page):
            return False
        try:
            box = page.locator(self.SEL_CHAT_INPUT).first
            box.fill(text)
            if self.SEL_CHAT_SEND:
                try:
                    page.locator(self.SEL_CHAT_SEND).first.click(timeout=2000)
                    return True
                except Exception:  # noqa: BLE001
                    pass
            box.press("Enter")
            return True
        except Exception:  # noqa: BLE001
            return False

    def read_chat(self, page) -> list[ChatMessage]:
        """The messages that appeared since the last call (best effort)."""
        if not self.SEL_CHAT_MSG or not self._ensure_chat(page):
            return []
        out: list[ChatMessage] = []
        try:
            items = page.locator(self.SEL_CHAT_MSG)
            count = min(items.count(), 200)
            for i in range(self._chat_seen, count):
                item = items.nth(i)
                out.append(ChatMessage((item.get_attribute(self.CHAT_SENDER_ATTR) or "").strip(), (item.inner_text(timeout=500) or "").strip()))
            self._chat_seen = max(self._chat_seen, count)
        except Exception:  # noqa: BLE001
            pass
        return out

    def exit_reason(self, page) -> str:
        """Why the bot is no longer in the call: "removed" (the host took it out), "ended" (the meeting is over) or "" (unknown)."""
        text = self._page_text(page).lower()
        if any(t in text for t in self.REMOVED_TEXTS):
            return "removed"
        if any(t in text for t in self.ENDED_TEXTS):
            return "ended"
        return ""

    @staticmethod
    def _page_text(page) -> str:
        try:
            return page.locator("body").first.inner_text(timeout=1500) or ""
        except Exception:  # noqa: BLE001
            return ""

    def leave(self, page) -> None:
        raise NotImplementedError
