"""Interactive development agent backed by the configured Sophon provider."""

from __future__ import annotations

import codecs
import datetime as dt
import difflib
import hashlib
import json
import math
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sophon import intelligent_ui
from sophon.tool_calling import (
    ASK_TOOL,
    BUILTIN_TOOLS,
    CODE_EXECUTION_TOOL,
    EXECUTION_TOOLS,
    FILE_MANAGE_TOOL_NAMES,
    FILE_MANAGE_TOOLS,
    PLANNING_TOOL,
    PLAN_BUILTIN_TOOLS,
    PLAN_FILE_TOOLS,
    WORKSPACE_TOOLS,
    dispatch_tool,
    tools_for_mode,
)
from sophon.config import (
    PROVIDERS,
    agent_instructions_path,
    endpoint_for,
    ensure_agent_instructions_file,
    read_config,
    write_config,
)
from sophon.core import (
    BORDER,
    MUTED,
    NOTICE_BORDER,
    NOTICE_TEXT,
    VERSION,
    WHITE,
    auto_compression_settings,
    card,
    centered,
    display_home_paths,
    display_width,
    paint,
    wrap_text,
)

SESSION_FILE = Path.home() / ".sophon" / "sessions" / "sessions.json"
LEGACY_SESSION_FILE = Path.home() / ".xernel" / "sessions" / "sessions.json"
WORKSPACE_ROOT = Path.home() / ".sophon" / "workspace"
DEFAULT_PROJECT_DIR = WORKSPACE_ROOT / "my-project"
PROJECT_DIR = DEFAULT_PROJECT_DIR
INPUT_PLACEHOLDER = "Create something with sophon"
ANIMATION_TICK = intelligent_ui.animation_interval()
REASONING_HITBOX: tuple[int, int, int] | None = None
EXIT_TO_TUI = 42
AGENT_MODES = {"chat", "plan", "build"}
SLASH_COMMANDS = (
    "/auto-compression", "/auto-update", "/cd", "/changelog", "/clear", "/compact", "/cwd", "/delete",
    "/exit", "/fork", "/help", "/history", "/mode", "/model", "/new", "/reasoning",
    "/reasoning-effort", "/rename", "/resume", "/review", "/sessions",
    "/threshold", "/undo", "/usage",
)

ARGUMENT_COMPLETIONS = {
    "auto-compression": ("off", "on"),
    "auto-update": ("off", "on"),
    "mode": ("build", "chat", "plan"),
    "reasoning": ("off", "on"),
    "reasoning-effort": ("high", "low", "medium"),
}


def _fresh_usage() -> dict[str, object]:
    return {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "estimated": False}


def _merge_usage(target: dict[str, object], addition: dict[str, object]) -> None:
    for field in ("requests", "prompt_tokens", "completion_tokens", "total_tokens"):
        current = target.get(field, 0)
        increment = addition.get(field, 0)
        if isinstance(current, int) and isinstance(increment, int):
            target[field] = current + increment
    target["estimated"] = target.get("estimated") is True or addition.get("estimated") is True


def _merge_session_usage(session: dict[str, object], addition: dict[str, object]) -> None:
    usage = session.get("usage")
    if not isinstance(usage, dict):
        usage = _fresh_usage()
        session["usage"] = usage
    _merge_usage(usage, addition)


def _auth_token(config: dict[str, object]) -> str:
    token = str(config.get("api_key", "")).strip()
    if token[:7].casefold() == "bearer ":
        token = token[7:].strip()
    return token


def _response_usage(result: object) -> dict[str, object] | None:
    if not isinstance(result, dict):
        return None
    value = result.get("usage")
    if not isinstance(value, dict) and isinstance(result.get("result"), dict):
        value = result["result"].get("usage")
    return value if isinstance(value, dict) else None


def _usage_values(usage: dict[str, object]) -> tuple[int, int, int]:
    def first_integer(*names: str) -> int | None:
        for name in names:
            value = usage.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return None

    prompt = first_integer("prompt_tokens", "input_tokens") or 0
    completion = first_integer("completion_tokens", "output_tokens") or 0
    total = first_integer("total_tokens")
    return prompt, completion, total if total is not None else prompt + completion


class AgentInterrupted(RuntimeError):
    """Raised when the user explicitly cancels an active model request."""


class AgentExitToTUI(RuntimeError):
    """Raised when Escape returns from Agent mode to the Sophon terminal UI."""


class ToggleReasoningView(RuntimeError):
    """Raised when the user toggles the displayed reasoning details."""

    def __init__(self, input_text: str = "") -> None:
        super().__init__(input_text)
        self.input_text = input_text


class RequestCancellation:
    def __init__(self) -> None:
        self.event = threading.Event()
        self._lock = threading.Lock()
        self._response = None

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()

    def check(self) -> None:
        if self.cancelled:
            raise AgentInterrupted("Response interrupted by user.")

    def attach(self, response) -> None:
        with self._lock:
            self._response = response
            cancelled = self.cancelled
        if cancelled:
            response.close()
            self.check()

    def detach(self, response) -> None:
        with self._lock:
            if self._response is response:
                self._response = None

    def cancel(self) -> None:
        self.event.set()
        with self._lock:
            response = self._response
        if response is not None:
            try:
                response.close()
            except OSError:
                pass


class QuestionHandoff:
    """Synchronize a worker-thread ask tool with the interactive TUI thread."""

    def __init__(self) -> None:
        self.ready = threading.Event()
        self.response = ""


SYSTEM_PROMPT = (
    "You are Sophon, a concise and practical development assistant. "
    "Use the File Manage tools to inspect and edit files in the active project or explicitly configured allowed_path entries. "
    "Do not access project files outside those boundaries. "
    "Use built-in tools for supported system operations and report their results accurately. "
    "Use code_execution for task-related terminal commands; it runs in the active project with a time and output limit. "
    "Keep newly created project files inside the active project unless the user explicitly configured another allowed_path."
)


def _ensure_workspace() -> Path:
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    PROJECT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    guide = WORKSPACE_ROOT / "WORKSPACE.md"
    if not guide.exists():
        legacy_guide = WORKSPACE_ROOT / "AGENT.md"
        try:
            content = legacy_guide.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            content = (
                "# Sophon Workspace\n\n"
                "This is the workspace root. Keep each project in its own top-level folder so files "
                "from separate projects stay organized. `my-project/` is the starter project. Use "
                "project-relative paths, and do not store API keys or other secrets here.\n"
            )
        try:
            descriptor = os.open(guide, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
    return PROJECT_DIR


def _instructions_file(config: dict[str, object] | None = None) -> Path:
    default_path = agent_instructions_path().resolve()
    config_root = default_path.parent.parent
    configured_path = Path(str((config or {}).get("agent_instructions_path") or default_path)).expanduser()
    if not configured_path.is_absolute():
        configured_path = config_root / configured_path
    candidate = configured_path.resolve()
    if candidate == config_root or config_root not in candidate.parents:
        candidate = default_path
    return candidate


def _ensure_agent_instructions(config: dict[str, object]) -> Path:
    path = _instructions_file(config)
    ensure_agent_instructions_file(path)
    stored_path = str(path)
    if config.get("agent_instructions_path") != stored_path:
        config["agent_instructions_path"] = stored_path
        write_config(config)
    return path


def _system_message(
    config: dict[str, object] | None = None,
    custom_system_prompt: str | None = None,
    mode: str = "chat",
) -> str:
    instructions_path = _instructions_file(config)
    try:
        instructions = instructions_path.read_text(encoding="utf-8").strip()
    except OSError:
        instructions = "Follow the user's project-specific instructions and keep project files in the active project directory."
    guide = WORKSPACE_ROOT / "WORKSPACE.md"
    try:
        workspace_guidance = guide.read_text(encoding="utf-8").strip()
    except OSError:
        workspace_guidance = "Create one top-level folder per project and keep project files inside it."
    system_prompt = SYSTEM_PROMPT
    mode_instruction = {
        "chat": "Mode: Chat. Focus on general conversation and advice. Do not change project files unless the user clearly asks for implementation.",
        "plan": "Mode: Plan. Inspect read-only. code_execution is limited to pwd, ls, rg, and read-only git inspection; use the planning tool only to maintain the active project's TODO.md. Do not modify other files. Ask clarifying questions with ask when needed, then present a concise plan. The user can approve implementation with /mode build.",
        "build": "Mode: Build. Implement the user's requested changes directly. Use File Manage for scoped edits and code_execution for relevant terminal commands in the active project. For multi-step work, create or update TODO.md and mark steps complete only after verification.",
    }.get(mode, "")
    if mode_instruction:
        system_prompt += "\n\n" + mode_instruction
    if custom_system_prompt and custom_system_prompt.strip():
        system_prompt += "\n\nUser-provided system prompt:\n" + custom_system_prompt.strip()
    return (
        f"{system_prompt}\n\n"
        f"Agent instructions (AGENT.md):\n{instructions}\n\n"
        f"Active project directory: {display_home_paths(PROJECT_DIR)}\n"
        "Additional allowed paths: "
        + (", ".join(display_home_paths(path) for path in _allowed_roots(config)[1:]) or "none")
        + "\n"
        "Display workspace paths as plain text using `~` for the home directory; never wrap a directory path in a fenced code block.\n"
        f"Workspace guidance (WORKSPACE.md):\n{workspace_guidance}"
    )


def _tools_for_mode(mode: str, config: dict[str, object] | None = None) -> list[dict[str, object]]:
    return tools_for_mode(mode, config)


def _event_data(response) -> Iterator[str]:
    fields: list[str] = []
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if fields:
                yield "\n".join(fields)
                fields.clear()
        elif line.startswith(":"):
            continue
        elif line.startswith("data:"):
            fields.append(line[5:].lstrip())
    if fields:
        yield "\n".join(fields)


def _response_content(result: object, provider: str) -> str | None:
    if not isinstance(result, dict):
        raise RuntimeError("The provider returned an unexpected response.")
    if provider == "openrouter":
        if result.get("error"):
            error = result["error"]
            detail = error.get("message", error) if isinstance(error, dict) else error
            raise RuntimeError(str(detail))
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return None
        delta = choices[0].get("delta") or choices[0].get("message") or {}
        content = delta.get("content") if isinstance(delta, dict) else None
    else:
        if result.get("success") is False:
            errors = result.get("errors") or []
            message = next(
                (item.get("message") for item in errors if isinstance(item, dict) and item.get("message")),
                "Cloudflare request failed",
            )
            raise RuntimeError(str(message))
        content = result.get("response")
        if content is None and isinstance(result.get("result"), dict):
            content = result["result"].get("response")
    if isinstance(content, list):
        return "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    return content if isinstance(content, str) else None


def _response_reasoning(result: object, provider: str) -> str | None:
    if not isinstance(result, dict):
        return None
    if provider == "openrouter":
        choices = result.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        delta = choice.get("delta") or choice.get("message") or {}
        if isinstance(delta, dict):
            value = delta.get("reasoning") or delta.get("reasoning_content") or delta.get("reasoning_details")
        else:
            value = None
    else:
        value = result.get("reasoning")
        if value is None and isinstance(result.get("result"), dict):
            value = result["result"].get("reasoning")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            str(part.get("text") or part.get("summary") or part.get("content") or "")
            for part in value
            if isinstance(part, dict)
        )
    return None


def _allowed_roots(config: dict[str, object] | None = None) -> list[Path]:
    roots = [PROJECT_DIR.expanduser().resolve()]
    configured = (config or {}).get("allowed_path", [])
    if isinstance(configured, list):
        for value in configured:
            if not isinstance(value, str) or not value.strip():
                continue
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = PROJECT_DIR / candidate
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved not in roots:
                roots.append(resolved)
    return roots


def _workspace_file(
    relative_path: str | Path,
    *,
    allow_root: bool = False,
    config: dict[str, object] | None = None,
) -> Path:
    requested = Path(relative_path or ".").expanduser()
    lexical = requested if requested.is_absolute() else PROJECT_DIR / requested
    if lexical.is_symlink():
        raise RuntimeError("Symbolic links are not supported by File Manage tools.")
    try:
        candidate = lexical.resolve()
    except OSError as error:
        raise RuntimeError(f"Could not resolve the requested path: {error}") from None
    roots = _allowed_roots(config)
    project_root = PROJECT_DIR.expanduser().resolve()
    authorized = False
    for root in roots:
        if candidate == root:
            authorized = True
            break
        if root.is_dir() and root in candidate.parents:
            authorized = True
            break
        if not root.exists() and root in candidate.parents:
            authorized = True
            break
    if not authorized:
        raise RuntimeError("File Manage paths must stay inside the active project or a configured allowed_path.")
    if candidate == project_root and not allow_root:
        raise RuntimeError("Choose a file or subfolder inside the active project.")
    return candidate


def _workspace_reference(path: Path) -> str:
    resolved = path.resolve()
    project_root = PROJECT_DIR.expanduser().resolve()
    try:
        return resolved.relative_to(project_root).as_posix()
    except ValueError:
        return str(resolved)


