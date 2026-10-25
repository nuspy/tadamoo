"""The repository map lists definitions per file, most relevant first, within a token budget, with the standard library only."""
import os
import time
from pathlib import Path

import pytest

from harness.repomap import build_repo_map, names_of, rank, scan, symbols_of
from harness.tools import create_standard_tool_registry

PY = '''import os


class Engine(Base):
    """Doc."""
    def start(self, port: int = 80) -> None:
        pass

    async def stop(self):
        pass

    class Inner:
        pass


def helper(a, b=2, *rest, key=None):
    return a


async def fetch(url: str) -> bytes:
    return b""


if True:
    def guarded():
        pass
'''

JS = '''export class Widget extends Base {
  constructor(a) { super(a); }
  render(props, ctx) {
    if (props) {
    }
  }
  static async load(id: number): Promise<Widget> {
    return null;
  }
}
export function makeWidget(a, b) {
  return new Widget(a);
}
export const compute = async (x) => x * 2;
export interface Options { a: number }
type Id = string;
const notExported = 5;
'''

CPP = '''#include <vector>
namespace basalt {
struct Layer {
  int x;
};
class Runner : public Base {
};
template <typename T>
static int add_values(int a, int b) {
  return a + b;
}
__global__ void scale_kernel(float* out, const float* in, int n)
{
  if (n > 0) {
    out[0] = in[0];
  }
}
void Runner::step(int n) const {
  for (int i = 0; i < n; ++i) {
  }
}
int declared_only(int a);
}  // namespace basalt
'''

GO = '''package main

type Server struct {
	addr string
}

type Handler interface {
	Serve()
}

func NewServer(addr string) *Server {
	return &Server{addr}
}

func (s *Server) Start(port int) error {
	return nil
}
'''

RUST = '''pub struct Config {
    pub name: String,
}

impl Config {
    pub fn new(name: &str) -> Self {
        Config { name: name.to_string() }
    }
    pub(crate) async fn load(&self) {}
}

pub trait Run {
    fn run(&self);
}

enum Mode { A, B }
'''

JAVA = '''package demo;

public class Account {
    private int balance;

    public Account(int start) {
        balance = start;
    }

    public synchronized void deposit(int amount) throws IllegalStateException {
        balance += amount;
    }

    public static <T> List<T> wrap(T item) {
        return null;
    }
}

interface Auditable { void audit(); }
'''


def lines(language, source):
    return [("  " * d) + text for d, text, _ in symbols_of(language, source)]


def test_python_classes_functions_and_methods():
    out = lines("python", PY)
    assert out == ["class Engine(Base)", "  def start(self, port: int=80) -> None", "  async def stop(self)", "  class Inner",
                   "def helper(a, b=2, *rest, key=None)", "async def fetch(url: str) -> bytes", "def guarded()"]


def test_a_python_file_that_does_not_parse_still_gives_its_definitions():
    out = lines("python", "class Half:\n    def one(self):\n        return (\n\ndef two(x):\n")
    assert out == ["class Half", "  def one(self)", "def two(x)"]


def test_javascript_and_typescript():
    out = lines("js", JS)
    assert "class Widget extends Base" in out and "  render(props, ctx)" in out and "  load(id: number)" in out
    assert "function makeWidget(a, b)" in out and "const compute = (...) =>" in out and "interface Options" in out and "type Id" in out
    assert not any("notExported" in x or "if (props)" in x or "constructor" in x.replace("class", "") for x in out)


def test_c_cpp_and_cuda():
    out = lines("c", CPP)
    assert "struct Layer" in out and "class Runner" in out
    assert any(x.startswith("static int add_values(int a, int b)") for x in out)
    assert any(x.startswith("__global__ void scale_kernel(float* out, const float* in, int n)") for x in out)   # the brace on the next line
    assert any(x.startswith("void Runner::step(int n) const") for x in out)
    assert not any("declared_only" in x or "for (" in x or "if (" in x or "include" in x for x in out)


def test_go_rust_and_java():
    go = lines("go", GO)
    assert go == ["type Server struct", "type Handler interface", "func NewServer(addr string)", "func (s *Server) Start(port int)"]
    rust = lines("rust", RUST)
    assert "struct Config" in rust and "impl Config" in rust and "  fn new(name: &str)" in rust and "trait Run" in rust and "enum Mode" in rust and "  fn load(&self)" in rust
    java = lines("java", JAVA)
    assert "class Account" in java and "  deposit(int amount)" in java and "  wrap(T item)" in java and "interface Auditable" in java


def test_names_skip_the_generic_and_the_short():
    names = names_of(symbols_of("python", "def run(): pass\ndef compute_total(a): pass\nclass X: pass\ndef __init__(self): pass\n"))
    assert names == {"compute_total"}


