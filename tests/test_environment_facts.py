"""The facts the model is given about its environment, the text returned for a file handed to the person, the files guidance and the
messages of the file tools when a path is missing or not allowed."""
import sys
from pathlib import Path

import pytest

from tadamoo import environment
from tadamoo.attachment_tools import register_attachment_tools
from tadamoo.attachments import AttachmentStore, OutputStore
from tadamoo.stack import AgentConfig, build_agent
from tadamoo.tools import create_standard_tool_registry
from tadamoo.tools.filesystem import WorkspaceSandbox
from tadamoo.tools.registry import ToolRegistry
from tadamoo.visuals import FILES_GUIDANCE


class Model:
    def chat_completion(self, messages, **kw):
        return {"choices": [{"message": {"content": "ok"}}]}


@pytest.fixture(autouse=True)
def fresh_probes():
    environment.forget_probes()
    yield
    environment.forget_probes()


# -- the Environment paragraph --------------------------------------------------------------------------------------------------

def test_the_paragraph_states_the_system_the_shell_python_and_the_working_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(environment, "working_pythons", lambda: {"python": ("C:/py/python.exe", "Python 3.12.1")})
    monkeypatch.setattr(environment, "is_windows", lambda: True)
    monkeypatch.setattr(environment, "os_description", lambda: "Windows 11 (version 10.0.26200)")
    monkeypatch.setattr(environment, "powershell_path", lambda: ("PowerShell 7 (pwsh)", "C:/pwsh.exe"))
    text = environment.environment_paragraph(tmp_path, output_files=True)
    assert "The system is Windows 11 (version 10.0.26200)." in text
    assert 'cmd.exe by default; with shell "powershell" it uses PowerShell 7 (pwsh).' in text
    assert "`python` (Python 3.12.1)" in text and "`python3`, `py` do not work" in text
    assert f"The working folder is {tmp_path.resolve()}; relative paths" in text
    assert "write_output_file is handed to the person and is not in the working folder" in text
    assert "write_file in the working folder can be read, counted and edited, and publish_output_file hands it to the person" in text
    assert "\n" not in text


def test_the_paragraph_leaves_out_the_output_file_sentence_when_there_is_no_such_tool(tmp_path):
    assert "write_output_file" not in environment.environment_paragraph(tmp_path, output_files=False)


def test_on_a_system_with_no_python_it_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(environment, "working_pythons", lambda: {})
    assert "No Python command (python, python3, py) runs on this machine." in environment.environment_paragraph(tmp_path)


def test_the_probe_calls_a_store_stub_that_prints_nothing_not_working(monkeypatch):
    monkeypatch.setattr(environment.shutil, "which", lambda name: f"C:/bin/{name}.exe" if name != "py" else None)

    class Done:
        def __init__(self, out, err, code):
            self.stdout, self.stderr, self.returncode = out, err, code

    def run(argv, **kw):
        return Done(b"Python 3.11.4\r\n", b"", 0) if argv[0].endswith("python.exe") else Done(b"", b"", 0)
    monkeypatch.setattr(environment.subprocess, "run", run)
    assert list(environment.working_pythons()) == ["python"]
    assert environment.pick_python("python3") == ("python", "C:/bin/python.exe")


def test_the_probe_runs_once(monkeypatch):
    calls = []
    monkeypatch.setattr(environment.shutil, "which", lambda name: f"C:/bin/{name}.exe")
    monkeypatch.setattr(environment, "_probe", lambda path: calls.append(path) or "Python 3.12.0")
    environment.working_pythons()
    environment.working_pythons()
    assert len(calls) == 3


def test_the_built_agent_has_the_environment_block_after_the_core(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    plain = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d"), use_memory=False, use_checkpoints=False), client=Model())
    prompt = plain.agent.system_prompt
    assert "# Environment\nThe system is " in prompt and f"The working folder is {ws.resolve()}" in prompt
    assert prompt.index("# Environment") > prompt.index("You are Basalt")
    assert "write_output_file" not in prompt
    with_files = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d2"), use_memory=False, use_checkpoints=False,
                                         attachments_dir=str(tmp_path / "att"), outputs_dir=str(tmp_path / "out"), run_id="s1"), client=Model())
    assert "A file made with write_output_file is handed to the person and is not in the working folder" in with_files.agent.system_prompt


# -- files for the person -------------------------------------------------------------------------------------------------------

def test_the_files_guidance_no_longer_tells_the_model_to_keep_text_off_the_screen():
    assert "paste a" not in FILES_GUIDANCE and "when a button will do" not in FILES_GUIDANCE
    assert "Offer a file when the person asks for a file or would want to keep one" in FILES_GUIDANCE
    assert "when the person asks to see a text on screen, the text goes in the answer" in FILES_GUIDANCE
    assert "basalt-file:ID" in FILES_GUIDANCE and "basalt-path:" in FILES_GUIDANCE


