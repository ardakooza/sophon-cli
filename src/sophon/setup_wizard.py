"""Keyboard-driven setup wizard for provider connection settings."""

from __future__ import annotations

import codecs
import json
import os
import select
import sys

from sophon.config import (
    PROVIDERS,
    agent_instructions_path,
    config_display_path,
    config_path,
    detect_environment,
    ensure_agent_instructions_file,
    read_config,
    write_config,
)
from sophon.core import (
    MUTED,
    WHITE,
    auto_compression_settings,
    auto_update_enabled,
    card,
    clear_terminal,
)


def _read_key(timeout: float = 0.2) -> str:
    import termios
    import tty

    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        if not select.select([fd], [], [], timeout)[0]:
            return "TIMEOUT"
        key = os.read(fd, 1).decode("utf-8", errors="ignore")
        if key == "\x1b":
            if not select.select([fd], [], [], 0.08)[0]:
                return "CANCEL"
            sequence = bytearray()
            while select.select([fd], [], [], 0.08)[0]:
                sequence.extend(os.read(fd, 1))
                if len(sequence) >= 2 and 0x40 <= sequence[-1] <= 0x7E:
                    break
            if not sequence or sequence[0] not in {ord("["), ord("O")}:
                return "CANCEL"
            suffix = chr(sequence[-1])
            return {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}.get(suffix, "OTHER")
        if key in {"\r", "\n"}:
            return "ENTER"
        if key == " ":
            return "SPACE"
        if key in {"q", "Q", "\x03"}:
            return "CANCEL"
        return "OTHER"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def _read_hidden_credential(
    credential_label: str,
    provider_label: str,
    model: str,
    *,
    saved_key_exists: bool,
) -> str | None:
    import termios
    import tty

    fd = sys.stdin.fileno()
    decoder = codecs.getincrementaldecoder("utf-8")("ignore")
    characters: list[str] = []
    last_size = None

    def redraw() -> None:
        nonlocal last_size
        size = os.get_terminal_size(sys.stdout.fileno()) if sys.stdout.isatty() else None
        last_size = size
        clear_terminal()
        value = "•" * len(characters) if characters else "[hidden input]"
        rows = [f"Provider: {provider_label}", f"Model: {model}", "", f"{credential_label}: {value}"]
        if saved_key_exists and not characters:
            rows.extend(["", "Enter to keep the saved credential."])
        rows.extend(["", "Enter continue · Backspace edit · Esc cancel"])
        card("Setup of Credentials", rows, top_right="3 of 4")

    def read_byte(timeout: float = 0.2) -> bytes:
        if not select.select([fd], [], [], timeout)[0]:
            return b""
        previous = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            return os.read(fd, 1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, previous)

    redraw()
    while True:
        try:
            size = os.get_terminal_size(sys.stdout.fileno()) if sys.stdout.isatty() else None
            if size != last_size:
                redraw()
            raw = read_byte()
        except (OSError, termios.error):
            return None
        if not raw:
            continue
        if raw in {b"\x03", b"\x1b"}:
            if raw == b"\x1b":
                # Consume any arrow-key sequence so it cannot become credential text.
                if select.select([fd], [], [], 0.03)[0]:
                    read_byte(0)
                    while select.select([fd], [], [], 0.01)[0]:
                        read_byte(0)
                else:
                    return None
            else:
                return None
        elif raw in {b"\r", b"\n"}:
            return "".join(characters)
        elif raw in {b"\x08", b"\x7f"}:
            if characters:
                characters.pop()
                redraw()
        else:
            character = decoder.decode(raw)
            if character and character.isprintable():
                characters.append(character)
                redraw()


