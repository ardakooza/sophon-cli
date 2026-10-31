"""Chat-only AI agent backed by the configured Xernel provider."""

from __future__ import annotations

import json
import os
import shutil
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from xernel.config import PROVIDERS, endpoint_for, read_config
from xernel.core import GREEN, MUTED, RESET, card, display_width, paint, read_prompt, wrap_text

SYSTEM_PROMPT = (
    "You are Xernel Agent, a concise and practical development assistant. "
    "You can discuss code, but you cannot access or modify the user's local files. "
    "Never claim to have run commands or changed files."
)


def _request(config: dict[str, object], messages: list[dict[str, str]]) -> str:
    provider = str(config["provider"])
    payload: dict[str, object] = {"messages": messages}
    if provider == "openrouter":
        payload["model"] = config["model"]
        payload["stream"] = False
    else:
        payload["max_tokens"] = 2048

    request = Request(
        endpoint_for(config),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read(2048).decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Provider returned HTTP {error.code}: {detail or error.reason}") from None
    except (OSError, TimeoutError, URLError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not reach the configured provider: {error}") from None

    if provider == "openrouter":
        try:
            content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("The provider returned an unexpected response.") from None
        if isinstance(content, list):
            content = "\n".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
    else:
        if not result.get("success", True):
            errors = result.get("errors") or []
            raise RuntimeError(str(errors[0].get("message", "Cloudflare request failed")) if errors else "Cloudflare request failed")
        content = (result.get("result") or {}).get("response")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("The provider returned an empty response.")
    return content.strip()


def _load_config() -> dict[str, object] | None:
    try:
        return read_config()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        card(None, [f"Provider configuration is invalid: {error}"])
        return {}


def _ask(config: dict[str, object], history: list[dict[str, str]], prompt: str) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history]
    messages.append({"role": "user", "content": prompt})
    answer = _request(config, messages)
    history.extend([{"role": "user", "content": prompt}, {"role": "assistant", "content": answer}])
    return answer


def _input_bar(config: dict[str, object]) -> str:
    if sys.stdout.isatty():
        width = max(20, shutil.get_terminal_size((80, 24)).columns - 4)
        bg = "\033[48;5;236m"
        placeholder = "Ask Xernel to do anything"
        prompt_width = 4
        placeholder = placeholder[: max(0, width - prompt_width)]
        sys.stdout.write(
            "  " + bg + " " * width + "\r\033[2C" + bg + "› "
            + f"\033[38;5;245m{placeholder}{bg}"
        )
        sys.stdout.write(f"\r\033[{prompt_width}C" + bg + " " * len(placeholder))
        sys.stdout.write(f"\r\033[{prompt_width}C" + bg)
        sys.stdout.flush()
        try:
            return read_prompt("").strip()
        finally:
            sys.stdout.write(RESET)
            sys.stdout.flush()
    return input(paint("› ", GREEN)).strip()


def _render_user_message(message: str) -> None:
    width = max(20, shutil.get_terminal_size((80, 24)).columns - 4)
    background = "\033[48;5;236m" if sys.stdout.isatty() else ""
    reset = RESET if background else ""
    first = True
    for paragraph in message.splitlines() or [message]:
        if not paragraph:
            print("  " + background + " " * width + reset)
            first = True
            continue
        prefix = " › " if first else "   "
        for line in wrap_text(paragraph, width - display_width(prefix)):
            text = prefix + line
            padding = " " * max(0, width - display_width(text))
            print("  " + background + text + padding + reset)
            first = False


def _render_agent_message(message: str) -> None:
    width = max(20, shutil.get_terminal_size((80, 24)).columns - 4)
    print()
    for paragraph in message.splitlines() or [message]:
        if not paragraph:
            print()
            continue
        for line in wrap_text(paragraph, width):
            print("  " + line)
    print()


def _clear_input_echo(message: str) -> None:
    if not sys.stdout.isatty():
        return
    columns = max(1, shutil.get_terminal_size((80, 24)).columns)
    rows = max(1, (4 + display_width(message) + columns - 1) // columns)
    sys.stdout.write(f"\033[{rows}A\r")
    for _ in range(rows):
        sys.stdout.write("\033[2K\033[1B")
    sys.stdout.write(f"\033[{rows}A\r")
    sys.stdout.flush()


def _interactive(config: dict[str, object]) -> int:
    provider = PROVIDERS[str(config["provider"])]["label"]
    model = str(config["model"])
    history: list[dict[str, str]] = []
    print(paint("Xernel Agent · Ask about code, debugging, or development.", MUTED))
    while True:
        try:
            prompt = _input_bar(config)
        except (EOFError, KeyboardInterrupt):
            print("\n" + paint("  › ", GREEN) + paint("Session closed. See you soon.", MUTED))
            return 0
        _clear_input_echo(prompt)
        if not prompt:
            continue
        if prompt.casefold() in {"/exit", "/quit", "q"}:
            print(paint("  › ", GREEN) + paint("Session closed. See you soon.", MUTED))
            return 0
        if prompt.casefold() == "/clear":
            history.clear()
            print(paint("Conversation cleared.", MUTED))
            continue
        _render_user_message(prompt)
        print(paint(f"{provider} · {model} · {os.getcwd()}", MUTED))
        print(paint("Enter sends · /clear resets · /exit closes", MUTED))
        try:
            answer = _ask(config, history, prompt)
        except RuntimeError as error:
            _render_agent_message(f"Request failed: {error}")
            continue
        _render_agent_message(answer)


def main(args: list[str]) -> int:
    one_shot: str | None = None
    if args:
        if len(args) == 2 and args[0] in {"-p", "--prompt"}:
            one_shot = args[1]
        else:
            print('Usage: xernel agent [-p "message"]', file=sys.stderr)
            return 2
    config = _load_config()
    if config is None:
        card(None, ["No provider is configured.", "Run xernel setup first."])
        return 1
    if not config:
        return 1
    if one_shot is None:
        return _interactive(config)
    if not one_shot.strip():
        print("Message cannot be empty.", file=sys.stderr)
        return 2
    try:
        print(_ask(config, [], one_shot))
    except RuntimeError as error:
        card("Agent request failed", [str(error)])
        return 1
    return 0
