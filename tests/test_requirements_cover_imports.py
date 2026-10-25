"""Every third-party package that harness/ or serve/ imports is in a requirements file, so an installed copy does not stop on a missing module."""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENT_FILES = (ROOT / "requirements.txt", ROOT / "harness" / "requirements.txt")
# the distribution that provides an import name when the two differ
DISTRIBUTION = {"PIL": "pillow", "yaml": "pyyaml", "fitz": "pymupdf"}
# imported only inside a try block or a function, with a plain message when absent (not installed by default)
OPTIONAL = {
    "jsonschema": "json_schema responses without it check for one JSON object",
    "referencing": "comes with jsonschema",
    "pypdf": "a PDF is read with pypdf or PyMuPDF, whichever is installed",
    "fitz": "PyMuPDF, the other PDF reader",
}
# modules of this repository that the code reaches through sys.path (tools/ is added at run time)
LOCAL_FOLDERS = (ROOT, ROOT / "tools", ROOT / "tools" / "convert")


def required():
    names = set()
    for path in REQUIREMENT_FILES:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*([A-Za-z0-9_.-]+)", line.split("#")[0])
            if m:
                names.add(m.group(1).lower().replace("_", "-"))
    return names


def local_names():
    names = set()
    for folder in LOCAL_FOLDERS:
        names |= {p.stem for p in folder.glob("*.py")} | {p.name for p in folder.iterdir() if (p / "__init__.py").exists()}
    return names


def imported():
    """Import name -> the files that import it, for every non-test file of harness/ and serve/."""
    found = {}
    for top in ("harness", "serve"):
        for path in (ROOT / top).rglob("*.py"):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                else:
                    continue
                for name in names:
                    found.setdefault(name.split(".")[0], set()).add(str(path.relative_to(ROOT)))
    return found


def test_every_third_party_import_is_in_a_requirements_file():
    have, local = required(), local_names()
    missing = {}
    for name, files in imported().items():
        if name in sys.stdlib_module_names or name in local or name in OPTIONAL:
            continue
        if DISTRIBUTION.get(name, name).lower().replace("_", "-") not in have:
            missing[name] = sorted(files)
    assert not missing, f"imported but in no requirements file: {missing}"


def test_the_installer_reads_the_file_that_has_the_vault_package():
    # setup installs only the top requirements.txt: a package the Wallet needs must be in it, not only in harness/requirements.txt
    top = {re.match(r"\s*([A-Za-z0-9_.-]+)", line).group(1).lower() for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
           if line.strip() and not line.lstrip().startswith("#")}
    assert "cryptography" in top
    assert "cryptography" in (ROOT / "tools" / "windows" / "setup" / "Program.cs").read_text(encoding="utf-8")        # and the setup checks that it loads


def test_the_optional_imports_are_still_optional():
    files = imported()
    for name in OPTIONAL:
        assert name in files, f"{name} is no longer imported: remove it from OPTIONAL"
