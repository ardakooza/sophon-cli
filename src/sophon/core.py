"""Shared, dependency-free Sophon CLI and terminal UI."""

from __future__ import annotations

import base64
import codecs
import json
import os
import platform
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from sophon import intelligent_ui
from sophon.config import (
    PROVIDERS,
    config_display_path,
    config_path,
    detect_environment,
    ensure_environment_config,
    read_config,
    write_config,
)
from sophon import __version__ as VERSION

RESET = "\033[0m"
BORDER = "\033[38;5;252m"
NOTICE_BORDER = "\033[38;5;88m"
NOTICE_TEXT = "\033[38;5;160m"
MUTED = "\033[38;5;245m"
WHITE = "\033[38;5;255m"
GREEN = "\033[38;5;114m"
UPDATE_BORDER = "\033[38;5;179m"
UPDATE_TEXT = "\033[38;5;221m"
LATEST_VERSION_URL = "https://raw.githubusercontent.com/ardakooza/sophon-cli/main/src/sophon/VERSION"
LATEST_VERSION_API_URL = "https://api.github.com/repos/ardakooza/sophon-cli/contents/src/sophon/VERSION"
RESTART_REQUESTED = False
RESTART_ARGS: list[str] | None = None
CONFIG_BACKUP_PATH = Path.home() / ".sophon" / "backup" / "config_backup.json"


def paint(text: str, color: str) -> str:
    return f"{color}{text}{RESET}" if intelligent_ui.color_enabled() else text


def display_home_paths(value: str | Path) -> str:
    text = str(value)
    home = Path.home().as_posix().rstrip("/")
    if not home:
        return text
    pattern = rf"(?<![A-Za-z0-9_.-]){re.escape(home)}(?=$|[/\\\s:])"
    return re.sub(pattern, "~", text)


def character_width(char: str) -> int:
    """Approximate terminal cell width using the Unicode database in stdlib."""
    code = ord(char)
    category = unicodedata.category(char)
    if (
        unicodedata.combining(char)
        or category in {"Mn", "Me", "Cf"}
        or 0xFE00 <= code <= 0xFE0F
        or 0xE0100 <= code <= 0xE01EF
        or 0x1F3FB <= code <= 0x1F3FF
        or code == 0x200D
    ):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def display_width(text: str) -> int:
    """Return approximate terminal columns, accounting for wide/joined glyphs."""
    width = 0
    joined = False
    cluster_width = 0
    regional = False
    for char in text:
        code = ord(char)
        if code == 0x200D:
            joined = True
            continue
        char_cells = character_width(char)
        if char_cells == 0:
            continue
        if 0x1F1E6 <= code <= 0x1F1FF:
            if regional:
                regional = False
                continue
            regional = True
        else:
            regional = False
        if joined:
            width += max(0, char_cells - cluster_width)
            cluster_width = max(cluster_width, char_cells)
            joined = False
        else:
            width += char_cells
            cluster_width = char_cells
    return width


