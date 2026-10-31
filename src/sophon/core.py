"""Shared, dependency-free Sophon CLI and terminal UI."""

from __future__ import annotations

import base64
import codecs
import json
import os
import select
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from sophon.config import PROVIDERS, config_display_path, config_path, read_config, write_config
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


def paint(text: str, color: str) -> str:
    return f"{color}{text}{RESET}" if sys.stdout.isatty() else text


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
    terminal_width = shutil.get_terminal_size((80, 24)).columns
    inner = min(width or 68, max(20, terminal_width - 8))
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


def read_prompt(prompt: str) -> str:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return input(prompt)

    import termios
    import tty

    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    decoder = codecs.getincrementaldecoder(sys.stdin.encoding or "utf-8")()
    characters: list[str] = []
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        tty.setraw(fd)
        while True:
            raw = os.read(fd, 1)
            if not raw:
                raise EOFError
            if raw == b"\x1b":
                if not select.select([fd], [], [], 0.08)[0]:
                    raise EOFError
                prefix = os.read(fd, 1)
                if prefix in {b"[", b"O"}:
                    while select.select([fd], [], [], 0.01)[0]:
                        suffix = os.read(fd, 1)
                        if suffix and 0x40 <= suffix[0] <= 0x7E:
                            break
                continue
            if raw in {b"\r", b"\n"}:
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                return "".join(characters)
            if raw == b"\x03":
                raise KeyboardInterrupt
            if raw == b"\x04":
                if not characters:
                    raise EOFError
                continue
            if raw in {b"\x08", b"\x7f"}:
                if characters:
                    width = display_width(characters.pop())
                    if width:
                        sys.stdout.write("\b \b" * width)
                        sys.stdout.flush()
                continue

            character = decoder.decode(raw)
            if character and (character.isprintable() or character == "\t"):
                characters.append(character)
                sys.stdout.write(character)
                sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def show_welcome(*, check_updates: bool = False) -> None:
    print()
    card(
        "Sophon",
        ["A minimal development environment.", "Build. Think. Execute.", "", f"READY    v{VERSION}"],
        centered_lines={"Sophon"},
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
    inner = min(68, max(20, shutil.get_terminal_size((80, 24)).columns - 8))
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
        show(["Update complete", f"sophon {version}", "Restarting Sophon"], final=True)
        if restart_after:
            restart_sophon()
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
    remote_version = latest_version(timeout=4)
    if remote_version and remote_version != VERSION:
        update(known_version=remote_version, restart_after=True)


def run_session() -> int:
    clear_terminal()
    show_welcome(check_updates=True)
    while True:
        try:
            value = read_prompt(paint("  › ", GREEN)).strip()
        except (EOFError, KeyboardInterrupt):
            sys.stdout.write("\r\033[2K")
            card(None, ["Session closed. See you soon."])
            return 0
        if not value:
            continue
        command = value.lower()
        if command in {"/exit", "/quit", "q"}:
            break
        if command.startswith("/"):
            if command == "/update":
                update()
            elif command == "/help":
                card("Sophon commands", ["/help — Show available commands", "/setup — Configure a provider", "/config — Show saved settings", "/auto-update on|off — Automatic updates at startup", "/clear — Redraw the welcome screen", "/version — Print the current version", "/update — Update and restart if changed", "/restart — Restart Sophon", "/exit — Close Sophon"])
            elif command == "/setup":
                from sophon.setup_wizard import run_setup_wizard

                if run_setup_wizard() == 0:
                    show_welcome()
            elif command == "/config":
                show_config()
            elif command.startswith("/auto-update"):
                _, _, value = command.partition(" ")
                value = value.strip()
                if value in {"on", "off"}:
                    set_auto_update(value == "on")
                    card("Automatic updates", [f"Auto-update is {value}."])
                elif not value:
                    status = "on" if auto_update_enabled() else "off"
                    card("Automatic updates", [f"Auto-update is {status}.", "Use /auto-update on or /auto-update off."])
                else:
                    card("Automatic updates", ["Use /auto-update on or /auto-update off."], border_color=NOTICE_BORDER, text_color=NOTICE_TEXT)
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
    card(None, ["Session closed. See you soon."])
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
        card(None, ["No provider is configured.", "Run sophon setup to connect one."])
        return 0

    provider = data["provider"]
    lines = [
        f"Provider: {PROVIDERS[provider]['label']}",
        f"Model: {data['model']}",
        "API key: hidden",
        f"Auto-update: {'on' if data.get('auto_update') is True else 'off'}",
    ]
    reasoning = data.get("reasoning") if isinstance(data.get("reasoning"), dict) else {}
    support = "supported" if reasoning.get("is_support") is True else "not supported"
    state = "on" if reasoning.get("enabled") is True else "off"
    lines.append(f"Reasoning: {support}, {state}, effort {reasoning.get('effort', 'medium')}")
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
        path = config_path()
        try:
            mode = path.stat().st_mode
        except OSError:
            record("FAIL", "Credential file could not be inspected")
        else:
            if os.name != "nt" and mode & 0o077:
                record("WARN", "Credential file permissions are broader than 600")
            else:
                record("PASS", "Credential file permissions")

    def check_writable(label: str, path: Path) -> None:
        candidate = path
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        if candidate.is_dir() and os.access(candidate, os.W_OK):
            record("PASS", f"{label} storage is writable")
        else:
            record("FAIL", f"{label} storage is not writable · {path}")

    check_writable("Configuration", config_path().parent)
    try:
        from sophon.agent import SESSION_FILE, _load_sessions

        sessions = _load_sessions()
    except (OSError, RuntimeError) as error:
        sessions = []
        record("FAIL", f"Session history is unreadable · {error}")
    else:
        detail = f"Session history · {len(sessions)} saved" if sessions else "Session history · none yet"
        record("PASS", detail)
    check_writable("Session", SESSION_FILE.parent)
    if SESSION_FILE.exists() and os.name != "nt":
        try:
            mode = SESSION_FILE.stat().st_mode
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
    card("Sophon Doctor", checks, border_color=border_color, text_color=text_color)
    return int(bool(counts["WARN"] or counts["FAIL"]))


def print_help() -> None:
    card(
        "sophon --help",
        [
            "Usage: sophon [run|update|init|setup|config|doctor|agent|--version|--help]",
            "",
            "Commands:",
            "run — Start the Sophon terminal interface",
            "update — Safely fetch the latest version",
            "init — Create a starter configuration",
            "setup — Configure an AI provider",
            "config — Show saved provider settings",
            "doctor — Check Sophon system and configuration health",
            "agent — Start an AI chat session (agent -p MESSAGE for one-shot)",
            "--version — Show the current version",
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
    if args != ["update"] and auto_update_enabled():
        auto_update_on_start()
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
        from sophon.agent import main as agent_main

        result = agent_main(args[1:])
        if RESTART_REQUESTED:
            restart_sophon(RESTART_ARGS)
        return result
    if not args or args == ["run"]:
        return run_session()
    print(f"Unknown command: {' '.join(args)}", file=sys.stderr)
    print("Run 'sophon --help' for usage.", file=sys.stderr)
    return 2
