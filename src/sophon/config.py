"""Local provider configuration storage for Sophon."""

from __future__ import annotations

import json
import os
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
    if not isinstance(data, dict) or data.get("provider") not in PROVIDERS:
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
    if source != path:
        write_config(data)
        source.unlink()
    return data


def write_config(data: dict[str, Any]) -> Path:
    provider = data.get("provider")
    if provider not in PROVIDERS:
        raise ValueError("Unsupported provider")
    if not data.get("model") or not data.get("api_key"):
        raise ValueError("Model and API key are required")
    if provider == "cloudflare" and not data.get("account_id"):
        raise ValueError("Cloudflare Account ID is required")

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
    if len(value) <= 4:
        return "••••"
    return "••••••••" + value[-4:]


def endpoint_for(data: dict[str, Any]) -> str:
    provider = data["provider"]
    if provider == "openrouter":
        return PROVIDERS[provider]["endpoint"]
    account_id = str(data.get("account_id", ""))
    model = str(data.get("model", PROVIDERS[provider]["default_model"]))
    return f"{PROVIDERS[provider]['endpoint_base']}/{account_id}/ai/run/{model}"
