"""The time at the left of each message of the Chat: where it comes from and how it is drawn."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
CHAT = (WEB / "chat.js").read_text(encoding="utf-8")
AGENT = (WEB / "agentchat.js").read_text(encoding="utf-8")
CSS = (WEB / "app.css").read_text(encoding="utf-8")


class MessageTimesTest(unittest.TestCase):
    def test_the_hour_is_bold_and_the_date_plain_in_a_small_type(self):
        stamp = re.search(r"function stamp\(seconds\) \{(.*?)\n  \}", CHAT, re.S).group(1)
        self.assertIn('document.createElement("b")', stamp)                                    # the hour
        self.assertIn('{ day: "2-digit", month: "2-digit", year: "numeric" }', stamp)         # the date
        self.assertIn("timeZoneName", stamp)                                                   # the zone is in the tooltip
        self.assertNotIn("innerHTML", stamp)
        rule = re.search(r"\n\.cts \{([^}]*)\}", CSS).group(1)
        self.assertRegex(rule, r"font: 400 11px")
        self.assertRegex(CSS, r"\.cts b \{[^}]*font-weight: 700")

    def test_the_agent_chat_stamps_the_person_every_block_of_the_answer_and_every_note(self):
        self.assertEqual(AGENT.count("window.chatUi.stamp("), 3)                               # a user bubble, a block of the answer, a note
        self.assertIn("window.chatUi.stamp(it.ts), bubble", AGENT)
        self.assertIn("window.chatUi.stamp(b.ts), b.el", AGENT)
        self.assertIn("window.chatUi.stamp(it.ts), body", AGENT)

    def test_the_time_is_the_events_or_the_saved_messages_and_otherwise_now(self):
        self.assertIn('stampAt = typeof e.t === "number" ? e.t : undefined', AGENT)           # a live event carries its time
        self.assertIn('stampAt = typeof m.ts === "number" ? m.ts : null', AGENT)              # a saved message too; an old one has none
        self.assertIn("stampAt === undefined ? Date.now() / 1000 : stampAt", AGENT)
        self.assertEqual(len(re.findall(r"ts: stampOf\(\)", AGENT)), 4)                        # user, note, text block, other block

    def test_the_model_only_chat_has_it_too_and_shares_the_function(self):
        self.assertIn("row.append(stamp(m.time / 1000), b)", CHAT)
        self.assertIn("row.append(stamp(m.time / 1000), body)", CHAT)
        self.assertIn("watchThink, paintThink, scrollDown, autosize, stamp,", CHAT)


if __name__ == "__main__":
    unittest.main()
