"""Local provider configuration storage for Sophon."""

from __future__ import annotations

import json
import os
import platform
import tempfile
from pathlib import Path
from typing import Any

PROVIDERS: dict[str, dict[str, str]] = {
    "openrouter": {
        "label": "OpenRouter",
        "default_model": "~openai/gpt-sol-latest",
        "endpoint": "https://openrouter.ai/api/v1/chat/completions",
    },
    "cloudflare": {
        "label": "Cloudflare Workers AI",
        "default_model": "@cf/meta/llama-3.1-8b-instruct",
        "endpoint_base": "https://api.cloudflare.com/client/v4/accounts",
    },
}

AGENT_MODES = ("plan", "build", "chat")


def _default_mode_tools() -> dict[str, list[str]]:
    return {mode: [] for mode in AGENT_MODES}


def _normalized_agent_access(data: dict[str, Any]) -> tuple[list[str], dict[str, list[str]]]:
    allowed_path = data.get("allowed_path", [])
    if not isinstance(allowed_path, list) or any(
        not isinstance(item, str) or not item.strip() for item in allowed_path
    ):
        raise ValueError("allowed_path must be a list of non-empty path strings")
    normalized_paths = list(dict.fromkeys(item.strip() for item in allowed_path))

    mode_value = data.get("mode", {})
    if not isinstance(mode_value, dict) or set(mode_value) - set(AGENT_MODES):
        raise ValueError("mode must contain only plan, build, and chat tool lists")
    normalized_mode: dict[str, list[str]] = {}
    for mode in AGENT_MODES:
        tools = mode_value.get(mode, [])
        if not isinstance(tools, list) or any(not isinstance(tool, str) or not tool.strip() for tool in tools):
            raise ValueError(f"mode.{mode} must be a list of non-empty tool names")
        normalized_mode[mode] = list(dict.fromkeys(tool.strip() for tool in tools))
    return normalized_paths, normalized_mode


def config_path() -> Path:
    xdg_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_home) / "sophon" if xdg_home else Path.home() / ".sophon"
    return base / "config" / "config.json"


def config_display_path() -> str:
    path = config_path()
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


def detect_environment() -> str:
    """Return a concise, stable platform label suitable for local config metadata."""
    if os.environ.get("TERMUX_VERSION") or os.environ.get("PREFIX", "").startswith("/data/data/com.termux"):
        return "Termux (Android)"
    system = platform.system()
    if system == "Linux":
        try:
            release = platform.freedesktop_os_release()
        except (AttributeError, OSError):
            release = {}
        pretty_name = release.get("PRETTY_NAME", "").strip()
        if pretty_name:
            return pretty_name
        distribution = release.get("NAME", "").strip()
        return f"{distribution} (Linux)" if distribution else "Linux"
    if system == "Darwin":
        return "macOS"
    return system or "Unknown"


def ensure_environment_config() -> str:
    """Persist Environment in config.json without disturbing provider settings."""
    environment = detect_environment()
    try:
        data = read_config()
    except (OSError, ValueError, json.JSONDecodeError):
        return environment
    if data is not None:
        if data.get("Environment") != environment:
            data["Environment"] = environment
            write_config(data)
        return environment
    path = config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {"schema_version": 1}
    except (OSError, ValueError, json.JSONDecodeError):
        return environment
    if not isinstance(raw, dict) or raw.get("provider") is not None:
        return environment
    if raw.get("Environment") != environment:
        raw["Environment"] = environment
        raw.setdefault("schema_version", 1)
        write_config(raw)
    return environment


def agent_instructions_path() -> Path:
    return config_path().parent.parent / "agent" / "AGENT.md"


def ensure_agent_instructions_file(path: Path | None = None) -> Path:
    target = path or agent_instructions_path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return target
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(
            "# Sophon Agent Instructions\n\n"
            "These instructions apply to every new Sophon Agent session. Edit this file to customize "
            "how Sophon plans and communicates.\n\n"
            "- Be concise, practical, and transparent about uncertainty.\n"
            "- Inspect relevant project files before proposing or making changes.\n"
            "- Keep all project changes inside the active project workspace.\n"
            "- Use built-in tools for supported system operations, and report their results accurately.\n"
            "- Never claim to have changed a file unless it actually happened.\n"
            "- Protect credentials and other secrets; do not include them in replies.\n"
        )
    return target


def legacy_config_path() -> Path:
    xdg_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_home:
        return Path(xdg_home) / "xernel" / "config" / "config.json"
    return Path.home() / ".xernel" / "config" / "config.json"


def _legacy_config_paths() -> tuple[Path, ...]:
    xdg_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_home:
        base = Path(xdg_home)
        return (
            legacy_config_path(),
            base / "xernel" / "config.json",
            Path.home() / ".xernel" / "config" / "config.json",
            Path.home() / ".config" / "xernel" / "config.json",
        )
    return (
        legacy_config_path(),
        Path.home() / ".config" / "xernel" / "config.json",
    )