def make_repo(tmp_path):
    root = tmp_path / "repo"
    files = {"core/engine.py": PY, "app/main.py": "from core.engine import Engine, helper\n\ndef serve_all():\n    return Engine(), helper(1)\n",
             "app/util.py": "from core.engine import helper\n\ndef another():\n    return helper(2)\n",
             "lib/old.py": "def lonely_function():\n    pass\n", "web/widget.ts": JS, "src/kernel.cu": CPP, "go/server.go": GO,
             "rs/config.rs": RUST, "java/Account.java": JAVA, "README.md": "# not code", "data/big.json": "{}"}
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    for skipped in ("node_modules/pkg/index.js", ".venv/lib/x.py", "build/gen.py", "__pycache__/c.py", ".git/hooks/h.py"):
        p = root / skipped
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("def should_not_appear():\n    pass\n", encoding="utf-8")
    old = time.time() - 90 * 86400
    for p in root.rglob("*"):
        if p.is_file():
            os.utime(p, (old, old))
    return root


def test_the_map_lists_each_language_and_skips_heavy_folders(tmp_path):
    root = make_repo(tmp_path)
    text = build_repo_map(root, max_tokens=4000)
    for expected in ("core/engine.py", "web/widget.ts", "src/kernel.cu", "go/server.go", "rs/config.rs", "java/Account.java", "class Engine(Base)",
                     "def serve_all()", "func NewServer(addr string)"):
        assert expected in text, expected
    for absent in ("should_not_appear", "README", "big.json"):
        assert absent not in text


def test_files_used_by_others_and_files_changed_lately_come_first(tmp_path):
    root = make_repo(tmp_path)
    files, _ = scan(root)
    order = [f.rel for f in rank(files)]
    assert order.index("core/engine.py") < order.index("lib/old.py")                    # two other files use helper() and Engine
    fresh = root / "lib" / "old.py"
    os.utime(fresh, None)                                                              # changed just now
    files, _ = scan(root)
    assert [f.rel for f in rank(files)][0] == "lib/old.py"
    assert [f.rel for f in rank(files, focus="Account")][0] == "java/Account.java"      # a focus beats both
    assert [f.rel for f in rank(files, focus="server")][0] == "go/server.go"


def test_the_map_stops_at_the_token_budget_and_says_what_it_left_out(tmp_path):
    root = make_repo(tmp_path)
    small = build_repo_map(root, max_tokens=200)
    assert len(small) < 200 * 3.5 + 600 and "more files not shown" in small
    big = build_repo_map(root, max_tokens=6000)
    assert "more files not shown" not in big and len(big) > len(small)
    assert "Repository map: " in small.splitlines()[0]


def test_one_file_cannot_take_the_whole_map(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    (root / "huge.py").write_text("\n".join(f"def function_number_{i}(a, b):\n    pass\n" for i in range(400)), encoding="utf-8")
    (root / "small.py").write_text("def small_one():\n    pass\n", encoding="utf-8")
    text = build_repo_map(root, max_tokens=600)
    assert "small.py" in text and text.count("function_number_") <= 40 and "more" in text


def test_the_same_tree_gives_the_same_map(tmp_path):
    root = make_repo(tmp_path)
    now = time.time()
    assert build_repo_map(root, 3000, now=now) == build_repo_map(root, 3000, now=now)


def test_large_and_unreadable_files_are_left_out_and_counted(tmp_path, monkeypatch):
    import harness.repomap as rm
    root = tmp_path / "r"
    root.mkdir()
    (root / "a.py").write_text("def alpha_function():\n    pass\n", encoding="utf-8")
    (root / "b.py").write_text("def beta_function():\n    pass\n" + "# padding\n" * 100, encoding="utf-8")
    monkeypatch.setattr(rm, "MAX_FILE_BYTES", 100)
    text = build_repo_map(root)
    assert "alpha_function" in text and "beta_function" not in text and "1 not read" in text
    assert build_repo_map(tmp_path / "empty_nowhere" if False else tmp_path).count("Repository map") == 1


def test_an_empty_folder_says_so(tmp_path):
    (tmp_path / "e").mkdir()
    assert "No definitions found" in build_repo_map(tmp_path / "e")


def test_the_tool_is_offered_stays_inside_the_workspace_and_changes_nothing(tmp_path):
    root = make_repo(tmp_path)
    registry, _ = create_standard_tool_registry(root)
    tool = registry.get("repo_map")
    assert tool is not None and tool.effect.value == "pure"
    assert "class Engine(Base)" in registry.execute("repo_map", {})
    assert "def serve_all()" in registry.execute("repo_map", {"path": "app", "focus": "serve"})
    with pytest.raises(PermissionError):
        registry.execute("repo_map", {"path": ".."})
    with pytest.raises(NotADirectoryError):
        registry.execute("repo_map", {"path": "core/engine.py"})
    assert len(registry.execute("repo_map", {"max_tokens": 99999})) < 8000 * 3.5 + 600           # the budget has a ceiling
