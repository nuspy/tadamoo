"""ask_document: the file goes to the model as one file part, with its pages when asked, and only the answer comes back."""
import base64
from pathlib import Path

from tadamoo import document_ask


class Registry:
    def register(self, name, description, params, effect, fn, **kw):
        self.tool = fn
        self.name = name


class Client:
    def __init__(self, answer="ok", fail=None):
        self.answer, self.fail, self.sent = answer, fail, None

    def chat_completion(self, messages, **kw):
        self.sent = messages
        if self.fail:
            raise self.fail
        return {"choices": [{"message": {"content": self.answer}}]}


def tool(tmp_path, client, name="paper.pdf", data=b"%PDF-1.4 x"):
    (tmp_path / name).write_bytes(data)
    reg = Registry()
    document_ask.register_document_ask(reg, lambda p: tmp_path / Path(p).name, client)
    return reg.tool


def test_the_file_is_one_part_of_the_message_and_the_pages_go_with_it(tmp_path):
    client = Client("The price is 1,200.")
    ask = tool(tmp_path, client)
    assert ask("paper.pdf", "What is the price?", pages="2-3") == "The price is 1,200."
    parts = client.sent[0]["content"]
    spec = parts[0]["file"]
    assert parts[0]["type"] == "file" and spec["filename"] == "paper.pdf" and spec["pages"] == "2-3"
    assert spec["file_data"].startswith("data:application/pdf;base64,") and base64.b64decode(spec["file_data"].split(",", 1)[1]) == b"%PDF-1.4 x"
    assert parts[1] == {"type": "text", "text": "What is the price?"}
    ask("paper.pdf", "again")
    assert "pages" not in client.sent[0]["content"][0]["file"]


def test_a_word_file_has_its_own_media_type_and_a_big_one_is_refused(tmp_path, monkeypatch):
    client = Client()
    ask = tool(tmp_path, client, "memo.docx", b"PK")
    ask("memo.docx", "q")
    assert client.sent[0]["content"][0]["file"]["file_data"].startswith("data:application/vnd.openxmlformats-officedocument.wordprocessingml.document;base64,")
    monkeypatch.setattr(document_ask, "MAX_BYTES", 1)
    assert "the most this reads" in ask("memo.docx", "q")


def test_a_refusal_of_the_server_says_what_to_do(tmp_path):
    ask = tool(tmp_path, Client(fail=RuntimeError("document exceeds the context")))
    text = ask("paper.pdf", "q")
    assert "could not read the document" in text and "pages" in text and "read_document" in text
    assert "Could not open" in ask("missing.pdf", "q") or "could not read" in ask("missing.pdf", "q").lower()
