"""Tool package providing registry, workspace sandboxing and shell execution."""
from pathlib import Path
from typing import Any, Callable, Optional

from tadamoo.secrets_broker import SecretsBroker
from .registry import ToolRegistry, ToolDefinition, EffectClass
from .filesystem import WorkspaceSandbox
from .search_code import register_search_code
from .tree import register_tree
from .shell import execute_shell_command
from .background import Jobs, register_job_tools
from .search import PageError, browse_url, format_page, offer_search
from tadamoo.repomap import register_repo_map
from tadamoo.repodiagram import register_repo_diagram


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

    def run_to_the_end(command: str, shell: str, timeout: Optional[float] = None):
        with sandbox.watching():                       # a command that edits files is reviewed like the file tools
            return execute_shell_command(command, cwd=sandbox.root, timeout=timeout, secrets=secrets, shell=shell)

    jobs = Jobs(lambda command, shell: run_to_the_end(command, shell))
    registry.jobs = jobs

    def run_shell(command: str, timeout: Optional[float] = None, shell: str = "cmd", background: bool = False):
        if background:
            job = jobs.start(command, shell)
            return {"job": job.id, "state": job.state, "note": "started; job_status, job_log and job_kill follow it, and you are told when it ends"}
        return run_to_the_end(command, shell, timeout)

    registry.register(
        name="read_file",
        description="Reads a file in the workspace, whole or a range of lines. A long file is read in parts: the answer says which lines it holds, "
                    "how many the file has and the offset to go on with. With anchor_line it returns the block (function, class, section) around "
                    "that line instead, found by indentation. Line numbers are not part of the file: leave them out of any text you write back.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to file"},
                "offset": {"type": "integer", "description": "First line to read, counting from 1 (default 1)"},
                "limit": {"type": "integer", "description": "How many lines to read (default: as many as one answer holds)"},
                "numbered": {"type": "boolean", "description": "Put the line number before each line (default false)"},
                "anchor_line": {"type": "integer", "description": "Return the block around this line instead of a range"},
                "max_levels": {"type": "integer", "description": "With anchor_line: how many enclosing blocks to include (default 1)"},
                "include_siblings": {"type": "boolean", "description": "With anchor_line: also the blocks next to the outermost one (default false)"},
            },
            "required": ["path"],
        },
        effect=EffectClass.PURE,
        handler=sandbox.read_file,
    )

    registry.register(
        name="write_file",
        description="Writes the whole content of a file in the workspace and records the diff. Give the complete text, not a summary of what stays. "
                    "An existing file must have been read in full since it last changed, or send overwrite=true. With append=true, content is added "
                    "as new whole lines at the end instead, for a file another conversation may also be writing to.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to file"},
                "content": {"type": "string", "description": "Full file content to write, or the lines to add with append=true"},
                "overwrite": {"type": "boolean", "description": "Replace an existing file you have not read in full, or with much less text (default false)"},
                "append": {"type": "boolean", "description": "Add content as new lines at the end instead of replacing the file (default false)"},
            },
            "required": ["path", "content"],
        },
        effect=EffectClass.REVERSIBLE,
        handler=sandbox.write_file,
    )

    registry.register(
        name="edit_file",
        description="Replaces text in a file and records the diff. Give target and replacement, or edits: a list of {target, replacement} applied "
                    "in order, all or none. The target is copied from the file; if only its whitespace or quote marks differ and one place matches, "
                    "it is still used. When it cannot be applied the answer shows the closest text with line numbers.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to file"},
                "target": {"type": "string", "description": "Text to replace, copied from the file"},
                "replacement": {"type": "string", "description": "Replacement text (empty to delete the target)"},
                "edits": {
                    "type": "array",
                    "description": "Several edits for this file instead of target and replacement; each is applied to the result of the one before",
                    "items": {
                        "type": "object",
                        "properties": {
                            "target": {"type": "string", "description": "Text to replace, copied from the file"},
                            "replacement": {"type": "string", "description": "Replacement text"},
                            "replace_all": {"type": "boolean", "description": "Replace every exact occurrence"},
                            "expected_count": {"type": "integer", "description": "Replace every exact occurrence, which must be this many"},
                        },
                        "required": ["target", "replacement"],
                    },
                },
                "replace_all": {"type": "boolean", "description": "Replace every exact occurrence of the target (default: it must appear once)"},
                "expected_count": {"type": "integer", "description": "Replace every exact occurrence of the target, which must be this many"},
            },
            "required": ["path"],
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
                "timeout": {"type": "number", "description": "Timeout in seconds, greater than 0; when it is left out the command runs until it ends (30 seconds, at most 600, when the person keeps commands contained)"},
                "background": {"type": "boolean", "default": False,
                               "description": "Start it as a background job and return at once (a server, a long build or test run); follow it with job_status, job_log and job_kill"},
                "shell": {"type": "string", "enum": ["cmd", "powershell"], "default": "cmd",
                          "description": "\"cmd\" is the default shell: cmd.exe on Windows, sh elsewhere. \"powershell\" runs the command in PowerShell "
                                         "(Windows only; pwsh when installed, else powershell.exe)."},
            },
            "required": ["command"],
        },
        effect=EffectClass.IRREVERSIBLE,
        handler=run_shell,
    )
    register_job_tools(registry, jobs)

    def read_page(url: str):
        try:
            return format_page(browse_url(url, allow_private=allow_private_pages))
        except PageError as exc:
            from tadamoo import websearch
            if allow_private_pages or not websearch.reader_may_help(str(exc)):
                raise
            read = websearch.read_after_failure(url, str(exc))
            if read is None:
                raise
            return read

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
    register_search_code(registry, sandbox.resolve_safe, sandbox.root)
    register_tree(registry, sandbox.resolve_safe, sandbox.root)
    register_repo_map(registry, sandbox.resolve_safe)
    register_repo_diagram(registry, sandbox.resolve_safe)
    from tadamoo.todo import register_todo_tool
    register_todo_tool(registry)
    registry.mark_stable()

    return registry, sandbox


__all__ = [
    "ToolRegistry",
    "ToolDefinition",
    "EffectClass",
    "WorkspaceSandbox",
    "create_standard_tool_registry",
]
