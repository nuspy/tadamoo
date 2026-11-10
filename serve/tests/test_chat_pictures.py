"""A picture in an answer is shown, whatever way the agent named it; the way is read from the page's own code."""
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
CHAT = (WEB / "chat.js").read_text(encoding="utf-8")


def at(marker: str) -> int:
    i = CHAT.find(marker)
    assert i >= 0, marker
    return i


class ChatPicturesTest(unittest.TestCase):
    def test_a_picture_is_made_before_a_link_is_because_it_holds_one(self):
        first_link = at(r"\[([^\]\n]+)\]\(tadamoo-file:")
        for marker in (r"!\[([^\]\n]*)\]\(tadamoo-file:", r"!\[([^\]\n]*)\]\(tadamoo-path:", r"!\[([^\]\n]*)\]\((data:image"):
            self.assertLess(at(marker), first_link)
        self.assertEqual(CHAT.count(r"!\[([^\]\n]*)\]\(tadamoo-file:"), 1)

    def test_only_raster_pictures_come_inline_from_the_text_and_never_an_address(self):
        self.assertIn(r"(data:image\/(?:png|jpeg|gif|webp);base64,", CHAT)
        self.assertNotIn("svg+xml;base64", CHAT)
        self.assertIn(r'.replace(/!(\[[^\]\n]*\]\(https?:)/g, "$1")', CHAT)          # a picture at an address stays a link

    def test_a_link_to_a_picture_of_the_folder_shows_it_and_keeps_the_save_button(self):
        self.assertIn(r"\.(?:png|jpe?g|gif|webp|svg))\)/gi", CHAT)
        self.assertIn("api/agent/sessions/${encodeURIComponent(sid)}/file?inline=1&path=", CHAT)
        self.assertIn('img.aimg[data-fid], img.aimg[data-path]', CHAT)
