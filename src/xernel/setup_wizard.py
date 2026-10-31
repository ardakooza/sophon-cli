"""Keyboard-driven setup wizard for provider connection settings."""

from __future__ import annotations

import getpass
import os
import select
import sys

from xernel.config import PROVIDERS, config_display_path, read_config, write_config
from xernel.core import card, clear_terminal


def _read_key() -> str:
    import termios
    import tty

    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        key = os.read(fd, 1).decode("utf-8", errors="ignore")
        if key == "\x1b":
            if not select.select([fd], [], [], 0.08)[0]:
                return "CANCEL"
            prefix = os.read(fd, 1).decode("utf-8", errors="ignore")
            if prefix not in {"[", "O"}:
                return "CANCEL"
            suffix = os.read(fd, 1).decode("utf-8", errors="ignore")
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


def _choose_provider() -> str | None:
    choices = ["openrouter", "cloudflare"]
    labels = [PROVIDERS[item]["label"] for item in choices]
    selected = 0
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive:
        print("\nProvider setup")
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
            "Setup of Provider",
            [
                "Choose a service for model requests.",
                "",
                f"{'›' if selected == 0 else ' '} {labels[0]}",
                f"{'›' if selected == 1 else ' '} {labels[1]}",
                "",
                "↑/↓ move   Enter/Space select   Q cancel",
            ],
            top_right="1 OF 4",
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
                f"{'›' if selected == 0 else ' '} {labels[0]}",
                f"{'›' if selected == 1 else ' '} {labels[1]}",
                "",
                "↑/↓ move   Enter/Space select   Q cancel",
            ],
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
        rows.extend(f"{'›' if index == selected else ' '} {label}" for index, (_, label) in enumerate(options))
        rows.extend(["", "↑/↓ move   Enter/Space select   Q cancel"])
        card("Setup of Model", rows, top_right="2 OF 4")
        key = _read_key()
        if key in {"UP", "LEFT"}:
            selected = (selected - 1) % len(options)
        elif key in {"DOWN", "RIGHT"}:
            selected = (selected + 1) % len(options)
        elif key in {"ENTER", "SPACE"}:
            return options[selected][0]
        elif key == "CANCEL":
            return None


def run_setup_wizard() -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        card("Xernel", ["Setup requires an interactive terminal.", "Run xernel setup from Termux or a shell."])
        return 1
    try:
        existing = read_config()
    except (OSError, ValueError) as error:
        card("Xernel", ["Existing configuration could not be read.", str(error)])
        return 1

    if existing is not None:
        action = _choose_existing_action(existing)
        if action == "keep":
            card(
                "Xernel",
                [
                    "Current provider settings kept.",
                    f"Provider: {PROVIDERS[existing['provider']]['label']}",
                    f"Model: {existing['model']}",
                ],
            )
            return 0
        if action is None:
            card("Xernel", ["Setup cancelled. No changes were made."])
            return 1

    provider = _choose_provider()
    if provider is None:
        card("Xernel", ["Setup cancelled."])
        return 1

    spec = PROVIDERS[provider]
    same_provider = bool(existing and existing.get("provider") == provider)
    saved_model = (existing or {}).get("model", "") if same_provider else ""
    model_choice = _choose_model(provider, spec["default_model"], saved_model)
    if model_choice is None:
        card("Xernel", ["Setup cancelled."])
        return 1
    if model_choice == "__custom__":
        card("Setup of Custom Model", ["Enter a model ID."], top_right="2 OF 4")
        model = input("Model ID: ").strip()
        if not model:
            card("Xernel", ["A model ID is required.", "No configuration was saved."])
            return 1
    else:
        model = model_choice

    old_key = (existing or {}).get("api_key", "") if same_provider else ""
    key_hint = "leave blank to keep saved key" if old_key else "required"
    credential_label = "API key" if provider == "openrouter" else "API token (Workers AI Read + Edit)"
    card("Setup of Credentials", [f"Enter {credential_label}."], top_right="3 OF 4")
    api_key = getpass.getpass(f"{credential_label} (hidden; {key_hint}): ").strip()
    if not api_key:
        api_key = old_key
    if not api_key:
        card("Xernel", ["An API key/token is required.", "No configuration was saved."])
        return 1

    data = {
        "schema_version": 1,
        "provider": provider,
        "model": model,
        "api_key": api_key,
    }
    if provider == "openrouter":
        data["endpoint"] = spec["endpoint"]
    else:
        old_account = (existing or {}).get("account_id", "") if same_provider else ""
        account_id = input(f"Cloudflare Account ID [{old_account}]: ").strip() or old_account
        if not account_id:
            card("Xernel", ["Cloudflare Account ID is required.", "No configuration was saved."])
            return 1
        if len(account_id) != 32 or any(char not in "0123456789abcdefABCDEF" for char in account_id):
            card("Xernel", ["Account ID must be 32 hexadecimal characters.", "No configuration was saved."])
            return 1
        data["account_id"] = account_id
        data["endpoint"] = f"{spec['endpoint_base']}/{account_id}/ai/run/{model}"

    clear_terminal()
    summary = [
        f"Provider: {spec['label']}",
        f"Model: {model}",
        "Credential: hidden; file is restricted to this user",
    ]
    if provider == "cloudflare":
        summary.append(f"Account ID: {data['account_id']}")
    summary.extend([f"Config: {config_display_path()}", "", "Press Enter to save, or Q to cancel."])
    card("Review Setup", summary, top_right="4 OF 4")
    if sys.stdin.isatty():
        key = _read_key()
        if key not in {"ENTER", "SPACE"}:
            card("Xernel", ["Setup cancelled. No changes were saved."])
            return 1
    else:
        if input("Save settings? [Y/n]: ").strip().lower() in {"n", "no", "q"}:
            card("Xernel", ["Setup cancelled. No changes were saved."])
            return 1

    try:
        path = write_config(data)
    except (OSError, ValueError) as error:
        card("Xernel", ["Could not save configuration.", str(error)])
        return 1
    card(
        "Xernel",
        [
            "SETUP COMPLETE",
            f"Provider: {spec['label']}",
            f"Model: {model}",
            f"Saved: {config_display_path()}",
            "API credential is not displayed.",
        ],
    )
    return 0
