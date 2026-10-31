"""Chat-only AI agent backed by the configured Sophon provider."""

from __future__ import annotations

import codecs
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sophon.config import PROVIDERS, endpoint_for, read_config, write_config
from sophon.core import BORDER, MUTED, NOTICE_BORDER, NOTICE_TEXT, WHITE, card, centered, display_width, paint, wrap_text

SESSION_FILE = Path.home() / ".sophon" / "sessions" / "sessions.json"
LEGACY_SESSION_FILE = Path.home() / ".xernel" / "sessions" / "sessions.json"
INPUT_PLACEHOLDER = "Create something with sophon"

SYSTEM_PROMPT = (
    "You are Sophon, a concise and practical development assistant. "
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

    if not isinstance(result, dict):
        raise RuntimeError("The provider returned an unexpected response.")
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
            message = next(
                (item.get("message") for item in errors if isinstance(item, dict) and item.get("message")),
                "Cloudflare request failed",
            )
            raise RuntimeError(str(message))
        cloudflare_result = result.get("result")
        if not isinstance(cloudflare_result, dict):
            raise RuntimeError("The provider returned an unexpected response.")
        content = cloudflare_result.get("response")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("The provider returned an empty response.")
    return content.strip()


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


def _ask(config: dict[str, object], history: list[dict[str, str]], prompt: str) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *(message for message in history if message.get("role") in {"user", "assistant"}),
    ]
    messages.append({"role": "user", "content": prompt})
    answer = _request(config, messages)
    history.extend([{"role": "user", "content": prompt}, {"role": "assistant", "content": answer}])
    return answer


def _timestamp() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _new_session() -> dict[str, object]:
    now = _timestamp()
    return {"id": uuid.uuid4().hex[:10], "title": "New session", "created_at": now, "updated_at": now, "messages": []}


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


def _card_rows(
    title: str | None,
    lines: list[str],
    *,
    columns: int,
    width: int | None = None,
    top_right: str | None = None,
    border_color: str = BORDER,
    text_color: str = WHITE,
) -> list[str]:
    inner = min(width, max(3, columns - 8)) if width is not None else min(68, max(20, columns - 8))
    top = "  ╭" + "─" * (inner + 2) + "╮"
    bottom = "  ╰" + "─" * (inner + 2) + "╯"
    rows = [paint(top, border_color)]
    content = list(lines)
    if title is not None and top_right is not None:
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
            rows.append(paint("  │", border_color) + paint(row, text_color) + paint("│", border_color))
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


