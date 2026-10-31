"""Chat-only AI agent backed by the configured Xernel provider."""

from __future__ import annotations

import codecs
import json
import os
import shutil
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from xernel.config import endpoint_for, read_config
from xernel.core import BORDER, MUTED, WHITE, card, display_width, paint, wrap_text

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


def _input_bar() -> str:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return input("› Ask Codex to do anything: ").strip()

    import select
    import termios
    import tty

    inner = min(68, max(20, shutil.get_terminal_size((80, 24)).columns - 8))
    placeholder = "Ask Codex to do anything"
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    characters: list[str] = []
    rows: list[str] = []
    cursor_row = 0
    cursor_col = 2
    previous_height = 0
    decoder = codecs.getincrementaldecoder(sys.stdin.encoding or "utf-8")()

    def layout() -> tuple[list[str], int, int]:
        lines = ["› "]
        placeholder_mode = not characters
        text = placeholder if placeholder_mode else "".join(characters)
        for character in text:
            if display_width(lines[-1]) + display_width(character) > inner:
                lines.append("")
            lines[-1] += character
        if placeholder_mode:
            return lines, 0, 2
        return lines, len(lines) - 1, display_width(lines[-1])

    def clear_box() -> None:
        nonlocal previous_height
        if not previous_height:
            return
        sys.stdout.write(f"\033[{cursor_row + 1}A\r")
        for index in range(previous_height):
            sys.stdout.write("\033[2K")
            if index < previous_height - 1:
                sys.stdout.write("\033[1B")
        if previous_height > 1:
            sys.stdout.write(f"\033[{previous_height - 1}A\r")
        previous_height = 0

    def redraw() -> None:
        nonlocal rows, cursor_row, cursor_col, previous_height
        clear_box()
        rows, cursor_row, cursor_col = layout()
        top = "  ╭" + "─" * (inner + 2) + "╮"
        bottom = "  ╰" + "─" * (inner + 2) + "╯"
        sys.stdout.write(paint(top, BORDER) + "\r\n")
        for index, row in enumerate(rows):
            prefix_width = display_width(row)
            padding = " " * max(0, inner - prefix_width)
            if index == 0 and row.startswith("› "):
                visible = paint("› ", WHITE) + paint(row[2:], MUTED if not characters else WHITE)
            else:
                visible = paint(row, MUTED if not characters else WHITE)
            sys.stdout.write(
                paint("  │ ", BORDER) + visible + padding + " " + paint("│", BORDER) + "\r\n"
            )
        sys.stdout.write(paint(bottom, BORDER) + "\r\n")
        previous_height = len(rows) + 2
        delta = previous_height - 1 - cursor_row
        sys.stdout.write(f"\033[{delta}A\r\033[{4 + cursor_col}C")
        sys.stdout.flush()

    def remove_box() -> None:
        clear_box()
        sys.stdout.flush()

    try:
        tty.setraw(fd)
        redraw()
        while True:
            raw = os.read(fd, 1)
            if not raw:
                remove_box()
                raise EOFError
            if raw == b"\x1b":
                if not select.select([fd], [], [], 0.08)[0]:
                    remove_box()
                    raise EOFError
                sequence = bytearray()
                while select.select([fd], [], [], 0.08)[0]:
                    sequence.extend(os.read(fd, 1))
                    if len(sequence) >= 2 and 0x40 <= sequence[-1] <= 0x7E:
                        break
                continue
            if raw in {b"\r", b"\n"}:
                remove_box()
                return "".join(characters).strip()
            if raw == b"\x03":
                remove_box()
                raise KeyboardInterrupt
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


def _render_user_message(message: str) -> None:
    terminal_width = max(28, shutil.get_terminal_size((80, 24)).columns - 8)
    width = min(68, max(3, min(terminal_width, display_width(message) + 2)))
    card(None, [f"› {message}"], width=width)


def _render_agent_message(message: str) -> None:
    width = max(20, shutil.get_terminal_size((80, 24)).columns - 4)
    print()
    first = True
    for paragraph in message.splitlines() or [message]:
        if not paragraph:
            print()
            continue
        text = paragraph
        if first and text.lstrip().startswith(("• ", "- ", "* ")):
            text = text.lstrip()[2:]
        prefix = "    • " if first else "      "
        for line in wrap_text(text, width - display_width(prefix)):
            if first:
                print(paint(prefix, MUTED) + line)
                first = False
            else:
                print("      " + line)
    print()


def _interactive(config: dict[str, object]) -> int:
    history: list[dict[str, str]] = []
    card("Xernel Agent", ["Ask about code, debugging, or development."])
    while True:
        try:
            prompt = _input_bar()
        except (EOFError, KeyboardInterrupt):
            print(paint("Session closed. See you soon.", MUTED))
            return 0
        if not prompt:
            continue
        if prompt.casefold() in {"/exit", "/quit", "q"}:
            print(paint("Session closed. See you soon.", MUTED))
            return 0
        if prompt.casefold() == "/clear":
            history.clear()
            print(paint("Conversation cleared.", MUTED))
            continue
        _render_user_message(prompt)
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