def _choose_provider(default_provider: str | None = None, *, title: str = "Setup of Provider") -> str | None:
    choices = ["openrouter", "cloudflare"]
    labels = [PROVIDERS[item]["label"] for item in choices]
    selected = choices.index(default_provider) if default_provider in choices else 0
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive:
        print(f"\n{title}")
        for index, label in enumerate(labels, 1):
            print(f"  {index}. {label}")
        print("  0. Cancel")
        answer = input("Choose provider [1]: ").strip()
        if not answer:
            return choices[0]
        if answer == "0":
            return None
        if answer in {"1", "2"}:
            return choices[int(answer) - 1]
        normalized = answer.casefold()
        return next((key for key, label in zip(choices, labels) if normalized == label.casefold()), None)

    while True:
        clear_terminal()
        card(
            title,
            [
                "Choose a service for model requests.",
                "",
                f"{'❯' if selected == 0 else '·'} {labels[0]}",
                "",
                f"{'❯' if selected == 1 else '·'} {labels[1]}",
                "",
                "←/→ previous/next · ↑/↓ move · Enter select · Esc cancel",
            ],
            top_right="1 of 4",
        )
        key = _read_key()
        if key in {"UP", "LEFT"}:
            selected = (selected - 1) % len(choices)
        elif key in {"DOWN", "RIGHT"}:
            selected = (selected + 1) % len(choices)
        elif key in {"ENTER", "SPACE"}:
            return choices[selected]
        elif key == "CANCEL":
            return None


def _choose_existing_action(existing: dict[str, object]) -> str | None:
    choices = ["keep", "reconfigure"]
    labels = ["Keep current settings", "Reconfigure provider"]
    provider = str(existing["provider"])
    selected = 0
    while True:
        clear_terminal()
        card(
            "Current Provider",
            [
                f"{PROVIDERS[provider]['label']} · {existing['model']}",
                "",
                f"{'❯' if selected == 0 else '·'} {labels[0]}",
                "",
                f"{'❯' if selected == 1 else '·'} {labels[1]}",
                "",
                "↑/↓ move · Enter select · Esc cancel",
            ],
            top_right=f"{selected + 1} of {len(labels)}",
        )
        key = _read_key()
        if key in {"UP", "LEFT"}:
            selected = (selected - 1) % len(choices)
        elif key in {"DOWN", "RIGHT"}:
            selected = (selected + 1) % len(choices)
        elif key in {"ENTER", "SPACE"}:
            return choices[selected]
        elif key == "CANCEL":
            return None


def _choose_model(provider: str, default_model: str, saved_model: str = "") -> str | None:
    options: list[tuple[str, str]] = []
    if saved_model and saved_model != default_model:
        options.append((saved_model, f"Keep saved model · {saved_model}"))
    options.append((default_model, f"Recommended · {default_model}"))
    options.append(("__custom__", "Enter a custom model ID"))
    selected = 0
    while True:
        clear_terminal()
        rows = [
            f"Provider: {PROVIDERS[provider]['label']}",
            "Choose a preset or enter your own model ID.",
            "",
        ]
        for index, (_, label) in enumerate(options):
            rows.append(f"{'❯' if index == selected else '·'} {label}")
            if index + 1 < len(options):
                rows.append("")
        rows.extend(["", "↑/↓ move · Enter select · Esc cancel"])
        card("Setup of Model", rows, top_right="2 of 4")
        key = _read_key()
        if key in {"UP", "LEFT"}:
            selected = (selected - 1) % len(options)
        elif key in {"DOWN", "RIGHT"}:
            selected = (selected + 1) % len(options)
        elif key in {"ENTER", "SPACE"}:
            return options[selected][0]
        elif key == "CANCEL":
            return None