def _message_rows(messages: list[dict[str, str]], columns: int) -> list[str]:
    rows: list[str] = []
    for message in messages:
        role, content = message["role"], message["content"]
        if role == "user":
            rows.extend(_user_rows(content, columns))
        elif role == "assistant":
            rows.extend(_agent_rows(content, columns))
        else:
            rows.extend(
                _card_rows(
                    "Agent request failed",
                    [content],
                    columns=columns,
                    border_color=NOTICE_BORDER,
                    text_color=NOTICE_TEXT,
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
                content += paint(content_text[actual_count:], MUTED)
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


def _draw_screen(
    session: dict[str, object],
    text: str,
    cursor: int,
    *,
    placeholder: str = INPUT_PLACEHOLDER,
    panel_rows: list[str] | None = None,
    notice_rows: list[str] | None = None,
    suggestion: str = "",
    status_message: str | None = None,
) -> tuple[int, int]:
    size = shutil.get_terminal_size((80, 24))
    columns, height = size.columns, max(8, size.lines)
    width = min(68, max(20, columns - 8))
    header = [] if session["messages"] else _card_rows(
        "Sophon Agent",
        ["Ask about code, debugging, and development."],
        columns=columns,
    )
    editor_lines, cursor_row, cursor_col = _editor_layout(text, cursor, width, placeholder, suggestion)
    max_editor_lines = max(1, min(5, height - len(header) - 3))
    visible_start = max(0, min(cursor_row, len(editor_lines) - max_editor_lines))
    visible_lines = editor_lines[visible_start : visible_start + max_editor_lines]
    box = _input_box_rows(visible_lines, width, placeholder=not text, text_length=len(text))

    if panel_rows is not None:
        feed = panel_rows
    else:
        feed = list(notice_rows or []) + _message_rows(session["messages"], columns)
        if status_message:
            feed.extend(_card_rows(None, [status_message], columns=columns, width=display_width(status_message)))
    room = max(0, height - len(header) - len(box))
    visible_feed = feed[-room:] if room else []
    if panel_rows is not None and room >= 2 and len(panel_rows) > room:
        visible_feed = [panel_rows[0], *panel_rows[1 : room - 1], panel_rows[-1]]
    screen = header + visible_feed + [""] * max(0, room - len(visible_feed)) + box
    screen = screen[-height:]
    if len(screen) < height:
        screen = [""] * (height - len(screen)) + screen
    output = "\033[2J\033[H" + "\r\n".join(screen)
    absolute_row = height - len(box) + 1 + cursor_row - visible_start
    absolute_col = 4 + cursor_col
    output += f"\033[{max(1, absolute_row + 1)};{max(1, absolute_col + 1)}H"
    sys.stdout.write(output)
    sys.stdout.flush()
    return cursor_row, cursor_col


def _read_key(timeout: float | None = None) -> str:
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
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
            return {b"A": "UP", b"B": "DOWN", b"C": "RIGHT", b"D": "LEFT"}.get(bytes(sequence[-1:]), "OTHER")
        try:
            character = first.decode("utf-8")
        except UnicodeDecodeError:
            return "OTHER"
        return f"CHAR:{character}" if character.isprintable() else "OTHER"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def _input_bar(draw, placeholder: str = INPUT_PLACEHOLDER) -> str:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return input(f"› {placeholder}: ").strip()

    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    decoder = codecs.getincrementaldecoder(sys.stdin.encoding or "utf-8")()
    characters: list[str] = []
    cursor = 0
    last_size = shutil.get_terminal_size((80, 24))

    def redraw() -> None:
        text = "".join(characters)
        draw(text, cursor, placeholder=placeholder)

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
                return "".join(characters).strip()
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
            character = decoder.decode(raw)
            if character and character.isprintable():
                characters[cursor:cursor] = list(character)
                cursor += len(character)
                redraw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def _model_request(url: str, token: str) -> dict[str, object]:
    request = Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(request, timeout=30) as response:
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


def _list_models(config: dict[str, object], search: str = "") -> list[dict[str, str]]:
    provider = str(config["provider"])
    token = str(config["api_key"])
    if provider == "openrouter":
        result = _model_request("https://openrouter.ai/api/v1/models", token)
        data = result.get("data")
        if not isinstance(data, list):
            raise RuntimeError("OpenRouter returned an invalid model list.")
        all_models = [
            {"id": str(item["id"]), "name": str(item.get("name") or item["id"])}
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
        result = _model_request(base + "?" + urlencode(params), token)
        data = result.get("result")
        if not isinstance(data, list):
            raise RuntimeError("Cloudflare returned an invalid model list.")
        batch = []
        for item in data:
            if not isinstance(item, dict):
                continue
            model_id = item.get("name") or item.get("id") or item.get("model")
            if model_id:
                batch.append({"id": str(model_id), "name": str(item.get("display_name") or item.get("description") or model_id)})
        all_models.extend(batch)
        info = result.get("result_info")
        if len(data) < 100 or (isinstance(info, dict) and page >= int(info.get("total_pages", page))):
            break
    return sorted(all_models, key=lambda model: model["id"].casefold())


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
        input_count = 3
        room = max(1, size.lines - header_count - input_count)
        compact = room < 13
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
                range_label += f" · {len(filtered)} models"
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


def _interactive(config: dict[str, object]) -> int:
    try:
        sessions = _load_sessions()
    except RuntimeError as error:
        card("Session storage error", [str(error)], border_color=NOTICE_BORDER, text_color=NOTICE_TEXT)
        return 1
    session = _new_session()
    notice_rows: list[str] = []
    waiting = {"message": None}

    def draw(
        text: str,
        cursor: int,
        *,
        placeholder: str = INPUT_PLACEHOLDER,
        panel_rows: list[str] | None = None,
        suggestion: str = "",
    ) -> None:
        _draw_screen(
            session,
            text,
            cursor,
            placeholder=placeholder,
            panel_rows=panel_rows,
            notice_rows=notice_rows,
            suggestion=suggestion,
            status_message=waiting["message"],
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

    def save_current() -> bool:
        try:
            _save_session(sessions, session)
            return True
        except OSError as error:
            notice("Session storage error", str(error), error=True)
            return False

    while True:
        try:
            prompt = _input_bar(draw)
        except (EOFError, KeyboardInterrupt):
            sys.stdout.write("\033[2J\033[H" + paint("Session closed. See you soon.", MUTED) + "\r\n")
            return 0
        if not prompt:
            notice_rows = []
            continue
        if prompt.casefold() == "q":
            sys.stdout.write("\033[2J\033[H" + paint("Session closed. See you soon.", MUTED) + "\r\n")
            return 0
        if prompt.startswith("/"):
            command, _, argument = prompt[1:].partition(" ")
            command = command.casefold()
            argument = argument.strip()
            notice_rows = []
            if command in {"exit", "quit"}:
                sys.stdout.write("\033[2J\033[H" + paint("Session closed. See you soon.", MUTED) + "\r\n")
                return 0
            if command == "help":
                notice_rows = _card_rows(
                    "Sophon Commands",
                    ["/model [search]  Choose a provider model", "/new  Start a fresh saved session", "/rename [title]  Rename this session", "/resume  Load a saved session", "/clear  Start a new session", "/exit or Q  Close Sophon Agent"],
                    columns=shutil.get_terminal_size((80, 24)).columns,
                )
            elif command in {"new", "clear"}:
                session = _new_session()
                notice_plain("New session")
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
                    notice_rows = []
                else:
                    notice("Resume cancelled", "The current session is still open.")
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
                    {"id": model["id"], "label": f"{model['id']} · {model['name']}"}
                    for model in models
                ]
                selected = _pick_item(session, draw, items, title="Available Models", current_id=str(config["model"]))
                if selected:
                    config["model"] = str(selected["id"])
                    if config["provider"] == "cloudflare":
                        config["endpoint"] = endpoint_for(config)
                    try:
                        write_config(config)
                    except (OSError, ValueError) as error:
                        notice("Model not changed", str(error), error=True)
                    else:
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
        waiting["message"] = "Sophon is thinking…"
        draw("", 0)
        try:
            answer = _request(config, [{"role": "system", "content": SYSTEM_PROMPT}, *(item for item in messages if item["role"] in {"user", "assistant"})])
        except RuntimeError as error:
            waiting["message"] = None
            messages.append({"role": "error", "content": str(error)})
            save_current()
            continue
        waiting["message"] = None
        messages.append({"role": "assistant", "content": answer})
        if not save_current():
            notice("Session storage error", "The reply is visible but could not be saved.", error=True)


def main(args: list[str]) -> int:
    one_shot: str | None = None
    if args:
        if len(args) == 2 and args[0] in {"-p", "--prompt"}:
            one_shot = args[1]
        else:
            card(
                "Agent request failed",
                ['Usage: sophon agent [-p "message"]'],
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
    card(None, ["Sophon is thinking…"], width=display_width("Sophon is thinking…"))
    try:
        _render_agent_message(_ask(config, [], one_shot))
    except RuntimeError as error:
        card(
            "Agent request failed",
            [str(error)],
            border_color=NOTICE_BORDER,
            text_color=NOTICE_TEXT,
        )
        return 1
    return 0


def _render_agent_message(message: str) -> None:
    for row in _agent_rows(message, shutil.get_terminal_size((80, 24)).columns):
        print(row)
