"""Meeting-bot locale (modello Recall.ai) — vedi MeetsAPI.md.

v1: backend Windows-loopback (UNA riunione alla volta, browser Playwright
visibile sul desktop, audio catturato dal loopback come il copilota).
Adapter: Google Meet (Teams/Zoom a seguire). WSL/PulseAudio multi-bot e
Telegram sono estensioni future (vedi audio.PulseAudioBot, stub).
"""
from .bot import MeetBotSession  # noqa: F401
