"""Shared tool registry and execution adapters for every Sophon Agent session."""

from __future__ import annotations

import json
import os
import signal
import shlex
import subprocess
import time
import threading
from pathlib import Path
from typing import Callable

WORKSPACE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "File Manage: List files and folders in the active project or an explicitly allowed path.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Project-relative folder path; defaults to ."}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "File Manage: Read a UTF-8 text file in the active project or an explicitly allowed path.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Project-relative file path"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "File Manage: Create or replace a UTF-8 text file in the active project or an explicitly allowed path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Project-relative file path"},
                    "content": {"type": "string", "description": "Complete file contents"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
]


def _function_tool(
    name: str,
    category: str,
    description: str,
    properties: dict[str, object],
    required: tuple[str, ...] = (),
) -> dict[str, object]:
    parameters: dict[str, object] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        parameters["required"] = list(required)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{category}: {description}",
            "parameters": parameters,
        },
    }


def _path_property(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


FILE_MANAGE_TOOL_NAMES = {
    "list_files", "read_file", "write_file", "file_create", "file_edit", "file_read",
    "file_delete", "file_rename", "file_copy", "file_move", "folder_create",
    "folder_rename", "tree_view", "folder_copy", "folder_move", "zip", "extract",
}

FILE_MANAGE_TOOLS = [
    _function_tool("file_create", "File Manage", "Create a new UTF-8 text file; fail if it already exists.", {
        "path": _path_property("Project-relative path or an explicitly allowed absolute path"),
        "content": {"type": "string", "description": "Complete UTF-8 file contents"},
    }, ("path", "content")),
    _function_tool("file_edit", "File Manage", "Replace the contents of an existing UTF-8 text file.", {
        "path": _path_property("Project-relative path or an explicitly allowed absolute path"),
        "new_content": {"type": "string", "description": "Complete replacement contents"},
    }, ("path", "new_content")),
    _function_tool("file_read", "File Manage", "Read a UTF-8 text file.", {
        "path": _path_property("File path inside the active project or an allowed path"),
    }, ("path",)),
    _function_tool("file_delete", "File Manage", "Delete a regular file, never a directory.", {
        "path": _path_property("File path inside the active project or an allowed path"),
    }, ("path",)),
    _function_tool("file_rename", "File Manage", "Rename a file within its current folder.", {
        "path": _path_property("Existing file path"),
        "new_name": _path_property("New file name only; must not include a directory"),
    }, ("path", "new_name")),
    _function_tool("file_copy", "File Manage", "Copy a regular file to a new path.", {
        "source_path": _path_property("Existing source file path"),
        "destination_path": _path_property("New destination file path"),
    }, ("source_path", "destination_path")),
    _function_tool("file_move", "File Manage", "Move a regular file to a new path.", {
        "source_path": _path_property("Existing source file path"),
        "destination_path": _path_property("New destination file path"),
    }, ("source_path", "destination_path")),
    _function_tool("folder_create", "File Manage", "Create a folder and any missing parents.", {
        "path": _path_property("New folder path"),
    }, ("path",)),
    _function_tool("folder_rename", "File Manage", "Rename a folder within its current parent.", {
        "path": _path_property("Existing folder path"),
        "new_name": _path_property("New folder name only; must not include a directory"),
    }, ("path", "new_name")),
    _function_tool("tree_view", "File Manage", "Show a bounded directory tree up to five levels deep.", {
        "path": _path_property("Folder path; defaults to the active project root"),
    }),
    _function_tool("folder_copy", "File Manage", "Copy a folder tree without overwriting an existing destination.", {
        "source_path": _path_property("Existing source folder path"),
        "destination_path": _path_property("New destination folder path"),
    }, ("source_path", "destination_path")),
    _function_tool("folder_move", "File Manage", "Move a folder tree to a new path.", {
        "source_path": _path_property("Existing source folder path"),
        "destination_path": _path_property("New destination folder path"),
    }, ("source_path", "destination_path")),
    _function_tool("zip", "File Manage", "Create a bounded ZIP archive from a file or folder.", {
        "path": _path_property("File or folder to archive"),
        "archive_path": _path_property("Optional destination archive path"),
    }, ("path",)),
    _function_tool("extract", "File Manage", "Safely extract a ZIP archive without path traversal or overwrite.", {
        "path": _path_property("ZIP archive path"),
        "destination_path": _path_property("Optional new destination folder; defaults beside the archive"),
    }, ("path",)),
]

WORKSPACE_TOOLS.extend(FILE_MANAGE_TOOLS)

PLANNING_TOOL = _function_tool("planning", "Planning", "Create or update TODO.md in the active project, or mark a numbered step complete.", {
    "action": {"type": "string", "enum": ["create", "update", "complete"]},
    "goal": {"type": "string", "description": "Short task goal for create/update"},
    "steps": {"type": "array", "items": {"type": "string"}, "maxItems": 40, "description": "Ordered actionable steps for create/update"},
    "step": {"type": "integer", "minimum": 1, "description": "One-based step number to mark complete"},
}, ("action",))

ASK_TOOL = {
    "type": "function",
    "function": {
        "name": "ask",
        "description": "Ask the user 1–6 clarifying questions during planning/build or to resolve a blocker. Each question can have up to three suggested answers.",
        "parameters": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 6,
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string", "description": "One concise question"},
                            "options": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                        },
                        "required": ["question"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["questions"],
            "additionalProperties": False,
        },
    },
}

