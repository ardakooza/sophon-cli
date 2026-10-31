"""Chat-only AI agent backed by the configured Xernel provider."""

from __future__ import annotations

import json
import os
import shutil
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from xernel.config import PROVIDERS, endpoint_for, read_config
from xernel.core import GREEN, MUTED, RESET, card, paint, read_prompt

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
        width = max(20, shutil.get_terminal_size((80, 24)).columns - 2)
        bg = "\033[48;5;236m"
        placeholder = "Ask Xernel to do anything"
        prompt_width = 4
        placeholder = placeholder[: max(0, width - prompt_width)]
        sys.stdout.write(bg + " " * width + "\r" + bg + "  › " + paint(placeholder, MUTED))
        sys.stdout.write(f"\r\033[{prompt_width}C" + " " * len(placeholder))
        sys.stdout.write(f"\r\033[{prompt_width}C")
        sys.stdout.flush()
        try:
            return read_prompt("").strip()
        finally:
            sys.stdout.write(RESET)
            sys.stdout.flush()
    return input(paint("› ", GREEN)).strip()


def _interactive(config: dict[str, object]) -> int:
    provider = PROVIDERS[str(config["provider"])]["label"]
    model = str(config["model"])
    history: list[dict[str, str]] = []
    card("Xernel Agent", ["Ask about code, debugging, or development.", "This session cannot read or edit local files."])
    while True:
        try:
            prompt = _input_bar(config)
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not prompt:
            continue
        if prompt.casefold() in {"/exit", "/quit"}:
            return 0
        if prompt.casefold() == "/clear":
            history.clear()
            card(None, ["Conversation cleared."])
            continue
        card(None, [f"{provider} · {model} · {os.getcwd()}", "Enter sends · /clear resets · /exit closes"])
        try:
            answer = _ask(config, history, prompt)
        except RuntimeError as error:
            card("Agent request failed", [str(error)])
            continue
        card("You", [prompt])
        card("Xernel Agent", answer.splitlines() or [answer])


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