def run_setup_wizard(*, reconfigure: bool = False) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        card("Sophon", ["Setup requires an interactive terminal.", "Run sophon setup from Termux or a shell."])
        return 1
    try:
        existing = read_config()
        if existing is None:
            try:
                raw_preferences = json.loads(config_path().read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                raw_preferences = None
            if isinstance(raw_preferences, dict) and raw_preferences.get("provider") is None:
                existing = raw_preferences
    except (OSError, ValueError) as error:
        card("Sophon", ["Existing configuration could not be read.", str(error)])
        return 1

    if existing is not None and existing.get("provider") is not None and not reconfigure:
        action = _choose_existing_action(existing)
        if action == "keep":
            card(
                "Sophon",
                [
                    "Current provider settings kept.",
                    f"Provider: {PROVIDERS[existing['provider']]['label']}",
                    f"Model: {existing['model']}",
                ],
            )
            return 0
        if action is None:
            card(None, ["Setup cancelled."], width=16)
            return 1

    provider = _choose_provider(
        str(existing.get("provider")) if reconfigure and existing and existing.get("provider") else None,
        title="Reconfiguration" if reconfigure else "Setup of Provider",
    )
    if provider is None:
        card(None, ["Setup cancelled."], width=16)
        return 1

    spec = PROVIDERS[provider]
    same_provider = bool(existing and existing.get("provider") == provider)
    saved_model = (existing or {}).get("model", "") if same_provider else ""
    model_choice = _choose_model(provider, spec["default_model"], saved_model)
    if model_choice is None:
        card(None, ["Setup cancelled."], width=16)
        return 1
    if model_choice == "__custom__":
        card("Setup of Custom Model", ["Enter a model ID."], top_right="2 of 4")
        model = input("Model ID: ").strip()
        if not model:
            card("Sophon", ["A model ID is required.", "No configuration was saved."])
            return 1
    else:
        model = model_choice

    old_key = (existing or {}).get("api_key", "") if same_provider else ""
    credential_label = "API key" if provider == "openrouter" else "API token (Workers AI Read + Edit)"
    api_key = _read_hidden_credential(
        credential_label,
        spec["label"],
        model,
        saved_key_exists=bool(old_key),
    )
    if api_key is None:
        card(None, ["Setup cancelled."], width=16)
        return 1
    api_key = api_key.strip()
    if not api_key:
        api_key = old_key
    if not api_key:
        card("Sophon", ["An API key/token is required.", "No configuration was saved."])
        return 1

    auto_compression, auto_compression_threshold = auto_compression_settings(existing)
    data = {
        "schema_version": 1,
        "provider": provider,
        "model": model,
        "api_key": api_key,
        "Environment": detect_environment(),
        "agent_instructions_path": str(agent_instructions_path()),
        "auto_update": auto_update_enabled(),
        "auto_compression": auto_compression,
        "auto_compression_threshold": auto_compression_threshold,
        "allowed_path": (existing or {}).get("allowed_path", []),
        "mode": (existing or {}).get("mode", {"plan": [], "build": [], "chat": []}),
    }
    if provider == "openrouter":
        data["endpoint"] = spec["endpoint"]
    else:
        old_account = (existing or {}).get("account_id", "") if same_provider else ""
        account_id = input(f"Cloudflare Account ID [{old_account}]: ").strip() or old_account
        if not account_id:
            card("Sophon", ["Cloudflare Account ID is required.", "No configuration was saved."])
            return 1
        if len(account_id) != 32 or any(char not in "0123456789abcdefABCDEF" for char in account_id):
            card("Sophon", ["Account ID must be 32 hexadecimal characters.", "No configuration was saved."])
            return 1
        data["account_id"] = account_id
        data["endpoint"] = f"{spec['endpoint_base']}/{account_id}/ai/run/{model}"

    saved_reasoning = (existing or {}).get("reasoning") if same_provider and saved_model == model else None
    if isinstance(saved_reasoning, dict):
        data["reasoning"] = saved_reasoning
    else:
        card("Model capabilities", [f"Checking reasoning support for {model}…"])
        try:
            from sophon.agent import _detect_reasoning_support

            is_support = _detect_reasoning_support(data)
        except (OSError, RuntimeError, ValueError):
            is_support = False
        data["reasoning"] = {"is_support": is_support, "enabled": False, "effort": "medium"}

    clear_terminal()
    summary = [
        f"Provider: {spec['label']}",
        f"Model: {model}",
        "Credential: hidden",
    ]
    if provider == "cloudflare":
        summary.append(f"Account ID: {data['account_id']}")
    summary.extend([f"Config: {config_display_path()}", "", "Press Enter to save, or Q to cancel."])
    card(
        "Setup Complete",
        summary,
        text_color=MUTED,
        title_color=WHITE,
        highlight_terms=("Provider:", "Model:", "Credential:", "Config:"),
    )
    if sys.stdin.isatty():
        key = _read_key()
        if key not in {"ENTER", "SPACE"}:
            card(None, ["Setup cancelled."], width=16)
            return 1
    else:
        if input("Save settings? [Y/n]: ").strip().lower() in {"n", "no", "q"}:
            card(None, ["Setup cancelled."], width=16)
            return 1

    try:
        ensure_agent_instructions_file(agent_instructions_path())
        path = write_config(data)
    except (OSError, ValueError) as error:
        card("Sophon", ["Could not save configuration.", str(error)])
        return 1
    return 0
