"""Tool package providing registry, workspace sandboxing and shell execution."""
from pathlib import Path
from .registry import ToolRegistry, ToolDefinition, EffectClass
from .filesystem import WorkspaceSandbox
from .shell import execute_shell_command


def create_standard_tool_registry(workspace_path: str | Path) -> tuple[ToolRegistry, WorkspaceSandbox]:
    sandbox = WorkspaceSandbox(workspace_path)
    registry = ToolRegistry()

    registry.register(
        name="read_file",
        description="Reads the content of a file within the workspace.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Relative path to file"}},
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
        description="Executes a shell command inside the workspace.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"},
                "timeout": {"type": "number", "description": "Timeout in seconds", "default": 30.0},
            },
            "required": ["command"],
        },
        effect=EffectClass.IRREVERSIBLE,
        handler=lambda command, timeout=30.0: execute_shell_command(command, cwd=sandbox.root, timeout=timeout),
    )

    return registry, sandbox


__all__ = [
    "ToolRegistry",
    "ToolDefinition",
    "EffectClass",
    "WorkspaceSandbox",
    "create_standard_tool_registry",
]
