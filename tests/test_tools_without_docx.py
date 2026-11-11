"""The standard tool registry builds where python-docx is not installed; the Word tools are simply absent."""
import tadamoo.tools as tools


def test_the_registry_builds_without_python_docx(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "find_spec", lambda name: None if name == "docx" else object())
    registry, _ = tools.create_standard_tool_registry(tmp_path)
    assert not [n for n in registry.names if n.startswith("docx")]
    assert "read_file" in registry.names
