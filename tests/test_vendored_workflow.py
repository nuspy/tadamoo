"""The vendored-tools workflow (.github/workflows/vendored-tools.yml) is read here, not run: it starts only by hand, every action is pinned to a commit, it builds the two programs from
third_party for Windows and Linux with the locked dependencies, lists the licences of the dependencies, and names files that exist in the vendored trees."""
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / ".github" / "workflows" / "vendored-tools.yml").read_text(encoding="utf-8")
WORKFLOW = yaml.safe_load(TEXT)


def test_it_starts_only_by_hand_and_asks_for_nothing_beyond_reading_the_repository():
    triggers = WORKFLOW.get("on") or WORKFLOW.get(True)
    assert list(triggers) == ["workflow_dispatch"]
    assert WORKFLOW["permissions"] == {"contents": "read"}


def test_every_action_is_pinned_to_a_commit():
    uses = re.findall(r"uses:\s*(\S+)", TEXT)
    assert uses and all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", u) for u in uses), uses


def test_both_programs_are_built_from_third_party_for_both_systems_with_the_locked_dependencies():
    assert set(WORKFLOW["jobs"]) == {"windows", "linux"}
    for job in WORKFLOW["jobs"].values():
        steps = "\n".join(str(s.get("run", "")) for s in job["steps"])
        assert "cargo build --locked --release -p agent-desktop" in steps and "third_party/agent-browser/cli" in steps and "cargo +stable build --locked --release" in steps
        assert "cargo +stable license --manifest-path third_party/agent-desktop/src/Cargo.toml" in steps and "deny" in steps and "check licenses" in steps
        assert "licenses-agent-browser" in steps and "licenses-agent-desktop" in steps
        assert "tools/vendored/agent-desktop" in steps and "tools/vendored/agent-browser" in steps
    linux = "\n".join(str(s.get("run", "")) for s in WORKFLOW["jobs"]["linux"]["steps"])
    assert "agent-desktop-linux --bins" in linux and "agent-desktop-linux-clipboard-owner" in linux          # the helper that goes beside the program
    assert WORKFLOW["jobs"]["linux"]["container"].startswith("ubuntu:22.04@sha256:")                         # the glibc floor of upstream's own release
    assert WORKFLOW["jobs"]["windows"]["runs-on"] == "windows-latest"


def test_what_it_names_exists_in_the_vendored_trees():
    for rel in ("third_party/agent-desktop/Cargo.toml", "third_party/agent-desktop/Cargo.lock", "third_party/agent-desktop/src/Cargo.toml", "third_party/agent-desktop/deny.toml",
                "third_party/agent-desktop/LICENSE", "third_party/agent-desktop/UPSTREAM.md", "third_party/agent-browser/cli/Cargo.toml", "third_party/agent-browser/cli/Cargo.lock",
                "third_party/agent-browser/LICENSE", "third_party/agent-browser/UPSTREAM.md"):
        assert (ROOT / rel).is_file(), rel
    linux = (ROOT / "third_party/agent-desktop/crates/linux/Cargo.toml").read_text(encoding="utf-8")
    assert 'name = "agent-desktop-linux"' in linux.replace('name    = ', 'name = ') and "agent-desktop-linux-clipboard-owner" in linux              # the package and the helper the Linux job copies


def test_each_system_attaches_one_archive_with_a_checksum():
    for job in WORKFLOW["jobs"].values():
        upload = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact@"))
        assert "tadamoo-vendored-tools-*.tar.gz" in upload["with"]["path"] and upload["with"]["if-no-files-found"] == "error"
