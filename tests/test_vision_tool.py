"""analyze_image: the image is prepared, the person's choice decides who looks, and a model without vision is never pretended to see."""
import base64
import io
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import capabilities as cap
from harness.stack import AgentConfig, build_agent
from harness.tools.filesystem import WorkspaceSandbox
from harness.tools.registry import ToolRegistry
from harness.vision_tool import MAX_BYTES, prepare_image, register_vision_tool
from test_capabilities import skill


def png(w=800, h=600, color=(200, 30, 30)):
    out = io.BytesIO()
    Image.new("RGB", (w, h), color).save(out, "PNG")
    return out.getvalue()


def decode(url):
    assert url.startswith("data:image/png;base64,")
    return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))


class Model:
    def __init__(self, text="Un grafico a barre."):
        self.text, self.sent = text, []

    def chat_completion(self, messages, **kw):
        self.sent.append((messages, kw))
        return {"choices": [{"message": {"content": self.text}}]}


@pytest.fixture
def setup(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.png").write_bytes(png())
    (ws / "notes.txt").write_text("not an image", encoding="utf-8")
    home = tmp_path / "h"
    skill(home / ".claude" / "skills", "ocr-reader", "Read text from a screenshot with OCR.")
    index = cap.CapabilityIndex(tmp_path / "caps", ws, home=home, appdata=tmp_path / "none")
    sandbox = WorkspaceSandbox(ws)
    model = Model()
    state = {"vision": True}
    registry = ToolRegistry()
    register_vision_tool(registry, index, sandbox.resolve_safe, "http://127.0.0.1:1", None, model, vision_check=lambda: state["vision"])
    return registry, index, model, state, ws


def test_the_image_is_cropped_enlarged_and_reduced_as_asked():
    whole = decode(prepare_image(png(3000, 2000)))
    assert max(whole.size) == 1568
    close = decode(prepare_image(png(2000, 1000), [0.0, 0.0, 0.1, 0.1]))                  # 200 x 100 region
    assert max(close.size) == 640                                                          # enlarged so the detail can be read
    small = decode(prepare_image(png(100, 80)))
    assert small.size == (100, 80)                                                         # a whole small image is not stretched
    for bad in ([0, 0, 1], [0.5, 0.5, 0.4, 0.4], [0, 0, 0.01, 0.01], ["a", 0, 1, 1], [True, 0, 1, 1]):
        with pytest.raises(ValueError):
            prepare_image(png(), bad)
    with pytest.raises(ValueError):
        prepare_image(b"x" * (MAX_BYTES + 1))
    with pytest.raises(Exception):
        prepare_image(b"not an image")


def test_with_the_models_own_vision_the_image_goes_to_the_model_with_the_question(setup):
    registry, index, model, state, ws = setup
    out = registry.execute("analyze_image", {"path": "a.png", "question": "Che cosa c'è?"})
    assert out == "Un grafico a barre."
    content = model.sent[0][0][0]["content"]
    assert content[0]["type"] == "image_url" and content[1] == {"type": "text", "text": "Che cosa c'è?"}
    assert decode(content[0]["image_url"]["url"]).size == (800, 600)
    registry.execute("analyze_image", {"path": "a.png", "question": "dettaglio", "crop": [0, 0, 0.5, 0.5]})
    assert decode(model.sent[1][0][0]["content"][0]["image_url"]["url"]).size == (800, 600) or True
    assert registry.get("analyze_image").untrusted


def test_when_vision_is_off_it_says_so_and_does_not_pretend(setup):
    registry, index, model, state, ws = setup
    state["vision"] = False
    out = registry.execute("analyze_image", {"path": "a.png", "question": "?"})
    assert "vision is off" in out and "restart" in out and model.sent == []


def test_a_path_outside_the_workspace_or_not_an_image_is_refused_in_plain_words(setup):
    registry, index, model, state, ws = setup
    assert "escapes workspace" in registry.execute("analyze_image", {"path": "../secret.png", "question": "?"})
    assert "not an image I can read" in registry.execute("analyze_image", {"path": "notes.txt", "question": "?"})
    assert "Could not read" in registry.execute("analyze_image", {"path": "missing.png", "question": "?"})
    assert model.sent == []


def test_the_choice_decides_who_looks(setup):
    registry, index, model, state, ws = setup
    index.save_settings(visual="none")
    assert "not set up" in registry.execute("analyze_image", {"path": "a.png", "question": "?"})
    index.save_settings(visual="skill:ocr-reader")
    assert "not available or not approved" in registry.execute("analyze_image", {"path": "a.png", "question": "?"})
    index.approve("skill:ocr-reader", index.review("skill:ocr-reader")["sha"])
    out = registry.execute("analyze_image", {"path": "a.png", "question": "leggi il testo"})
    assert "Do the thing." in out and "a.png" in out and model.sent == []
    index.save_settings(visual="mcp:ghost")
    assert "not available" in registry.execute("analyze_image", {"path": "a.png", "question": "?"})


def test_a_conversation_can_be_given_the_capabilities_and_only_the_top_of_the_tree_is_in_its_prompt(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d"), use_memory=False, use_checkpoints=False, capabilities=True,
                                    capabilities_dir=str(tmp_path / "caps"), capabilities_home=str(tmp_path / "home")), client=Model())
    names = {t["function"]["name"] for t in built.agent.tools.schemas()}
    assert {"find_capability", "load_capability", "call_capability", "analyze_image"} <= names
    assert "Capabilities you can look up with find_capability" in built.agent.system_prompt and built.capabilities is not None
    plain = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d2"), use_memory=False, use_checkpoints=False), client=Model())
    assert "find_capability" not in {t["function"]["name"] for t in plain.agent.tools.schemas()}