@pytest.fixture
def tools(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    registry = ToolRegistry()
    sandbox = WorkspaceSandbox(ws)
    register_attachment_tools(registry, AttachmentStore(tmp_path / "att"), OutputStore(tmp_path / "out"), "s1", sandbox.resolve_safe)
    return registry, ws


def test_a_handed_over_text_file_states_its_size_words_and_lines_and_where_it_is(tools):
    registry, ws = tools
    content = "Once upon a time\nthere was a story\n\nthe end"
    out = registry.execute("write_output_file", {"name": "story.txt", "content": content})
    size = len(content.encode("utf-8"))
    assert size == 43
    assert out.startswith("[file:") and out.split("]")[0].endswith(":story.txt")
    assert f"({size} bytes, opens correctly, 10 words, 4 lines)" in out
    assert "outside your working folder" in out and "Do not describe it as saved elsewhere." in out
    (ws / "notes.md").write_text("alpha beta\ngamma\n", encoding="utf-8")
    pub = registry.execute("publish_output_file", {"path": "notes.md"})
    assert "opens correctly, 3 words, 2 lines)" in pub and "outside your working folder" in pub and pub.startswith("[file:")


def test_a_binary_file_gets_its_size_only(tools):
    registry, ws = tools
    (ws / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    out = registry.execute("publish_output_file", {"path": "a.png"})
    assert "108 bytes" in out or "did not pass" in out
    assert "words" not in out


def test_the_file_marker_is_still_what_other_code_parses(tools):
    registry, _ = tools
    out = registry.execute("write_output_file", {"name": "a b.txt", "content": "x"})
    import re
    match = re.match(r"\[file:([a-f0-9]{12}):([^\]]+)\]", out)
    assert match and match.group(2).endswith(".txt")


# -- messages of the file tools -------------------------------------------------------------------------------------------------

@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "work"
    (root / "docs").mkdir(parents=True)
    (root / "story.txt").write_text("x", encoding="utf-8")
    (root / "docs" / "report.md").write_text("x", encoding="utf-8")
    return WorkspaceSandbox(root)


def test_read_file_of_a_missing_file_names_the_working_folder_and_similar_names(ws):
    with pytest.raises(FileNotFoundError) as err:
        ws.read_file("storie.txt")
    text = str(err.value)
    assert text.startswith("File not found: storie.txt.") and str(ws.root) in text and "relative paths are relative to it" in text
    assert "Similar names: story.txt." in text


def test_a_file_that_exists_in_a_subfolder_is_found_by_its_name(ws):
    with pytest.raises(FileNotFoundError) as err:
        ws.read_file("report.md")
    assert "docs/report.md" in str(err.value)


def test_a_missing_folder_in_the_path_is_named(ws):
    with pytest.raises(FileNotFoundError) as err:
        ws.read_file("doc/report.md")
    text = str(err.value)
    assert "The folder 'doc' does not exist." in text and "Similar names: docs/." in text


def test_nothing_similar_is_said_plainly(ws):
    with pytest.raises(FileNotFoundError) as err:
        ws.read_file("zzzzzz.bin")
    assert "No similar name." in str(err.value)


def test_edit_file_of_a_missing_file_says_the_same(ws):
    with pytest.raises(FileNotFoundError) as err:
        ws.edit_file("storey.txt", "x", "b")
    assert str(ws.root) in str(err.value) and "story.txt" in str(err.value)


def test_a_folder_given_for_a_file_is_said_to_be_a_folder(ws):
    with pytest.raises(FileNotFoundError) as err:
        ws.read_file("docs")
    assert "exists but is a folder, not a file" in str(err.value)
    with pytest.raises(IsADirectoryError) as err:
        ws.write_file("docs", "x")
    assert "is a folder, not a file" in str(err.value) and str(ws.root) in str(err.value)


def test_list_directory_of_a_missing_or_file_path_says_what_exists(ws):
    with pytest.raises(NotADirectoryError) as err:
        ws.list_directory("doc")
    assert str(ws.root) in str(err.value) and "Similar names: docs/." in str(err.value)
    with pytest.raises(NotADirectoryError) as err:
        ws.list_directory("story.txt")
    assert "exists but is a file, not a folder" in str(err.value)


def test_a_path_outside_the_working_folder_says_where_the_folder_is(ws):
    for call in (lambda: ws.read_file("../secret.txt"), lambda: ws.write_file("../x.txt", "a"), lambda: ws.list_directory("..")):
        with pytest.raises(PermissionError) as err:
            call()
        assert "escapes workspace boundary" in str(err.value) and str(ws.root) in str(err.value) and "inside it, relative to it" in str(err.value)


def test_the_tool_registry_hands_these_messages_to_the_model(tmp_path):
    registry, _ = create_standard_tool_registry(tmp_path)
    (tmp_path / "chapter1.txt").write_text("x", encoding="utf-8")
    with pytest.raises(FileNotFoundError) as err:
        registry.execute("read_file", {"path": "chapter_1.txt"})
    assert "chapter1.txt" in str(err.value)


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe")
def test_a_shell_command_that_cannot_find_a_file_says_where_it_ran(tmp_path):
    from tadamoo.tools.shell import execute_shell_command
    (tmp_path / "chapter1.txt").write_text("x", encoding="utf-8")
    res = execute_shell_command("type chapter_1.txt", tmp_path)
    assert not res["success"] and str(tmp_path.resolve()) in res["hint"] and "chapter1.txt" in res["hint"]


def test_a_blocked_program_message_lists_what_is_blocked():
    from tadamoo.sandbox import blocked_reason
    why = blocked_reason("echo x && reg add y")
    assert "Command 'reg' is blocked by sandbox security policy" in why and "netsh" in why
