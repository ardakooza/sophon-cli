"""Chat-only AI agent backed by the configured Sophon provider."""

from __future__ import annotations

import codecs
import datetime as dt
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

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
PROJECT_DIR = WORKSPACE_ROOT / "my-project"
INPUT_PLACEHOLDER = "Create something with sophon"
ANIMATION_TICK = 0.075
AGENT_MODES = {"chat", "plan", "build"}
PLAN_BUILTIN_TOOLS = {"get_version", "is_update", "system_health", "workspace_list", "session_list", "get_time", "get_date"}
SLASH_COMMANDS = (
    "/auto-compression", "/changelog", "/clear", "/compact", "/cwd", "/delete",
    "/exit", "/fork", "/help", "/mode", "/model", "/new", "/reasoning",
    "/reasoning-effort", "/rename", "/resume", "/sessions", "/threshold",
)


class AgentInterrupted(RuntimeError):
    """Raised when the user explicitly cancels an active model request."""


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

SYSTEM_PROMPT = (
    "You are Sophon, a concise and practical development assistant. "
    "Use the provided file tools to inspect and edit project files in the active workspace. "
    "Do not access project files outside that workspace or claim to have run arbitrary shell commands. "
    "Use built-in tools for supported system operations and report their results accurately. "
    "Keep all project files inside the active project folder."
)

WORKSPACE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and folders in the active project workspace.",
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
            "description": "Read a UTF-8 text file inside the active project workspace.",
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
            "description": "Create or replace a UTF-8 text file inside the active project workspace.",
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
        "plan": "Mode: Plan. Inspect and reason read-only, then present a concise implementation plan and ask for approval. Do not modify files or run mutating tools. The user can approve implementation with /mode build.",
        "build": "Mode: Build. Implement the user's requested changes directly, use available workspace tools, and verify the result.",
    }.get(mode, "")
    if mode_instruction:
        system_prompt += "\n\n" + mode_instruction
    if custom_system_prompt and custom_system_prompt.strip():
        system_prompt += "\n\nUser-provided system prompt:\n" + custom_system_prompt.strip()
    return (
        f"{system_prompt}\n\n"
        f"Agent instructions (AGENT.md):\n{instructions}\n\n"
        f"Active project directory: {PROJECT_DIR}\n"
        f"Workspace guidance (WORKSPACE.md):\n{workspace_guidance}"
    )


def _tools_for_mode(mode: str) -> list[dict[str, object]]:
    if mode != "plan":
        return [*BUILTIN_TOOLS, *WORKSPACE_TOOLS]
    builtins = [
        tool for tool in BUILTIN_TOOLS
        if tool["function"]["name"] in PLAN_BUILTIN_TOOLS
    ]
    workspace = [
        tool for tool in WORKSPACE_TOOLS
        if tool["function"]["name"] in {"list_files", "read_file"}
    ]
    return [*builtins, *workspace]


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


def _workspace_file(relative_path: str, *, allow_root: bool = False) -> Path:
    root = PROJECT_DIR.resolve()
    requested = Path(relative_path or ".")
    if requested.is_absolute():
        raise RuntimeError("Workspace tool paths must be project-relative.")
    candidate = (root / requested).resolve()
    if candidate != root and root not in candidate.parents:
        raise RuntimeError("Workspace tool paths must stay inside the active project.")
    if candidate == root and not allow_root:
        raise RuntimeError("Choose a file path inside the active project.")
    return candidate


