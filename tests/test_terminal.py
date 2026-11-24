"""The person's terminal as a message describes it: what is kept, what the agent is told, and the tool that writes one line on the person's prompt."""
from tadamoo.stack import AgentConfig, build_agent
from tadamoo.terminal import Terminal, clean

HERE = {"shell": "PowerShell", "version": "7.7.0", "os": "Windows 11", "cwd": "C:\\Projects\\demo", "venv": ""}


def test_only_short_plain_facts_are_kept_and_a_shell_with_a_folder_is_needed():
    held = clean({**HERE, "cwd": "C:\\a\nb " + "x" * 700, "secret": "key", "venv": ".venv"})
    assert set(held) == {"shell", "version", "os", "cwd", "venv"} and "\n" not in held["cwd"] and len(held["cwd"]) == 500
    for bad in (None, "pwsh", {}, {"shell": "PowerShell"}, {"cwd": "C:\\x"}, {"shell": 7, "cwd": "C:\\x"}):
        assert clean(bad) is None


def test_the_note_comes_once_and_again_only_when_the_terminal_changed():
    term = Terminal()
    assert term.note() is None
    term.set(HERE)
    first = term.note()
    assert "PowerShell 7.7.0 on Windows 11" in first and "C:\\Projects\\demo" in first and "terminal_run" in first and "ONE command line" in first
    assert term.note() is None
    term.set({**HERE, "cwd": "C:\\Projects\\demo\\sub", "venv": ".venv"})
    assert "C:\\Projects\\demo\\sub" in term.note() and term.note() is None


def test_a_command_is_written_on_the_prompt_as_one_line_and_never_run():
    term, seen = Terminal(), []
    assert "no terminal is attached" in term.run("cd ..")
    term.set(HERE)
    assert "no terminal is attached" in term.run("cd ..")                                  # nobody carries it yet
    term.on_command = seen.append
    said = term.run("  mkdir ciao; cd ciao  ", "makes the folder and goes in")
    assert "waiting for their Enter" in said
    assert seen[0]["command"] == "mkdir ciao; cd ciao" and seen[0]["why"] == "makes the folder and goes in" and len(seen[0]["id"]) == 8
    for refused in ("", "   ", "a\nb", "a\rb", "x" * 2001, None, 5):
        assert term.run(refused).startswith("Nothing was written")
    assert len(seen) == 1


def built(tmp_path):
    (tmp_path / "w").mkdir(exist_ok=True)
    return build_agent(AgentConfig(workspace=str(tmp_path / "w"), data_dir=str(tmp_path / "d"), use_memory=False, use_checkpoints=False), client=None)


def test_the_tool_appears_with_the_first_description_and_not_before(tmp_path):
    agent = built(tmp_path)
    try:
        assert "terminal_run" not in agent.agent.tools.names()
        assert agent.take_terminal({"shell": "PowerShell"}) is False and "terminal_run" not in agent.agent.tools.names()
        assert agent.take_terminal(HERE) is True
        tool = agent.agent.tools.get("terminal_run")
        assert tool.effect.value == "pure" and tool.parameters["required"] == ["command"]
        assert "waits for their Enter" not in tool.description and "presses Enter" in tool.description
        sent = []
        agent.terminal.on_command = sent.append
        assert "waiting for their Enter" in tool.handler(command="cd C:\\Projects")
        assert sent[0]["command"] == "cd C:\\Projects"
        assert agent.take_terminal({**HERE, "cwd": "D:\\x"}) and agent.terminal.held["cwd"] == "D:\\x"        # the newest description answers
    finally:
        agent.close()