def _execute_workspace_tool(
    name: str,
    arguments: object,
    *,
    change_log: list[dict[str, object]] | None = None,
    config: dict[str, object] | None = None,
) -> str:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            raise RuntimeError("The workspace tool arguments were not valid JSON.") from None
    if not isinstance(arguments, dict):
        raise RuntimeError("The workspace tool arguments must be an object.")
    def text_argument(key: str, *, optional: bool = False, default: str = "") -> str:
        value = arguments.get(key, default)
        if optional and value is None:
            return default
        if not isinstance(value, str) or (not optional and not value.strip()):
            raise RuntimeError(f"{key} must be a non-empty string.")
        return value.strip() if not optional else value.strip()

    def record(change: dict[str, object]) -> None:
        if change_log is not None:
            change_log.append(change)

    def creation_change_index(reference: str, *, is_folder: bool) -> int | None:
        if change_log is None:
            return None
        for index in range(len(change_log) - 1, -1, -1):
            entry = change_log[index]
            if entry.get("path") != reference:
                continue
            kind = entry.get("kind")
            if is_folder and kind == "created_folder":
                return index
            if not is_folder and (
                kind in {"created_file", "created_file_digest"}
                or (kind == "write_file" and entry.get("before_exists") is False)
            ):
                return index
        return None

    def remap_change_paths(source: str, destination: str, start_index: int) -> None:
        if change_log is None:
            return
        source_prefix = source.rstrip("/\\")
        for entry in change_log[start_index:]:
            for key in ("path", "destination"):
                value = entry.get(key)
                if not isinstance(value, str):
                    continue
                if value == source:
                    entry[key] = destination
                elif value.startswith(source_prefix + "/") or value.startswith(source_prefix + "\\"):
                    entry[key] = destination.rstrip("/\\") + value[len(source_prefix):]

    def snapshot_file(path: Path) -> tuple[bytes, int]:
        try:
            data = path.read_bytes()
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError as error:
            raise RuntimeError(f"Could not safely snapshot {display_home_paths(path)}: {error}") from None
        if len(data) > 10_000_000:
            raise RuntimeError("Files larger than 10 MB cannot be changed because undo safety cannot be guaranteed.")
        return data, mode

    def digest_path(path: Path) -> str:
        digest = hashlib.sha256()
        if path.is_file():
            digest.update(path.read_bytes())
            return digest.hexdigest()
        for item in sorted(path.rglob("*"), key=lambda entry: entry.relative_to(path).as_posix()):
            relative = item.relative_to(path).as_posix()
            digest.update(relative.encode("utf-8", errors="replace"))
            if item.is_symlink():
                digest.update(b"symlink")
                digest.update(os.readlink(item).encode("utf-8", errors="replace"))
            elif item.is_file():
                with item.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(64 * 1024), b""):
                        digest.update(chunk)
        return digest.hexdigest()

    def bounded_tree_entries(path: Path) -> list[Path]:
        entries = [path, *path.rglob("*")]
        if len(entries) > 2000 or sum(
            item.stat().st_size for item in entries if item.is_file() and not item.is_symlink()
        ) > 100_000_000:
            raise RuntimeError("Folder operations are limited to 2,000 entries and 100 MB.")
        return entries

    def create_parent_folders(parent_path: Path) -> None:
        missing: list[Path] = []
        parent = parent_path
        while not parent.exists() and parent != parent.parent:
            missing.append(parent)
            parent = parent.parent
        parent_path.mkdir(parents=True, exist_ok=True)
        for created_parent in reversed(missing):
            reference = _workspace_reference(created_parent)
            if any(
                entry.get("kind") == "created_folder" and entry.get("path") == reference
                for entry in (change_log or [])
            ):
                continue
            record({"kind": "created_folder", "path": reference, "after_digest": digest_path(created_parent)})

    def read_text_file(path: Path) -> str:
        if not path.is_file():
            raise RuntimeError("The requested path is not a file.")
        if path.stat().st_size > 500_000:
            raise RuntimeError("Text files must be smaller than 500 KB to read.")
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise RuntimeError(f"Could not read the requested file: {error}") from None

    def write_text_file(path: Path, content: object) -> str:
        if not isinstance(content, str):
            raise RuntimeError("File content must be text.")
        encoded = content.encode("utf-8")
        if len(encoded) > 1_000_000:
            raise RuntimeError("Text files must be smaller than 1 MB to write.")
        before_exists = path.exists()
        before = None
        before_mode = None
        if before_exists:
            before, before_mode = snapshot_file(path)
            if not path.is_file():
                raise RuntimeError("Only regular files can be edited.")
        create_parent_folders(path.parent)
        try:
            path.write_bytes(encoded)
        except OSError as error:
            raise RuntimeError(f"Could not write file: {error}") from None
        reference = _workspace_reference(path)
        previous = next((
            item for item in reversed(change_log or [])
            if item.get("path") == reference and item.get("kind") in {"write_file", "created_file"}
        ), None)
        if previous is None:
            record({
                "kind": "write_file",
                "path": reference,
                "before_exists": before_exists,
                "before": before,
                "before_mode": before_mode,
                "after": encoded,
            })
        else:
            previous["after"] = encoded
        verb = "Created" if name == "file_create" else "Wrote"
        return f"{verb} {display_home_paths(reference)}."

    if name in {"list_files", "tree_view"}:
        folder = _workspace_file(text_argument("path", optional=True, default="."), allow_root=True, config=config)
        if not folder.is_dir():
            raise RuntimeError("The requested path is not a folder.")
        if name == "list_files":
            entries = sorted(folder.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold()))
            return "\n".join(item.name + ("/" if item.is_dir() else "") for item in entries[:200]) or "This folder is empty."
        rows = [folder.name + "/"]
        limit = 500
        def visit(directory: Path, depth: int, prefix: str) -> None:
            if depth >= 5 or len(rows) >= limit:
                return
            try:
                entries = sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold()))
            except OSError:
                return
            for index, item in enumerate(entries):
                if len(rows) >= limit:
                    rows.append("… more entries omitted")
                    return
                last = index == len(entries) - 1
                branch = "└── " if last else "├── "
                is_directory = item.is_dir() and not item.is_symlink()
                rows.append(prefix + branch + item.name + ("/" if is_directory else ""))
                if is_directory:
                    visit(item, depth + 1, prefix + ("    " if last else "│   "))
        visit(folder, 0, "")
        return "\n".join(rows)

    if name in {"read_file", "file_read"}:
        return read_text_file(_workspace_file(text_argument("path"), config=config))

    if name in {"write_file", "file_create", "file_edit"}:
        path = _workspace_file(text_argument("path"), config=config)
        if name == "file_create" and path.exists():
            raise RuntimeError("The file already exists; file_create never overwrites.")
        if name == "file_edit" and not path.is_file():
            raise RuntimeError("file_edit requires an existing regular file.")
        content_key = "new_content" if name == "file_edit" else "content"
        return write_text_file(path, arguments.get(content_key))

    if name == "file_delete":
        path = _workspace_file(text_argument("path"), config=config)
        if not path.is_file():
            raise RuntimeError("file_delete only removes an existing regular file.")
        before, before_mode = snapshot_file(path)
        reference = _workspace_reference(path)
        path.unlink()
        previous = next((
            item for item in reversed(change_log or [])
            if item.get("path") == reference and item.get("kind") in {"write_file", "created_file", "created_file_digest"}
        ), None)
        if previous is not None and (previous.get("before_exists") is not True or previous.get("kind") in {"created_file", "created_file_digest"}):
            change_log.remove(previous)
        else:
            record({"kind": "delete_file", "path": reference, "before_exists": True, "before": before, "before_mode": before_mode, "after": None})
        return f"Deleted {display_home_paths(reference)}."

    if name in {"file_rename", "folder_rename"}:
        source = _workspace_file(text_argument("path"), config=config)
        new_name = text_argument("new_name")
        if new_name in {".", ".."} or "/" in new_name or "\\" in new_name or ":" in new_name or "\x00" in new_name:
            raise RuntimeError("new_name must be a single safe file or folder name.")
        is_folder = name == "folder_rename"
        if is_folder and (not source.is_dir() or source in _allowed_roots(config)):
            raise RuntimeError("folder_rename requires a non-root folder.")
        if not is_folder and not source.is_file():
            raise RuntimeError("file_rename requires an existing regular file.")
        destination = _workspace_file(source.parent / new_name, config=config)
        if destination.exists():
            raise RuntimeError("The rename destination already exists.")
        if is_folder:
            bounded_tree_entries(source)
            before = None
            before_mode = None
        else:
            before, before_mode = snapshot_file(source)
        original_digest = digest_path(source)
        source_reference = _workspace_reference(source)
        destination_reference = _workspace_reference(destination)
        creation_index = creation_change_index(source_reference, is_folder=is_folder)
        created_before = creation_index is not None
        try:
            source.rename(destination)
        except OSError as error:
            raise RuntimeError(f"Could not rename path: {error}") from None
        if created_before:
            remap_change_paths(source_reference, destination_reference, creation_index)
        else:
            record({"kind": "move_folder" if is_folder else "move_file", "path": source_reference, "destination": destination_reference, "before_exists": True, "before": before, "before_mode": before_mode, "after_digest": original_digest})
        return f"Renamed {display_home_paths(source_reference)} to {display_home_paths(destination_reference)}."

    if name in {"file_copy", "file_move", "folder_copy", "folder_move"}:
        source = _workspace_file(text_argument("source_path"), config=config)
        destination = _workspace_file(text_argument("destination_path"), config=config)
        is_folder = name.startswith("folder_")
        is_move = name.endswith("move")
        if is_folder and is_move and source in _allowed_roots(config):
            raise RuntimeError("An authorized workspace root cannot be moved.")
        if is_folder != source.is_dir() or (not is_folder and not source.is_file()):
            raise RuntimeError("The source path type does not match the requested file/folder operation.")
        if destination.exists():
            raise RuntimeError("The destination already exists; File Manage tools never overwrite destinations.")
        if is_folder and (source == destination or source in destination.parents):
            raise RuntimeError("A folder cannot be copied or moved into itself.")
        if is_folder:
            bounded_tree_entries(source)
        if not destination.parent.is_dir():
            raise RuntimeError("The destination's parent folder must already exist.")
        before = None
        before_mode = None
        if not is_folder:
            before, before_mode = snapshot_file(source)
        original_digest = digest_path(source)
        source_reference = _workspace_reference(source)
        destination_reference = _workspace_reference(destination)
        creation_index = creation_change_index(source_reference, is_folder=is_folder)
        created_before = creation_index is not None
        try:
            if is_folder:
                if is_move:
                    shutil.move(str(source), str(destination))
                else:
                    shutil.copytree(source, destination, symlinks=True)
            elif is_move:
                shutil.move(str(source), str(destination))
            else:
                shutil.copy2(source, destination)
        except (OSError, shutil.Error) as error:
            raise RuntimeError(f"File operation failed: {error}") from None
        if is_move:
            if created_before:
                remap_change_paths(source_reference, destination_reference, creation_index)
            else:
                record({"kind": "move_folder" if is_folder else "move_file", "path": source_reference, "destination": destination_reference, "before_exists": True, "before": before, "before_mode": before_mode, "after_digest": original_digest})
        elif is_folder:
            record({"kind": "created_folder", "path": destination_reference, "after_digest": digest_path(destination)})
        else:
            copied, copied_mode = snapshot_file(destination)
            record({"kind": "created_file", "path": destination_reference, "before_exists": False, "before": None, "after": copied, "before_mode": copied_mode})
        verb = "Moved" if is_move else "Copied"
        return f"{verb} {display_home_paths(source_reference)} to {display_home_paths(destination_reference)}."

    if name == "folder_create":
        path = _workspace_file(text_argument("path"), config=config)
        if path.exists():
            raise RuntimeError("The folder already exists.")
        create_parent_folders(path.parent)
        path.mkdir(exist_ok=False)
        record({"kind": "created_folder", "path": _workspace_reference(path), "after_digest": digest_path(path)})
        return f"Created folder {display_home_paths(_workspace_reference(path))}."

    if name == "zip":
        source = _workspace_file(text_argument("path"), allow_root=True, config=config)
        if not source.exists() or not (source.is_file() or source.is_dir()):
            raise RuntimeError("zip requires an existing regular file or folder.")
        default_archive = source / f"{source.name or 'archive'}.zip" if source.is_dir() else source.with_suffix(source.suffix + ".zip")
        archive = _workspace_file(text_argument("archive_path", optional=True, default=str(default_archive)), config=config)
        if archive.exists() or archive == source:
            raise RuntimeError("The ZIP destination already exists or conflicts with the source.")
        entries = [source] if source.is_file() else [item for item in bounded_tree_entries(source) if not item.is_symlink()]
        file_entries = [item for item in entries if item.is_file()]
        if len(entries) > 2000 or sum(item.stat().st_size for item in file_entries) > 100_000_000:
            raise RuntimeError("ZIP input is too large (limit: 2,000 entries and 100 MB uncompressed).")
        create_parent_folders(archive.parent)
        try:
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as stream:
                for item in entries:
                    if item == archive:
                        continue
                    if item == source:
                        arcname = source.name + ("/" if source.is_dir() else "")
                    else:
                        arcname = (Path(source.name) / item.relative_to(source)).as_posix()
                    if item.is_dir():
                        stream.writestr(arcname.rstrip("/") + "/", "")
                    else:
                        stream.write(item, arcname)
        except (OSError, zipfile.BadZipFile) as error:
            archive.unlink(missing_ok=True)
            raise RuntimeError(f"Could not create ZIP archive: {error}") from None
        record({"kind": "created_file_digest", "path": _workspace_reference(archive), "after_digest": digest_path(archive)})
        return f"Created ZIP archive {display_home_paths(_workspace_reference(archive))}."

    if name == "extract":
        archive = _workspace_file(text_argument("path"), config=config)
        if not archive.is_file() or archive.stat().st_size > 100_000_000:
            raise RuntimeError("extract requires an existing ZIP file smaller than 100 MB.")
        default_destination = archive.parent / archive.stem
        destination = _workspace_file(text_argument("destination_path", optional=True, default=str(default_destination)), config=config)
        if destination.exists():
            raise RuntimeError("The extraction destination already exists; extraction never overwrites.")
        try:
            with zipfile.ZipFile(archive, "r") as stream:
                entries = stream.infolist()
                if len(entries) > 2000 or sum(item.file_size for item in entries) > 100_000_000:
                    raise RuntimeError("ZIP archive is too large (limit: 2,000 entries and 100 MB uncompressed).")
                members: list[tuple[zipfile.ZipInfo, Path]] = []
                for item in entries:
                    member = PurePosixPath(item.filename)
                    mode = item.external_attr >> 16
                    if member.is_absolute() or ".." in member.parts or "\\" in item.filename or ":" in item.filename or "\x00" in item.filename or (mode & 0o170000) == stat.S_IFLNK:
                        raise RuntimeError("ZIP archive contains an unsafe path or symbolic link.")
                    target = (destination / Path(*member.parts)).resolve()
                    if target != destination.resolve() and destination.resolve() not in target.parents:
                        raise RuntimeError("ZIP archive path escapes the extraction folder.")
                    members.append((item, target))
                create_parent_folders(destination.parent)
                destination.mkdir(exist_ok=False)
                for item, target in members:
                    if item.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with stream.open(item, "r") as source_stream, target.open("xb") as target_stream:
                        shutil.copyfileobj(source_stream, target_stream, length=64 * 1024)
        except RuntimeError:
            if destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            raise
        except (OSError, zipfile.BadZipFile, RuntimeError) as error:
            if destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            raise RuntimeError(f"Could not safely extract ZIP archive: {error}") from None
        record({"kind": "created_folder", "path": _workspace_reference(destination), "after_digest": digest_path(destination)})
        return f"Extracted archive to {display_home_paths(_workspace_reference(destination))}."

    if name == "planning":
        action = text_argument("action").casefold()
        todo = PROJECT_DIR / "TODO.md"
        if action in {"create", "update"}:
            goal = text_argument("goal")
            steps = arguments.get("steps")
            if not isinstance(steps, list) or not 1 <= len(steps) <= 40 or any(not isinstance(step, str) or not step.strip() for step in steps):
                raise RuntimeError("Planning create/update requires 1–40 non-empty steps.")
            clean_steps = [step.strip().replace("\n", " ")[:300] for step in steps]
            completed: set[str] = set()
            if action == "create" and todo.exists():
                raise RuntimeError("TODO.md already exists; use planning action=update to revise it.")
            if action == "update" and todo.exists():
                previous = read_text_file(_workspace_file("TODO.md", config=config))
                completed = {match.group(2).strip() for match in re.finditer(r"^- \[x\] (\d+)\. (.+)$", previous, re.MULTILINE | re.IGNORECASE)}
            rows = ["# Task Plan", "", f"Goal: {goal.strip()[:500]}", ""]
            rows.extend(f"- [{'x' if step in completed else ' '}] {index}. {step}" for index, step in enumerate(clean_steps, 1))
            content = "\n".join(rows) + "\n"
            write_text_file(_workspace_file("TODO.md", config=config), content)
            return f"{action.title()}d TODO.md with {len(clean_steps)} tracked steps."
        if action == "complete":
            step_number = arguments.get("step")
            if not isinstance(step_number, int) or isinstance(step_number, bool) or step_number < 1:
                raise RuntimeError("Planning complete requires a one-based integer step.")
            path = _workspace_file("TODO.md", config=config)
            content = read_text_file(path)
            pattern = re.compile(rf"^- \[ \] {step_number}\. (.+)$", re.MULTILINE)
            updated, count = pattern.subn(r"- [x] " + str(step_number) + r". \1", content, count=1)
            if not count:
                if re.search(rf"^- \[x\] {step_number}\. ", content, re.MULTILINE | re.IGNORECASE):
                    return f"Step {step_number} is already complete in TODO.md."
                raise RuntimeError(f"TODO.md has no open step {step_number}.")
            write_text_file(path, updated)
            return f"Marked step {step_number} complete in TODO.md."
        raise RuntimeError("Planning action must be create, update, or complete.")

    raise RuntimeError(f"Unknown workspace tool: {name}")