def read_config() -> dict[str, Any] | None:
    path = config_path()
    source = path
    if not source.exists():
        legacy_path = next((candidate for candidate in _legacy_config_paths() if candidate.exists()), None)
        if legacy_path is None:
            return None
        source = legacy_path
    with source.open(encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError("Unsupported or malformed provider configuration")
    if data.get("provider") is None:
        allowed_preferences = {
            "schema_version", "auto_update", "agent_instructions_path", "reasoning",
            "auto_compression", "auto_compression_threshold", "Environment", "allowed_path", "mode",
        }
        if set(data) <= allowed_preferences:
            _normalized_agent_access(data)
            return None
        raise ValueError("Unsupported or malformed provider configuration")
    if data.get("provider") not in PROVIDERS:
        raise ValueError("Unsupported or malformed provider configuration")
    if not isinstance(data.get("model"), str) or not data["model"].strip():
        raise ValueError("Configuration is missing a model ID")
    if not isinstance(data.get("api_key"), str) or not data["api_key"].strip():
        raise ValueError("Configuration is missing an API credential")
    if data["provider"] == "cloudflare":
        account_id = data.get("account_id", "")
        if not isinstance(account_id, str) or len(account_id) != 32 or any(
            char not in "0123456789abcdefABCDEF" for char in account_id
        ):
            raise ValueError("Configuration has an invalid Cloudflare Account ID")
    changed = False
    migrated_auto_update = False
    if not isinstance(data.get("agent_instructions_path"), str) or not data["agent_instructions_path"].strip():
        data["agent_instructions_path"] = str(agent_instructions_path())
        changed = True
    if not isinstance(data.get("auto_update"), bool):
        legacy_settings = config_path().parent.parent / "settings.json"
        try:
            settings = json.loads(legacy_settings.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            settings = {}
        data["auto_update"] = isinstance(settings, dict) and settings.get("auto_update") is True
        migrated_auto_update = isinstance(settings, dict) and isinstance(settings.get("auto_update"), bool)
        changed = True
    reasoning = data.get("reasoning")
    if not isinstance(reasoning, dict):
        reasoning = {}
    support = reasoning.get("is_support") is True
    enabled = support and reasoning.get("enabled") is True
    effort = reasoning.get("effort")
    if effort not in {"low", "medium", "high"}:
        effort = "medium"
    normalized_reasoning = {"is_support": support, "enabled": enabled, "effort": effort}
    if reasoning != normalized_reasoning:
        data["reasoning"] = normalized_reasoning
        changed = True
    auto_compression = data.get("auto_compression")
    if not isinstance(auto_compression, bool):
        auto_compression = True
    threshold = data.get("auto_compression_threshold")
    if not isinstance(threshold, int) or isinstance(threshold, bool) or not 30 <= threshold <= 95:
        threshold = 80
    if data.get("auto_compression") is not auto_compression:
        data["auto_compression"] = auto_compression
        changed = True
    if data.get("auto_compression_threshold") != threshold:
        data["auto_compression_threshold"] = threshold
        changed = True
    allowed_path, mode_tools = _normalized_agent_access(data)
    if data.get("allowed_path") != allowed_path:
        data["allowed_path"] = allowed_path
        changed = True
    if data.get("mode") != mode_tools:
        data["mode"] = mode_tools
        changed = True
    if source != path or changed:
        write_config(data)
    if migrated_auto_update:
        try:
            legacy_settings.unlink()
        except OSError:
            pass
    if source != path:
        source.unlink()
    return data


def write_config(data: dict[str, Any]) -> Path:
    data = dict(data)
    provider = data.get("provider")
    if provider is None:
        allowed_preferences = {
            "schema_version", "auto_update", "agent_instructions_path", "reasoning",
            "auto_compression", "auto_compression_threshold", "Environment", "allowed_path", "mode",
        }
        if set(data) - allowed_preferences:
            raise ValueError("Preferences-only config contains unsupported fields")
        if "auto_update" in data and not isinstance(data["auto_update"], bool):
            raise ValueError("Auto-update preference must be a boolean")
        if "auto_compression" in data and not isinstance(data["auto_compression"], bool):
            raise ValueError("Auto-compression preference must be a boolean")
        threshold = data.get("auto_compression_threshold")
        if threshold is not None and (
            not isinstance(threshold, int) or isinstance(threshold, bool) or not 30 <= threshold <= 95
        ):
            raise ValueError("Auto-compression threshold must be between 30 and 95")
        environment = data.get("Environment")
        if environment is not None and (not isinstance(environment, str) or not environment.strip()):
            raise ValueError("Environment must be a non-empty string")
        allowed_path, mode_tools = _normalized_agent_access(data)
    else:
        if provider not in PROVIDERS:
            raise ValueError("Unsupported provider")
        if not data.get("model") or not data.get("api_key"):
            raise ValueError("Model and API key are required")
        if provider == "cloudflare" and not data.get("account_id"):
            raise ValueError("Cloudflare Account ID is required")
        allowed_path, mode_tools = _normalized_agent_access(data)

    if "allowed_path" in data:
        data["allowed_path"] = allowed_path
    if "mode" in data:
        data["mode"] = mode_tools

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent.parent, 0o700)
    os.chmod(path.parent, 0o700)
    fd, temp_name = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
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
    return path


def mask_secret(value: str) -> str:
    if not value:
        return "Not set"
    visible = 4 if len(value) > 4 else 1
    return value[:visible] + "*" * (len(value) - visible)


def endpoint_for(data: dict[str, Any]) -> str:
    provider = data["provider"]
    if provider == "openrouter":
        return PROVIDERS[provider]["endpoint"]
    account_id = str(data.get("account_id", ""))
    model = str(data.get("model", PROVIDERS[provider]["default_model"]))
    return f"{PROVIDERS[provider]['endpoint_base']}/{account_id}/ai/run/{model}"
