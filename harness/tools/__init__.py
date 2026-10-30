"""Tool package providing registry, workspace sandboxing and shell execution."""
from pathlib import Path
from typing import Any, Callable, Optional

from harness.secrets_broker import SecretsBroker
from .registry import ToolRegistry, ToolDefinition, EffectClass
from .filesystem import WorkspaceSandbox
from .shell import execute_shell_command
from .search import browse_url, format_page, offer_search
from harness.repomap import register_repo_map
from harness.repodiagram import register_repo_diagram


def create_standard_tool_registry(
    workspace_path: str | Path,
    secrets: Optional[SecretsBroker] = None,
    allow_private_pages: bool | Callable[[], bool] = False,
    mcp_hub: Any = None,
    permissions: Any = None,
) -> tuple[ToolRegistry, WorkspaceSandbox]:
    sandbox = WorkspaceSandbox(workspace_path)
    registry = ToolRegistry()
    if secrets is not None:
        sandbox.protected_hit = secrets.path_hit
    if permissions is not None:
        def confirm_git_write(tool: str, arguments: dict) -> tuple[bool, str]:
            """A write inside .git follows the rules of any workspace write (the checkpoint keeps the file, so a rewind restores it)."""
            return permissions.evaluate(f"{tool}:{arguments['path']}", EffectClass.REVERSIBLE, arguments)
        sandbox.confirm_git_write = confirm_git_write

    def run_shell(command: str, timeout: float = 30.0, shell: str = "cmd"):
        with sandbox.watching():                       # a command that edits files is reviewed like the file tools
            return execute_shell_command(command, cwd=sandbox.root, timeout=timeout, secrets=secrets, shell=shell)

    registry.register(
        name="read_file",
        description="Reads the content of a file within the workspace, whole or a range of lines. A long file is read in parts: "
                    "the answer says which lines it holds, how many the file has and the offset to go on with.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to file"},
                "offset": {"type": "integer", "description": "First line to read, counting from 1 (default 1)"},
                "limit": {"type": "integer", "description": "How many lines to read (default: as many as one answer holds)"},
            },
            "required": ["path"],
        },
        effect=EffectClass.PURE,
        handler=sandbox.read_file,
    )

    registry.register(
        name="write_file",
        description="Writes content to a file in the workspace. Records unified diff.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to file"},
                "content": {"type": "string", "description": "Full file content to write"},
            },
            "required": ["path", "content"],
        },
        effect=EffectClass.REVERSIBLE,
        handler=sandbox.write_file,
    )

    registry.register(
        name="edit_file",
        description="Replaces target string with replacement in a file. Records unified diff.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to file"},
                "target": {"type": "string", "description": "Exact text to replace"},
                "replacement": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "target", "replacement"],
        },
        effect=EffectClass.REVERSIBLE,
        handler=sandbox.edit_file,
    )

    registry.register(
        name="list_directory",
        description="Lists entries in a directory within the workspace.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Relative directory path", "default": "."}},
        },
        effect=EffectClass.PURE,
        handler=sandbox.list_directory,
    )

    registry.register(
        name="shell_command",
        description=("Executes a shell command inside the workspace and returns exit_code, stdout and stderr. "
                     "Write {{secret:NAME}} where a stored credential is needed. A command of several lines is run as a script, in order."),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run; it may have several lines"},
                "timeout": {"type": "number", "description": "Timeout in seconds, greater than 0 (at most 600)", "default": 30.0},
                "shell": {"type": "string", "enum": ["cmd", "powershell"], "default": "cmd",
                          "description": "\"cmd\" is the default shell: cmd.exe on Windows, sh elsewhere. \"powershell\" runs the command in PowerShell "
                                         "(Windows only; pwsh when installed, else powershell.exe)."},
            },
            "required": ["command"],
        },
        effect=EffectClass.IRREVERSIBLE,
        handler=run_shell,
    )

    def read_page(url: str):
        return format_page(browse_url(url, allow_private=allow_private_pages))

    registry.register(
        name="browse_url",
        description="Reads a web page and returns its text. Pages on this PC or its network are refused unless the person allowed them.",
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "http or https address of the page"}},
            "required": ["url"],
        },
        effect=EffectClass.PURE,
        handler=read_page,
        untrusted=True,
        outward=True,
    )
    offer_search(registry, secrets=secrets, hub=mcp_hub)     # search_web exists only when a backend does
    register_repo_map(registry, sandbox.resolve_safe)
    register_repo_diagram(registry, sandbox.resolve_safe)
    registry.mark_stable()

    return registry, sandbox


__all__ = [
    "ToolRegistry",
    "ToolDefinition",
    "EffectClass",
    "WorkspaceSandbox",
    "create_standard_tool_registry",
]