def _execute_builtin_tool(name: str, arguments: object) -> str:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            raise RuntimeError("The built-in tool arguments were not valid JSON.") from None
    if not isinstance(arguments, dict):
        raise RuntimeError("Built-in tool arguments must be an object.")
    if name == "session_rename":
        if set(arguments) != {"session_id", "title"}:
            raise RuntimeError("session_rename requires only session_id and title.")
    elif arguments:
        raise RuntimeError("This built-in tool does not accept arguments.")

    from sophon import __version__
    from sophon import core

    if name == "get_version":
        return f"Sophon CLI version: {__version__}"
    if name == "is_update":
        latest = core.latest_version(timeout=5)
        if latest is None:
            return "Could not check for updates because GitHub is unavailable."
        available = latest != __version__
        return f"Current version: {__version__}\nLatest version: {latest}\nUpdate available: {'yes' if available else 'no'}"
    if name == "update":
        status = core.update(quiet=True, restart_after=False)
        if status != 0:
            return f"Update failed with exit status {status}."
        if core.RESTART_REQUESTED:
            core.RESTART_ARGS = ["agent"]
            return "Update installed. Sophon will restart after this agent response."
        return f"Sophon is already current ({__version__})."
    if name == "system_health":
        try:
            package_root = str(Path(__file__).resolve().parents[1])
            python_path = os.pathsep.join(filter(None, (package_root, os.environ.get("PYTHONPATH", ""))))
            result = subprocess.run(
                [sys.executable, "-m", "sophon.cli", "doctor"],
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONPATH": python_path},
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return f"Sophon doctor could not run: {error}"
        output = (result.stdout or result.stderr).strip()
        return output or f"Sophon doctor exited with status {result.returncode}."
    if name == "workspace_list":
        WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        projects = sorted(
            (path.name for path in WORKSPACE_ROOT.iterdir() if path.is_dir()),
            key=str.casefold,
        )
        return "\n".join(projects) if projects else "No project folders are in the workspace yet."
    if name == "session_list":
        sessions = _load_sessions()
        if not sessions:
            return "No saved sessions."
        return "\n".join(
            f"{item['id']} | {item['title']} | {len(item['messages'])} messages | {str(item.get('updated_at', ''))[:19]}"
            for item in sessions
        )
    if name == "session_rename":
        session_id = arguments.get("session_id")
        title = arguments.get("title")
        if not isinstance(session_id, str) or not session_id.strip() or not isinstance(title, str) or not title.strip():
            raise RuntimeError("Provide a session_id and a non-empty title.")
        sessions = _load_sessions()
        matches = [item for item in sessions if str(item["id"]).startswith(session_id.strip())]
        if len(matches) != 1:
            raise RuntimeError("The session ID was not found or is ambiguous.")
        target = matches[0]
        target["title"] = title.strip()[:80]
        target["updated_at"] = _timestamp()
        _write_sessions(sessions)
        return f"Renamed session {target['id']} to {target['title']}."
    if name == "get_time":
        now = dt.datetime.now().astimezone()
        return now.strftime("%H:%M:%S %Z (UTC%z)")
    if name == "get_date":
        return dt.datetime.now().astimezone().strftime("%Y-%m-%d")
    raise RuntimeError(f"Unknown Sophon built-in tool: {name}")


def _decoded_tool_arguments(value: object) -> dict[str, object]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {"arguments": "[invalid JSON]"}
    return value if isinstance(value, dict) else {"arguments": "[invalid argument object]"}


def _history_safe_arguments(value: object) -> dict[str, object]:
    arguments = _decoded_tool_arguments(value)
    safe: dict[str, object] = {}
    for key, item in arguments.items():
        name = str(key)
        normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
        if name.casefold() == "command" and isinstance(item, str):
            safe[name] = f"[command omitted · {len(item)} characters]"
        elif any(marker in normalized for marker in ("apikey", "token", "secret", "password", "credential", "authorization", "privatekey")):
            safe[name] = "[redacted]"
        elif normalized in {"content", "newcontent", "prompt", "text", "goal"} and isinstance(item, str):
            lines = len(item.splitlines())
            safe[name] = f"[omitted · {len(item.encode('utf-8'))} bytes · {lines} lines]"
        elif isinstance(item, (str, int, float, bool)) or item is None:
            safe[name] = item[:240] + "…" if isinstance(item, str) and len(item) > 240 else item
        else:
            safe[name] = "[omitted]"
    return safe


def _change_line_counts(before: bytes | None, after: bytes) -> tuple[int, int]:
    old_lines = before.decode("utf-8", errors="replace").splitlines() if before is not None else []
    new_lines = after.decode("utf-8", errors="replace").splitlines()
    added = removed = 0
    for operation, old_start, old_end, new_start, new_end in difflib.SequenceMatcher(
        a=old_lines,
        b=new_lines,
        autojunk=False,
    ).get_opcodes():
        if operation in {"insert", "replace"}:
            added += new_end - new_start
        if operation in {"delete", "replace"}:
            removed += old_end - old_start
    return added, removed


def _tool_change_details(
    name: str,
    arguments: dict[str, object],
    change_log: list[dict[str, object]] | None,
    previous_state: dict[str, bytes | None],
    config: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    if name not in FILE_MANAGE_TOOL_NAMES | {"planning"} or change_log is None:
        return []
    references: set[str] = set()
    candidate_values = [arguments.get(key) for key in ("path", "source_path", "destination_path", "archive_path")]
    if name == "planning":
        candidate_values.append("TODO.md")
    for value in candidate_values:
        if not isinstance(value, str) or not value:
            continue
        try:
            references.add(_workspace_reference(_workspace_file(value, allow_root=True, config=config)))
        except (OSError, RuntimeError, ValueError):
            continue
    details: list[dict[str, object]] = []
    for change in reversed(change_log):
        if not isinstance(change, dict):
            continue
        path = str(change.get("path", ""))
        destination = str(change.get("destination", ""))
        matching = [reference for reference in (path, destination) if reference and reference in references]
        if not matching and not (name == "zip" and change.get("kind") == "created_file_digest"):
            continue
        after = change.get("after")
        before = change.get("before")
        for reference in matching or [path]:
            if not reference:
                continue
            before_value = previous_state.get(reference, before if change.get("before_exists") else None)
            before_bytes = len(before_value) if isinstance(before_value, bytes) else 0
            after_bytes = len(after) if isinstance(after, bytes) else 0
            added, removed = _change_line_counts(before_value if isinstance(before_value, bytes) else None, after) if isinstance(after, bytes) else (0, 0)
            kind = str(change.get("kind", name))
            operation = {
                "write_file": "updated" if change.get("before_exists") else "created",
                "delete_file": "deleted",
                "move_file": "moved",
                "move_folder": "moved",
                "created_file": "copied",
                "created_file_digest": "archived",
                "created_folder": "created/extracted",
            }.get(kind, name)
            details.append({
                "path": reference,
                "operation": operation,
                "before_bytes": before_bytes,
                "after_bytes": after_bytes,
                "added_lines": added,
                "removed_lines": removed,
            })
        if details:
            break
    return details


def _tool_result_summary(name: str, result: str) -> str:
    if result.startswith("Tool error:"):
        return result[:400]
    if name == "ask":
        return "Collected answers to the user's clarification questions." if "User responses" in result else "The user cancelled the clarification card."
    if name == "planning":
        return "Updated the active project's TODO.md plan."
    if name == "code_execution":
        first_line = result.splitlines()[0] if result else ""
        return f"Terminal command · {first_line}"[:400]
    if name in {"read_file", "file_read"}:
        encoded = result.encode("utf-8")
        return f"Read file · {len(encoded)} bytes · {len(result.splitlines())} lines."
    if name in {"list_files", "tree_view"}:
        count = 0 if result == "This folder is empty." else len(result.splitlines())
        return f"Listed {count} entries."
    if name == "session_list":
        count = len(result.splitlines())
        return f"Listed {count} saved sessions."
    if name == "workspace_list":
        count = len(result.splitlines())
        return f"Listed {count} workspace entries."
    return result[:400]


def _request(
    config: dict[str, object],
    messages: list[dict[str, object]],
    on_chunk: Callable[[str], None] | None = None,
    tool_calls_out: list[dict[str, object]] | None = None,
    *,
    tools_enabled: bool = True,
    on_reasoning: Callable[[str], None] | None = None,
    usage_out: dict[str, object] | None = None,
    mode: str = "chat",
    cancellation: RequestCancellation | None = None,
) -> str:
    provider = str(config["provider"])
    payload: dict[str, object] = {"messages": messages}
    if provider == "openrouter":
        payload["model"] = config["model"]
    else:
        payload["max_tokens"] = 2048
    payload["stream"] = True
    reasoning = _reasoning_settings(config)
    if reasoning["is_support"] and reasoning["enabled"]:
        if provider == "openrouter":
            payload["reasoning"] = {"effort": reasoning["effort"]}
            payload["include_reasoning"] = True
        else:
            payload["reasoning"] = {"effort": reasoning["effort"]}
    if tools_enabled:
        all_tools = _tools_for_mode(mode, config)
        payload["tools"] = all_tools if provider == "openrouter" else [
            tool["function"] for tool in all_tools
        ]

    request = Request(
        endpoint_for(config),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_auth_token(config)}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    if usage_out is not None:
        usage_out["requests"] = int(usage_out.get("requests", 0)) + 1
    chunks: list[str] = []
    openrouter_tools: dict[int, dict[str, str]] = {}
    cloudflare_tools: list[dict[str, object]] = []
    response_usage: dict[str, object] | None = None

    def collect_tool_calls(result: object) -> None:
        nonlocal response_usage
        if not isinstance(result, dict):
            return
        usage = _response_usage(result)
        if usage is not None:
            response_usage = usage
        if provider == "openrouter":
            choices = result.get("choices")
            choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
            delta = choice.get("delta") or choice.get("message") or {}
            calls = delta.get("tool_calls", []) if isinstance(delta, dict) else []
            if isinstance(calls, list):
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    index = call.get("index", len(openrouter_tools))
                    if not isinstance(index, int):
                        index = len(openrouter_tools)
                    state = openrouter_tools.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    if isinstance(call.get("id"), str):
                        state["id"] += call["id"]
                    function = call.get("function")
                    if isinstance(function, dict):
                        if isinstance(function.get("name"), str):
                            state["name"] += function["name"]
                        if isinstance(function.get("arguments"), str):
                            state["arguments"] += function["arguments"]
        else:
            calls = result.get("tool_calls")
            if calls is None and isinstance(result.get("result"), dict):
                calls = result["result"].get("tool_calls")
            if isinstance(calls, list):
                cloudflare_tools[:] = [call for call in calls if isinstance(call, dict)]

    def append_content(content: str | None) -> None:
        if content:
            chunks.append(content)
            if on_chunk is not None:
                on_chunk(content)

    def append_reasoning(reasoning_text: str | None) -> None:
        if reasoning_text and on_reasoning is not None:
            on_reasoning(reasoning_text)

    try:
        if cancellation is not None:
            cancellation.check()
        with urlopen(request, timeout=120) as response:
            if cancellation is not None:
                cancellation.attach(response)
            try:
                content_type = response.headers.get("Content-Type", "").casefold()
                if "text/event-stream" not in content_type:
                    result = json.loads(response.read().decode("utf-8"))
                    collect_tool_calls(result)
                    append_reasoning(_response_reasoning(result, provider))
                    append_content(_response_content(result, provider))
                else:
                    for data in _event_data(response):
                        if cancellation is not None:
                            cancellation.check()
                        if data != "[DONE]":
                            try:
                                result = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            collect_tool_calls(result)
                            append_reasoning(_response_reasoning(result, provider))
                            append_content(_response_content(result, provider))
                        else:
                            break
            finally:
                if cancellation is not None:
                    cancellation.detach(response)
    except HTTPError as error:
        if cancellation is not None and cancellation.cancelled:
            raise AgentInterrupted("Response interrupted by user.") from None
        detail = error.read(2048).decode("utf-8", errors="replace").strip()
        if tools_enabled and ("tool" in detail.casefold() or "function" in detail.casefold()):
            return _request(
                config,
                messages,
                on_chunk=on_chunk,
                tool_calls_out=tool_calls_out,
                tools_enabled=False,
                on_reasoning=on_reasoning,
                usage_out=usage_out,
                mode=mode,
                cancellation=cancellation,
            )
        if error.code == 401:
            try:
                error_data = json.loads(detail)
            except json.JSONDecodeError:
                error_data = {}
            provider_error = error_data.get("error") if isinstance(error_data, dict) else None
            message = provider_error.get("message") if isinstance(provider_error, dict) else None
            if not isinstance(message, str) or not message.strip():
                message = detail or str(error.reason)
            if provider == "openrouter":
                guidance = "OpenRouter rejected the key/account. Verify it is active and belongs to OpenRouter, then replace it with `sophon setup`."
            else:
                guidance = "Cloudflare rejected the token. Verify the Account ID and an active API token with Workers AI permissions, then replace it with `sophon setup`."
            raise RuntimeError(f"{guidance}\nProvider detail: {message}") from None
        raise RuntimeError(f"Provider returned HTTP {error.code}: {detail or error.reason}") from None
    except (OSError, TimeoutError, URLError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        if cancellation is not None and cancellation.cancelled:
            raise AgentInterrupted("Response interrupted by user.") from None
        raise RuntimeError(f"Could not reach the configured provider: {error}") from None
    if tool_calls_out is not None:
        if provider == "openrouter":
            for index, call in sorted(openrouter_tools.items()):
                if call["name"]:
                    tool_calls_out.append({
                        "id": call["id"] or f"call-{index}",
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": call["arguments"] or "{}",
                        },
                    })
        else:
            for index, call in enumerate(cloudflare_tools):
                name = call.get("name")
                if isinstance(name, str):
                    arguments = call.get("arguments", {})
                    tool_calls_out.append({
                        "id": str(call.get("id") or f"cloudflare-call-{index}"),
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
                        },
                    })
    content = "".join(chunks).strip()
    if not content and not tool_calls_out:
        raise RuntimeError("The provider returned an empty response.")
    if usage_out is not None:
        if response_usage is not None:
            prompt_tokens, completion_tokens, total_tokens = _usage_values(response_usage)
        else:
            prompt_text = json.dumps(messages, ensure_ascii=False)
            completion_text = "".join(chunks)
            if openrouter_tools:
                completion_text += json.dumps(openrouter_tools, ensure_ascii=False)
            if cloudflare_tools:
                completion_text += json.dumps(cloudflare_tools, ensure_ascii=False)
            prompt_tokens = max(1, math.ceil(len(prompt_text) / 4))
            completion_tokens = math.ceil(len(completion_text) / 4)
            total_tokens = prompt_tokens + completion_tokens
            usage_out["estimated"] = True
        usage_out["prompt_tokens"] = int(usage_out.get("prompt_tokens", 0)) + prompt_tokens
        usage_out["completion_tokens"] = int(usage_out.get("completion_tokens", 0)) + completion_tokens
        usage_out["total_tokens"] = int(usage_out.get("total_tokens", 0)) + total_tokens
    return content


def _load_config() -> dict[str, object] | None:
    try:
        return read_config()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        card(
            "Configuration error",
            [f"Provider configuration is invalid: {error}"],
            border_color=NOTICE_BORDER,
            text_color=NOTICE_TEXT,
        )
        return {}


def _complete_turn(
    config: dict[str, object],
    history: list[dict[str, str]],
    on_chunk: Callable[[str], None] | None = None,
    on_reasoning: Callable[[str], None] | None = None,
    custom_system_prompt: str | None = None,
    mode: str = "chat",
    cancellation: RequestCancellation | None = None,
    usage_out: dict[str, object] | None = None,
    change_log: list[dict[str, object]] | None = None,
    activity_out: list[dict[str, object]] | None = None,
    on_questions: Callable[[object], str] | None = None,
    on_planning: Callable[[str], None] | None = None,
) -> str:
    allowed_tool_names = {
        str(tool["function"]["name"])
        for tool in _tools_for_mode(mode, config)
        if isinstance(tool.get("function"), dict)
    }
    messages: list[dict[str, object]] = [
        {"role": "system", "content": _system_message(config, custom_system_prompt, mode)},
        *(
            {"role": message["role"], "content": message["content"]}
            for message in history
            if message.get("role") in {"user", "assistant"}
        ),
    ]
    for _ in range(8):
        if cancellation is not None:
            cancellation.check()
        tool_calls: list[dict[str, object]] = []
        answer = _request(
            config,
            messages,
            on_chunk=on_chunk,
            tool_calls_out=tool_calls,
            on_reasoning=on_reasoning,
            usage_out=usage_out,
            mode=mode,
            cancellation=cancellation,
        )
        if not tool_calls:
            return answer
        messages.append({"role": "assistant", "content": answer or None, "tool_calls": tool_calls})
        for call in tool_calls:
            if cancellation is not None:
                cancellation.check()
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            name = str(function.get("name", ""))
            raw_arguments = function.get("arguments", "{}")
            decoded_arguments = _decoded_tool_arguments(raw_arguments)
            safe_arguments = _history_safe_arguments(raw_arguments)
            previous_state = {
                str(change.get("path", "")): change.get("after")
                for change in (change_log or [])
                if isinstance(change.get("path"), str)
            }
            tool_started = time.monotonic()
            status = "ok"
            try:
                if name not in allowed_tool_names:
                    if mode == "plan" and name in {tool["function"]["name"] for tool in [*BUILTIN_TOOLS, *WORKSPACE_TOOLS, *EXECUTION_TOOLS]}:
                        raise RuntimeError("This tool is disabled in plan mode; switch to /mode build after approval.")
                    raise RuntimeError(f"The {name or 'requested'} tool is not enabled for {mode} mode.")
                if name == "ask":
                    if mode not in {"plan", "build"}:
                        raise RuntimeError("The ask tool is available in plan and build modes only.")
                    if on_questions is None:
                        raise RuntimeError("The ask tool requires an interactive TUI.")
                    result = on_questions(raw_arguments)
                elif name == "planning":
                    if mode not in {"plan", "build"}:
                        raise RuntimeError("The planning tool is available in plan and build modes only.")
                    if on_planning is not None:
                        on_planning("Updating TODO.md")
                    try:
                        result = _execute_workspace_tool(
                            name,
                            raw_arguments,
                            change_log=change_log,
                            config=config,
                        )
                    except (RuntimeError, OSError, ValueError, KeyError, TypeError):
                        if on_planning is not None:
                            on_planning("TODO.md update failed")
                        raise
                    else:
                        if on_planning is not None:
                            on_planning("TODO.md updated")
                else:
                    result = dispatch_tool(
                        name,
                        raw_arguments,
                        workspace=PROJECT_DIR,
                        cancellation=cancellation,
                        mode=mode,
                        builtin_executor=lambda: _execute_builtin_tool(name, raw_arguments),
                        workspace_executor=lambda: _execute_workspace_tool(
                            name,
                            raw_arguments,
                            change_log=change_log,
                            config=config,
                        ),
                    )
                if name == "code_execution":
                    result_status = result.partition("\n")[0]
                    nonzero_exit = result_status.startswith("Exit code: ") and result_status != "Exit code: 0"
                    if result.startswith("Command timed out") or nonzero_exit:
                        status = "error"
            except AgentInterrupted:
                raise
            except (RuntimeError, OSError, ValueError, KeyError, TypeError) as error:
                result = f"Tool error: {error}"
                status = "error"
            if activity_out is not None:
                activity_out.append({
                    "timestamp": _timestamp(),
                    "tool": name,
                    "arguments": safe_arguments,
                    "status": status,
                    "summary": _tool_result_summary(name, result),
                    "duration_ms": round((time.monotonic() - tool_started) * 1000),
                    "changes": _tool_change_details(name, decoded_arguments, change_log, previous_state, config),
                })
            messages.append({
                "role": "tool",
                "tool_call_id": str(call.get("id", "")),
                "name": name,
                "content": result,
            })
            from sophon import core

            if core.RESTART_REQUESTED:
                return "Sophon has been updated and will restart now."
    raise RuntimeError("The agent reached the workspace tool-call limit for this turn.")


def _ask(
    config: dict[str, object],
    history: list[dict[str, str]],
    prompt: str,
    on_chunk: Callable[[str], None] | None = None,
    on_reasoning: Callable[[str], None] | None = None,
    custom_system_prompt: str | None = None,
    mode: str = "chat",
) -> str:
    messages = [*history, {"role": "user", "content": prompt}]
    answer = _complete_turn(
        config,
        messages,
        on_chunk=on_chunk,
        on_reasoning=on_reasoning,
        custom_system_prompt=custom_system_prompt,
        mode=mode,
    )
    history.extend([{"role": "user", "content": prompt}, {"role": "assistant", "content": answer}])
    return answer


def _timestamp() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _new_session() -> dict[str, object]:
    now = _timestamp()
    return {
        "id": uuid.uuid4().hex[:10],
        "title": "New session",
        "created_at": now,
        "updated_at": now,
        "mode": "chat",
        "messages": [],
        "usage": _fresh_usage(),
        "workspace_dir": str(PROJECT_DIR.resolve()),
        "workspace_root": str(WORKSPACE_ROOT.resolve()),
        "activity_log": [],
    }


def _fork_session(source: dict[str, object]) -> dict[str, object]:
    now = _timestamp()
    title = str(source.get("title") or "Session")
    return {
        "id": uuid.uuid4().hex[:10],
        "title": f"Fork of {title}"[:80],
        "created_at": now,
        "updated_at": now,
        "mode": "chat",
        "messages": [dict(message) for message in source.get("messages", [])],
        "usage": _fresh_usage(),
        "workspace_dir": str(_session_workspace_path(source)),
        "workspace_root": str(_session_workspace_root(source)),
        "activity_log": [],
    }


def _validated_workspace_directory(
    value: str | Path,
    *,
    relative_to: Path | None = None,
    workspace_root: Path | None = None,
    allow_root: bool = False,
) -> Path:
    root = (workspace_root or WORKSPACE_ROOT).expanduser().resolve()
    requested = Path(value).expanduser()
    if not requested.is_absolute():
        requested = (relative_to or PROJECT_DIR) / requested
    candidate = requested.resolve()
    if (candidate == root and not allow_root) or (candidate != root and root not in candidate.parents):
        raise RuntimeError("Choose a project directory inside the directory tree shown by /cd.")
    if not candidate.is_dir():
        raise RuntimeError("The requested workspace directory does not exist.")
    return candidate


def _session_workspace_root(session: dict[str, object]) -> Path:
    saved_root = session.get("workspace_root")
    if isinstance(saved_root, str) and saved_root.strip():
        try:
            candidate = Path(saved_root).expanduser().resolve()
            if candidate.is_dir():
                return candidate
        except (OSError, ValueError):
            pass
    return WORKSPACE_ROOT.resolve()


def _session_workspace_path(session: dict[str, object]) -> Path:
    saved = session.get("workspace_dir")
    if isinstance(saved, str) and saved.strip():
        try:
            return _validated_workspace_directory(
                saved,
                relative_to=_session_workspace_root(session),
                workspace_root=_session_workspace_root(session),
                allow_root=True,
            )
        except (OSError, RuntimeError, ValueError):
            pass
    return (WORKSPACE_ROOT / "my-project").resolve()


def _activate_session_workspace(session: dict[str, object]) -> Path:
    global PROJECT_DIR
    PROJECT_DIR = _session_workspace_path(session)
    session["workspace_dir"] = str(PROJECT_DIR)
    session["workspace_root"] = str(_session_workspace_root(session))
    return PROJECT_DIR


def _workspace_location(command: str) -> tuple[str, str, bool]:
    if command == "cwd":
        return "Current directory", display_home_paths(Path.cwd().resolve()), False
    if command == "cd":
        return "Active project directory", display_home_paths(PROJECT_DIR.resolve()), False
    return "Unknown Agent command", "Use /cwd to show the current directory.", True


def _load_sessions() -> list[dict[str, object]]:
    source = SESSION_FILE if SESSION_FILE.exists() else LEGACY_SESSION_FILE
    if not source.exists():
        return []
    try:
        with source.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read saved sessions: {error}") from None
    if not isinstance(value, list):
        raise RuntimeError("Saved sessions file has an invalid format.")
    sessions: list[dict[str, object]] = []
    for item in value:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not isinstance(item.get("title"), str)
            or not isinstance(item.get("messages"), list)
        ):
            raise RuntimeError("Saved sessions file has an invalid entry.")
        messages = item["messages"]
        if any(
            not isinstance(message, dict)
            or message.get("role") not in {"user", "assistant", "error"}
            or not isinstance(message.get("content"), str)
            for message in messages
        ):
            raise RuntimeError("Saved sessions file contains an invalid message.")
        item.setdefault("mode", "chat")
        if item.get("mode") not in AGENT_MODES:
            item["mode"] = "chat"
        item["workspace_root"] = str(_session_workspace_root(item))
        item["workspace_dir"] = str(_session_workspace_path(item))
        activity_log = item.get("activity_log")
        if not isinstance(activity_log, list):
            activity_log = []
        item["activity_log"] = [
            event
            for event in activity_log[-250:]
            if isinstance(event, dict)
            and isinstance(event.get("tool"), str)
            and isinstance(event.get("summary"), str)
            and isinstance(event.get("timestamp"), str)
        ]
        usage = item.get("usage")
        if not isinstance(usage, dict):
            item["usage"] = _fresh_usage()
        else:
            defaults = _fresh_usage()
            for field, value in defaults.items():
                if field not in usage:
                    usage[field] = value
        sessions.append(item)
    sessions.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
    if source == LEGACY_SESSION_FILE:
        _write_sessions(sessions)
    return sessions