def break_long_word(word: str, width: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for char in word:
        candidate = current + char
        if current and display_width(candidate) > width:
            chunks.append(current)
            current = char
        else:
            current = candidate
    if current or not chunks:
        chunks.append(current)
    return chunks


def wrap_text(text: str, width: int) -> list[str]:
    """Wrap at word boundaries, splitting only words wider than the card."""
    wrapped: list[str] = []
    for paragraph in text.splitlines() or [""]:
        words = paragraph.split()
        if not words:
            wrapped.append("")
            continue
        line = ""
        for word in words:
            if display_width(word) > width:
                if line:
                    wrapped.append(line)
                    line = ""
                parts = break_long_word(word, width)
                wrapped.extend(parts[:-1])
                line = parts[-1]
            elif not line:
                line = word
            elif display_width(line) + 1 + display_width(word) <= width:
                line += " " + word
            else:
                wrapped.append(line)
                line = word
        if line:
            wrapped.append(line)
    return wrapped


def centered(text: str, width: int) -> str:
    spare = max(0, width - display_width(text))
    return " " * (spare // 2) + text + " " * (spare - spare // 2)


def _paint_highlight_terms(text: str, terms: tuple[str, ...], base_color: str, highlight_color: str) -> str:
    if not terms:
        return paint(text, base_color)
    output: list[str] = []
    cursor = 0
    while cursor < len(text):
        matches = [(text.find(term, cursor), term) for term in terms]
        matches = [(index, term) for index, term in matches if index >= 0]
        if not matches:
            output.append(paint(text[cursor:], base_color))
            break
        index, term = min(matches, key=lambda item: item[0])
        if index > cursor:
            output.append(paint(text[cursor:index], base_color))
        output.append(paint(term, highlight_color))
        cursor = index + len(term)
    return "".join(output)


def card(
    title: str | None,
    lines: list[str],
    *,
    width: int | None = None,
    centered_lines: set[str] | None = None,
    top_right: str | None = None,
    border_color: str = BORDER,
    text_color: str = WHITE,
    title_color: str | None = None,
    highlight_terms: tuple[str, ...] = (),
) -> None:
    title = display_home_paths(title) if title is not None else None
    lines = [display_home_paths(line) for line in lines]
    terminal_width = intelligent_ui.terminal_layout().columns
    inner = min(width, intelligent_ui.card_width(terminal_width)) if width is not None else intelligent_ui.card_width(terminal_width)
    centered_lines = centered_lines or set()
    top_border = "  ╭" + "─" * (inner + 2) + "╮"
    print(paint(top_border, border_color))
    content_lines = list(lines)
    if title is not None:
        if top_right:
            badge = top_right[:inner]
            title_width = display_width(title)
            badge_width = display_width(badge)
            left_pad = max(0, (inner - title_width) // 2)
            right_start = inner - badge_width
            if left_pad + title_width + 1 <= right_start:
                heading = " " * left_pad + title + " " * max(1, right_start - left_pad - title_width) + badge
                heading = heading + " " * max(0, inner - display_width(heading))
                title_content = " " + heading + " "
                print(paint("  │", border_color) + paint(title_content, title_color or text_color) + paint("│", border_color))
            else:
                title_text = centered(title, inner)
                title_content = " " + title_text + " " * (inner - display_width(title_text) + 1)
                print(paint("  │", border_color) + paint(title_content, title_color or text_color) + paint("│", border_color))
                badge_text = " " + " " * (inner - badge_width) + badge + " "
                print(paint("  │", border_color) + paint(badge_text, title_color or text_color) + paint("│", border_color))
        else:
            content_lines.insert(0, "")
            content_lines.insert(0, title)
    if title is not None and top_right:
        content_lines.insert(0, "")
    for line in content_lines:
        is_centered = (line == title and not top_right) or line in centered_lines
        chunks = wrap_text(line, inner)
        for chunk in chunks:
            text = centered(chunk, inner) if is_centered else chunk
            content = " " + text + " " * (inner - display_width(text) + 1)
            if line == title and title_color is not None:
                colored_content = paint(content, title_color)
            else:
                colored_content = _paint_highlight_terms(content, highlight_terms, text_color, WHITE)
            print(paint("  │", border_color) + colored_content + paint("│", border_color))
    print(paint("  ╰" + "─" * (inner + 2) + "╯", border_color))


def clear_terminal() -> None:
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="", flush=True)


def _main_completion_choices(text: str, cursor: int) -> list[str]:
    if cursor != len(text) or not text.startswith("/"):
        return []
    commands = (
        "/agent", "/auto-backup", "/auto-update", "/backup", "/clear", "/config",
        "/exit", "/help", "/identity", "/reconfig", "/restore", "/restart",
        "/setup", "/status", "/update", "/version",
    )
    if " " not in text:
        prefix = text.casefold()
        return [command[len(text):] + (" " if command in {"/auto-backup", "/auto-update"} else "")
                for command in commands if command.startswith(prefix) and command != prefix]
    command, _, argument = text.partition(" ")
    options: tuple[str, ...] = ()
    if command.casefold() == "/auto-backup":
        if " " not in argument:
            options = ("on", "off", "frequency ", "amount ")
        else:
            sub, _, tail = argument.partition(" ")
            if sub == "amount":
                options = tuple(str(value) for value in range(1, 11))
                argument = tail
            elif sub in {"on", "off", "frequency"}:
                return []
    elif command.casefold() == "/auto-update":
        options = ("on", "off")
    prefix = argument.casefold()
    return [option[len(prefix):] for option in options
            if option.casefold().startswith(prefix) and option.casefold() != prefix]


def read_prompt(prompt: str) -> str:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return input(prompt)

    import termios
    import tty

    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    decoder = codecs.getincrementaldecoder(sys.stdin.encoding or "utf-8")()
    characters: list[str] = []
    cursor = 0
    completion_index = 0
    last_size = shutil.get_terminal_size((80, 24))

    def redraw() -> None:
        text = "".join(characters)
        sys.stdout.write("\r\033[2K" + prompt + text + "\033[" + str(display_width(text[cursor:])) + "D")
        sys.stdout.flush()

    try:
        tty.setraw(fd)
        sys.stdout.write(prompt)
        sys.stdout.flush()
        while True:
            if not select.select([fd], [], [], 0.2)[0]:
                size = shutil.get_terminal_size((80, 24))
                if size != last_size:
                    last_size = size
                    redraw()
                continue
            raw = os.read(fd, 1)
            if not raw:
                raise EOFError
            if raw == b"\x1b":
                if not select.select([fd], [], [], 0.06)[0]:
                    raise EOFError
                sequence = bytearray()
                while select.select([fd], [], [], 0.03)[0]:
                    sequence.extend(os.read(fd, 1))
                    if len(sequence) >= 2 and 0x40 <= sequence[-1] <= 0x7E:
                        break
                key = bytes(sequence[-1:])
                if key == b"D":
                    cursor = max(0, cursor - 1)
                elif key == b"C":
                    choices = _main_completion_choices("".join(characters), cursor)
                    if choices:
                        characters[cursor:cursor] = list(choices[completion_index % len(choices)])
                        cursor += len(choices[completion_index % len(choices)])
                        completion_index = 0
                    else:
                        cursor = min(len(characters), cursor + 1)
                elif key in {b"A", b"B"}:
                    choices = _main_completion_choices("".join(characters), cursor)
                    if choices:
                        completion_index = (completion_index + (1 if key == b"B" else -1)) % len(choices)
                redraw()
                continue
            if raw in {b"\r", b"\n"}:
                choices = _main_completion_choices("".join(characters), cursor)
                if choices and cursor == len(characters):
                    completion = choices[completion_index % len(choices)]
                    characters.extend(completion)
                    cursor += len(completion)
                    completion_index = 0
                    redraw()
                    continue
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                return "".join(characters)
            if raw == b"\x03":
                raise KeyboardInterrupt
            if raw == b"\x04":
                if not characters:
                    raise EOFError
                continue
            if raw == b"\t":
                choices = _main_completion_choices("".join(characters), cursor)
                if choices:
                    completion = choices[completion_index % len(choices)]
                    characters[cursor:cursor] = list(completion)
                    cursor += len(completion)
                    completion_index = 0
                    redraw()
                continue
            if raw in {b"\x08", b"\x7f"}:
                if cursor:
                    characters.pop(cursor - 1)
                    cursor -= 1
                    completion_index = 0
                    redraw()
                continue
            character = decoder.decode(raw)
            if character and character.isprintable():
                characters[cursor:cursor] = list(character)
                cursor += len(character)
                completion_index = 0
                redraw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def show_welcome(*, check_updates: bool = False) -> None:
    print()
    card(
        "Sophon",
        ["A minimal development environment.", "Build. Think. Execute.", "", f"READY    v{VERSION}"],
        centered_lines={"Sophon"},
        text_color=MUTED,
        title_color=WHITE,
        highlight_terms=("READY", f"v{VERSION}"),
    )
    if check_updates and sys.stdin.isatty() and sys.stdout.isatty():
        remote_version = latest_version(timeout=4)
        if remote_version and remote_version != VERSION:
            card(
                None,
                [f"Update available · {VERSION} → {remote_version}", "Run /update to install."],
                border_color=UPDATE_BORDER,
                text_color=UPDATE_TEXT,
            )
    print()


def update_card(lines: list[str], *, initial: bool = False, final: bool = False) -> None:
    if not sys.stdout.isatty():
        if final:
            card(None, lines)
        return
    if initial:
        card(None, lines)
        return
    inner = intelligent_ui.card_width(intelligent_ui.terminal_layout().columns)
    for index, line in enumerate(lines):
        # Update status rows in place; the fixed progress copy fits narrow cards.
        text = wrap_text(line, inner)[0]
        content = " " + text + " " * (inner - display_width(text) + 1)
        up = 4 - index
        down = 3 - index
        sys.stdout.write(
            f"\033[{up}A\r\033[2K"
            + paint("  │", BORDER)
            + paint(content, WHITE)
            + paint("│", BORDER)
            + f"\033[{down}B\r"
            + paint("  ╰" + "─" * (inner + 2) + "╯", BORDER)
            + "\n"
        )
        sys.stdout.flush()


def run_git_animated(args: list[str], lines: list[str], active_line: int, message: str, *, animate: bool = True):
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError:
        return None, None, 1
    dots = 0
    while True:
        try:
            stdout, stderr = process.communicate(timeout=0.25)
            return stdout, stderr, process.returncode
        except subprocess.TimeoutExpired:
            if animate and sys.stdout.isatty():
                dots = dots % 3 + 1
                lines[active_line] = message + "." * dots
                update_card(lines)


def latest_version(timeout: float = 10) -> str | None:
    request_headers = {"User-Agent": "Sophon-CLI", "Accept": "application/vnd.github+json"}
    for url in (LATEST_VERSION_URL, LATEST_VERSION_API_URL):
        try:
            request = Request(url, headers=request_headers)
            with urlopen(request, timeout=timeout) as response:
                payload = response.read().decode("utf-8").strip()
            if url == LATEST_VERSION_API_URL:
                content = json.loads(payload).get("content", "")
                payload = base64.b64decode(content).decode("utf-8").strip()
            if payload:
                return payload
        except (OSError, TimeoutError, URLError, UnicodeError, ValueError, KeyError, TypeError):
            continue
    return None


def settings_path() -> Path:
    return config_path().parent.parent / "settings.json"


def auto_compression_settings(data: dict[str, object] | None = None) -> tuple[bool, int]:
    if data is None:
        try:
            data = read_config()
        except (OSError, ValueError):
            data = None
    if data is None:
        try:
            candidate = json.loads(config_path().read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            candidate = None
        data = candidate if isinstance(candidate, dict) else {}
    enabled = data.get("auto_compression")
    if not isinstance(enabled, bool):
        enabled = True
    threshold = data.get("auto_compression_threshold")
    if not isinstance(threshold, int) or isinstance(threshold, bool) or not 30 <= threshold <= 95:
        threshold = 80
    return enabled, threshold


def auto_update_enabled() -> bool:
    try:
        data = read_config()
    except (OSError, ValueError):
        data = None
    if data is not None and isinstance(data.get("auto_update"), bool):
        return data["auto_update"]
    try:
        raw_config = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        raw_config = None
    if isinstance(raw_config, dict) and isinstance(raw_config.get("auto_update"), bool):
        return raw_config["auto_update"]
    try:
        value = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    enabled = isinstance(value, dict) and value.get("auto_update") is True
    if enabled:
        preferences = raw_config if isinstance(raw_config, dict) and raw_config.get("provider") is None else {"schema_version": 1}
        preferences["auto_update"] = True
        try:
            write_config(preferences)
        except (OSError, ValueError):
            pass
        else:
            try:
                settings_path().unlink()
            except OSError:
                pass
    return enabled


def set_auto_update(enabled: bool) -> None:
    try:
        data = read_config()
    except (OSError, ValueError):
        data = None
    if data is not None:
        data["auto_update"] = enabled
        write_config(data)
        return
    try:
        raw_config = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        raw_config = None
    if not isinstance(raw_config, dict) or raw_config.get("provider") is None:
        preferences = raw_config if isinstance(raw_config, dict) else {"schema_version": 1}
        preferences["auto_update"] = enabled
        write_config(preferences)
        return
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps({"auto_update": enabled}, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def restart_sophon(arguments: list[str] | None = None) -> None:
    """Replace this process with the same Sophon invocation."""
    restart_args = sys.argv[1:] if arguments is None else arguments
    os.execv(sys.executable, [sys.executable, sys.argv[0], *restart_args])


def update(*, quiet: bool = False, restart_after: bool = True, known_version: str | None = None) -> int:
    global RESTART_ARGS, RESTART_REQUESTED
    RESTART_REQUESTED = False
    RESTART_ARGS = None

    def show(lines: list[str], *, initial: bool = False, final: bool = False) -> None:
        if not quiet:
            update_card(lines, initial=initial, final=final)

    def finish_update(version: str) -> None:
        global RESTART_REQUESTED
        if restart_after:
            if not quiet:
                for remaining in (3, 2, 1):
                    show(["Update complete", f"sophon {version}", f"Restarting Sophon in {remaining}…"], final=True)
                    time.sleep(0.65)
            restart_sophon()
        else:
            show(["Update complete", f"sophon {version}", "Restart when ready"], final=True)
        RESTART_REQUESTED = True

    repo = Path(__file__).resolve().parents[2]
    lines = ["Checking status", "Waiting for updates", "Fast-forward only"]
    if not (repo / ".git").exists():
        lines[:] = ["Checking installed version", "Contacting GitHub", ""]
        show(lines, initial=True)
        remote_version = known_version or latest_version()
        if remote_version is None:
            show(["Version check failed", "Could not reach GitHub", "Retry later"], final=True)
            return 1
        if remote_version == VERSION:
            show(["! Version already current.", "", f"sophon {VERSION}"], final=True)
            return 0

        lines[:] = ["Update available", f"{VERSION} → {remote_version}", "Installing with pip"]
        show(lines)
        _, _, status_code = run_git_animated(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--upgrade",
                "--force-reinstall",
                "https://github.com/ardakooza/sophon-cli/archive/refs/heads/main.tar.gz",
            ],
            lines,
            1,
            "Installing latest version",
            animate=not quiet,
        )
        if status_code != 0:
            show(["Update failed", "Check network or pip", "Retry later"], final=True)
            return status_code
        finish_update(remote_version)
        return 0

    show(lines, initial=True)
    stdout, _, status_code = run_git_animated(
        ["git", "-C", str(repo), "status", "--porcelain"], lines, 0, "Checking status", animate=not quiet
    )
    if status_code != 0:
        show(["Git status failed", "Install Git first", ""], final=True)
        return status_code
    if stdout and stdout.strip():
        show(["Local changes found", "Nothing was changed", "Commit or stash first"], final=True)
        return 1

    lines[:] = ["Repository clean", "Fetching updates", "Fast-forward only"]
    show(lines)
    stdout, _, pull_code = run_git_animated(
        ["git", "-C", str(repo), "pull", "--ff-only"], lines, 1, "Fetching updates", animate=not quiet
    )
    if pull_code != 0:
        show(["Update failed", "Check network or Git", "Retry later"], final=True)
        return pull_code
    try:
        updated_version = (repo / "src" / "sophon" / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        updated_version = "latest"
    if updated_version == VERSION:
        show(["! Version already current.", "", f"sophon {VERSION}"], final=True)
    else:
        finish_update(updated_version)
    return 0


def auto_update_on_start() -> None:
    if not auto_update_enabled():
        return
    if sys.stdin.isatty() and sys.stdout.isatty():
        result: list[str | None] = []
        worker = threading.Thread(target=lambda: result.append(latest_version(timeout=4)), daemon=True)
        update_card(["Sophon is ready", "Checking for updates ·", "Restart only if an update is found"], initial=True)
        worker.start()
        frame = 0
        while worker.is_alive():
            dots = "." * (frame % 3 + 1)
            update_card(["Sophon is ready", f"Checking for updates{dots}", "Restart only if an update is found"])
            time.sleep(0.2)
            frame += 1
        worker.join()
        remote_version = result[0] if result else None
    else:
        remote_version = latest_version(timeout=4)
    if remote_version and remote_version != VERSION:
        update(known_version=remote_version, restart_after=True)


def _write_private_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(path.parent.parent, 0o700)
        os.chmod(path.parent, 0o700)
    fd, temporary_path = tempfile.mkstemp(prefix=".backup-", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        os.replace(temporary_path, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


def _backup_preferences() -> tuple[dict[str, object], dict[str, object]]:
    try:
        raw = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        raw = {"schema_version": 1}
    if not isinstance(raw, dict):
        raw = {"schema_version": 1}
    backup = raw.get("backup")
    if not isinstance(backup, dict):
        backup = {}
    normalized = {
        "enabled": backup.get("enabled") is True,
        "frequency": backup.get("frequency") if isinstance(backup.get("frequency"), str) else "Every 2 days",
        "backup_amount": backup.get("backup_amount") if isinstance(backup.get("backup_amount"), int) and not isinstance(backup.get("backup_amount"), bool) and 1 <= backup["backup_amount"] <= 10 else 1,
        "last_backup_at": backup.get("last_backup_at") if isinstance(backup.get("last_backup_at"), str) else "",
    }
    return raw, normalized


def _save_backup_preferences(settings: dict[str, object]) -> None:
    raw, _ = _backup_preferences()
    raw["backup"] = settings
    write_config(raw)


def _backup_files() -> list[Path]:
    directory = CONFIG_BACKUP_PATH.parent
    files = list(directory.glob("config_backup*.json")) if directory.exists() else []
    return sorted((path for path in files if path.is_file()), key=lambda path: path.stat().st_mtime, reverse=True)


def _backup_payload(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _backup_comparison(data: dict[str, object]) -> str:
    comparable = dict(data)
    nested = comparable.get("backup")
    if isinstance(nested, dict):
        nested = dict(nested)
        nested.pop("last_backup_at", None)
        comparable["backup"] = nested
    return json.dumps(comparable, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def backup_config(*, automatic: bool = False) -> int:
    source = config_path()
    try:
        if not source.is_file():
            read_config()
            source = config_path()
        raw = source.read_bytes()
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("Configuration must be a JSON object.")
        _, settings = _backup_preferences()
        files = _backup_files()
        if files and _backup_comparison(_backup_payload(files[0]) or {}) == _backup_comparison(parsed):
            settings["last_backup_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            _save_backup_preferences(settings)
            card("Configuration Backup", ["Backup is already current.", f"Latest: {display_home_paths(files[0])}"], top_right="Unchanged")
            return 0
        amount = int(settings["backup_amount"])
        deleted = max(0, len(files) + 1 - amount)
        if not automatic and amount == 1 and files:
            card("Configuration Backup", ["The existing backup will be replaced.", f"{len(files)} backup file(s) will be deleted.", "Type BACKUP to confirm; anything else cancels."], border_color=UPDATE_BORDER, text_color=UPDATE_TEXT)
            try:
                confirmation = read_prompt("  › ").strip()
            except (EOFError, KeyboardInterrupt):
                confirmation = ""
            if confirmation != "BACKUP":
                card("Configuration Backup", ["Backup cancelled. The existing backup was kept."])
                return 0
        elif not automatic and deleted:
            card("Configuration Backup", [f"{deleted} backup file(s) will be deleted to keep the configured limit.", "Type BACKUP to confirm; anything else cancels."], border_color=UPDATE_BORDER, text_color=UPDATE_TEXT)
            try:
                confirmation = read_prompt("  › ").strip()
            except (EOFError, KeyboardInterrupt):
                confirmation = ""
            if confirmation != "BACKUP":
                card("Configuration Backup", ["Backup cancelled. Existing backups were kept."])
                return 0
        if amount == 1:
            target = CONFIG_BACKUP_PATH
        else:
            target = CONFIG_BACKUP_PATH.with_name(time.strftime("config_backup_%Y%m%dT%H%M%S") + f"_{time.time_ns() % 1_000_000_000:09d}.json")
        _write_private_bytes(target, raw)
        for old in _backup_files()[amount:]:
            try:
                old.unlink()
            except OSError:
                pass
        settings["last_backup_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        _save_backup_preferences(settings)
    except FileNotFoundError:
        card("Configuration Backup", ["No saved configuration was found to back up."], border_color=NOTICE_BORDER, text_color=NOTICE_TEXT)
        return 1
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        card("Configuration Backup", [f"Backup failed: {error}"], border_color=NOTICE_BORDER, text_color=NOTICE_TEXT)
        return 1
    card("Configuration Backup", ["Configuration data saved securely.", f"Backup: {display_home_paths(target)}"], top_right="Saved")
    return 0


def _normalize_backup_frequency(text: str) -> str | None:
    folded = unicodedata.normalize("NFKD", text.casefold())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch)).replace("ı", "i")
    folded = re.sub(r"\s+", " ", folded.strip())
    if folded in {"", "default", "varsayilan", "varsayılan"}:
        return "Every 2 days"
    if re.search(r"(twice|2 times|gunde 2|gunde iki|day twice)", folded):
        return "Twice a day"
    time_match = re.search(r"(?:at|saat)\s*(\d{1,2})(?::(\d{2}))?", folded)
    at = f"{int(time_match.group(1)):02d}:{int(time_match.group(2) or 0):02d}" if time_match else None
    if at and (int(at[:2]) > 23 or int(at[3:]) > 59):
        return None
    if any(word in folded for word in ("weekend", "weekends", "haftasonu", "hafta sonu")):
        return f"Every weekend at {at}" if at else "Every weekend"
    if re.search(r"(month|ayda bir|her ay)", folded):
        return "Every month"
    if re.search(r"(week|hafta|weekly)", folded):
        return "Once a week"
    if at:
        return f"Every day at {at}"
    match = re.search(r"(?:every|her)\s+(\d+)\s*(day|days|gun|g[uü]n|week|weeks|hafta)", folded)
    if match:
        count = int(match.group(1))
        unit = "week" if match.group(2) in {"week", "weeks", "hafta"} else "day"
        return f"Every {count} {unit}{'' if count == 1 else 's'}" if count > 0 else None
    if any(word in folded for word in ("daily", "every day", "her gun", "her gün", "gunde bir", "günde bir", "gunde 1 kez", "her gun 1 kez")):
        return "Every day"
    return None


def _backup_is_due(settings: dict[str, object]) -> bool:
    frequency = str(settings.get("frequency", "Every 2 days"))
    last = str(settings.get("last_backup_at", ""))
    if not last:
        return True
    try:
        previous = time.strptime(last, "%Y-%m-%dT%H:%M:%S%z")
        elapsed = max(0, time.time() - time.mktime(previous))
    except (ValueError, OverflowError):
        return True
    if frequency == "Twice a day":
        return elapsed >= 12 * 3600
    if frequency.startswith("Every weekend"):
        local = time.localtime()
        match = re.search(r"at (\d{2}):", frequency)
        target_hour = int(match.group(1)) if match else 0
        return local.tm_wday >= 5 and local.tm_hour >= target_hour and elapsed >= 24 * 3600
    if frequency.startswith("Every day at "):
        return time.localtime().tm_hour >= int(frequency[-5:-3]) and elapsed >= 20 * 3600
    if frequency == "Every day":
        return elapsed >= 24 * 3600
    if frequency == "Once a week":
        return elapsed >= 7 * 86400
    if frequency == "Every month":
        return elapsed >= 30 * 86400
    match = re.fullmatch(r"Every (\d+) (day|days|week|weeks)", frequency)
    if match:
        return elapsed >= int(match.group(1)) * (86400 if match.group(2).startswith("day") else 7 * 86400)
    return elapsed >= 2 * 86400


def _auto_backup_startup() -> None:
    _, settings = _backup_preferences()
    if settings["enabled"] and _backup_is_due(settings):
        backup_config(automatic=True)


def auto_backup_command(argument: str) -> int:
    raw, settings = _backup_preferences()
    parts = argument.split(maxsplit=1)
    action = parts[0].casefold() if parts else ""
    value = parts[1].strip() if len(parts) > 1 else ""
    if action in {"on", "off"} and not value:
        settings["enabled"] = action == "on"
        if action == "on" and "frequency" not in (raw.get("backup") if isinstance(raw.get("backup"), dict) else {}):
            try:
                answer = read_prompt("Backup frequency (blank = every 2 days): ").strip()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            normalized = _normalize_backup_frequency(answer)
            if normalized is None:
                card("Automatic backups", ["Frequency not recognized; using Every 2 days."])
                normalized = "Every 2 days"
            settings["frequency"] = normalized
        _save_backup_preferences(settings)
        card("Automatic backups", [f"Automatic backup is {action}.", f"Frequency: {settings['frequency']}", f"Keep: {settings['backup_amount']} backup(s)"], top_right=action.upper())
        return 0
    if action == "frequency" and value:
        normalized = _normalize_backup_frequency(value)
        if normalized is None:
            card("Automatic backups", ["Frequency not recognized.", "Examples: every day, twice a day, once a week, every month, every day at 09:00, every weekend at 10:00."], border_color=NOTICE_BORDER, text_color=NOTICE_TEXT)
            return 1
        settings["frequency"] = normalized
    elif action == "amount" and value.isdigit() and 1 <= int(value) <= 10:
        settings["backup_amount"] = int(value)
    elif not action:
        card("Automatic backups", [f"Frequency: {settings['frequency']}", f"Keep: {settings['backup_amount']} backup(s)"], top_right="ON" if settings["enabled"] else "OFF", text_color=MUTED, title_color=WHITE, highlight_terms=("Frequency:", "Keep:"))
        return 0
    else:
        card("Automatic backups", ["Use /auto-backup on|off, /auto-backup frequency <schedule>, or /auto-backup amount <1-10>."], border_color=NOTICE_BORDER, text_color=NOTICE_TEXT)
        return 1
    _save_backup_preferences(settings)
    card("Automatic backups", [f"Frequency: {settings['frequency']}", f"Keep: {settings['backup_amount']} backup(s)"])
    return 0

def _validate_backup_payload(data: dict[str, object]) -> None:
    provider = data.get("provider")
    if provider is None:
        allowed = {
            "schema_version", "auto_update", "agent_instructions_path", "reasoning",
            "auto_compression", "auto_compression_threshold", "Environment", "allowed_path", "mode", "backup",
        }
        if set(data) - allowed:
            raise ValueError("Backup contains unsupported preferences-only fields.")
        if "auto_update" in data and not isinstance(data["auto_update"], bool):
            raise ValueError("Backup has an invalid auto-update preference.")
        if "auto_compression" in data and not isinstance(data["auto_compression"], bool):
            raise ValueError("Backup has an invalid auto-compression preference.")
        threshold = data.get("auto_compression_threshold")
        if threshold is not None and (
            not isinstance(threshold, int) or isinstance(threshold, bool) or not 30 <= threshold <= 95
        ):
            raise ValueError("Backup has an auto-compression threshold outside 30–95.")
        environment = data.get("Environment")
        if environment is not None and (not isinstance(environment, str) or not environment.strip()):
            raise ValueError("Backup has an invalid environment label.")
        backup = data.get("backup")
        if backup is not None:
            if not isinstance(backup, dict):
                raise ValueError("Backup has invalid automatic backup settings.")
            amount = backup.get("backup_amount", 1)
            if not isinstance(amount, int) or isinstance(amount, bool) or not 1 <= amount <= 10:
                raise ValueError("Backup has an invalid backup retention amount.")
            if "enabled" in backup and not isinstance(backup["enabled"], bool):
                raise ValueError("Backup has an invalid automatic backup preference.")
        return
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise ValueError("Backup names an unsupported provider.")
    if not isinstance(data.get("model"), str) or not data["model"].strip():
        raise ValueError("Backup is missing a valid model ID.")
    if not isinstance(data.get("api_key"), str) or not data["api_key"].strip():
        raise ValueError("Backup is missing a valid provider credential.")
    if provider == "cloudflare":
        account_id = data.get("account_id")
        if not isinstance(account_id, str) or len(account_id) != 32 or any(
            character not in "0123456789abcdefABCDEF" for character in account_id
        ):
            raise ValueError("Backup has an invalid Cloudflare Account ID.")


def restore_config() -> int:
    backup_files = _backup_files()
    restore_path = backup_files[0] if backup_files else CONFIG_BACKUP_PATH
    try:
        raw = restore_path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Backup must contain a JSON object.")
        _validate_backup_payload(data)
    except FileNotFoundError:
        card("Configuration Restore", ["No backup found.", f"Expected: {display_home_paths(CONFIG_BACKUP_PATH)}"], border_color=NOTICE_BORDER, text_color=NOTICE_TEXT)
        return 1
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        card("Configuration Restore", [f"Backup could not be restored: {error}"], border_color=NOTICE_BORDER, text_color=NOTICE_TEXT)
        return 1

    card(
        "Restore Configuration",
        [
            "This will overwrite the current provider settings and preferences.",
            f"Source: {display_home_paths(restore_path)}",
            "Type RESTORE to confirm; any other input cancels.",
        ],
        border_color=UPDATE_BORDER,
        text_color=UPDATE_TEXT,
    )
    try:
        confirmation = read_prompt("  › ").strip()
    except (EOFError, KeyboardInterrupt):
        confirmation = ""
    if confirmation != "RESTORE":
        card("Configuration Restore", ["Restore cancelled. No settings were changed."])
        return 0
    try:
        path = write_config(data)
        read_config()
    except (OSError, ValueError) as error:
        card("Configuration Restore", [f"Restore failed: {error}"], border_color=NOTICE_BORDER, text_color=NOTICE_TEXT)
        return 1
    card("Configuration Restore", ["Configuration and preferences restored.", f"Config: {display_home_paths(path)}"], top_right="Restored")
    return 0


def run_session() -> int:
    clear_terminal()
    show_welcome(check_updates=False)
    _auto_backup_startup()
    if auto_update_enabled():
        auto_update_on_start()
        if not RESTART_REQUESTED:
            clear_terminal()
            show_welcome()
    while True:
        try:
            value = read_prompt(paint("  › ", GREEN)).strip()
        except (EOFError, KeyboardInterrupt):
            sys.stdout.write("\r\033[2K")
            card(None, ["Interactive closed. See you soon."])
            return 0
        if not value:
            continue
        command, _, argument = value.partition(" ")
        command = command.casefold()
        argument = argument.strip()
        if command in {"/exit", "/quit", "q"}:
            break
        if command.startswith("/"):
            if command == "/update":
                update()
            elif command == "/help":
                card(
                    "Sophon Commands",
                    [
                        "/agent — Open Sophon Agent",
                        "/auto-update on|off — Set startup updates",
                        "/auto-backup on|off — Configure automatic backups",
                        "/auto-backup frequency <schedule> — Set backup frequency",
                        "/auto-backup amount <1-10> — Set backup retention",
                        "/backup — Save a private configuration backup",
                        "/clear — Redraw the welcome screen",
                        "/config — Show saved settings",
                        "/help — Show available commands",
                        "/identity — Edit AGENT.md in the card",
                        "/reconfig — Reconfigure the provider directly",
                        "/restore — Restore config backup after confirmation",
                        "/restart — Restart Sophon",
                        "/setup — Configure a provider",
                        "/status — Inspect system and workspace health",
                        "/update — Update and restart if changed",
                        "/version — Show the current version",
                        "/exit — Close Sophon",
                    ],
                    text_color=MUTED,
                    title_color=WHITE,
                    highlight_terms=("/agent", "/auto-update", "/auto-backup", "/backup", "/clear", "/config", "/help", "/identity", "/reconfig", "/restore", "/restart", "/setup", "/status", "/update", "/version", "/exit"),
                )
            elif command == "/agent":
                from sophon.agent import main as agent_main

                agent_main([])
                if RESTART_REQUESTED:
                    restart_sophon(RESTART_ARGS)
                clear_terminal()
                show_welcome()
            elif command == "/status":
                system_status()
            elif command == "/identity":
                manage_identity()
            elif command == "/backup":
                backup_config()
            elif command == "/auto-backup":
                auto_backup_command(argument)
            elif command == "/restore":
                if restore_config() == 0:
                    show_welcome()
            elif command == "/setup":
                from sophon.setup_wizard import run_setup_wizard

                if run_setup_wizard() == 0:
                    show_welcome()
            elif command == "/reconfig":
                from sophon.setup_wizard import run_setup_wizard

                if run_setup_wizard(reconfigure=True) == 0:
                    show_welcome()
            elif command == "/config":
                show_config()
            elif command.startswith("/auto-update"):
                if argument in {"on", "off"}:
                    set_auto_update(argument == "on")
                    next_state = "off" if argument == "on" else "on"
                    card("Automatic updates", [f"Auto-update is {argument}.", f"Use /auto-update {next_state} to {next_state}."], text_color=MUTED, title_color=WHITE, highlight_terms=("Auto-update is", f"{argument}."))
                elif not argument:
                    status = "on" if auto_update_enabled() else "off"
                    next_state = "off" if status == "on" else "on"
                    card("Automatic updates", [f"Auto-update is {status}.", f"Use /auto-update {next_state} to {next_state}."], text_color=MUTED, title_color=WHITE, highlight_terms=("Auto-update is", f"{status}."))
                else:
                    status = "on" if auto_update_enabled() else "off"
                    next_state = "off" if status == "on" else "on"
                    card("Automatic updates", [f"Auto-update is unchanged ({status}).", f"Use /auto-update {next_state} to {next_state}."], border_color=NOTICE_BORDER, text_color=MUTED, title_color=WHITE, highlight_terms=("Auto-update is unchanged", f"({status})."))
            elif command == "/restart":
                card(None, ["Restarting Sophon..."])
                restart_sophon()
            elif command == "/version":
                card(None, [f"sophon {VERSION}"])
            elif command == "/clear":
                clear_terminal()
                show_welcome()
            else:
                card(
                    None,
                    [f"{value} is not a Sophon command.", "Type /help to see available commands."],
                    border_color=NOTICE_BORDER,
                    text_color=NOTICE_TEXT,
                )
        else:
            card(
                None,
                ["Commands must start with /.", "Type /help to see available commands."],
                border_color=NOTICE_BORDER,
                text_color=NOTICE_TEXT,
            )
    card(None, ["Interactive closed. See you soon."])
    return 0


def init_config() -> int:
    config = Path(".sophon.toml")
    if config.exists():
        card(None, ["Configuration already exists", ".sophon.toml"])
        return 1
    config.write_text('version = 1\n\n[ui]\ntheme = "dark"\n', encoding="utf-8")
    card(None, ["Created .sophon.toml"])
    return 0


def show_config() -> int:
    try:
        data = read_config()
    except (OSError, ValueError):
        card(None, ["Configuration is invalid or unreadable.", f"Path: {config_display_path()}"])
        return 1
    if data is None:
        environment = "unknown"
        try:
            preferences = json.loads(config_path().read_text(encoding="utf-8"))
            if isinstance(preferences, dict) and isinstance(preferences.get("Environment"), str):
                environment = preferences["Environment"]
        except (OSError, ValueError):
            pass
        card(None, ["No provider is configured.", f"Environment: {environment}", "Run sophon setup to connect one."])
        return 0

    provider = data["provider"]
    lines = [
        f"Provider: {PROVIDERS[provider]['label']}",
        f"Model: {data['model']}",
        "API key: hidden",
        f"Environment: {data.get('Environment', 'unknown')}",
        f"Auto-update: {'on' if data.get('auto_update') is True else 'off'}",
    ]
    reasoning = data.get("reasoning") if isinstance(data.get("reasoning"), dict) else {}
    if reasoning.get("is_support") is not True:
        lines.append("Reasoning: not supported")
    else:
        state = "on" if reasoning.get("enabled") is True else "off"
        lines.append(f"Reasoning: {state}")
        if state == "on":
            lines.append(f"Reasoning effort: {reasoning.get('effort', 'medium')}")
    instructions = str(data.get("agent_instructions_path", ""))
    if instructions:
        lines.append(f"Agent instructions: {instructions}")
    if provider == "cloudflare":
        lines.append(f"Account ID: {data['account_id']}")
    lines.append(f"Config: {config_display_path()}")
    card(None, lines)
    return 0


def project_workspace() -> Path | None:
    """Return the nearest project root or Sophon's active agent workspace."""
    current = Path.cwd().resolve()
    try:
        from sophon.agent import PROJECT_DIR

        project_dir = PROJECT_DIR.resolve()
        if current == project_dir or project_dir in current.parents:
            return project_dir
    except (ImportError, OSError):
        pass

    markers = (".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod")
    for candidate in (current, *current.parents):
        if any((candidate / marker).exists() for marker in markers):
            return candidate
    return None


def doctor() -> int:
    checks: list[str] = []
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0}

    def record(status: str, detail: str) -> None:
        counts[status] += 1
        checks.append(f"{status} · {detail}")

    if sys.version_info >= (3, 10):
        record("PASS", f"Python {sys.version_info.major}.{sys.version_info.minor}")
    else:
        record("FAIL", "Python 3.10 or newer is required")
    interpreter = Path(sys.executable)
    if interpreter.is_file() and os.access(interpreter, os.X_OK):
        record("PASS", "Python executable")
    else:
        record("FAIL", "Python executable is missing or not executable")
    record("PASS", f"Sophon CLI · {VERSION}")

    try:
        data = read_config()
    except (OSError, ValueError) as error:
        data = None
        record("FAIL", f"Provider configuration is invalid · {error}")
    if data is None and not any(line.startswith("FAIL · Provider configuration") for line in checks):
        record("WARN", "Provider is not configured · Run sophon setup")
    elif data is not None:
        record("PASS", f"Provider · {PROVIDERS[data['provider']]['label']}")
        record("PASS", f"Model · {data['model']}")
    config_file = config_path()
    if config_file.exists():
        try:
            mode = config_file.stat().st_mode
        except OSError:
            record("WARN", "Configuration file permissions could not be inspected")
        else:
            if os.name != "nt" and mode & 0o077:
                record("WARN", "Configuration file permissions are broader than 600")
            else:
                record("PASS", "Configuration file permissions")

    def check_writable(label: str, path: Path) -> None:
        candidate = path
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        if candidate.is_dir() and os.access(candidate, os.W_OK):
            record("PASS", f"{label} storage is writable")
        else:
            record("FAIL", f"{label} storage is not writable · {path}")

    check_writable("Configuration", config_path().parent)
    workspace_root = Path.home() / ".sophon" / "workspace"
    project_dir = workspace_root / "my-project"
    session_file = Path.home() / ".sophon" / "sessions" / "sessions.json"
    from sophon.config import agent_instructions_path

    try:
        from sophon.agent import PROJECT_DIR as project_dir, SESSION_FILE as session_file, WORKSPACE_ROOT as workspace_root, _load_sessions

        sessions = _load_sessions()
    except (OSError, RuntimeError) as error:
        sessions = []
        record("FAIL", f"Session history is unreadable · {error}")
    else:
        detail = f"Session history · {len(sessions)} saved" if sessions else "Session history · none yet"
        record("PASS", detail)
    model_list_check = None
    try:
        from sophon.agent import FILE_MANAGE_TOOL_NAMES, _list_models, _tools_for_mode

        model_list_check = _list_models
        build_tools = _tools_for_mode("build", {})
        tool_names = [tool["function"]["name"] for tool in build_tools]
        required_tools = {"ask", "planning", "file_create", "file_edit", "file_read", "file_delete", "tree_view", "zip", "extract"}
        if len(tool_names) != len(set(tool_names)) or not required_tools.issubset(set(tool_names)):
            record("FAIL", "Agent tool registry is incomplete or has duplicate names")
        else:
            record("PASS", f"Agent tool registry · {len(tool_names)} tools · {len(FILE_MANAGE_TOOL_NAMES)} File Manage tools")
    except (ImportError, KeyError, TypeError, ValueError) as error:
        record("FAIL", f"Agent tool registry could not load · {error}")
    if data is not None and model_list_check is not None:
        try:
            models = model_list_check(data, str(data.get("model", "")), timeout=6)
            if any(model.get("id") == data.get("model") for model in models if isinstance(model, dict)):
                record("PASS", "Agent provider endpoint and model catalog")
            else:
                record("WARN", "Agent model is not listed by the provider catalog · verify the model ID")
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
            message = str(error)
            api_key = data.get("api_key")
            if isinstance(api_key, str) and api_key:
                message = message.replace(api_key, "[redacted]")
            record("WARN", f"Agent provider check failed · {message[:180]}")
    check_writable("Session", session_file.parent)
    check_writable("Workspace", workspace_root)
    instructions_path = Path((data or {}).get("agent_instructions_path") or agent_instructions_path())
    if instructions_path.is_file() and os.access(instructions_path, os.R_OK):
        record("PASS", "Agent instructions file")
    else:
        record("WARN", "Agent instructions file is missing · start sophon agent or run setup")
    if project_dir.is_dir():
        record("PASS", "Project workspace exists")
    else:
        record("WARN", "Project workspace has not been initialized")
    if session_file.exists() and os.name != "nt":
        try:
            mode = session_file.stat().st_mode
        except OSError:
            record("WARN", "Session file permissions could not be inspected")
        else:
            if mode & 0o077:
                record("WARN", "Session file permissions are broader than 600")
            else:
                record("PASS", "Session file permissions")

    summary = f"{counts['PASS']} passed · {counts['WARN']} warnings · {counts['FAIL']} errors"
    checks.insert(0, summary)
    checks.insert(1, "")
    if counts["FAIL"]:
        border_color, text_color = NOTICE_BORDER, NOTICE_TEXT
    elif counts["WARN"]:
        border_color, text_color = UPDATE_BORDER, UPDATE_TEXT
    else:
        border_color, text_color = GREEN, GREEN
    card(
        "Sophon Doctor",
        checks,
        border_color=border_color,
        text_color=text_color,
        top_right=f"{counts['PASS']} of {sum(counts.values())}",
    )
    return int(bool(counts["WARN"] or counts["FAIL"]))


def system_status() -> int:
    warnings: list[str] = []
    terminal_size = shutil.get_terminal_size((80, 24))
    rows = [
        f"Environment: {detect_environment()}",
        f"Platform: {platform.system()} {platform.release()} · {platform.machine() or 'unknown architecture'}",
        f"Python: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        f"Runtime: {sys.implementation.name} · PID {os.getpid()}",
        f"Terminal: {'interactive' if sys.stdin.isatty() and sys.stdout.isatty() else 'non-interactive'} · {terminal_size.columns}×{terminal_size.lines}",
        f"Encoding: {getattr(sys.stdout, 'encoding', None) or 'unknown'}",
        f"Sophon: {VERSION}",
        f"Executable: {display_home_paths(sys.executable)}",
        "",
    ]
    if sys.version_info < (3, 10):
        warnings.append("Python 3.10 or newer is required")

    try:
        config = read_config()
    except (OSError, ValueError) as error:
        config = None
        warnings.append(f"Configuration could not be read: {error}")
    if config is None:
        rows.extend(["Provider: not configured", "Model: not selected"])
        if not warnings:
            warnings.append("No provider is configured")
    else:
        provider = str(config.get("provider", "unknown"))
        rows.extend([
            f"Provider: {PROVIDERS.get(provider, {}).get('label', provider)}",
            f"Model: {config.get('model', 'unknown')}",
            f"Auto-update: {'on' if config.get('auto_update') is True else 'off'}",
        ])
        reasoning = config.get("reasoning") if isinstance(config.get("reasoning"), dict) else {}
        if reasoning.get("is_support") is not True:
            rows.append("Reasoning: not supported")
        else:
            rows.append(f"Reasoning: {'on' if reasoning.get('enabled') is True else 'off'}")

    config_file = config_path()
    config_readable = config_file.is_file() and os.access(config_file, os.R_OK)
    rows.append(f"Config: {display_home_paths(config_file)} · {'readable' if config_readable else 'not found or unreadable'}")
    if not config_readable and config is not None:
        warnings.append("The configured provider file is missing or unreadable")
    if config_file.exists() and os.name != "nt":
        try:
            if config_file.stat().st_mode & 0o077:
                warnings.append("Config permissions are broader than 600")
        except OSError:
            warnings.append("Config permissions could not be checked")

    try:
        from sophon.agent import LEGACY_SESSION_FILE, PROJECT_DIR, SESSION_FILE, WORKSPACE_ROOT
        from sophon.agent import _instructions_file

        workspace_state = (
            "writable"
            if PROJECT_DIR.is_dir() and os.access(PROJECT_DIR, os.W_OK)
            else "not writable or not initialized"
        )
        rows.extend([
            f"Active project: {display_home_paths(PROJECT_DIR)}",
            f"Workspace storage: {workspace_state}",
        ])
        if workspace_state != "writable":
            warnings.append("The active workspace is not writable")
        instructions = _instructions_file(config or {})
        identity_state = "readable" if instructions.is_file() and os.access(instructions, os.R_OK) else "not found"
        rows.append(f"Agent identity: {display_home_paths(instructions)} · {identity_state}")
        if identity_state != "readable":
            warnings.append("AGENT.md is missing; it will be created when Agent mode starts")
        session_file = SESSION_FILE
        if not session_file.exists() and LEGACY_SESSION_FILE.exists():
            session_file = LEGACY_SESSION_FILE
        try:
            raw_sessions = json.loads(session_file.read_text(encoding="utf-8"))
            if not isinstance(raw_sessions, list) or any(not isinstance(item, dict) for item in raw_sessions):
                raise ValueError("Saved sessions file has an invalid format")
        except FileNotFoundError:
            rows.append(f"Sessions: none yet · {display_home_paths(session_file)}")
        except (OSError, UnicodeError, ValueError) as error:
            warnings.append(f"Session history unavailable: {error}")
            rows.append("Sessions: unavailable")
        else:
            rows.append(f"Sessions: {len(raw_sessions)} saved · {display_home_paths(session_file)}")
        rows.append(f"Workspace root: {display_home_paths(WORKSPACE_ROOT)}")
    except (ImportError, OSError, ValueError) as error:
        warnings.append(f"Workspace status unavailable: {error}")

    try:
        free_bytes = shutil.disk_usage(Path.home()).free
        rows.append(f"Home storage free: {free_bytes / (1024 ** 3):.1f} GiB")
        if free_bytes < 512 * 1024 * 1024:
            warnings.append("Less than 512 MiB of home storage is free")
    except OSError:
        rows.append("Home storage: unable to inspect free space")

    utilities = [
        f"{name}: {'available' if shutil.which(name) else 'not found'}"
        for name in ("git", "curl", "nano", "vi")
    ]
    rows.extend(["", "Utilities: " + " · ".join(utilities)])
    rows.extend(["", "Network reachability is not tested by /status."])
    warning_count = len(warnings)
    if warnings:
        rows.extend(["", "Notes:", *[f"• {warning}" for warning in warnings]])
        border_color, text_color, badge = UPDATE_BORDER, UPDATE_TEXT, f"{warning_count} notes"
    else:
        border_color, text_color, badge = GREEN, GREEN, "READY"
    card(
        "Sophon System Status",
        rows,
        border_color=border_color,
        text_color=text_color,
        top_right=badge,
    )
    return 0


def _identity_visible_text(text: str, cursor: int, max_cells: int) -> str:
    if len(text) <= max_cells:
        return text[:cursor] + "▏" + text[cursor:]
    start = max(0, min(cursor - max_cells // 2, len(text) - max_cells))
    end = min(len(text), start + max_cells)
    fragment = text[start:end]
    caret = max(0, min(cursor - start, len(fragment)))
    return ("…" if start else "") + fragment[:caret] + "▏" + fragment[caret:] + ("…" if end < len(text) else "")


def _edit_identity_card(path: Path, content: str) -> str | None:
    from sophon.agent import _read_key

    original = content
    lines = content.split("\n") if content else [""]
    trailing_newline = content.endswith("\n")
    selected = 0
    cursor = 0
    scroll = 0
    while True:
        size = shutil.get_terminal_size((80, 24))
        visible_count = max(1, min(18, size.lines - 7))
        scroll = max(0, min(selected - visible_count // 2, len(lines) - visible_count))
        inner_width = intelligent_ui.card_width(size.columns)
        max_line_cells = max(8, inner_width - 4)
        rows = [f"File: {display_home_paths(path)}", ""]
        for index, line in enumerate(lines[scroll : scroll + visible_count], scroll):
            safe_line = "".join(character if character.isprintable() else "�" for character in line)
            visible_line = _identity_visible_text(safe_line, cursor, max_line_cells) if index == selected else safe_line[:max_line_cells]
            rows.append(("› " if index == selected else "  ") + visible_line)
        rows.extend(["", "ctrl+x to save" + " " * max(1, inner_width - len("ctrl+x to save") - len("esc to cancel")) + "esc to cancel"])
        clear_terminal()
        card("Agent Identity", rows, top_right="AGENT.md", border_color=UPDATE_BORDER, text_color=WHITE)
        key = _read_key(timeout=0.2)
        if key == "TIMEOUT":
            continue
        if key == "UP":
            selected = max(0, selected - 1)
            cursor = min(cursor, len(lines[selected]))
        elif key == "DOWN":
            if selected == len(lines) - 1:
                lines.append("")
                selected += 1
                cursor = 0
                trailing_newline = False
            else:
                selected += 1
                cursor = min(cursor, len(lines[selected]))
        elif key == "LEFT":
            cursor = max(0, cursor - 1)
        elif key == "RIGHT":
            cursor = min(len(lines[selected]), cursor + 1)
        elif key == "BACKSPACE":
            if cursor:
                lines[selected] = lines[selected][:cursor - 1] + lines[selected][cursor:]
                cursor -= 1
            elif selected:
                cursor = len(lines[selected - 1])
                lines[selected - 1] += lines.pop(selected)
                selected -= 1
        elif key == "DELETE":
            if cursor < len(lines[selected]):
                lines[selected] = lines[selected][:cursor] + lines[selected][cursor + 1:]
            elif selected + 1 < len(lines):
                lines[selected] += lines.pop(selected + 1)
        elif key == "ENTER":
            remainder = lines[selected][cursor:]
            lines[selected] = lines[selected][:cursor]
            lines.insert(selected + 1, remainder)
            selected += 1
            cursor = 0
            trailing_newline = False
        elif key == "SAVE":
            result = "\n".join(lines)
            if trailing_newline and not result.endswith("\n"):
                result += "\n"
            if result == original:
                return result
            clear_terminal()
            card("Save Agent Identity", ["Save changes to AGENT.md?", "Y save · N continue editing"], border_color=UPDATE_BORDER, text_color=WHITE)
            answer = _read_key()
            if answer == "CHAR:y" or answer == "CHAR:Y":
                return result
        elif key.startswith("CHAR:"):
            character = key[5:]
            lines[selected] = lines[selected][:cursor] + character + lines[selected][cursor:]
            cursor += len(character)
        elif key in {"CANCEL", "DOUBLE_CANCEL"}:
            return None


def _write_identity_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_path = tempfile.mkstemp(prefix=".AGENT-", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary_path, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


def manage_identity() -> int:
    from sophon.agent import _instructions_file
    from sophon.config import ensure_agent_instructions_file

    try:
        config = read_config() or {}
    except (OSError, ValueError):
        config = {}
    try:
        path = ensure_agent_instructions_file(_instructions_file(config))
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as error:
        card("Agent Identity", [f"Could not read AGENT.md: {error}"], border_color=NOTICE_BORDER, text_color=NOTICE_TEXT)
        return 1

    try:
        updated = _edit_identity_card(path, content)
    except (EOFError, KeyboardInterrupt):
        return 0
    except (OSError, ValueError) as error:
        card("Agent Identity", [f"Editor input failed: {error}"], border_color=NOTICE_BORDER, text_color=NOTICE_TEXT)
        return 1
    if updated is None:
        card("Agent Identity", ["Changes discarded.", f"File: {display_home_paths(path)}"])
        return 0
    try:
        _write_identity_file(path, updated)
    except OSError as error:
        card("Agent Identity", [f"Could not save AGENT.md: {error}"], border_color=NOTICE_BORDER, text_color=NOTICE_TEXT)
        return 1
    card("Agent Identity", ["AGENT.md saved.", f"File: {display_home_paths(path)}"], top_right="Saved")
    return 0


def print_help() -> None:
    card(
        "sophon --help",
        [
            "Usage: sophon [run|update|init|setup|config|doctor|agent]",
            "Flags: --version, --help",
            "",
            "Commands:",
            "run — Start terminal UI",
            "update — Install latest version",
            "init — Create starter config",
            "setup — Configure a provider",
            "config — Show provider settings",
            "doctor — Check system health",
            "agent — Chat (-m MESSAGE, -t TASK)",
            "agent -p SYSTEM_PROMPT -m MESSAGE — Custom system prompt",
            "-v, --version — Show installed version",
            "--help — Show this help",
        ],
    )


def main() -> int:
    args = sys.argv[1:]
    if args in (["--version"], ["--v"], ["-v"]):
        print(f"sophon {VERSION}")
        return 0
    if args in (["--help"], ["-h"]):
        print_help()
        return 0
    try:
        ensure_environment_config()
    except (OSError, ValueError):
        pass
    if args == ["update"]:
        return update()
    if args == ["init"]:
        return init_config()
    if args == ["setup"]:
        from sophon.setup_wizard import run_setup_wizard

        return run_setup_wizard()
    if args == ["config"]:
        return show_config()
    if args == ["doctor"]:
        return doctor()
    if args and args[0] == "agent":
        from sophon.agent import EXIT_TO_TUI, main as agent_main

        result = agent_main(args[1:])
        if RESTART_REQUESTED:
            restart_sophon(RESTART_ARGS)
        if result == EXIT_TO_TUI:
            return run_session()
        return result
    if not args or args == ["run"]:
        result = run_session()
        if RESTART_REQUESTED:
            restart_sophon(RESTART_ARGS)
        return result
    print(f"Unknown command: {' '.join(args)}", file=sys.stderr)
    print("Run 'sophon --help' for usage.", file=sys.stderr)
    return 2