def _execute_workspace_tool(name: str, arguments: object) -> str:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            raise RuntimeError("The workspace tool arguments were not valid JSON.") from None
    if not isinstance(arguments, dict):
        raise RuntimeError("The workspace tool arguments must be an object.")
    if name == "list_files":
        folder = _workspace_file(str(arguments.get("path") or "."), allow_root=True)
        if not folder.is_dir():
            raise RuntimeError("The requested workspace path is not a folder.")
        entries = sorted(folder.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold()))
        if not entries:
            return "This folder is empty."
        return "\n".join(
            (item.name + ("/" if item.is_dir() else ""))
            for item in entries[:200]
        )
    if name == "read_file":
        path = _workspace_file(str(arguments.get("path", "")))
        if not path.is_file():
            raise RuntimeError("The requested workspace file does not exist.")
        if path.stat().st_size > 500_000:
            raise RuntimeError("Workspace files must be smaller than 500 KB to read.")
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise RuntimeError(f"Could not read workspace file: {error}") from None
    if name == "write_file":
        path = _workspace_file(str(arguments.get("path", "")))
        content = arguments.get("content")
        if not isinstance(content, str):
            raise RuntimeError("File content must be text.")
        if len(content.encode("utf-8")) > 1_000_000:
            raise RuntimeError("Workspace files must be smaller than 1 MB to write.")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(content, encoding="utf-8")
        except OSError as error:
            raise RuntimeError(f"Could not write workspace file: {error}") from None
        return f"Wrote {path.relative_to(PROJECT_DIR.resolve())}."
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


def _request(
    config: dict[str, object],
    messages: list[dict[str, object]],
    on_chunk: Callable[[str], None] | None = None,
    tool_calls_out: list[dict[str, object]] | None = None,
    *,
    tools_enabled: bool = True,
    on_reasoning: Callable[[str], None] | None = None,
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
        all_tools = _tools_for_mode(mode)
        payload["tools"] = all_tools if provider == "openrouter" else [
            tool["function"] for tool in all_tools
        ]

    request = Request(
        endpoint_for(config),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    chunks: list[str] = []
    openrouter_tools: dict[int, dict[str, str]] = {}
    cloudflare_tools: list[dict[str, object]] = []

    def collect_tool_calls(result: object) -> None:
        if not isinstance(result, dict):
            return
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
                mode=mode,
                cancellation=cancellation,
            )
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
) -> str:
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
            try:
                if mode == "plan" and name not in PLAN_BUILTIN_TOOLS | {"list_files", "read_file"}:
                    raise RuntimeError("This tool is disabled in plan mode; switch to /mode build after approval.")
                if name in {tool["function"]["name"] for tool in BUILTIN_TOOLS}:
                    result = _execute_builtin_tool(name, function.get("arguments", "{}"))
                else:
                    result = _execute_workspace_tool(name, function.get("arguments", "{}"))
            except (RuntimeError, OSError, ValueError) as error:
                result = f"Tool error: {error}"
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
    }


