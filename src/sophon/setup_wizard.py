"""Keyboard-driven setup wizard for provider connection settings."""

from __future__ import annotations

import codecs
import json
import os
import select
import shutil
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
    saved_key: str = "",
) -> str | None:
    """Read a masked credential in-place; saved credentials are never echoed."""
    import termios
    import tty

    fd = sys.stdin.fileno()
    decoder = codecs.getincrementaldecoder("utf-8")("ignore")
    characters: list[str] = []
    last_size = None
    selected_action = 0
    action_choices = ["Keep saved key", "New key"]
    action = "keep" if saved_key_exists else "new"

    def redraw(*, place_cursor: bool = True) -> None:
        nonlocal last_size
        size = shutil.get_terminal_size((80, 24))
        last_size = size
        clear_terminal()
        rows = [f"Provider: {provider_label}", f"Model: {model}", ""]
        input_row = len(rows)
        rows.append(f"{credential_label}: " + "•" * len(characters))
        if action == "choose":
            rows.extend(["", *(f"{'›' if i == selected_action else ' '} {label}" for i, label in enumerate(action_choices))])
            rows.extend(["", "↑/↓ choose · Enter select · Esc cancel"])
        else:
            rows.extend(["", "Enter continue · Esc cancel"])
        card("Setup of Credentials", rows, top_right="3 of 4", text_color=MUTED, title_color=WHITE, highlight_terms=("Provider:", "Model:", f"{credential_label}:", "Keep saved key", "New key", "›"))
        if place_cursor and action == "new":
            # Return the terminal cursor to the credential row, immediately after its masked value.
            lines_after_input = len(rows) + 1 - input_row
            column = 4 + len(credential_label) + 2 + len(characters)
            if sys.stdout.isatty():
                try:
                    sys.stdout.fileno()
                except (AttributeError, OSError, ValueError):
                    pass
                else:
                    sys.stdout.write(f"\033[{lines_after_input}A\r\033[{column}C")
                    sys.stdout.flush()

    def read_byte(timeout: float = 0.2) -> bytes:
        if not select.select([fd], [], [], timeout)[0]:
            return b""
        return os.read(fd, 1)

    if saved_key_exists:
        action = "choose"
        redraw(place_cursor=False)
    else:
        action = "new"
        redraw()
    previous = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            try:
                size = shutil.get_terminal_size((80, 24))
                if size != last_size:
                    redraw(place_cursor=action == "new")
                raw = read_byte()
            except (OSError, termios.error):
                return None
            if not raw:
                continue
            if raw == b"\x1b":
                # Distinguish a standalone Escape from arrow-key sequences. Never feed
                # escape bytes to the secret decoder (which previously rendered as "^[").
                if select.select([fd], [], [], 0.05)[0]:
                    first = read_byte(0)
                    sequence = bytearray(first)
                    while first and select.select([fd], [], [], 0.01)[0]:
                        first = read_byte(0)
                        sequence.extend(first)
                        if len(sequence) >= 2 and first and 0x40 <= first[-1] <= 0x7E:
                            break
                    if action == "choose" and sequence.startswith(b"["):
                        suffix = sequence[-1:]
                        if suffix == b"A":
                            selected_action = (selected_action - 1) % len(action_choices)
                            redraw(place_cursor=False)
                        elif suffix == b"B":
                            selected_action = (selected_action + 1) % len(action_choices)
                            redraw(place_cursor=False)
                    continue
                return None
            if raw == b"\x03":
                return None
            if action == "choose":
                if raw in {b"\r", b"\n"}:
                    if selected_action == 0:
                        return saved_key
                    action = "new"
                    redraw()
                    continue
                if raw in {b"k", b"K"}:
                    selected_action = (selected_action - 1) % len(action_choices)
                    redraw(place_cursor=False)
                elif raw in {b"j", b"J"}:
                    selected_action = (selected_action + 1) % len(action_choices)
                    redraw(place_cursor=False)
                continue
            if raw in {b"\r", b"\n"}:
                return "".join(characters)
            if raw in {b"\x08", b"\x7f"}:
                if characters:
                    characters.pop()
                    redraw()
                continue
            character = decoder.decode(raw)
            if character and character.isprintable():
                characters.append(character)
                redraw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)

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
        saved_key=old_key,
    )
    if api_key is None:
        card(None, ["Setup cancelled."], width=16)
        return 1
    api_key = api_key.strip()
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