def _write_sessions(sessions: list[dict[str, object]]) -> None:
    path = SESSION_FILE
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent.parent, 0o700)
    os.chmod(path.parent, 0o700)
    fd, temp_name = tempfile.mkstemp(prefix=".sessions-", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(sessions, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _save_session(sessions: list[dict[str, object]], session: dict[str, object]) -> None:
    messages = session.get("messages", [])
    if not messages and session.get("title") == "New session":
        return
    session["updated_at"] = _timestamp()
    for index, existing in enumerate(sessions):
        if existing.get("id") == session.get("id"):
            sessions[index] = session
            break
    else:
        sessions.append(session)
    sessions.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
    _write_sessions(sessions)


def _delete_session(sessions: list[dict[str, object]], session_id: str) -> bool:
    updated = [item for item in sessions if item.get("id") != session_id]
    if len(updated) == len(sessions):
        return False
    _write_sessions(updated)
    sessions[:] = updated
    return True


def _compact_history(
    config: dict[str, object],
    messages: list[dict[str, object]],
    context_window: int,
    usage_out: dict[str, object] | None = None,
) -> tuple[list[dict[str, object]], str]:
    if len(messages) < 2:
        raise RuntimeError("There is not enough conversation to compact yet.")
    if len(messages) < 4:
        recent_start = 0
    else:
        recent_start = max(2, len(messages) - 6)
        max_recent_start = len(messages) - 2
        while recent_start < max_recent_start and messages[recent_start].get("role") != "user":
            recent_start += 1
        if recent_start >= len(messages):
            recent_start = max_recent_start
    earlier = messages[:recent_start]
    recent = messages[recent_start:]
    transcript = "\n\n".join(
        f"{str(message.get('role', 'assistant')).upper()}: {message.get('content', '')}"
        for message in earlier
        if message.get("role") in {"user", "assistant", "error"}
    )
    if not transcript.strip():
        raise RuntimeError("There is no earlier conversation to compact.")
    max_chars = max(2000, context_window * 2)
    if len(transcript) > max_chars:
        transcript = "[Some oldest turns omitted to fit the summary request.]\n\n" + transcript[-max_chars:]
    summary_config = config.copy()
    reasoning = _reasoning_settings(summary_config)
    reasoning["enabled"] = False
    summary_config["reasoning"] = reasoning
    summary = _request(
        summary_config,
        [
            {
                "role": "system",
                "content": (
                    "Summarize this earlier Sophon Agent conversation for continuation. Preserve the user's goal, "
                    "important decisions, constraints, file names, completed work, unresolved issues, and next steps. "
                    "Be concise and factual; do not invent details or answer the original request."
                ),
            },
            {"role": "user", "content": transcript},
        ],
        tools_enabled=False,
        usage_out=usage_out,
    ).strip()
    if not summary:
        raise RuntimeError("The provider returned an empty context summary.")
    summary_message: dict[str, object] = {
        "role": "assistant",
        "content": "Earlier conversation summary:\n" + summary,
        "summary": True,
    }
    return [summary_message, *(dict(message) for message in recent)], summary


def _paint_highlight_terms(text: str, terms: tuple[str, ...], base_color: str, highlight_color: str) -> str:
    if not terms:
        return paint(text, base_color)
    pieces: list[str] = []
    cursor = 0
    while cursor < len(text):
        found = [(text.find(term, cursor), term) for term in terms]
        found = [(position, term) for position, term in found if position >= 0]
        if not found:
            pieces.append(paint(text[cursor:], base_color))
            break
        position, term = min(found, key=lambda item: item[0])
        if position > cursor:
            pieces.append(paint(text[cursor:position], base_color))
        pieces.append(paint(term, highlight_color))
        cursor = position + len(term)
    return "".join(pieces)


def _card_rows(
    title: str | None,
    lines: list[str],
    *,
    columns: int,
    width: int | None = None,
    top_left: str | None = None,
    top_right: str | None = None,
    border_color: str = BORDER,
    text_color: str = MUTED,
    title_color: str = WHITE,
    highlight_terms: tuple[str, ...] = (),
) -> list[str]:
    title = display_home_paths(title) if title is not None else None
    lines = [display_home_paths(line) for line in lines]
    top_left = display_home_paths(top_left) if top_left is not None else None
    top_right = display_home_paths(top_right) if top_right is not None else None
    preferred = min(width, intelligent_ui.card_width(columns)) if width is not None else intelligent_ui.card_width(columns)
    inner = max(1, min(preferred, columns - 6))
    top = "  ╭" + "─" * (inner + 2) + "╮"
    bottom = "  ╰" + "─" * (inner + 2) + "╯"
    title = _truncate_display(title, inner) if title is not None else None
    top_left = _truncate_display(top_left, inner) if top_left is not None else None
    top_right = _truncate_display(top_right, inner) if top_right is not None else None
    rows = [paint(top, border_color)]
    content = list(lines)
    if title is None and top_left is not None and top_right is not None:
        left = _truncate_display(top_left, inner)
        badge = _truncate_display(top_right, inner)
        if display_width(left) + display_width(badge) + 1 <= inner:
            heading = left + " " * (inner - display_width(left) - display_width(badge)) + badge
        else:
            heading = _truncate_display(left, max(1, inner - display_width(badge) - 1))
            heading += " " * max(1, inner - display_width(heading) - display_width(badge)) + badge
        rows.append(paint("  │", border_color) + paint(" " + heading + " ", title_color) + paint("│", border_color))
    elif title is not None and top_left is not None and top_right is not None:
        left = _truncate_display(top_left, inner)
        heading = _truncate_display(title, inner)
        badge = _truncate_display(top_right, inner)
        left_start = 0
        title_start = max(0, (inner - display_width(heading)) // 2)
        right_start = inner - display_width(badge)
        if left_start + display_width(left) < title_start and title_start + display_width(heading) < right_start:
            row = left + " " * (title_start - display_width(left)) + heading
            row += " " * max(0, right_start - display_width(row)) + badge
            rows.append(paint("  │", border_color) + paint(" " + row + " ", title_color) + paint("│", border_color))
        else:
            title_row = centered(_truncate_display(heading, inner), inner)
            left_row = left + " " * max(0, inner - display_width(left))
            right_row = " " * max(0, inner - display_width(badge)) + badge
            for text in (title_row, left_row, right_row):
                rows.append(paint("  │", border_color) + paint(" " + text + " ", title_color) + paint("│", border_color))
        content.insert(0, "")
    elif title is not None and top_right is not None:
        badge = _truncate_display(top_right, inner)
        title_width, badge_width = display_width(title), display_width(badge)
        left_pad = max(0, (inner - title_width) // 2)
        right_start = inner - badge_width
        if left_pad + title_width + 1 <= right_start:
            heading = " " * left_pad + title + " " * max(1, right_start - left_pad - title_width) + badge
            heading += " " * max(0, inner - display_width(heading))
            rows.append(paint("  │", border_color) + paint(" " + heading + " ", title_color) + paint("│", border_color))
        else:
            heading = centered(title, inner)
            rows.append(paint("  │", border_color) + paint(" " + heading + " " * (inner - display_width(heading) + 1), title_color) + paint("│", border_color))
            rows.append(paint("  │", border_color) + paint(" " + " " * (inner - badge_width) + badge + " ", title_color) + paint("│", border_color))
        content.insert(0, "")
    elif title is not None:
        content.insert(0, "")
        content.insert(0, title)
    for line in content:
        is_title = line == title and title is not None and top_right is None
        for wrapped in wrap_text(line, inner):
            text = " " * ((inner - display_width(wrapped)) // 2) + wrapped if is_title else wrapped
            padding = max(0, inner - display_width(text))
            row = " " + text + " " * (padding + 1)
            if is_title:
                colored_row = paint(row, title_color)
            elif line.startswith(("❯ ", "› ")):
                colored_row = paint(row, WHITE)
            else:
                colored_row = _paint_highlight_terms(row, highlight_terms, text_color, WHITE)
            rows.append(paint("  │", border_color) + colored_row + paint("│", border_color))
    rows.append(paint(bottom, border_color))
    return rows


def _user_rows(message: str, columns: int) -> list[str]:
    width = min(68, max(3, min(max(20, columns - 8), display_width(message) + 2)))
    return _card_rows(None, [f"› {message}"], columns=columns, width=width)


def _truncate_display(text: str, width: int) -> str:
    if display_width(text) <= width:
        return text
    suffix = "…"
    result = ""
    for character in text:
        if display_width(result + character + suffix) > width:
            break
        result += character
    return result + suffix


def _agent_rows(message: str, columns: int) -> list[str]:
    message = display_home_paths(message)
    width = max(20, columns - 4)
    rows: list[str] = []
    first = True
    for paragraph in message.splitlines() or [message]:
        if not paragraph:
            rows.append("")
            continue
        text = paragraph
        if first and text.lstrip().startswith(("• ", "- ", "* ")):
            text = text.lstrip()[2:]
        # Start the gray bullet at the same left edge as the user's message card.
        prefix = "  • " if first else "    "
        for line in wrap_text(text, width - display_width(prefix)):
            rows.append(paint(prefix, MUTED) + line if first else "    " + line)
            first = False
    return rows


def _message_rows(messages: list[dict[str, object]], columns: int) -> list[str]:
    rows: list[str] = []
    for message in messages:
        role, content = str(message["role"]), str(message["content"])
        if role == "user":
            rows.extend(_user_rows(content, columns))
        elif role == "assistant":
            if message.get("summary") is True:
                rows.extend(
                    _card_rows(
                        "Context Summary",
                        [content],
                        columns=columns,
                        border_color=MUTED,
                        text_color=MUTED,
                    )
                )
            else:
                reasoning_text = message.get("reasoning")
                if isinstance(reasoning_text, str) and reasoning_text.strip():
                    rows.extend(
                        _reasoning_rows(
                            reasoning_text,
                            columns,
                            connect_to_answer=True,
                            expanded=message.get("reasoning_expanded") is True,
                        )
                    )
                rows.extend(_agent_rows(content, columns))
        else:
            rows.extend(
                _card_rows(
                    "Response interrupted" if message.get("cancelled") is True else "Agent request failed",
                    [content],
                    columns=columns,
                    border_color=MUTED if message.get("cancelled") is True else NOTICE_BORDER,
                    text_color=MUTED if message.get("cancelled") is True else NOTICE_TEXT,
                )
            )
        rows.append("")
    return rows


def _editor_layout(
    text: str,
    cursor: int,
    width: int,
    placeholder: str,
    suggestion: str = "",
) -> tuple[list[str], int, int]:
    empty = not text
    completion = suggestion if text and cursor == len(text) else ""
    visible_text = placeholder if empty else text + completion
    lines = ["› "]
    cursor_row, cursor_col = 0, 2
    for index, character in enumerate(visible_text):
        if display_width(lines[-1]) + display_width(character) > width:
            lines.append("")
        if not empty and index == cursor:
            cursor_row, cursor_col = len(lines) - 1, display_width(lines[-1])
        lines[-1] += character
    if not empty and cursor == len(text) and not completion:
        cursor_row, cursor_col = len(lines) - 1, display_width(lines[-1])
    return lines, cursor_row, cursor_col


def _slash_completion_choices(text: str, cursor: int) -> list[str]:
    if not text.startswith("/") or cursor != len(text):
        return []
    if not any(char.isspace() for char in text):
        prefix = text.casefold()
        choices = []
        for command in sorted(SLASH_COMMANDS):
            if command.startswith(prefix) and command != prefix:
                suffix = command[len(text):]
                if command[1:] in ARGUMENT_COMPLETIONS or command in {"/cd", "/model", "/rename", "/threshold"}:
                    suffix += " "
                choices.append(suffix)
        if prefix in SLASH_COMMANDS and (
            prefix[1:] in ARGUMENT_COMPLETIONS or prefix in {"/cd", "/model", "/rename", "/threshold"}
        ):
            choices.append(" ")
        return choices
    command, _, argument_text = text[1:].partition(" ")
    if command.casefold() == "cd":
        prefix = argument_text
        expanded = Path(prefix).expanduser()
        typed_parent = prefix.rsplit("/", 1)[0] + "/" if "/" in prefix else ""
        leaf = prefix.rsplit("/", 1)[-1]
        base = expanded.parent if "/" in prefix else PROJECT_DIR
        if not expanded.is_absolute() and "/" in prefix:
            base = (PROJECT_DIR / typed_parent).expanduser()
        try:
            base = base.resolve()
            active = PROJECT_DIR.expanduser().resolve()
            if base != active and active not in base.parents:
                return []
            return [str(entry.name)[len(leaf):] + ("/" if entry.name.casefold() == leaf.casefold() else "/" if entry.is_dir() else "") for entry in sorted(base.iterdir(), key=lambda item: item.name.casefold()) if entry.is_dir() and entry.name.casefold().startswith(leaf.casefold())]
        except OSError:
            return []
    options = ARGUMENT_COMPLETIONS.get(command.casefold())
    if options is None:
        return []
    prefix = argument_text.rsplit(" ", 1)[-1].casefold()
    if " " in argument_text:
        return []
    return [value[len(prefix):] for value in options if value.startswith(prefix) and value != prefix]


def _slash_command_completion(text: str, cursor: int, choice_index: int = 0) -> str:
    choices = _slash_completion_choices(text, cursor)
    return choices[choice_index % len(choices)] if choices else ""


def _accept_slash_completion(
    text: str,
    cursor: int,
    choice_index: int = 0,
) -> tuple[str, int, bool]:
    completion = _slash_command_completion(text, cursor, choice_index)
    if not completion:
        return text, cursor, False
    return text[:cursor] + completion + text[cursor:], cursor + len(completion), True


def _input_box_rows(lines: list[str], width: int, *, placeholder: bool, text_length: int = 0) -> list[str]:
    rows = [paint("  ╭" + "─" * (width + 2) + "╮", BORDER)]
    remaining = text_length
    for index, line in enumerate(lines):
        padding = " " * max(0, width - display_width(line))
        if index == 0 and line.startswith("› "):
            content_text = line[2:]
            actual_count = min(len(content_text), remaining)
            content = paint("› ", WHITE) + paint(content_text[:actual_count], WHITE if not placeholder else MUTED)
            if actual_count < len(content_text):
                tail = content_text[actual_count:]
                if placeholder and tail.endswith("sophon"):
                    prefix = tail[:-len("sophon")]
                    content += paint(prefix, MUTED) + paint("sophon", WHITE)
                else:
                    content += paint(tail, MUTED)
            remaining -= actual_count
        else:
            actual_count = min(len(line), remaining)
            content = paint(line[:actual_count], WHITE if not placeholder else MUTED)
            if actual_count < len(line):
                content += paint(line[actual_count:], MUTED)
            remaining -= actual_count
        rows.append(paint("  │ ", BORDER) + content + padding + " " + paint("│", BORDER))
    rows.append(paint("  ╰" + "─" * (width + 2) + "╯", BORDER))
    return rows


def _interrupt_warning_rows(columns: int) -> list[str]:
    message = "! İptal etmek için bir kez daha basın"
    width = min(68, max(20, columns - 8))
    content = " " + message + " "
    if display_width(content) > width:
        content = " ! Press Esc again to cancel "
    padding = " " * max(0, width - display_width(content))
    return [
        paint("  ╭" + "─" * (width + 2) + "╮", NOTICE_BORDER),
        paint("  │", NOTICE_BORDER) + paint(content + padding, NOTICE_TEXT) + paint("│", NOTICE_BORDER),
        paint("  ╰" + "─" * (width + 2) + "╯", NOTICE_BORDER),
    ]


def _draw_screen(
    session: dict[str, object],
    text: str,
    cursor: int,
    *,
    model: str,
    placeholder: str = INPUT_PLACEHOLDER,
    panel_rows: list[str] | None = None,
    notice_rows: list[str] | None = None,
    suggestion: str = "",
    answering: bool = False,
    animation_frame: int = 0,
    streaming_text: str = "",
    reasoning_text: str = "",
    planning_text: str = "",
    context_window: int | None = None,
    mode: str = "chat",
    reasoning_state: str = "off",
    reasoning_expanded: bool = False,
    display_message_start: int = 0,
    interrupt_warning: bool = False,
) -> tuple[int, int]:
    layout = intelligent_ui.terminal_layout()
    animation_frame = intelligent_ui.animation_frame(animation_frame)
    columns, height = layout.columns, max(8, layout.rows)
    width = intelligent_ui.card_width(columns)
    header = [] if session["messages"] else _card_rows(
        "Sophon Agent",
        ["Ask about code, debugging, and development."],
        columns=columns,
        text_color=MUTED,
        highlight_terms=("Sophon Agent",),
    )
    footer = _footer_rows(
        model,
        session["messages"],
        context_window,
        columns,
        mode=mode,
        reasoning=reasoning_state,
    )
    visible_messages = session["messages"][max(0, display_message_start):]
    editor_lines, cursor_row, cursor_col = _editor_layout(text, cursor, width, placeholder, suggestion)
    max_editor_lines = max(1, min(4 if layout.compact else 5, height - len(header) - len(footer) - 3))
    visible_start = max(0, min(cursor_row, len(editor_lines) - max_editor_lines))
    visible_lines = editor_lines[visible_start : visible_start + max_editor_lines]
    box = _input_box_rows(visible_lines, width, placeholder=not text, text_length=len(text))

    if panel_rows is not None:
        feed = panel_rows
    else:
        feed = list(notice_rows or []) + _message_rows(visible_messages, columns)
        if answering and reasoning_text.strip():
            feed.extend(_reasoning_rows(
                reasoning_text,
                columns,
                frame=animation_frame,
                connect_to_answer=bool(streaming_text.strip()),
                expanded=reasoning_expanded,
            ))
        if answering and planning_text.strip():
            feed.extend(_answering_rows(animation_frame, "Planning"))
            feed.append(paint("  │  " + _truncate_display(planning_text, max(12, columns - 12)), MUTED))
        elif answering and not streaming_text.strip() and not reasoning_text.strip():
            label = "Reasoning" if reasoning_state not in {"off", "not supported"} else "Answering"
            feed.extend(_answering_rows(animation_frame, label))
        if streaming_text:
            feed.extend(_answering_rows(animation_frame, "Answering"))
            feed.extend(_agent_rows(streaming_text, columns))
    warning_rows = _interrupt_warning_rows(columns) if interrupt_warning else []
    room = intelligent_ui.available_rows(height, len(header), len(box), len(footer), len(warning_rows))
    visible_feed = feed[-room:] if room else []
    if panel_rows is not None and room >= 2 and len(panel_rows) > room:
        visible_feed = [panel_rows[0], *panel_rows[1 : room - 1], panel_rows[-1]]
    screen = header + visible_feed + [""] * max(0, room - len(visible_feed)) + warning_rows + box + footer
    screen = screen[-height:]
    if len(screen) < height:
        screen = [""] * (height - len(screen)) + screen
    global REASONING_HITBOX
    REASONING_HITBOX = None
    for row_index, row_text in enumerate(screen, 1):
        plain_row = re.sub(r"\033\[[0-9;]*m", "", row_text)
        match = re.search(r"\bReasoning\b(?!:)", plain_row)
        if match:
            REASONING_HITBOX = (row_index, match.start() + 1, match.end() + 1)
            break
    output = "\033[2J\033[H" + "\r\n".join(screen)
    absolute_row = height - len(box) - len(footer) - len(warning_rows) + 1 + cursor_row - visible_start
    absolute_col = 4 + cursor_col
    output += f"\033[{max(1, absolute_row + 1)};{max(1, absolute_col + 1)}H"
    sys.stdout.write(output)
    sys.stdout.flush()
    return cursor_row, cursor_col


def _reasoning_mouse_click(key: str) -> bool:
    if not key.startswith("MOUSE:"):
        return False
    try:
        button, mouse_x, mouse_y = (int(part) for part in key[6:].split(","))
    except (ValueError, TypeError):
        return False
    hitbox = REASONING_HITBOX
    return bool(button & 3 == 0 and hitbox and mouse_y == hitbox[0] and hitbox[1] <= mouse_x <= hitbox[2])


def _read_key_raw(timeout: float | None = None) -> str:
    import select

    fd = sys.stdin.fileno()
    if timeout is not None and not select.select([fd], [], [], timeout)[0]:
        return "TIMEOUT"
    first = os.read(fd, 1)
    if first in {b"\r", b"\n"}:
        return "ENTER"
    if first == b"\x03":
        return "CANCEL"
    if first == b"\x12":
        return "TOGGLE_REASONING"
    if first == b"\x18":
        return "SAVE"
    if first == b"\x0e":
        return "INSERT_LINE"
    if first == b"\x0b":
        return "DELETE_LINE"
    if first in {b"\x08", b"\x7f"}:
        return "BACKSPACE"
    if first == b"\x1b":
        if not select.select([fd], [], [], 0.08)[0]:
            return "CANCEL"
        sequence = bytearray()
        while select.select([fd], [], [], 0.08)[0]:
            sequence.extend(os.read(fd, 1))
            if len(sequence) >= 2 and 0x40 <= sequence[-1] <= 0x7E:
                break
        if sequence == b"\x1b":
            return "DOUBLE_CANCEL"
        if sequence.startswith(b"[<") and sequence[-1:] == b"M":
            try:
                button, mouse_x, mouse_y = (int(part) for part in sequence[2:-1].split(b";"))
            except (ValueError, TypeError):
                return "OTHER"
            return f"MOUSE:{button},{mouse_x},{mouse_y}"
        if bytes(sequence) in {b"[3~", b"O3~"}:
            return "DELETE"
        return {b"A": "UP", b"B": "DOWN", b"C": "RIGHT", b"D": "LEFT"}.get(bytes(sequence[-1:]), "OTHER")
    first_byte = first[0]
    expected_bytes = 1 if first_byte < 0x80 else 2 if first_byte < 0xE0 else 3 if first_byte < 0xF0 else 4
    encoded = bytearray(first)
    for _ in range(expected_bytes - 1):
        if not select.select([fd], [], [], 0.05)[0]:
            break
        encoded.extend(os.read(fd, 1))
    try:
        character = encoded.decode("utf-8")
    except UnicodeDecodeError:
        return "OTHER"
    return f"CHAR:{character}" if character.isprintable() else "OTHER"


def _read_key(timeout: float | None = None) -> str:
    import termios
    import tty

    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return _read_key_raw(timeout)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def _input_key_reader(events: queue.Queue[str], stop_event: threading.Event) -> None:
    """Read typeahead in raw mode for a complete response, without terminal echo."""
    if not sys.stdin.isatty():
        return
    import termios
    import tty

    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        sys.stdout.write("\033[?1000h\033[?1006h")
        sys.stdout.flush()
        while not stop_event.is_set():
            key = _read_key_raw(timeout=0.1)
            if key != "TIMEOUT":
                events.put(key)
    except OSError:
        pass
    finally:
        sys.stdout.write("\033[?1000l\033[?1006l")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def _input_bar(
    draw,
    placeholder: str = INPUT_PLACEHOLDER,
    *,
    prefill: str = "",
) -> str:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return input(f"› {placeholder}: {prefill}").strip()

    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    decoder = codecs.getincrementaldecoder(sys.stdin.encoding or "utf-8")()
    characters: list[str] = list(prefill)
    cursor = len(characters)
    completion_index = 0
    last_size = shutil.get_terminal_size((80, 24))

    def redraw() -> None:
        text = "".join(characters)
        draw(
            text,
            cursor,
            placeholder=placeholder,
            suggestion=_slash_command_completion(text, cursor, completion_index),
        )

    try:
        tty.setraw(fd)
        sys.stdout.write("\033[?1000h\033[?1006h")
        sys.stdout.flush()
        redraw()
        while True:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if not ready:
                size = shutil.get_terminal_size((80, 24))
                if size != last_size:
                    last_size = size
                    redraw()
                continue
            raw = os.read(fd, 1)
            if not raw:
                raise EOFError
            if raw == b"\x1b":
                if not select.select([fd], [], [], 0.08)[0]:
                    raise AgentExitToTUI
                sequence = bytearray()
                while select.select([fd], [], [], 0.08)[0]:
                    sequence.extend(os.read(fd, 1))
                    if len(sequence) >= 2 and 0x40 <= sequence[-1] <= 0x7E:
                        break
                if sequence.startswith(b"[<") and sequence[-1:] == b"M":
                    try:
                        button, mouse_x, mouse_y = (int(part) for part in sequence[2:-1].split(b";"))
                    except (ValueError, TypeError):
                        continue
                    hitbox = REASONING_HITBOX
                    if button & 3 == 0 and hitbox and mouse_y == hitbox[0] and hitbox[1] <= mouse_x <= hitbox[2]:
                        raise ToggleReasoningView("".join(characters))
                    continue
                key = bytes(sequence[-1:])
                if key == b"D":
                    cursor = max(0, cursor - 1)
                    completion_index = 0
                elif key == b"C":
                    cursor = min(len(characters), cursor + 1)
                    completion_index = 0
                elif key in {b"A", b"B"}:
                    choices = _slash_completion_choices("".join(characters), cursor)
                    if choices:
                        if key == b"A":
                            completion_index = (completion_index - 1) % len(choices)
                        else:
                            completion_index = (completion_index + 1) % len(choices)
                    redraw()
                    continue
                else:
                    continue
                redraw()
                continue
            if raw == b"\x12":
                raise ToggleReasoningView("".join(characters))
            if raw in {b"\r", b"\n"}:
                text = "".join(characters)
                completed, new_cursor, did_complete = _accept_slash_completion(text, cursor, completion_index)
                if did_complete:
                    characters[:] = list(completed)
                    cursor = new_cursor
                    completion_index = 0
                    redraw()
                    continue
                return text.strip()
            if raw == b"\x03":
                raise KeyboardInterrupt
            if raw in {b"\x08", b"\x7f"}:
                if cursor:
                    characters.pop(cursor - 1)
                    cursor -= 1
                    completion_index = 0
                    redraw()
                continue
            if raw == b"\x04":
                if cursor < len(characters):
                    characters.pop(cursor)
                    completion_index = 0
                    redraw()
                continue
            if raw == b"\t":
                completed, new_cursor, did_complete = _accept_slash_completion(
                    "".join(characters), cursor, completion_index
                )
                if did_complete:
                    characters[:] = list(completed)
                    cursor = new_cursor
                    completion_index = 0
                    redraw()
                continue
            character = decoder.decode(raw)
            if character and character.isprintable():
                characters[cursor:cursor] = list(character)
                cursor += len(character)
                completion_index = 0
                redraw()
    finally:
        sys.stdout.write("\033[?1000l\033[?1006l")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def _model_request(url: str, token: str, *, timeout: float = 30) -> dict[str, object]:
    token = token.strip()
    if token[:7].casefold() == "bearer ":
        token = token[7:].strip()
    request = Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read(2048).decode("utf-8", errors="replace").strip()
        if error.code == 401:
            try:
                payload = json.loads(detail)
            except json.JSONDecodeError:
                payload = {}
            error_data = payload.get("error") if isinstance(payload, dict) else None
            message = error_data.get("message") if isinstance(error_data, dict) else None
            if not isinstance(message, str) or not message.strip():
                message = detail or str(error.reason)
            raise RuntimeError(
                f"Provider rejected the saved credential (HTTP 401). Verify it is active, then replace it with `sophon setup`. Provider detail: {message}"
            ) from None
        raise RuntimeError(f"Model list request failed (HTTP {error.code}): {detail or error.reason}") from None
    except (OSError, TimeoutError, URLError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not load models: {error}") from None
    if not isinstance(result, dict):
        raise RuntimeError("Provider returned an invalid model list.")
    if result.get("success") is False:
        errors = result.get("errors") or []
        message = next((item.get("message") for item in errors if isinstance(item, dict)), "Provider rejected the model list request.")
        raise RuntimeError(str(message))
    return result


def _context_window(item: dict[str, object]) -> int | None:
    for key in ("context_length", "context_window", "max_context_length", "max_context_tokens"):
        value = item.get(key)
        if isinstance(value, int) and value > 0:
            return value
    properties = item.get("properties")
    if isinstance(properties, dict):
        for key in ("context_length", "context_window", "max_context_length", "max_context_tokens"):
            value = properties.get(key)
            if isinstance(value, int) and value > 0:
                return value
    return None


def _model_supports_reasoning(item: dict[str, object]) -> bool:
    if item.get("supports_reasoning") is True:
        return True
    capabilities = item.get("capabilities")
    if isinstance(capabilities, dict) and capabilities.get("reasoning") is True:
        return True
    properties = item.get("properties")
    candidates = [item, properties] if isinstance(properties, dict) else [item]
    for candidate in candidates:
        if candidate.get("supports_reasoning") is True or candidate.get("reasoning") is True:
            return True
        parameters = candidate.get("supported_parameters")
        if isinstance(parameters, list) and any(
            isinstance(parameter, str)
            and parameter.casefold() in {"reasoning", "include_reasoning", "reasoning_effort"}
            for parameter in parameters
        ):
            return True
    return False


def _detect_reasoning_support(config: dict[str, object]) -> bool:
    model_id = str(config.get("model", ""))
    try:
        if config.get("provider") == "cloudflare":
            base = f"https://api.cloudflare.com/client/v4/accounts/{config['account_id']}/ai/models/search"
            result = _model_request(
                base + "?" + urlencode({"per_page": "100", "page": "1", "search": model_id}),
                _auth_token(config),
                timeout=8,
            )
            items = result.get("result")
            if not isinstance(items, list):
                return False
            match = next(
                (item for item in items if isinstance(item, dict) and str(item.get("name") or item.get("id") or item.get("model")) == model_id),
                None,
            )
            return bool(match and _model_supports_reasoning(match))
        models = _list_models(config, model_id, timeout=8)
    except (OSError, RuntimeError, ValueError):
        return False
    match = next((item for item in models if item.get("id") == model_id), None)
    return bool(match and match.get("reasoning_supported"))


def _reasoning_settings(config: dict[str, object]) -> dict[str, object]:
    value = config.get("reasoning")
    value = value if isinstance(value, dict) else {}
    is_support = value.get("is_support") is True
    effort = value.get("effort")
    if effort not in {"low", "medium", "high"}:
        effort = "medium"
    return {
        "is_support": is_support,
        "enabled": is_support and value.get("enabled") is True,
        "effort": effort,
    }


def _list_models(
    config: dict[str, object], search: str = "", *, timeout: float = 30
) -> list[dict[str, object]]:
    provider = str(config["provider"])
    token = _auth_token(config)
    if provider == "openrouter":
        result = _model_request("https://openrouter.ai/api/v1/models", token, timeout=timeout)
        data = result.get("data")
        if not isinstance(data, list):
            raise RuntimeError("OpenRouter returned an invalid model list.")
        all_models = [
            {
                "id": str(item["id"]),
                "name": str(item.get("name") or item["id"]),
                "context_window": _context_window(item),
                "reasoning_supported": _model_supports_reasoning(item),
            }
            for item in data
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        if search:
            needle = search.casefold()
            all_models = [model for model in all_models if needle in model["id"].casefold() or needle in model["name"].casefold()]
        return sorted(all_models, key=lambda model: model["id"].casefold())

    base = f"https://api.cloudflare.com/client/v4/accounts/{config['account_id']}/ai/models/search"
    all_models = []
    for page in range(1, 51):
        params = {"per_page": "100", "page": str(page)}
        if search:
            params["search"] = search
        result = _model_request(base + "?" + urlencode(params), token, timeout=timeout)
        data = result.get("result")
        if not isinstance(data, list):
            raise RuntimeError("Cloudflare returned an invalid model list.")
        batch = []
        for item in data:
            if not isinstance(item, dict):
                continue
            model_id = item.get("name") or item.get("id") or item.get("model")
            if model_id:
                batch.append(
                    {
                        "id": str(model_id),
                        "name": str(item.get("display_name") or item.get("description") or model_id),
                        "context_window": _context_window(item),
                        "reasoning_supported": _model_supports_reasoning(item),
                    }
                )
        all_models.extend(batch)
        info = result.get("result_info")
        if len(data) < 100 or (isinstance(info, dict) and page >= int(info.get("total_pages", page))):
            break
    return sorted(all_models, key=lambda model: model["id"].casefold())


def _discover_context_window(config: dict[str, object], state: dict[str, object]) -> None:
    model_id = str(config["model"])
    try:
        models = _list_models(config, model_id, timeout=6)
    except (OSError, RuntimeError, ValueError):
        if state.get("model") == model_id:
            state["discovery_attempted"] = True
        return
    match = next((model for model in models if model.get("id") == model_id), None)
    window = _context_window(match) if isinstance(match, dict) else None
    if state.get("model") == model_id:
        if window:
            state["window"] = window
        state["discovery_attempted"] = True


def _estimated_context_tokens(messages: list[dict[str, object]]) -> int:
    content = [
        str(message.get("content", ""))
        for message in messages
        if message.get("role") in {"user", "assistant"}
    ]
    tool_schema = json.dumps([*BUILTIN_TOOLS, *WORKSPACE_TOOLS, *EXECUTION_TOOLS], ensure_ascii=False)
    return (
        sum(max(1, math.ceil(len(text) / 3)) + 4 for text in content)
        + math.ceil(len(_system_message()) / 3)
        + math.ceil(len(tool_schema) / 3)
    )


def _context_percentage(messages: list[dict[str, object]], context_window: int) -> int:
    return min(100, round(_estimated_context_tokens(messages) * 100 / context_window))


def _tail_truncate(text: str, width: int) -> str:
    if display_width(text) <= width:
        return text
    if width <= 1:
        return "…"
    return "…" + text[-(width - 1):]


def _footer_rows(
    model: str,
    messages: list[dict[str, object]],
    context_window: int | None,
    columns: int,
    *,
    mode: str = "chat",
    reasoning: str = "off",
) -> list[str]:
    inner = min(68, max(20, columns - 8))
    context = "—"
    if context_window:
        percent = min(100, round(_estimated_context_tokens(messages) * 100 / context_window))
        context = f"~{percent}%"
    context_label = f"Context: {context}"
    try:
        directory = "~/" + PROJECT_DIR.relative_to(Path.home()).as_posix()
    except ValueError:
        directory = display_home_paths(PROJECT_DIR)
    status_label = f"Reasoning: {reasoning} · Mode: {mode}"
    combined_status = f"{status_label} · {context_label}"
    body = [
        _truncate_display("Model: " + model, inner),
    ]
    if display_width(combined_status) > inner:
        body.extend([
            _truncate_display(status_label, inner),
            _truncate_display(context_label, inner),
        ])
    else:
        body.append(combined_status)
    body.append(_truncate_display("Directory: " + directory, inner))
    return _card_rows(
        None,
        body,
        columns=columns,
        width=inner,
        text_color=MUTED,
        highlight_terms=("Model:", "Reasoning:", "Mode:", "Directory:", "Context:"),
    )


def _answering_rows(frame: int, label: str = "Answering") -> list[str]:
    if not intelligent_ui.motion_enabled():
        return [paint(f"  • {label}", MUTED)]
    center = (frame // 1) % (len(label) + 6) - 3
    shimmer = ""
    for index, character in enumerate(label):
        distance = abs(index - center)
        brightness = max(0.0, 1.0 - distance / 2.0)
        color = 240 + round(15 * brightness**2)
        shimmer += paint(character, f"\033[38;5;{color}m")
    pulse = (238, 241, 244, 247, 250, 253, 255, 253, 250, 247, 244, 241)
    dot_color = f"\033[38;5;{pulse[(frame // 2) % len(pulse)]}m"
    return ["  " + paint("•", dot_color) + " " + shimmer]


def _reasoning_rows(
    text: str,
    columns: int,
    *,
    frame: int | None = None,
    connect_to_answer: bool = False,
    expanded: bool = False,
) -> list[str]:
    text = display_home_paths(text)
    label = "Reasoning"
    rows = _answering_rows(frame, label) if frame is not None else [paint(f"  • {label}", MUTED)]
    width = max(20, columns - 8)
    if expanded:
        for paragraph in text.splitlines() or [text]:
            if not paragraph:
                rows.append(paint("  │", MUTED))
                continue
            rows.extend(
                paint("  │  ", MUTED) + paint(line, MUTED)
                for line in wrap_text(paragraph, max(1, width - 6))
            )
    if connect_to_answer:
        rows.append(paint("  │", MUTED))
    return rows


def _pick_item(
    session: dict[str, object],
    draw,
    items: list[dict[str, object]],
    *,
    title: str,
    current_id: str = "",
) -> dict[str, object] | None:
    if not items:
        return None
    if title == "Available Models":
        items = sorted(items, key=lambda item: str(item.get("id", "")).casefold())
    selected_id = current_id
    query = ""
    last_size = shutil.get_terminal_size((80, 24))
    while True:
        size = shutil.get_terminal_size((80, 24))
        header_count = 0 if session.get("messages") else len(_card_rows("Sophon Agent", ["Ask about code, debugging, and development."], columns=size.columns))
        input_count = 3 + 4
        room = max(1, size.lines - header_count - input_count)
        compact = room < 20
        if compact:
            visible_count = max(1, min(8, room - 6))
        else:
            visible_count = max(1, min(8, (room - 7) // 2))
        filtered = [item for item in items if not query or query.casefold() in str(item.get("id", "")).casefold() or query.casefold() in str(item.get("label", item.get("title", ""))).casefold()]
        if not filtered:
            selected = 0
            shown = []
            start = 0
        else:
            selected = next((i for i, item in enumerate(filtered) if str(item.get("id", "")) == selected_id), 0)
            if query and selected == 0:
                selected_id = str(filtered[0].get("id", ""))
            start = max(0, min(selected - visible_count + 1, len(filtered) - visible_count))
            shown = filtered[start : start + visible_count]

        inner_width = min(68, max(20, size.columns - 8))

        def render(text: str, cursor: int) -> tuple[list[str], str]:
            # The picker only autocompletes provider model IDs; session names stay literal.
            completion = ""
            if title == "Available Models" and text:
                match = next(
                    (str(item.get("id", "")) for item in items if str(item.get("id", "")).casefold().startswith(text.casefold())),
                    "",
                )
                if match and cursor == len(text) and len(match) > len(text):
                    completion = match[len(text):]
            body = [f"Current model: {_truncate_display(current_id, inner_width - 15)}"] if title == "Available Models" else []
            if not compact:
                body.append("")
            if not shown:
                body.append("No matching models." if title == "Available Models" else "No saved sessions.")
            else:
                for index, item in enumerate(shown, start):
                    label = str(item.get("label") or item.get("title") or item.get("id") or "Session")
                    label = _truncate_display(label, inner_width - 3)
                    marker = paint("›", WHITE) if index == selected else paint("·", WHITE)
                    body.append(marker + " " + label)
                    if not compact and index + 1 < start + len(shown):
                        body.append("")
            if not compact:
                body.append("")
            footer = "↑/↓ move · Enter select · Esc cancel"
            body.append(_truncate_display(footer, inner_width))
            range_label = f"{start + 1 if shown else 0}–{start + len(shown)} of {len(filtered)}"
            if title == "Available Models":
                return _card_rows(
                    None,
                    body,
                    columns=size.columns,
                    top_left=f"{len(filtered)} models",
                    top_right=range_label,
                ), completion
            return _card_rows(title, body, columns=size.columns, top_right=range_label), completion

        panel, completion = render(query, len(query))
        placeholder = "Type a model ID or use ↑/↓" if title == "Available Models" else "Type to filter or use ↑/↓"
        draw(query, len(query), panel_rows=panel, placeholder=placeholder, suggestion=completion)
        while True:
            key = _read_key(timeout=0.2)
            if key != "TIMEOUT":
                break
            size = shutil.get_terminal_size((80, 24))
            if size != last_size:
                last_size = size
                key = "RESIZE"
                break
        if key == "RESIZE":
            continue
        if key in {"UP", "LEFT"}:
            if filtered:
                selected = (selected - 1) % len(filtered)
                selected_id = str(filtered[selected].get("id", ""))
        elif key in {"DOWN", "RIGHT"}:
            if filtered:
                selected = (selected + 1) % len(filtered)
                selected_id = str(filtered[selected].get("id", ""))
        elif key == "ENTER":
            if not filtered:
                continue
            exact = next((item for item in filtered if str(item.get("id", "")).casefold() == query.casefold()), None)
            if exact:
                return exact
            if title == "Available Models" and query:
                candidate = str(filtered[0].get("id", ""))
                if candidate.casefold().startswith(query.casefold()) and len(candidate) > len(query):
                    query = candidate
                    selected_id = candidate
                    continue
            return filtered[selected]
        elif key == "CANCEL":
            return None
        elif key == "BACKSPACE":
            query = query[:-1]
            selected_id = ""
        elif key.startswith("CHAR:"):
            character = key[5:]
            if not query and character == "Q":
                return None
            query += character
            selected_id = ""


def _session_item(
    session: dict[str, object],
    fallback_context_window: int | None = None,
) -> dict[str, object]:
    updated = str(session.get("updated_at", ""))
    messages = session.get("messages", [])
    messages = messages if isinstance(messages, list) else []
    window = session.get("context_window")
    if not isinstance(window, int) or window < 1:
        window = fallback_context_window
    context = f"~{_context_percentage(messages, window)}%" if isinstance(window, int) and window > 0 else "unknown context"
    title = str(session.get("title") or "Session")
    project = display_home_paths(_session_workspace_path(session))
    return {
        "id": str(session["id"]),
        "title": title,
        "label": f"{title} · Context {context} · {len(messages)} messages · {updated[:16].replace('T', ' ')} · {project}",
        "session": session,
    }


def _sessions_panel_rows(
    sessions: list[dict[str, object]],
    fallback_context_window: int | None,
) -> list[str]:
    if not sessions:
        return ["No saved sessions."]
    rows: list[str] = []
    for item in sorted(sessions, key=lambda value: str(value.get("title", "")).casefold()):
        messages = item.get("messages", [])
        messages = messages if isinstance(messages, list) else []
        window = item.get("context_window")
        if not isinstance(window, int) or window < 1:
            window = fallback_context_window
        if isinstance(window, int) and window > 0:
            context = f"~{_context_percentage(messages, window)}% of {window:,}"
        else:
            context = "unknown"
        updated = str(item.get("updated_at") or item.get("created_at") or "")[:10] or "unknown date"
        title = " ".join(str(item.get("title") or "Untitled").split())
        rows.append(f"{title} — Context {context} — {updated}")
    return rows


def _directory_browser_root(session: dict[str, object]) -> Path:
    saved_root = _session_workspace_root(session)
    if saved_root != WORKSPACE_ROOT.resolve():
        return saved_root
    return Path.cwd().resolve()


def _directory_tree_nodes(root: Path, *, max_depth: int = 5, max_nodes: int = 500) -> list[dict[str, object]]:
    base = root.expanduser().resolve()
    nodes: list[dict[str, object]] = [{
        "id": str(base),
        "path": str(base),
        "directory": True,
        "depth": 0,
        "tree": (base.name or str(base)) + "/",
    }]
    truncated = False

    def visit(directory: Path, depth: int, prefix: str) -> None:
        nonlocal truncated
        if depth >= max_depth or truncated:
            return
        try:
            entries = list(directory.iterdir())
        except OSError:
            return
        ignored_directories = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache"}

        def entry_key(entry: Path) -> tuple[bool, str]:
            try:
                is_directory = entry.is_dir() and not entry.is_symlink()
            except OSError:
                is_directory = False
            return not is_directory, entry.name.casefold()

        entries.sort(key=entry_key)
        for index, entry in enumerate(entries):
            if entry.name in ignored_directories and entry.is_dir() and not entry.is_symlink():
                continue
            if len(nodes) >= max_nodes:
                truncated = True
                return
            try:
                is_directory = entry.is_dir() and not entry.is_symlink()
                resolved = entry.resolve()
                if resolved != base and base not in resolved.parents:
                    continue
            except OSError:
                is_directory = False
                resolved = entry
            is_last = index + 1 == len(entries)
            branch = "└── " if is_last else "├── "
            relative_tree = prefix + branch + entry.name + ("/" if is_directory else "")
            node = {
                "id": str(resolved),
                "path": str(resolved),
                "directory": is_directory,
                "depth": depth + 1,
                "tree": relative_tree,
            }
            nodes.append(node)
            if is_directory:
                visit(resolved, depth + 1, prefix + ("    " if is_last else "│   "))
                if truncated:
                    return

    visit(base, 0, "")
    if truncated:
        nodes.append({"id": "__truncated__", "directory": False, "depth": max_depth, "tree": "… more entries omitted"})
    return nodes


def _pick_directory_tree(
    session: dict[str, object],
    draw,
    root: Path,
    current: Path,
) -> Path | None:
    """Render a compact, responsive tree picker matching the terminal reference."""
    root = root.expanduser().resolve()
    nodes = _directory_tree_nodes(root)
    directory_positions = [index for index, node in enumerate(nodes) if node.get("directory") is True]
    if not directory_positions:
        return None
    current_path = current.expanduser().resolve()
    selected = next(
        (position for position, node_index in enumerate(directory_positions)
         if position > 0 and nodes[node_index].get("path") == str(current_path)),
        min(1, len(directory_positions) - 1),
    )

    while True:
        size = shutil.get_terminal_size((80, 24))
        selected_node_index = directory_positions[selected]
        # The active directory is shown in the heading; list its descendants only.
        tree_indices = list(range(1, len(nodes)))
        selected_display_index = max(0, selected_node_index - 1)
        visible_count = max(1, min(24, size.lines - 8))
        start = max(0, min(selected_display_index - visible_count // 2, max(0, len(tree_indices) - visible_count)))
        visible_indices = tree_indices[start : start + visible_count]
        inner = max(1, min(intelligent_ui.card_width(size.columns), size.columns - 6))
        rows: list[str] = []
        for index in visible_indices:
            node = nodes[index]
            tree = str(node.get("tree", ""))
            if node.get("id") == "__truncated__":
                rows.append(paint("  " + _truncate_display(tree, inner - 2), MUTED))
                continue
            is_selected = node.get("directory") is True and index == selected_node_index
            prefix = "› " if is_selected else "  "
            plain = _truncate_display(prefix + tree, max(1, inner - 1))
            if is_selected:
                rows.append(paint(" " + plain, "\033[48;5;208;38;5;16m"))
            else:
                rows.append(paint(" " + plain, MUTED))
        footer = "↑↓ move · ← parent · → child · Enter select · Esc back"
        # Keep the reference's path / command heading, thin separators and muted footer.
        panel = [paint("  ╭" + "─" * (inner + 2) + "╮", BORDER)]
        heading_left = _truncate_display(root.name or str(root), max(1, inner // 2 - 2))
        heading_right = " / change directory"
        heading = _truncate_display(heading_left + heading_right, inner)
        panel.append(
            paint("  │", BORDER)
            + paint(" " + heading[:len(heading_left)], MUTED)
            + paint(heading[len(heading_left):], WHITE)
            + " " * max(0, inner - display_width(heading))
            + " " + paint("│", BORDER)
        )
        panel.append(paint("  ├" + "─" * (inner + 2) + "┤", BORDER))
        for row in rows:
            # Rows already carry their text color; only pad them to the live terminal width.
            plain = re.sub(r"\033\[[0-9;]*m", "", row)
            padding = max(0, inner - display_width(plain))
            panel.append(paint("  │", BORDER) + row + " " * padding + " " + paint("│", BORDER))
        panel.append(paint("  ├" + "─" * (inner + 2) + "┤", BORDER))
        panel.append(paint("  │", BORDER) + paint(" " + _truncate_display(footer, inner).ljust(inner) + " ", MUTED) + paint("│", BORDER))
        panel.append(paint("  ╰" + "─" * (inner + 2) + "╯", BORDER))
        draw("", 0, panel_rows=panel, placeholder="Choose a directory from the tree")
        key = _read_key(timeout=0.2)
        if key == "TIMEOUT":
            continue
        if key in {"UP", "LEFT"}:
            if key == "UP":
                selected = (selected - 1) % len(directory_positions)
            else:
                parent = Path(str(nodes[selected_node_index].get("path", root))).parent
                selected = next(
                    (position for position, node_index in enumerate(directory_positions) if nodes[node_index].get("path") == str(parent)),
                    selected,
                )
        elif key in {"DOWN", "RIGHT"}:
            if key == "DOWN":
                selected = (selected + 1) % len(directory_positions)
            else:
                selected_path = Path(str(nodes[selected_node_index].get("path", root)))
                selected = next(
                    (position for position, node_index in enumerate(directory_positions)
                     if Path(str(nodes[node_index].get("path", root))).parent == selected_path),
                    selected,
                )
        elif key == "ENTER":
            return Path(str(nodes[directory_positions[selected]].get("path", root)))
        elif key in {"CANCEL", "DOUBLE_CANCEL"} or key.startswith("CHAR:") and key[5:].casefold() == "q":
            return None


def _workspace_digest(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(path.read_bytes())
    else:
        for item in sorted(path.rglob("*"), key=lambda entry: entry.relative_to(path).as_posix()):
            digest.update(item.relative_to(path).as_posix().encode("utf-8", errors="replace"))
            if item.is_symlink():
                digest.update(b"symlink")
                digest.update(os.readlink(item).encode("utf-8", errors="replace"))
            elif item.is_file():
                with item.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(64 * 1024), b""):
                        digest.update(chunk)
    return digest.hexdigest()


def _map_reference_through_moves(reference: str, later_changes: list[dict[str, object]]) -> str:
    location = Path(reference)
    for change in later_changes:
        if change.get("kind") not in {"move_file", "move_folder"}:
            continue
        source_value = change.get("path")
        destination_value = change.get("destination")
        if not isinstance(source_value, str) or not isinstance(destination_value, str):
            continue
        source = Path(source_value)
        destination = Path(destination_value)
        try:
            relative = location.relative_to(source)
        except ValueError:
            continue
        if change.get("kind") == "move_file" and relative != Path("."):
            continue
        location = destination / relative
    return str(location)


def _refresh_undo_digests(changes: list[dict[str, object]], config: dict[str, object] | None = None) -> None:
    for index, change in enumerate(changes):
        kind = str(change.get("kind", ""))
        reference = change.get("destination") if kind in {"move_file", "move_folder"} else change.get("path")
        if kind not in {"created_folder", "created_file_digest", "move_file", "move_folder"} or not isinstance(reference, str):
            continue
        try:
            mapped = _map_reference_through_moves(reference, changes[index + 1 :])
            path = _workspace_file(mapped, allow_root=True, config=config)
            if path.exists():
                change["after_digest"] = _workspace_digest(path)
        except (OSError, RuntimeError, ValueError):
            continue


def _restore_workspace_changes(
    changes: list[dict[str, object]],
    config: dict[str, object] | None = None,
) -> str:
    plans: list[tuple[Path, Path | None, dict[str, object]]] = []
    conflicts: list[str] = []
    for index, change in enumerate(changes):
        relative = str(change.get("path", ""))
        kind = str(change.get("kind", "write_file"))
        path = _workspace_file(relative, allow_root=True, config=config)
        destination_value = change.get("destination")
        destination = _workspace_file(str(destination_value), allow_root=True, config=config) if isinstance(destination_value, str) else None
        later_changes = changes[index + 1 :]
        try:
            if kind in {"write_file", "created_file"}:
                after = change.get("after")
                current_path = _workspace_file(
                    _map_reference_through_moves(relative, later_changes),
                    allow_root=True,
                    config=config,
                )
                current = current_path.read_bytes() if current_path.is_file() else None
                deleted_later = any(
                    item.get("kind") == "delete_file"
                    and item.get("path") == _map_reference_through_moves(relative, later_changes)
                    for item in later_changes
                )
                if (not isinstance(after, bytes) or current != after) and not deleted_later:
                    conflicts.append(relative)
            elif kind == "delete_file":
                current_reference = _map_reference_through_moves(relative, later_changes)
                current_path = _workspace_file(current_reference, allow_root=True, config=config)
                recreated_later = any(item.get("path") == current_reference and item.get("before_exists") is False for item in later_changes)
                if current_path.exists() and not recreated_later:
                    conflicts.append(relative)
                if not isinstance(change.get("before"), bytes):
                    raise RuntimeError(f"Undo data for {relative} is invalid.")
            elif kind == "created_file_digest":
                if not path.is_file() or _workspace_digest(path) != change.get("after_digest"):
                    conflicts.append(relative)
            elif kind == "created_folder":
                if not path.is_dir() or _workspace_digest(path) != change.get("after_digest"):
                    conflicts.append(relative)
            elif kind in {"move_file", "move_folder"}:
                mapped_destination = _workspace_file(
                    _map_reference_through_moves(str(destination_value or ""), later_changes),
                    allow_root=True,
                    config=config,
                ) if isinstance(destination_value, str) else None
                deleted_after_move = any(
                    item.get("kind") == "delete_file"
                    and item.get("path") == _map_reference_through_moves(str(destination_value or ""), later_changes)
                    for item in later_changes
                )
                if destination is None or path.exists() or (
                    (mapped_destination is None or not mapped_destination.exists())
                    and not deleted_after_move
                ) or (
                    mapped_destination is not None
                    and mapped_destination.exists()
                    and _workspace_digest(mapped_destination) != change.get("after_digest")
                ):
                    conflicts.append(relative)
            else:
                raise RuntimeError(f"Undo data for {relative} has an unsupported operation.")
        except OSError as error:
            raise RuntimeError(f"Could not check {relative} before undo: {error}") from None
        plans.append((path, destination, change))
    if conflicts:
        raise RuntimeError(
            "Undo stopped because these paths changed after the Agent edit or operation: "
            + ", ".join(conflicts)
            + ". Review them manually to avoid overwriting newer work."
        )

    restored = 0
    for path, destination, change in reversed(plans):
        kind = str(change.get("kind", "write_file"))
        if kind in {"write_file", "created_file"}:
            if change.get("before_exists") is not True:
                path.unlink()
            else:
                before = change.get("before")
                if not isinstance(before, bytes):
                    raise RuntimeError(f"Undo data for {path} is invalid.")
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix=".sophon-undo-", dir=path.parent)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(before)
                    mode = change.get("before_mode")
                    if isinstance(mode, int):
                        os.chmod(temporary, mode)
                    os.replace(temporary, path)
                except BaseException:
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass
                    raise
        elif kind == "delete_file":
            before = change.get("before")
            assert isinstance(before, bytes)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(before)
            mode = change.get("before_mode")
            if isinstance(mode, int):
                os.chmod(path, mode)
        elif kind in {"created_file_digest", "created_folder"}:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        elif kind in {"move_file", "move_folder"}:
            assert destination is not None
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(destination), str(path))
            mode = change.get("before_mode")
            if kind == "move_file" and isinstance(mode, int):
                os.chmod(path, mode)
        restored += 1
    return f"Undid the last Agent operation in {restored} file/path{'s' if restored != 1 else ''}."


def _change_diff(changes: list[dict[str, object]], max_chars: int = 24_000) -> str:
    sections: list[str] = []
    for change in changes:
        relative = str(change.get("path", "unknown"))
        before = change.get("before") if change.get("before_exists") is True else b""
        after = change.get("after")
        if not isinstance(before, bytes) or not isinstance(after, bytes):
            continue
        old_text = before.decode("utf-8", errors="replace")
        new_text = after.decode("utf-8", errors="replace")
        if old_text == new_text:
            continue
        diff = "\n".join(difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile=f"a/{relative}" if change.get("before_exists") is True else "/dev/null",
            tofile=f"b/{relative}",
            lineterm="",
        ))
        if diff:
            sections.append(diff)
    output = "\n\n".join(sections)
    if len(output) > max_chars:
        output = output[:max_chars] + "\n[Diff truncated to keep the review request bounded.]"
    return output


def _review_changes(
    config: dict[str, object],
    task: str,
    changes: list[dict[str, object]],
    usage_out: dict[str, object] | None = None,
) -> str:
    diff = _change_diff(changes)
    if not diff:
        raise RuntimeError("The last Agent edit contains no text changes to review.")
    return _request(
        config,
        [
            {
                "role": "system",
                "content": (
                    "Review the provided code diff for concrete bugs, regressions, security issues, and missing edge cases. "
                    "Do not edit files or call tools. Report only actionable findings, ordered by severity, with file and line "
                    "references where possible. If you find none, say no actionable issue was found and note that this is a "
                    "static review, not a test run."
                ),
            },
            {
                "role": "user",
                "content": f"Original task:\n{task or '(not available)'}\n\nReview this last Agent change:\n```diff\n{diff}\n```",
            },
        ],
        tools_enabled=False,
        usage_out=usage_out,
    )


def _provider_limit_rows(config: dict[str, object]) -> list[str]:
    if config.get("provider") != "openrouter":
        return [
            "Cloudflare Workers AI: 10,000 free Neurons/day; check current use in the Workers AI dashboard.",
            "Remaining quota is not available from the inference token; daily allocation resets at 00:00 UTC.",
        ]
    request = Request(
        "https://openrouter.ai/api/v1/key",
        headers={"Authorization": f"Bearer {_auth_token(config)}"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        if error.code == 401:
            return ["OpenRouter limits unavailable: the saved credential was rejected; verify it with `sophon setup`."]
        return [f"OpenRouter limits unavailable (HTTP {error.code})."]
    except (OSError, TimeoutError, URLError, UnicodeError, json.JSONDecodeError, ValueError):
        return ["OpenRouter limits unavailable; check your network and try /usage again."]
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return ["OpenRouter did not return current-key limit data."]
    rows: list[str] = []
    remaining = data.get("limit_remaining")
    limit = data.get("limit")
    if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
        rows.append(f"OpenRouter key credit limit remaining: ${remaining:,.2f}.")
    elif limit is None:
        rows.append("OpenRouter per-key credit cap: none reported.")
    budget = data.get("effective_budget")
    if isinstance(budget, dict) and isinstance(budget.get("remaining_usd"), (int, float)):
        row = f"Effective budget remaining: ${budget['remaining_usd']:,.2f}"
        resets_at = budget.get("resets_at")
        if isinstance(resets_at, str) and resets_at:
            row += f" · resets {resets_at}"
        rows.append(row + ".")
    free_requests = data.get("free_model_daily_requests")
    if isinstance(free_requests, dict):
        left = free_requests.get("remaining")
        maximum = free_requests.get("limit")
        if isinstance(left, int) and isinstance(maximum, int):
            rows.append(f"Free-model requests remaining today: {left} of {maximum}.")
    if not rows:
        rows.append("OpenRouter returned no per-key remaining-limit fields.")
    return rows


def _usage_panel_rows(session: dict[str, object], config: dict[str, object]) -> list[str]:
    usage = session.get("usage")
    if not isinstance(usage, dict):
        usage = _fresh_usage()
    requests = usage.get("requests", 0)
    prompt = usage.get("prompt_tokens", 0)
    completion = usage.get("completion_tokens", 0)
    total = usage.get("total_tokens", 0)
    requests = requests if isinstance(requests, int) else 0
    prompt = prompt if isinstance(prompt, int) else 0
    completion = completion if isinstance(completion, int) else 0
    total = total if isinstance(total, int) else 0
    token_note = " (some counts estimated)" if usage.get("estimated") is True else ""
    return [
        f"Model requests in this session: {requests:,}.",
        f"Tokens: {total:,} total · {prompt:,} prompt · {completion:,} completion{token_note}.",
        "",
        *_provider_limit_rows(config),
    ]


def _history_display_text(value: object, limit: int = 400) -> str:
    text = str(value)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = "".join(character for character in text if character.isprintable() or character in "\t ")
    return text[:limit] + ("…" if len(text) > limit else "")


def _history_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _history_panel(session: dict[str, object], draw) -> None:
    stored = session.get("activity_log")
    events = list(reversed(stored[-250:])) if isinstance(stored, list) else []
    selected = 0
    while True:
        if not events:
            body = ["No tool calls have been recorded for this session yet.", "", "↑/↓ browse · Esc return"]
            panel = _card_rows(
                "Agent History",
                body,
                columns=shutil.get_terminal_size((80, 24)).columns,
                top_left="This session",
                top_right="0 of 0",
            )
        else:
            event = events[selected]
            arguments = event.get("arguments") if isinstance(event.get("arguments"), dict) else {}
            argument_rows = [
                f"{_history_display_text(key, 60)}: {_history_display_text(value, 140)}"
                for key, value in arguments.items()
            ]
            changes = event.get("changes") if isinstance(event.get("changes"), list) else []
            change_rows = []
            for change in changes:
                if not isinstance(change, dict):
                    continue
                change_rows.append(
                    "  "
                    + _history_display_text(change.get("operation", "updated"), 20)
                    + " · "
                    + _history_display_text(change.get("path", "unknown file"), 160)
                    + f" · +{_history_int(change.get('added_lines'))}/-{_history_int(change.get('removed_lines'))} lines"
                    + f" · {_history_int(change.get('before_bytes'))}→{_history_int(change.get('after_bytes'))} bytes"
                )
            event_time = _history_display_text(event.get("timestamp", "Unknown time"), 40).replace("T", " ")
            status = _history_display_text(event.get("status", "ok"), 20).upper()
            body = [
                f"Time: {event_time}",
                f"Tool: {_history_display_text(event.get('tool', 'unknown'), 80)} · {status}",
                f"Duration: {_history_int(event.get('duration_ms'))} ms",
                "",
                "Arguments:",
                *(argument_rows or ["  No arguments"]),
                "",
                "Result:",
                _history_display_text(event.get("summary", "No result summary"), 400),
            ]
            if changes:
                body.extend(["", "File changes:", *(change_rows or ["  No file change recorded"])])
            body.extend(["", "↑/↓ older/newer · Esc return"])
            panel = _card_rows(
                "Agent History",
                body,
                columns=shutil.get_terminal_size((80, 24)).columns,
                top_left="This session",
                top_right=f"{selected + 1} of {len(events)}",
                border_color=NOTICE_BORDER if status == "ERROR" else BORDER,
            )
        draw("", 0, panel_rows=panel, placeholder="Browse this session's activity")
        key = _read_key(timeout=0.2)
        if key == "TIMEOUT":
            continue
        if key in {"CANCEL", "ENTER"} or key.startswith("CHAR:") and key[5:].casefold() == "q":
            return
        if events and key == "UP":
            selected = min(len(events) - 1, selected + 1)
        elif events and key == "DOWN":
            selected = max(0, selected - 1)
        elif events and key == "LEFT":
            selected = len(events) - 1
        elif events and key == "RIGHT":
            selected = 0


def _validated_questions(arguments: object) -> list[dict[str, object]]:
    payload = _decoded_tool_arguments(arguments)
    raw_questions = payload.get("questions")
    if not isinstance(raw_questions, list) or not 1 <= len(raw_questions) <= 6:
        raise RuntimeError("ask requires between one and six questions.")
    questions: list[dict[str, object]] = []
    for item in raw_questions:
        if not isinstance(item, dict) or not isinstance(item.get("question"), str) or not item["question"].strip():
            raise RuntimeError("Each ask item needs a non-empty question.")
        question = _history_display_text(item["question"], 1200)
        raw_options = item.get("options", [])
        if not isinstance(raw_options, list) or len(raw_options) > 3 or any(not isinstance(option, str) or not option.strip() for option in raw_options):
            raise RuntimeError("Each ask question may contain up to three non-empty preset answers.")
        questions.append({
            "question": question,
            "options": [_history_display_text(option, 500) for option in raw_options],
        })
    return questions


def _questions_panel(
    arguments: object,
    draw,
    key_events: queue.Queue[str],
    answering: dict[str, object],
    cancellation: RequestCancellation | None = None,
) -> str | None:
    questions = _validated_questions(arguments)
    responses: list[dict[str, object]] = []
    frame = int(answering.get("frame", 0))
    last_escape_at: float | None = None
    for question_index, item in enumerate(questions, 1):
        question = str(item["question"])
        options = item["options"]
        input_text = ""
        while True:
            body = [f"Q: {question}", ""]
            body.extend(f"{chr(65 + index)}) {option}" for index, option in enumerate(options))
            if options:
                body.append("")
            body.extend([
                "S) Skip this question",
                "",
                "Choose A–C for a preset, or type a custom answer.",
                "Press Enter to continue to the next question.",
            ])
            panel = _card_rows(
                "Questions",
                body,
                columns=shutil.get_terminal_size((80, 24)).columns,
                top_right=f"{question_index} of {len(questions)}",
            )
            draw(input_text, len(input_text), panel_rows=panel, placeholder="Type an answer, or enter A–C/S then Enter")
            try:
                key = key_events.get(timeout=ANIMATION_TICK)
            except queue.Empty:
                frame += 1
                answering["frame"] = frame
                continue
            if key in {"CANCEL", "DOUBLE_CANCEL"}:
                now = time.monotonic()
                if key == "DOUBLE_CANCEL" or (last_escape_at is not None and now - last_escape_at <= 1.5):
                    if cancellation is not None:
                        cancellation.cancel()
                    raise AgentInterrupted("Response interrupted by user.")
                last_escape_at = now
                answering["interrupt_warning"] = True
                continue
            if key == "BACKSPACE":
                input_text = input_text[:-1]
            elif key == "ENTER":
                shortcut = input_text.strip().casefold()
                if len(shortcut) == 1 and shortcut in {chr(97 + index) for index in range(len(options))}:
                    response = options[ord(shortcut) - 97]
                    skipped = False
                elif shortcut == "s":
                    response = ""
                    skipped = True
                elif input_text.startswith("="):
                    response = input_text[1:].strip()
                    skipped = False
                else:
                    response = input_text.strip()
                    skipped = False
                responses.append({"question": question, "answer": response, "skipped": skipped})
                break
            elif key.startswith("CHAR:"):
                value = key[5:]
                input_text = (input_text + value)[:1000]
            elif key in {"INSERT_LINE", "DELETE_LINE", "DELETE", "UP", "DOWN", "LEFT", "RIGHT", "TOGGLE_REASONING"}:
                continue
            frame += 1
            answering["frame"] = frame

    summary_rows = ["Review the responses before sending them to the Agent.", ""]
    for index, response in enumerate(responses, 1):
        summary_rows.append(f"Q{index}: {response['question']}")
        summary_rows.append("  Skipped" if response["skipped"] else f"  A: {response['answer'] or '(blank)'}")
    summary_rows.extend(["", "Press Enter to submit all answers · Esc to cancel"])
    summary = _card_rows(
        "Questions",
        summary_rows,
        columns=shutil.get_terminal_size((80, 24)).columns,
        top_right=f"{len(questions)} of {len(questions)}",
    )
    while True:
        draw("", 0, panel_rows=summary, placeholder="Press Enter to send the answers")
        try:
            key = key_events.get(timeout=ANIMATION_TICK)
        except queue.Empty:
            continue
        if key == "ENTER":
            return "User responses to the clarification questions:\n" + json.dumps(responses, ensure_ascii=False)
        if key in {"CANCEL", "DOUBLE_CANCEL"}:
            now = time.monotonic()
            if key == "DOUBLE_CANCEL" or (last_escape_at is not None and now - last_escape_at <= 1.5):
                if cancellation is not None:
                    cancellation.cancel()
                raise AgentInterrupted("Response interrupted by user.")
            last_escape_at = now
            answering["interrupt_warning"] = True


def _interactive(config: dict[str, object]) -> int:
    global PROJECT_DIR
    try:
        sessions = _load_sessions()
    except RuntimeError as error:
        card("Session storage error", [str(error)], border_color=NOTICE_BORDER, text_color=NOTICE_TEXT)
        return 1
    session = _new_session()
    last_changes_by_session: dict[str, list[dict[str, object]]] = {}
    notice_rows: list[str] = []
    display_message_start = 0
    pending_prompts: list[str] = []
    prefill_characters: list[str] = []
    context_window = config.get("context_window")
    context_state: dict[str, object] = {
        "model": str(config["model"]),
        "window": context_window if isinstance(context_window, int) and context_window > 0 else None,
        "discovery_attempted": isinstance(context_window, int) and context_window > 0,
    }
    if context_state["window"] is None:
        threading.Thread(
            target=_discover_context_window,
            args=(config.copy(), context_state),
            daemon=True,
        ).start()
    answering = {"active": False, "frame": 0, "text": "", "reasoning": "", "planning": "", "planning_until": 0.0, "reasoning_expanded": False}

    def draw(
        text: str,
        cursor: int,
        *,
        placeholder: str = INPUT_PLACEHOLDER,
        panel_rows: list[str] | None = None,
        suggestion: str = "",
    ) -> None:
        reasoning_preferences = _reasoning_settings(config)
        if not reasoning_preferences["is_support"]:
            reasoning_state = "not supported"
        elif reasoning_preferences["enabled"]:
            reasoning_state = str(reasoning_preferences["effort"])
        else:
            reasoning_state = "off"
        _draw_screen(
            session,
            text,
            cursor,
            model=str(config["model"]),
            placeholder=placeholder,
            panel_rows=panel_rows,
            notice_rows=notice_rows,
            suggestion=suggestion,
            answering=bool(answering["active"]),
            animation_frame=int(answering["frame"]),
            streaming_text=str(answering["text"]),
            reasoning_text=str(answering["reasoning"]),
            planning_text=(
                str(answering.get("planning", ""))
                if time.monotonic() < float(answering.get("planning_until", 0.0))
                else ""
            ),
            context_window=context_state["window"] if isinstance(context_state["window"], int) else None,
            mode=str(session.get("mode", "chat")),
            reasoning_state=reasoning_state,
            reasoning_expanded=bool(answering.get("reasoning_expanded")),
            display_message_start=display_message_start,
            interrupt_warning=bool(answering.get("interrupt_warning")),
        )

    def notice(title: str, message: str, *, error: bool = False) -> None:
        nonlocal notice_rows
        notice_rows = _card_rows(
            title,
            [message],
            columns=shutil.get_terminal_size((80, 24)).columns,
            border_color=NOTICE_BORDER if error else BORDER,
            text_color=NOTICE_TEXT if error else WHITE,
        )

    def notice_plain(message: str) -> None:
        nonlocal notice_rows
        notice_rows = _card_rows(
            None,
            [message],
            columns=shutil.get_terminal_size((80, 24)).columns,
            width=display_width(message),
        )

    def toggle_saved_reasoning() -> None:
        nonlocal notice_rows
        message = next(
            (
                item
                for item in reversed(session["messages"])
                if item.get("role") == "assistant"
                and isinstance(item.get("reasoning"), str)
                and str(item.get("reasoning", "")).strip()
            ),
            None,
        )
        if message is None:
            notice("Reasoning details", "No saved reasoning details are available in this session.")
        else:
            message["reasoning_expanded"] = message.get("reasoning_expanded") is not True
            if save_current():
                notice_rows = []

    def close_with_card() -> None:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        card(None, ["Session closed. See you soon."])

    def save_current() -> bool:
        try:
            session["model"] = str(config["model"])
            session["workspace_dir"] = str(PROJECT_DIR.resolve())
            window = context_state.get("window")
            if isinstance(window, int) and window > 0:
                session["context_window"] = window
            _save_session(sessions, session)
            return True
        except OSError as error:
            notice("Session storage error", str(error), error=True)
            return False

    def refresh_sessions_after_tools() -> None:
        nonlocal sessions
        try:
            refreshed = _load_sessions()
        except RuntimeError:
            return
        for index, saved in enumerate(refreshed):
            if saved.get("id") == session.get("id"):
                session["title"] = saved.get("title", session.get("title"))
                session["updated_at"] = saved.get("updated_at", session.get("updated_at"))
                refreshed[index] = session
                break
        sessions = refreshed

    def save_reasoning_preferences(preferences: dict[str, object]) -> None:
        updated_config = config.copy()
        updated_config["reasoning"] = preferences
        write_config(updated_config)
        config.update(updated_config)

    def known_context_window(*, discover: bool = False) -> int | None:
        window = context_state.get("window")
        if not isinstance(window, int) and discover and context_state.get("discovery_attempted") is not True:
            _discover_context_window(config.copy(), context_state)
            window = context_state.get("window")
        return window if isinstance(window, int) and window > 0 else None

    def maybe_auto_compress() -> None:
        nonlocal display_message_start
        enabled, threshold = auto_compression_settings(config)
        messages = session["messages"]
        if len(messages) < 2 or not enabled:
            return
        window = known_context_window(discover=True)
        if window is None or _context_percentage(messages, window) < threshold:
            return
        before = _context_percentage(messages, window)
        compression_usage = _fresh_usage()
        notice("Auto-compression", "Summarizing earlier conversation…")
        draw("", 0)
        try:
            compressed, _ = _compact_history(config, messages, window, usage_out=compression_usage)
        except (RuntimeError, OSError, ValueError) as error:
            _merge_session_usage(session, compression_usage)
            save_current()
            notice("Auto-compression failed", str(error), error=True)
            return
        _merge_session_usage(session, compression_usage)
        messages[:] = compressed
        display_message_start = min(display_message_start, len(messages))
        if save_current():
            after = _context_percentage(messages, window)
            notice_plain(f"Context auto-compressed · ~{before}% to ~{after}%")

    while True:
        try:
            if pending_prompts:
                prompt = pending_prompts.pop(0)
            else:
                prompt = _input_bar(draw, prefill="".join(prefill_characters))
                prefill_characters.clear()
        except AgentExitToTUI:
            return EXIT_TO_TUI
        except ToggleReasoningView as error:
            prefill_characters[:] = list(error.input_text)
            toggle_saved_reasoning()
            continue
        except (EOFError, KeyboardInterrupt):
            close_with_card()
            return 0
        if not prompt:
            notice_rows = []
            continue
        if prompt.casefold() == "q":
            close_with_card()
            return 0
        if prompt.startswith("/"):
            command, _, argument = prompt[1:].partition(" ")
            command = command.casefold()
            argument = argument.strip()
            notice_rows = []
            if command in {"exit", "quit"}:
                close_with_card()
                return 0
            if command == "help":
                notice_rows = _card_rows(
                    "Agent Commands",
                    [
                        "/auto-compression on|off — Toggle automatic mode",
                        "/auto-update on|off — Toggle automatic startup updates",
                        "/cd [path] — Browse current directory, up to five levels deep",
                        "/changelog — Show latest changes",
                        "/clear — Clear the screen, keep this session",
                        "/compact — Summarize context at 50% or more",
                        "Ctrl+R — Toggle reasoning details",
                        "/cwd — Show current directory",
                        "/delete — Delete a saved session",
                        "/exit or Q — Close Sophon Agent",
                        "/fork — Fork a saved session",
                        "/help — Show available commands",
                        "/history — Inspect this session's tool and file-change log",
                        "/mode plan|build|chat — Set the Agent mode",
                        "/model [search] — Choose a model",
                        "/new — Start a new session",
                        "/reasoning on|off — Toggle model reasoning",
                        "/reasoning-effort low|medium|high — Set effort",
                        "/rename [title] — Rename this session",
                        "/resume — Load a saved session",
                        "/review — Review the last Agent file changes",
                        "/sessions — Browse and open saved sessions",
                        "/threshold 30–95 — Set auto-compression threshold",
                        "/undo — Revert the last Agent file edits",
                        "/usage — Show session tokens and provider limits",
                    ],
                    columns=shutil.get_terminal_size((80, 24)).columns,
                    text_color=MUTED,
                    highlight_terms=("Agent Commands", "/auto-compression", "/auto-update", "/cd", "/changelog", "/clear", "/compact", "Ctrl+R", "/cwd", "/delete", "/exit", "/fork", "/help", "/history", "/mode", "/model", "/new", "/reasoning", "/rename", "/resume", "/review", "/sessions", "/threshold", "/undo", "/usage"),
                )
            elif command == "changelog":
                updated = VERSION.split(" – ", 1)[0]
                notice_rows = _card_rows(
                    "Sophon Changelog",
                    [
                        f"Version: {VERSION}",
                        f"Last updated: {updated}",
                        "",
                        "FEATURES",
                        "  /cd five levels deep · /history activity log · /undo safe rollback",
                        "  /review change review · /usage token and provider limits",
                        "  /sessions, /mode, /threshold and /cwd workspace controls",
                        "",
                        "INTERFACE",
                        "  Responsive terminal cards with bright headings and muted body text",
                        "  Keyboard-driven provider, model and session selection",
                        "  Masked credentials with Keep saved key / New key choices",
                        "  Inline completion in the main interface and Agent",
                        "  Tap Reasoning to expand details; aligned faster animations",
                        "",
                        "RELIABILITY",
                        "  Setup runs before the first interactive launch when needed",
                        "  Existing settings can be kept or reconfigured once; updates do not repeat the prompt",
                        "  Session-specific project directories and context management",
                        "  Read-only planning, build approval and 30–95% auto-compression",
                        "  double-Esc interruption and clearer provider errors",
                        "  argument-aware command completion and expandable reasoning details",
                    ],
                    columns=shutil.get_terminal_size((80, 24)).columns,
                    text_color=MUTED,
                    highlight_terms=("Sophon Changelog", "Version:", "Last updated:", "FEATURES", "INTERFACE", "RELIABILITY", "/cd", "/history", "/undo", "/review", "/usage", "/sessions", "/mode", "/threshold", "/cwd"),
                )
            elif command == "cd":
                if not save_current():
                    continue
                old_directory = PROJECT_DIR
                old_root = str(session.get("workspace_root", WORKSPACE_ROOT))
                browser_root = _directory_browser_root(session)
                if argument:
                    try:
                        target_directory = _validated_workspace_directory(
                            argument,
                            relative_to=browser_root,
                            workspace_root=browser_root,
                            allow_root=True,
                        )
                        if len(target_directory.relative_to(browser_root).parts) > 5:
                            raise RuntimeError("Choose a directory no more than five levels below the current directory.")
                    except (OSError, RuntimeError, ValueError) as error:
                        notice("Directory unchanged", str(error), error=True)
                        continue
                else:
                    selected_directory = _pick_directory_tree(
                        session,
                        draw,
                        browser_root,
                        PROJECT_DIR,
                    )
                    if selected_directory is None:
                        notice("Directory selection cancelled", "The active project is unchanged.")
                        continue
                    try:
                        target_directory = _validated_workspace_directory(
                            selected_directory,
                            relative_to=browser_root,
                            workspace_root=browser_root,
                            allow_root=True,
                        )
                    except (OSError, RuntimeError, ValueError) as error:
                        notice("Directory unchanged", str(error), error=True)
                        continue
                PROJECT_DIR = target_directory
                session["workspace_dir"] = str(PROJECT_DIR)
                session["workspace_root"] = str(browser_root)
                if not save_current():
                    PROJECT_DIR = old_directory
                    session["workspace_dir"] = str(old_directory)
                    session["workspace_root"] = old_root
                    continue
                notice("Active project directory", display_home_paths(PROJECT_DIR))
            elif command == "cwd":
                title, message, error = _workspace_location(command)
                notice(title, message, error=error)
            elif command == "usage":
                notice_rows = _card_rows(
                    "Session Usage",
                    _usage_panel_rows(session, config),
                    columns=shutil.get_terminal_size((80, 24)).columns,
                )
            elif command == "history":
                _history_panel(session, draw)
                notice_rows = []
            elif command == "review":
                changes = last_changes_by_session.get(str(session.get("id")), [])
                if not changes:
                    notice("Change Review", "No recent Agent file changes are available to review.")
                    continue
                task = next(
                    (str(item.get("content", "")) for item in reversed(session["messages"]) if item.get("role") == "user"),
                    "",
                )
                review_usage = _fresh_usage()
                notice("Change Review", "Reviewing the last Agent diff…")
                draw("", 0)
                try:
                    review = _review_changes(config.copy(), task, changes, review_usage)
                except (RuntimeError, OSError, ValueError) as error:
                    _merge_session_usage(session, review_usage)
                    save_current()
                    notice("Review unavailable", str(error), error=True)
                else:
                    _merge_session_usage(session, review_usage)
                    save_current()
                    notice_rows = _card_rows(
                        "Change Review",
                        review.splitlines() or ["The reviewer returned no text."],
                        columns=shutil.get_terminal_size((80, 24)).columns,
                    )
            elif command == "undo":
                session_changes = last_changes_by_session.get(str(session.get("id")), [])
                if not session_changes:
                    notice("Undo unavailable", "No Agent file edit is available to undo.", error=True)
                    continue
                try:
                    result = _restore_workspace_changes(session_changes, config)
                except (RuntimeError, OSError) as error:
                    notice("Undo stopped", str(error), error=True)
                    continue
                last_changes_by_session.pop(str(session.get("id")), None)
                save_current()
                notice_plain(result)
            elif command == "auto-update":
                value = argument.casefold()
                if value not in {"on", "off"}:
                    status = "on" if config.get("auto_update") is True else "off"
                    next_state = "off" if status == "on" else "on"
                    notice_rows = _card_rows("Automatic updates", [f"Auto-update is {status}.", f"Use /auto-update {next_state} to {next_state}."], columns=shutil.get_terminal_size((80, 24)).columns, text_color=MUTED, highlight_terms=("Automatic updates", "Auto-update is", f"{status}."))
                    continue
                updated_config = config.copy()
                updated_config["auto_update"] = value == "on"
                try:
                    write_config(updated_config)
                except (OSError, ValueError) as error:
                    notice("Automatic updates", str(error), error=True)
                else:
                    config.update(updated_config)
                    next_state = "off" if value == "on" else "on"
                    notice_rows = _card_rows("Automatic updates", [f"Auto-update is {value}.", f"Use /auto-update {next_state} to {next_state}."], columns=shutil.get_terminal_size((80, 24)).columns, text_color=MUTED, highlight_terms=("Automatic updates", "Auto-update is", f"{value}."))
            elif command == "sessions":
                if not save_current():
                    continue
                session_items = sorted(
                    sessions,
                    key=lambda item: str(item.get("title", "")).casefold(),
                )
                selected_session = _pick_item(
                    session,
                    draw,
                    [_session_item(item, known_context_window()) for item in session_items],
                    title="Saved Sessions",
                    current_id=str(session["id"]),
                )
                if selected_session:
                    session = selected_session["session"]
                    _activate_session_workspace(session)
                    display_message_start = 0
                    notice_rows = []
                else:
                    notice("Session selection cancelled", "The current session is still open.")
            elif command == "new":
                workspace_root = _session_workspace_root(session)
                session = _new_session()
                session["workspace_root"] = str(workspace_root)
                session["workspace_dir"] = str(PROJECT_DIR.resolve())
                display_message_start = 0
                notice_plain("New session created.")
            elif command == "clear":
                display_message_start = len(session["messages"])
                notice_rows = []
                sys.stdout.write("\033[2J\033[H")
                sys.stdout.flush()
            elif command == "rename":
                if argument:
                    title = argument
                else:
                    try:
                        title = _input_bar(draw, "Enter a session title")
                    except AgentExitToTUI:
                        return EXIT_TO_TUI
                    except ToggleReasoningView as error:
                        prefill_characters[:] = list(error.input_text)
                        toggle_saved_reasoning()
                        continue
                if title.strip():
                    session["title"] = title.strip()[:80]
                    if save_current():
                        notice_plain(f'Session renamed "{session["title"]}"')
                else:
                    notice("Session not renamed", "A title is required.", error=True)
            elif command == "resume":
                if not save_current():
                    continue
                items = [_session_item(item) for item in sessions]
                selected = _pick_item(session, draw, items, title="Resume Session", current_id=str(session["id"]))
                if selected:
                    session = selected["session"]
                    _activate_session_workspace(session)
                    display_message_start = 0
                    notice_rows = []
                else:
                    notice("Resume cancelled", "The current session is still open.")
            elif command == "fork":
                if not save_current():
                    continue
                items = [_session_item(item) for item in sessions]
                selected = _pick_item(session, draw, items, title="Fork Session", current_id=str(session["id"]))
                if selected:
                    source = selected["session"]
                    forked = _fork_session(source)
                    sessions.append(forked)
                    session = forked
                    _activate_session_workspace(session)
                    display_message_start = 0
                    if save_current():
                        notice_plain(f"Forked session {source['title']} into a new session.")
                else:
                    notice("Fork cancelled", "The current session is still open.")
            elif command == "delete":
                if not save_current():
                    continue
                selected = _pick_item(
                    session,
                    draw,
                    [_session_item(item) for item in sessions],
                    title="Delete Session",
                    current_id=str(session["id"]),
                )
                if selected is None:
                    notice("Delete cancelled", "No session was removed.")
                    continue
                notice(
                    "Confirm session deletion",
                    f'Type DELETE to remove "{selected["title"]}" permanently.',
                    error=True,
                )
                try:
                    confirmation = _input_bar(draw, "Type DELETE to confirm")
                except AgentExitToTUI:
                    return EXIT_TO_TUI
                except ToggleReasoningView as error:
                    prefill_characters[:] = list(error.input_text)
                    toggle_saved_reasoning()
                    continue
                if confirmation != "DELETE":
                    notice("Delete cancelled", "No session was removed.")
                    continue
                selected_id = str(selected["id"])
                try:
                    deleted = _delete_session(sessions, selected_id)
                except OSError as error:
                    notice("Session not deleted", str(error), error=True)
                    continue
                if not deleted:
                    notice("Session not found", "The selected session no longer exists.", error=True)
                    continue
                if str(session.get("id")) == selected_id:
                    session = _new_session()
                    display_message_start = 0
                notice_plain(f'Deleted session "{selected["title"]}".')
            elif command == "auto-compression":
                enabled, threshold = auto_compression_settings(config)
                value = argument.casefold()
                updated_config = config.copy()
                if value in {"on", "off"}:
                    updated_config["auto_compression"] = value == "on"
                    status = f"Auto-compression is {value} at {threshold}%."
                else:
                    notice(
                        "Auto-compression",
                        f"Currently {'on' if enabled else 'off'} with threshold {threshold}%. Use /auto-compression on or /auto-compression off.",
                    )
                    continue
                try:
                    write_config(updated_config)
                except (OSError, ValueError) as error:
                    notice("Preference not saved", str(error), error=True)
                else:
                    config.update(updated_config)
                    notice("Auto-compression", status)
            elif command == "threshold":
                enabled, threshold = auto_compression_settings(config)
                if not argument.isdecimal() or not 30 <= int(argument) <= 95:
                    notice("Auto-compression threshold", f"Current threshold: {threshold}%. Use /threshold 30–95.", error=True)
                    continue
                threshold = int(argument)
                updated_config = config.copy()
                updated_config["auto_compression_threshold"] = threshold
                try:
                    write_config(updated_config)
                except (OSError, ValueError) as error:
                    notice("Preference not saved", str(error), error=True)
                else:
                    config.update(updated_config)
                    notice("Auto-compression threshold", f"Set to {threshold}% (automatic compression is {'on' if enabled else 'off'}).")
            elif command == "mode":
                requested_mode = argument.casefold()
                if requested_mode not in AGENT_MODES:
                    notice("Agent mode", f"Current mode: {session.get('mode', 'chat')}. Use /mode plan, /mode build, or /mode chat.", error=True)
                    continue
                previous_mode = str(session.get("mode", "chat"))
                session["mode"] = requested_mode
                if session["messages"] and not save_current():
                    session["mode"] = previous_mode
                    continue
                descriptions = {
                    "chat": "General conversation; file changes require a clear request.",
                    "plan": "Read-only planning. Approve implementation with /mode build.",
                    "build": "Implement directly and verify the result.",
                }
                notice("Agent mode", f"Mode set to {requested_mode}. {descriptions[requested_mode]}")
            elif command == "compact":
                window = known_context_window(discover=True)
                if window is None:
                    notice("Context window unavailable", "The selected model did not report its context size.", error=True)
                    continue
                before = _context_percentage(session["messages"], window)
                if before < 50:
                    notice("Context is below the limit", f"Current usage is ~{before}%; /compact is available at 50% or above.")
                    continue
                notice("Context compression", "Summarizing earlier conversation…")
                draw("", 0)
                compact_usage = _fresh_usage()
                try:
                    compacted, _ = _compact_history(
                        config,
                        session["messages"],
                        window,
                        usage_out=compact_usage,
                    )
                except (RuntimeError, OSError, ValueError) as error:
                    _merge_session_usage(session, compact_usage)
                    save_current()
                    notice("Context not compacted", str(error), error=True)
                    continue
                _merge_session_usage(session, compact_usage)
                session["messages"][:] = compacted
                display_message_start = min(display_message_start, len(session["messages"]))
                if save_current():
                    after = _context_percentage(session["messages"], window)
                    notice_plain(f"Context compacted · ~{before}% to ~{after}%")
            elif command == "reasoning":
                argument = argument.casefold()
                preferences = _reasoning_settings(config)
                if argument not in {"on", "off"}:
                    state = "on" if preferences["enabled"] else "off"
                    support = "supported" if preferences["is_support"] else "not detected for this model"
                    notice("Model reasoning", f"Reasoning is {state}; {support}. Use /reasoning on or /reasoning off.")
                    continue
                if argument == "on" and not preferences["is_support"]:
                    preferences["is_support"] = _detect_reasoning_support(config)
                if argument == "on" and not preferences["is_support"]:
                    try:
                        save_reasoning_preferences(preferences)
                    except (OSError, ValueError) as error:
                        notice("Reasoning preference not saved", str(error), error=True)
                    else:
                        notice("Reasoning unavailable", "The selected model does not advertise reasoning support.", error=True)
                    continue
                preferences["enabled"] = argument == "on"
                try:
                    save_reasoning_preferences(preferences)
                except (OSError, ValueError) as error:
                    notice("Reasoning preference not saved", str(error), error=True)
                else:
                    notice("Model reasoning", f"Reasoning turned {argument}.")
            elif command == "reasoning-effort":
                argument = argument.casefold()
                preferences = _reasoning_settings(config)
                if argument not in {"low", "medium", "high"}:
                    notice("Reasoning effort", f"Current effort: {preferences['effort']}. Choose low, medium or high.")
                    continue
                if not preferences["is_support"]:
                    preferences["is_support"] = _detect_reasoning_support(config)
                if not preferences["is_support"]:
                    try:
                        save_reasoning_preferences(preferences)
                    except (OSError, ValueError) as error:
                        notice("Reasoning preference not saved", str(error), error=True)
                    else:
                        notice("Reasoning unavailable", "The selected model does not advertise reasoning support.", error=True)
                    continue
                preferences["effort"] = argument
                try:
                    save_reasoning_preferences(preferences)
                except (OSError, ValueError) as error:
                    notice("Reasoning preference not saved", str(error), error=True)
                else:
                    notice("Reasoning effort", f"Reasoning effort set to {argument}.")
            elif command == "model":
                try:
                    models = _list_models(config, argument)
                except RuntimeError as error:
                    notice("Model list error", str(error), error=True)
                    continue
                if not models:
                    notice("No models found", f"No models match {argument!r}." if argument else "The provider returned no available models.", error=True)
                    continue
                items = [
                    {
                        "id": model["id"],
                        "label": f"{model['id']} · {model['name']}",
                        "context_window": model.get("context_window"),
                        "reasoning_supported": model.get("reasoning_supported", False),
                    }
                    for model in models
                ]
                selected = _pick_item(session, draw, items, title="Available Models", current_id=str(config["model"]))
                if selected:
                    updated_config = config.copy()
                    updated_config["model"] = str(selected["id"])
                    old_reasoning = _reasoning_settings(config)
                    support = selected.get("reasoning_supported") is True
                    updated_config["reasoning"] = {
                        "is_support": support,
                        "enabled": support and old_reasoning["enabled"],
                        "effort": old_reasoning["effort"],
                    }
                    selected_window = selected.get("context_window")
                    if isinstance(selected_window, int) and selected_window > 0:
                        updated_config["context_window"] = selected_window
                    else:
                        updated_config.pop("context_window", None)
                    if updated_config["provider"] == "cloudflare":
                        updated_config["endpoint"] = endpoint_for(updated_config)
                    try:
                        write_config(updated_config)
                    except (OSError, ValueError) as error:
                        notice("Model not changed", str(error), error=True)
                    else:
                        config.update(updated_config)
                        context_state["model"] = str(updated_config["model"])
                        context_state["window"] = selected_window if isinstance(selected_window, int) else None
                        context_state["discovery_attempted"] = isinstance(selected_window, int)
                        if context_state["window"] is None:
                            threading.Thread(
                                target=_discover_context_window,
                                args=(updated_config.copy(), context_state),
                                daemon=True,
                            ).start()
                        notice("Model changed", str(config["model"]))
                else:
                    notice("Model selection cancelled", "The current model is unchanged.")
            else:
                notice("Unknown command", f"/{command} is not an agent command. Type /help.", error=True)
            continue

        notice_rows = []
        messages = session["messages"]
        if not messages and session["title"] == "New session":
            session["title"] = " ".join(prompt.split())[:48] or "New session"
        messages.append({"role": "user", "content": prompt})
        if not save_current():
            messages.pop()
            continue
        maybe_auto_compress()
        answering["active"] = True
        answering["frame"] = 0
        answering["text"] = ""
        answering["reasoning"] = ""
        answering["planning"] = ""
        answering["reasoning_expanded"] = False
        answering["interrupt_warning"] = False
        request_started = time.monotonic()
        draw("", 0)
        events: queue.Queue[tuple[str, object]] = queue.Queue()
        key_events: queue.Queue[str] = queue.Queue()
        cancellation = RequestCancellation()
        turn_mode = str(session.get("mode", "chat"))
        turn_session_id = str(session.get("id", ""))
        turn_usage = _fresh_usage()
        turn_changes: list[dict[str, object]] = []
        turn_activity: list[dict[str, object]] = []
        request_messages = [
            item for item in messages if item["role"] in {"user", "assistant"}
        ]

        def request_user_questions(arguments: object) -> str:
            if not sys.stdin.isatty():
                return "Interactive questions are unavailable because Sophon is not attached to a terminal. Continue with a clearly stated assumption."
            handoff = QuestionHandoff()
            events.put(("questions", (arguments, handoff)))
            while not handoff.ready.wait(0.1):
                cancellation.check()
            if handoff.response:
                return handoff.response
            return "The user cancelled the question card. Continue without assuming an answer and do not immediately ask the same questions again."

        def set_planning_status(value: str) -> None:
            events.put(("planning", value))

        def request_answer() -> None:
            try:
                result = _complete_turn(
                    config.copy(),
                    request_messages,
                    on_chunk=lambda chunk: events.put(("chunk", chunk)),
                    on_reasoning=lambda chunk: events.put(("reasoning", chunk)),
                    mode=turn_mode,
                    cancellation=cancellation,
                    usage_out=turn_usage,
                    change_log=turn_changes,
                    activity_out=turn_activity,
                    on_questions=request_user_questions,
                    on_planning=set_planning_status,
                )
            except Exception as error:
                events.put(("error", error))
            else:
                events.put(("done", result))

        threading.Thread(target=request_answer, daemon=True).start()
        parts: list[str] = []
        reasoning_parts: list[str] = []
        answer: str | None = None
        request_error: Exception | None = None
        interrupted_by_user = False
        last_escape_at: float | None = None
        key_stop = threading.Event()
        key_reader = None
        if sys.stdin.isatty():
            key_reader = threading.Thread(
                target=_input_key_reader,
                args=(key_events, key_stop),
                daemon=True,
            )
            key_reader.start()
        while answer is None and request_error is None:
            batch: list[tuple[str, object]] = []
            try:
                batch = [events.get(timeout=ANIMATION_TICK)]
            except queue.Empty:
                pass
            while True:
                try:
                    batch.append(events.get_nowait())
                except queue.Empty:
                    break
            for kind, value in batch:
                if kind == "chunk":
                    parts.append(str(value))
                elif kind == "reasoning":
                    reasoning_parts.append(str(value))
                elif kind == "planning":
                    if str(value):
                        answering["planning"] = str(value)
                        answering["planning_until"] = time.monotonic() + 1.2
                    elif str(answering.get("planning", "")):
                        answering["planning"] = "TODO.md plan updated"
                        answering["planning_until"] = time.monotonic() + 0.6
                elif kind == "questions" and isinstance(value, tuple) and len(value) == 2:
                    question_arguments, handoff = value
                    if isinstance(handoff, QuestionHandoff):
                        try:
                            handoff.response = _questions_panel(
                                question_arguments,
                                draw,
                                key_events,
                                answering,
                                cancellation,
                            ) or ""
                        except AgentInterrupted as error:
                            cancellation.cancel()
                            interrupted_by_user = True
                            request_error = error
                        except (RuntimeError, ValueError) as error:
                            handoff.response = "The question card could not be shown: " + str(error)
                        finally:
                            handoff.ready.set()
                elif kind == "done":
                    answer = str(value)
                elif kind == "error":
                    request_error = value if isinstance(value, Exception) else RuntimeError(str(value))
            if answer is not None or request_error is not None:
                while True:
                    try:
                        queued_key = key_events.get_nowait()
                    except queue.Empty:
                        break
                    if queued_key.startswith("CHAR:"):
                        prefill_characters.extend(list(queued_key[5:]))
                    elif queued_key == "TOGGLE_REASONING" or _reasoning_mouse_click(queued_key):
                        answering["reasoning_expanded"] = not bool(answering.get("reasoning_expanded"))
                    elif queued_key == "BACKSPACE" and prefill_characters:
                        prefill_characters.pop()
                    elif queued_key == "ENTER" and prefill_characters:
                        pending_prompts.append("".join(prefill_characters).strip())
                        prefill_characters.clear()
                break
            now = time.monotonic()
            answering["frame"] = int((now - request_started) / 0.14)
            while True:
                try:
                    key = key_events.get_nowait()
                except queue.Empty:
                    break
                if key in {"CANCEL", "DOUBLE_CANCEL"}:
                    if key == "DOUBLE_CANCEL" or (last_escape_at is not None and now - last_escape_at <= 1.5):
                        cancellation.cancel()
                        interrupted_by_user = True
                        break
                    last_escape_at = now
                    answering["interrupt_warning"] = True
                    break
                elif key.startswith("CHAR:"):
                    prefill_characters.extend(list(key[5:]))
                elif key == "TOGGLE_REASONING" or _reasoning_mouse_click(key):
                    answering["reasoning_expanded"] = not bool(answering.get("reasoning_expanded"))
                elif key == "BACKSPACE" and prefill_characters:
                    prefill_characters.pop()
                elif key == "ENTER" and prefill_characters:
                    pending_prompts.append("".join(prefill_characters).strip())
                    prefill_characters.clear()
            if last_escape_at is not None and now - last_escape_at > 1.5:
                last_escape_at = None
                answering["interrupt_warning"] = False
            answering["text"] = "".join(parts)
            answering["reasoning"] = "".join(reasoning_parts)
            draw("", 0)

        if key_reader is not None:
            key_stop.set()
            key_reader.join(timeout=0.25)
        while True:
            try:
                queued_key = key_events.get_nowait()
            except queue.Empty:
                break
            if queued_key.startswith("CHAR:"):
                prefill_characters.extend(list(queued_key[5:]))
            elif queued_key == "TOGGLE_REASONING" or _reasoning_mouse_click(queued_key):
                answering["reasoning_expanded"] = not bool(answering.get("reasoning_expanded"))
            elif queued_key == "BACKSPACE" and prefill_characters:
                prefill_characters.pop()
            elif queued_key == "ENTER" and prefill_characters:
                pending_prompts.append("".join(prefill_characters).strip())
                prefill_characters.clear()
        answering["active"] = False
        answering["text"] = ""
        answering["reasoning"] = ""
        answering["planning"] = ""
        answering["interrupt_warning"] = False
        _merge_session_usage(session, turn_usage)
        activity_log = session.get("activity_log")
        if not isinstance(activity_log, list):
            activity_log = []
        if turn_activity:
            session["activity_log"] = [*activity_log, *turn_activity][-250:]
        if turn_changes:
            _refresh_undo_digests(turn_changes, config)
            last_changes_by_session[turn_session_id] = turn_changes
        refresh_sessions_after_tools()
        if interrupted_by_user:
            partial = "".join(parts).strip()
            if partial:
                messages.append({"role": "assistant", "content": partial, "interrupted": True})
            messages.append({"role": "error", "content": "Stopped by user.", "cancelled": True})
            save_current()
            notice_plain("Response interrupted.")
            continue
        if request_error is not None:
            partial = "".join(parts).strip()
            if partial:
                messages.append({"role": "assistant", "content": partial})
            messages.append({"role": "error", "content": str(request_error)})
            save_current()
            continue
        assert answer is not None
        assistant_message: dict[str, object] = {"role": "assistant", "content": answer}
        reasoning_text = "".join(reasoning_parts).strip()
        if reasoning_text:
            assistant_message["reasoning"] = reasoning_text
            assistant_message["reasoning_expanded"] = bool(answering.get("reasoning_expanded"))
        messages.append(assistant_message)
        if not save_current():
            notice("Session storage error", "The reply is visible but could not be saved.", error=True)
        from sophon import core

        if core.RESTART_REQUESTED:
            return 0
        maybe_auto_compress()


def _parse_agent_args(args: list[str]) -> tuple[str | None, str | None, str | None]:
    if not args:
        return None, None, None
    values: dict[str, str] = {}
    aliases = {
        "-p": "system",
        "--system-prompt": "system",
        "-m": "message",
        "--message": "message",
        "--prompt": "legacy_prompt",
        "-t": "task",
        "--task": "task",
    }
    index = 0
    while index < len(args):
        name = aliases.get(args[index])
        if name is None or index + 1 >= len(args) or name in values:
            return None, None, 'Usage: sophon agent [-p SYSTEM_PROMPT -m MESSAGE] | [-m MESSAGE] | [-t TASK]'
        values[name] = args[index + 1]
        index += 2
    if "task" in values and ("system" in values or "message" in values or "legacy_prompt" in values):
        return None, None, 'Usage: sophon agent [-p SYSTEM_PROMPT -m MESSAGE] | [-m MESSAGE] | [-t TASK]'
    if "legacy_prompt" in values:
        if "system" in values or "message" in values or "task" in values:
            return None, None, 'Usage: sophon agent [-p SYSTEM_PROMPT -m MESSAGE] | [-m MESSAGE] | [-t TASK]'
        return values["legacy_prompt"], None, None
    if "task" in values:
        return values["task"], None, None
    if "message" in values:
        return values["message"], values.get("system"), None
    if "system" in values:
        # Preserve the pre-existing `sophon agent -p MESSAGE` one-shot form.
        return values["system"], None, None
    return None, None, 'Usage: sophon agent [-p SYSTEM_PROMPT -m MESSAGE] | [-m MESSAGE] | [-t TASK]'


def main(args: list[str]) -> int:
    one_shot, system_prompt, parse_error = _parse_agent_args(args)
    if parse_error:
        card(
            "Agent request failed",
            [parse_error],
            border_color=NOTICE_BORDER,
            text_color=NOTICE_TEXT,
        )
        return 2
    config = _load_config()
    if config is None:
        card(
            "Provider required",
            ["No provider is configured.", "Run sophon setup first."],
            border_color=NOTICE_BORDER,
            text_color=NOTICE_TEXT,
        )
        return 1
    if not config:
        return 1
    try:
        _ensure_agent_instructions(config)
        _ensure_workspace()
    except (OSError, ValueError) as error:
        card("Workspace unavailable", [str(error)], border_color=NOTICE_BORDER, text_color=NOTICE_TEXT)
        return 1
    if one_shot is None:
        return _interactive(config)
    if not one_shot.strip():
        card(
            "Agent request failed",
            ["Message cannot be empty."],
            border_color=NOTICE_BORDER,
            text_color=NOTICE_TEXT,
        )
        return 2
    try:
        _run_one_shot_stream(config, one_shot, system_prompt)
    except RuntimeError as error:
        card(
            "Agent request failed",
            [str(error)],
            border_color=NOTICE_BORDER,
            text_color=NOTICE_TEXT,
        )
        return 1
    return 0


def _run_one_shot_stream(
    config: dict[str, object],
    prompt: str,
    custom_system_prompt: str | None = None,
) -> None:
    events: queue.Queue[tuple[str, object]] = queue.Queue()

    def request_answer() -> None:
        try:
            answer = _ask(
                config.copy(),
                [],
                prompt,
                on_chunk=lambda chunk: events.put(("chunk", chunk)),
                custom_system_prompt=custom_system_prompt,
            )
        except Exception as error:
            events.put(("error", error))
        else:
            events.put(("done", answer))

    threading.Thread(target=request_answer, daemon=True).start()
    tty = sys.stdout.isatty()
    frame = 0
    started = False
    finished = False
    columns = shutil.get_terminal_size((80, 24)).columns
    line_width = max(20, columns - 4)
    line_column = 0
    home_prefix = Path.home().as_posix()
    pending_display = ""

    def write_wrapped(chunk: str) -> None:
        nonlocal line_column
        for character in chunk:
            if character == "\r":
                continue
            if character == "\n":
                sys.stdout.write("\n    ")
                line_column = 4
                continue
            character_width = display_width(character)
            if line_column + character_width > line_width:
                sys.stdout.write("\n    ")
                line_column = 4
            sys.stdout.write(character)
            line_column += character_width

    def write_display_chunk(chunk: str, *, final: bool = False) -> None:
        nonlocal pending_display
        combined = pending_display + chunk
        pending_display = ""
        if not final and home_prefix:
            keep = 0
            for length in range(min(len(combined), len(home_prefix)), 0, -1):
                if home_prefix.startswith(combined[-length:]):
                    keep = length
                    break
            if keep:
                pending_display = combined[-keep:]
                combined = combined[:-keep]
        write_wrapped(display_home_paths(combined))

    while not finished:
        try:
            batch = [events.get(timeout=ANIMATION_TICK)]
        except queue.Empty:
            if tty and not started:
                sys.stdout.write("\r\033[2K" + _answering_rows(frame)[0])
                sys.stdout.flush()
                frame += 1
            continue
        while True:
            try:
                batch.append(events.get_nowait())
            except queue.Empty:
                break
        for kind, value in batch:
            if kind == "chunk":
                if not started:
                    if tty:
                        sys.stdout.write("\r\033[2K")
                    sys.stdout.write(paint("  • ", MUTED))
                    started = True
                    line_column = 4
                write_display_chunk(str(value))
                sys.stdout.flush()
            elif kind == "error":
                if tty and not started:
                    sys.stdout.write("\r\033[2K")
                if started:
                    sys.stdout.write("\n")
                error = value if isinstance(value, Exception) else RuntimeError(str(value))
                raise RuntimeError(str(error)) from None
            elif kind == "done":
                if started:
                    write_display_chunk("", final=True)
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                elif not str(value).strip():
                    raise RuntimeError("The provider returned an empty response.")
                else:
                    _render_agent_message(str(value))
                finished = True


def _render_agent_message(message: str) -> None:
    for row in _agent_rows(message, shutil.get_terminal_size((80, 24)).columns):
        print(row)