def _workspace_location(command: str) -> tuple[str, str, bool]:
    if command == "cwd":
        return "Current directory", display_home_paths(Path.cwd().resolve()), False
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
    text_color: str = WHITE,
    highlight_terms: tuple[str, ...] = (),
) -> list[str]:
    title = display_home_paths(title) if title is not None else None
    lines = [display_home_paths(line) for line in lines]
    top_left = display_home_paths(top_left) if top_left is not None else None
    top_right = display_home_paths(top_right) if top_right is not None else None
    inner = min(width, max(3, columns - 8)) if width is not None else min(68, max(20, columns - 8))
    top = "  ╭" + "─" * (inner + 2) + "╮"
    bottom = "  ╰" + "─" * (inner + 2) + "╯"
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
        rows.append(paint("  │", border_color) + paint(" " + heading + " ", text_color) + paint("│", border_color))
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
            rows.append(paint("  │", border_color) + paint(" " + row + " ", text_color) + paint("│", border_color))
        else:
            title_row = centered(_truncate_display(heading, inner), inner)
            left_row = left + " " * max(0, inner - display_width(left))
            right_row = " " * max(0, inner - display_width(badge)) + badge
            for text in (title_row, left_row, right_row):
                rows.append(paint("  │", border_color) + paint(" " + text + " ", text_color) + paint("│", border_color))
        content.insert(0, "")
    elif title is not None and top_right is not None:
        badge = _truncate_display(top_right, inner)
        title_width, badge_width = display_width(title), display_width(badge)
        left_pad = max(0, (inner - title_width) // 2)
        right_start = inner - badge_width
        if left_pad + title_width + 1 <= right_start:
            heading = " " * left_pad + title + " " * max(1, right_start - left_pad - title_width) + badge
            heading += " " * max(0, inner - display_width(heading))
            rows.append(paint("  │", border_color) + paint(" " + heading + " ", text_color) + paint("│", border_color))
        else:
            heading = centered(title, inner)
            rows.append(paint("  │", border_color) + paint(" " + heading + " " * (inner - display_width(heading) + 1), text_color) + paint("│", border_color))
            rows.append(paint("  │", border_color) + paint(" " + " " * (inner - badge_width) + badge + " ", text_color) + paint("│", border_color))
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
                    rows.extend(_reasoning_rows(reasoning_text, columns, connect_to_answer=True))
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


def _slash_command_completion(text: str, cursor: int) -> str:
    if not text.startswith("/") or cursor != len(text) or any(char.isspace() for char in text):
        return ""
    candidate = next((command for command in SLASH_COMMANDS if command.startswith(text) and command != text), None)
    return candidate[len(text):] if candidate else ""


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
    context_window: int | None = None,
    mode: str = "chat",
    reasoning_state: str = "off",
    display_message_start: int = 0,
    interrupt_warning: bool = False,
) -> tuple[int, int]:
    size = shutil.get_terminal_size((80, 24))
    columns, height = size.columns, max(8, size.lines)
    width = min(68, max(20, columns - 8))
    header = [] if session["messages"] else _card_rows(
        "Sophon Agent",
        ["Ask about code, debugging, and development."],
        columns=columns,
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
    max_editor_lines = max(1, min(5, height - len(header) - len(footer) - 3))
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
                frame=None if streaming_text.strip() else animation_frame,
                connect_to_answer=bool(streaming_text.strip()),
            ))
        elif answering and not streaming_text.strip():
            label = "Reasoning" if reasoning_state not in {"off", "not supported"} else "Answering"
            feed.extend(_answering_rows(animation_frame, label))
        if streaming_text:
            feed.extend(_answering_rows(animation_frame, "Answering"))
            feed.extend(_agent_rows(streaming_text, columns))
    warning_rows = _interrupt_warning_rows(columns) if interrupt_warning else []
    room = max(0, height - len(header) - len(box) - len(footer) - len(warning_rows))
    visible_feed = feed[-room:] if room else []
    if panel_rows is not None and room >= 2 and len(panel_rows) > room:
        visible_feed = [panel_rows[0], *panel_rows[1 : room - 1], panel_rows[-1]]
    screen = header + visible_feed + [""] * max(0, room - len(visible_feed)) + warning_rows + box + footer
    screen = screen[-height:]
    if len(screen) < height:
        screen = [""] * (height - len(screen)) + screen
    output = "\033[2J\033[H" + "\r\n".join(screen)
    absolute_row = height - len(box) - len(footer) - len(warning_rows) + 1 + cursor_row - visible_start
    absolute_col = 4 + cursor_col
    output += f"\033[{max(1, absolute_row + 1)};{max(1, absolute_col + 1)}H"
    sys.stdout.write(output)
    sys.stdout.flush()
    return cursor_row, cursor_col


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
        while not stop_event.is_set():
            key = _read_key_raw(timeout=0.1)
            if key != "TIMEOUT":
                events.put(key)
    except OSError:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def _input_bar(draw, placeholder: str = INPUT_PLACEHOLDER, *, prefill: str = "") -> str:
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
    last_size = shutil.get_terminal_size((80, 24))

    def redraw() -> None:
        text = "".join(characters)
        draw(text, cursor, placeholder=placeholder, suggestion=_slash_command_completion(text, cursor))

    try:
        tty.setraw(fd)
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
                    raise EOFError
                sequence = bytearray()
                while select.select([fd], [], [], 0.08)[0]:
                    sequence.extend(os.read(fd, 1))
                    if len(sequence) >= 2 and 0x40 <= sequence[-1] <= 0x7E:
                        break
                key = bytes(sequence[-1:])
                if key == b"D":
                    cursor = max(0, cursor - 1)
                elif key == b"C":
                    cursor = min(len(characters), cursor + 1)
                else:
                    continue
                redraw()
                continue
            if raw in {b"\r", b"\n"}:
                text = "".join(characters)
                completion = _slash_command_completion(text, cursor)
                return (text + completion).strip()
            if raw == b"\x03":
                raise KeyboardInterrupt
            if raw in {b"\x08", b"\x7f"}:
                if cursor:
                    characters.pop(cursor - 1)
                    cursor -= 1
                    redraw()
                continue
            if raw == b"\x04":
                if cursor < len(characters):
                    characters.pop(cursor)
                    redraw()
                continue
            if raw == b"\t":
                completion = _slash_command_completion("".join(characters), cursor)
                if completion:
                    characters[cursor:cursor] = list(completion)
                    cursor += len(completion)
                    redraw()
                continue
            character = decoder.decode(raw)
            if character and character.isprintable():
                characters[cursor:cursor] = list(character)
                cursor += len(character)
                redraw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def _model_request(url: str, token: str, *, timeout: float = 30) -> dict[str, object]:
    request = Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read(2048).decode("utf-8", errors="replace").strip()
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
                str(config["api_key"]),
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
    token = str(config["api_key"])
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
    tool_schema = json.dumps([*BUILTIN_TOOLS, *WORKSPACE_TOOLS], ensure_ascii=False)
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
    body = [
        _truncate_display("Model: " + model, inner),
        _truncate_display("Reasoning: " + reasoning, inner),
        _truncate_display(f"Mode: {mode} · {context_label}", inner),
        _truncate_display("Directory: " + directory, inner),
    ]
    return _card_rows(
        None,
        body,
        columns=columns,
        width=inner,
        text_color=MUTED,
        highlight_terms=("Model:", "Reasoning:", "Mode:", "Directory:", "Context:"),
    )