BUILTIN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_version",
            "description": "Get the installed Sophon CLI version.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "is_update",
            "description": "Check whether a newer Sophon CLI version is available.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update",
            "description": "Update Sophon CLI to the latest version. Sophon will restart after the current agent response.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system_health",
            "description": "Run Sophon doctor and return the system health report.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_list",
            "description": "List project folders in the Sophon workspace.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "session_list",
            "description": "List saved Sophon Agent sessions with IDs, titles and message counts.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "session_rename",
            "description": "Rename a saved session by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "ID (or unique ID prefix) of the saved session"},
                    "title": {"type": "string", "description": "New session title"},
                },
                "required": ["session_id", "title"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Get the current local time and time zone.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_date",
            "description": "Get the current local date.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]

CODE_EXECUTION_TOOL = _function_tool(
    "code_execution",
    "Terminal",
    "Run a non-interactive shell command in the active project. Keep commands tied to the user's task; avoid destructive or unrelated operations.",
    {
        "command": {"type": "string", "description": "Shell command to run"},
        "timeout": {"type": "integer", "minimum": 1, "maximum": 120, "description": "Maximum run time in seconds (default 30)"},
    },
    ("command",),
)


EXECUTION_TOOLS = [CODE_EXECUTION_TOOL]
PLAN_BUILTIN_TOOLS = {"get_version", "is_update", "system_health", "workspace_list", "session_list", "get_time", "get_date"}
PLAN_FILE_TOOLS = {"list_files", "read_file", "file_read", "tree_view"}


def tools_for_mode(mode: str, config: dict[str, object] | None = None) -> list[dict[str, object]]:
    """Return the shared tool registry filtered by the active Agent mode."""
    all_tools = [*BUILTIN_TOOLS, *WORKSPACE_TOOLS, *EXECUTION_TOOLS]
    if mode in {"plan", "build"}:
        all_tools.extend([ASK_TOOL, PLANNING_TOOL])
    if mode == "plan":
        allowed = PLAN_BUILTIN_TOOLS | PLAN_FILE_TOOLS | {"ask", "planning", "code_execution"}
        tools = [tool for tool in all_tools if tool["function"]["name"] in allowed]
    else:
        tools = all_tools
    mode_config = (config or {}).get("mode")
    configured = mode_config.get(mode) if isinstance(mode_config, dict) else None
    if isinstance(configured, list) and configured:
        requested = {item for item in configured if isinstance(item, str)}
        tools = [tool for tool in tools if tool["function"]["name"] in requested]
    return tools


def execute_code(
    arguments: object,
    *,
    workspace: Path,
    cancellation=None,
    mode: str = "chat",
) -> str:
    """Run a bounded command from the active project; plan mode uses a read-only command set."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            raise RuntimeError("Terminal tool arguments were not valid JSON.") from None
    if not isinstance(arguments, dict) or set(arguments) - {"command", "timeout"}:
        raise RuntimeError("Terminal tool expects only command and optional timeout fields.")
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        raise RuntimeError("command must be a non-empty string.")
    if len(command) > 16_000:
        raise RuntimeError("Commands are limited to 16,000 characters.")
    timeout = arguments.get("timeout", 30)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 120:
        raise RuntimeError("timeout must be an integer between 1 and 120 seconds.")
    directory = workspace.expanduser().resolve()
    if not directory.is_dir():
        raise RuntimeError("The active project directory is unavailable.")
    shell_command = command
    shell = True
    if mode == "plan":
        shell_command = _plan_command(command)
        shell = False
    process = subprocess.Popen(
        shell_command,
        shell=shell,
        cwd=directory,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=(os.name != "nt"),
    )
    output_limit = 16_000
    captured = {"stdout": bytearray(), "stderr": bytearray(), "size": 0, "truncated": False}
    output_lock = threading.Lock()

    def drain(stream, key: str) -> None:
        while True:
            block = stream.read(4096)
            if not block:
                return
            with output_lock:
                remaining = max(0, output_limit - int(captured["size"]))
                if remaining:
                    captured[key].extend(block[:remaining])
                    captured["size"] = int(captured["size"]) + min(len(block), remaining)
                if len(block) > remaining:
                    captured["truncated"] = True

    readers = [
        threading.Thread(target=drain, args=(process.stdout, "stdout"), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, "stderr"), daemon=True),
    ]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    while process.poll() is None:
        if cancellation is not None and cancellation.cancelled:
            _stop_process(process)
            for reader in readers:
                reader.join(timeout=1)
            raise RuntimeError("Terminal command stopped because the Agent request was cancelled.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _stop_process(process)
            break
        try:
            process.wait(timeout=min(0.1, remaining))
        except subprocess.TimeoutExpired:
            continue
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    for reader in readers:
        reader.join(timeout=1)
    stdout = bytes(captured["stdout"]).decode("utf-8", errors="replace")
    stderr = bytes(captured["stderr"]).decode("utf-8", errors="replace")
    output = stdout + stderr
    if captured["truncated"]:
        output += "\n… output truncated at 16,000 characters."
    if timed_out:
        return f"Command timed out after {timeout} seconds.\n{output}".rstrip()
    return f"Exit code: {process.returncode}\n{output}".rstrip()



def _plan_command(command: str) -> list[str]:
    """Translate plan-mode requests to a small read-only command allowlist."""
    try:
        parts = shlex.split(command)
    except ValueError:
        raise RuntimeError("Plan mode could not parse that read-only command.") from None
    if not parts:
        raise RuntimeError("Plan mode requires a read-only inspection command.")
    executable = parts[0]
    if executable == "pwd" and len(parts) == 1:
        return parts
    if executable == "ls" and all(part in {"-a", "-l", "-la", "-al", "-1", "--all", "--long"} for part in parts[1:]):
        return parts
    if executable == "rg" and len(parts) == 2 and not parts[1].startswith("-") and not any(part in {";", "|", "&", ">", "<"} for part in parts):
        return ["rg", "--no-heading", "--color", "never", "--", parts[1], "."]
    if executable == "git" and len(parts) >= 2:
        subcommand = parts[1]
        safe_arguments = {
            "status": {"--short", "--branch", "--porcelain"},
            "diff": {"--stat", "--name-only", "--cached"},
            "log": {"--oneline", "--decorate", "-5", "-10"},
            "branch": {"--all", "--show-current"},
            "show": {"--stat", "--oneline"},
        }
        if subcommand in safe_arguments and all(part in safe_arguments[subcommand] for part in parts[2:]):
            return parts
    raise RuntimeError("Plan mode allows only read-only pwd, ls, rg, and git inspection commands.")

def _stop_process(process: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def dispatch_tool(
    name: str,
    arguments: object,
    *,
    workspace: Path,
    workspace_executor: Callable[[], str],
    builtin_executor: Callable[[], str],
    cancellation=None,
    mode: str = "chat",
) -> str:
    """Dispatch registered tools to their domain-specific executors."""
    if name == "code_execution":
        return execute_code(arguments, workspace=workspace, cancellation=cancellation, mode=mode)
    if name in {tool["function"]["name"] for tool in BUILTIN_TOOLS}:
        return builtin_executor()
    return workspace_executor()
