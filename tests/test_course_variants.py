"""The watch of the course sees one kind of command tried again and again with other numbers, even when every try works."""
from types import SimpleNamespace

from harness import course


def start(command, tool="shell_command"):
    return {"type": "tool_start", "name": tool, "arguments": {"command": command}}


def analysis(i):
    return f"cd /app && python3 -c \"import numpy as np; from load import load; x,y=load(); nb={40 + i}; k={3 * i + 1}; ys=np.convolve(y,np.ones(k)/k); print(np.histogram(x,nb))\""


AGENT = SimpleNamespace(_loop_strikes=0, tools=None, max_turns=0, context=None)


def test_variations_of_one_command_get_one_note_that_asks_for_a_new_question_not_new_parameters():
    w = course.Watcher()
    for i in range(course.VARIANTS - 1):
        w.observe(start(analysis(i)))
        assert w.check(AGENT, i * 3) is None
    w.observe(start(analysis(99)))
    got = w.check(AGENT, 100)
    assert got is not None and got.kind == "variants" and got.stage == "note"
    assert "Change the question, not the parameters" in got.note and str(course.VARIANTS) in got.note
    w.observe(start(analysis(100)))
    assert w.check(AGENT, 200) is None                       # told once


def test_different_commands_and_short_ones_do_not_count():
    w = course.Watcher()
    other = ["git log --oneline | head -20 && git status --short && git diff --stat HEAD~1", "pip install numpy scipy matplotlib && python -c 'import scipy; print(scipy.__version__)'",
             "ls -la /app/data && wc -l /app/data/*.csv && head -3 /app/data/a.csv", "cat /etc/os-release && uname -a && python3 --version && df -h /app",
             "grep -rn TODO src | sort | uniq -c | sort -rn | head", "find /app -name '*.py' -newer setup.cfg -exec wc -l {} + | tail -5", "docker ps"]
    for i, c in enumerate(other * 3):
        w.observe(start(c))
        assert w.check(AGENT, i * 3) is None
    assert w.variants <= 3                                    # the same listing run three times is three, not a way of working