def _answering_rows(frame: int, label: str = "Answering") -> list[str]:
    center = (frame // 2) % (len(label) + 6) - 3
    shimmer = ""
    for index, character in enumerate(label):
        distance = abs(index - center)
        brightness = max(0.0, 1.0 - distance / 2.0)
        color = 240 + round(15 * brightness**2)
        shimmer += paint(character, f"\033[38;5;{color}m")
    pulse = (238, 241, 244, 247, 250, 253, 255, 253, 250, 247, 244, 241)
    dot_color = f"\033[38;5;{pulse[(frame // 6) % len(pulse)]}m"
    return ["  " + paint("•", dot_color) + " " + shimmer]


def _reasoning_rows(
    text: str,
    columns: int,
    *,
    frame: int | None = None,
    connect_to_answer: bool = False,
) -> list[str]:
    text = display_home_paths(text)
    rows = _answering_rows(frame, "Reasoning") if frame is not None else [paint("  • Reasoning", MUTED)]
    width = max(20, columns - 8)
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
                    marker = "❯" if index == selected else "·"
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


def _session_item(session: dict[str, object]) -> dict[str, object]:
    updated = str(session.get("updated_at", ""))
    return {
        "id": str(session["id"]),
        "title": str(session["title"]),
        "label": f"{session['title']} · {len(session['messages'])} messages · {updated[:16].replace('T', ' ')}",
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


def _interactive(config: dict[str, object]) -> int:
    try:
        sessions = _load_sessions()
    except RuntimeError as error:
        card("Session storage error", [str(error)], border_color=NOTICE_BORDER, text_color=NOTICE_TEXT)
        return 1
    session = _new_session()
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
    answering = {"active": False, "frame": 0, "text": "", "reasoning": ""}

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
            context_window=context_state["window"] if isinstance(context_state["window"], int) else None,
            mode=str(session.get("mode", "chat")),
            reasoning_state=reasoning_state,
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

    def close_with_card() -> None:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        card(None, ["Session closed. See you soon."])

    def save_current() -> bool:
        try:
            session["model"] = str(config["model"])
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
        notice("Auto-compression", "Summarizing earlier conversation…")
        draw("", 0)
        try:
            compressed, _ = _compact_history(config, messages, window)
        except (RuntimeError, OSError, ValueError) as error:
            notice("Auto-compression failed", str(error), error=True)
            return
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
                        "/help — Show available commands",
                        "/model [search] — Choose a model",
                        "/new — Start a new session",
                        "/rename [title] — Rename this session",
                        "/resume — Load a saved session",
                        "/fork — Fork a saved session",
                        "/sessions — List sessions by title and context",
                        "/delete — Delete a saved session",
                        "/compact — Summarize context at 50% or more",
                        "/auto-compression on|off — Toggle automatic mode",
                        "/threshold 30–95 — Set auto-compression threshold",
                        "/mode plan|build|chat — Set the Agent mode",
                        "/changelog — Show latest changes",
                        "/reasoning on|off — Toggle model reasoning",
                        "/reasoning-effort low|medium|high — Set effort",
                        "/cwd — Show current directory",
                        "/clear — Clear the screen, keep this session",
                        "/exit or Q — Close Sophon Agent",
                    ],
                    columns=shutil.get_terminal_size((80, 24)).columns,
                )
            elif command == "changelog":
                updated = VERSION.split(" – ", 1)[0]
                notice_rows = _card_rows(
                    "Sophon Changelog",
                    [
                        f"Version: {VERSION}",
                        f"Last updated: {updated}",
                        "",
                        "Added: /sessions, /mode, /threshold and the single /cwd command.",
                        "Added: default chat sessions, read-only planning, and build approval flow.",
                        "Added: auto-compression at 30–95%; /compact unlocks at 50% context.",
                        "Improved: double-Esc interruption, slash completion, and connected reasoning.",
                        "Improved: session context/date list, environment detection, and update progress.",
                    ],
                    columns=shutil.get_terminal_size((80, 24)).columns,
                )
            elif command == "cwd":
                title, message, error = _workspace_location(command)
                notice(title, message, error=error)
            elif command == "sessions":
                notice_rows = _card_rows(
                    "Saved Sessions",
                    _sessions_panel_rows(sessions, known_context_window()),
                    columns=shutil.get_terminal_size((80, 24)).columns,
                )
            elif command == "new":
                session = _new_session()
                display_message_start = 0
                notice_plain("New session created.")
            elif command == "clear":
                display_message_start = len(session["messages"])
                notice_rows = []
                sys.stdout.write("\033[2J\033[H")
                sys.stdout.flush()
            elif command == "rename":
                title = argument or _input_bar(draw, "Enter a session title")
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
                confirmation = _input_bar(draw, "Type DELETE to confirm")
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
                try:
                    compacted, _ = _compact_history(config, session["messages"], window)
                except (RuntimeError, OSError, ValueError) as error:
                    notice("Context not compacted", str(error), error=True)
                    continue
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
        answering["interrupt_warning"] = False
        request_started = time.monotonic()
        draw("", 0)
        events: queue.Queue[tuple[str, object]] = queue.Queue()
        cancellation = RequestCancellation()
        turn_mode = str(session.get("mode", "chat"))
        request_messages = [
            item for item in messages if item["role"] in {"user", "assistant"}
        ]

        def request_answer() -> None:
            try:
                result = _complete_turn(
                    config.copy(),
                    request_messages,
                    on_chunk=lambda chunk: events.put(("chunk", chunk)),
                    on_reasoning=lambda chunk: events.put(("reasoning", chunk)),
                    mode=turn_mode,
                    cancellation=cancellation,
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
        key_events: queue.Queue[str] = queue.Queue()
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
            elif queued_key == "BACKSPACE" and prefill_characters:
                prefill_characters.pop()
            elif queued_key == "ENTER" and prefill_characters:
                pending_prompts.append("".join(prefill_characters).strip())
                prefill_characters.clear()
        answering["active"] = False
        answering["text"] = ""
        answering["reasoning"] = ""
        answering["interrupt_warning"] = False
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
        assistant_message: dict[str, str] = {"role": "assistant", "content": answer}
        reasoning_text = "".join(reasoning_parts).strip()
        if reasoning_text:
            assistant_message["reasoning"] = reasoning_text
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
