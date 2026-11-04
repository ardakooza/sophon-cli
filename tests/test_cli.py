import os
import re
import subprocess
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sophon.config import config_path, write_config
from sophon.core import card, display_width, wrap_text

VERSION = (ROOT / "src" / "sophon" / "VERSION").read_text(encoding="utf-8").strip()
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src")}


def run(*args, input_text=None, env_overrides=None):
    env = {**ENV, **(env_overrides or {})}
    return subprocess.run(
        [sys.executable, *map(str, args)],
        cwd=ROOT,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )


def test_version_is_dynamic_and_shared():
    standalone = run(ROOT / "sophon", "--version")
    package_cli = run("-m", "sophon.cli", "--version")
    assert standalone.returncode == package_cli.returncode == 0
    assert standalone.stdout == package_cli.stdout == f"sophon {VERSION}\n"


def test_help_is_shared_between_entry_points():
    standalone = run(ROOT / "sophon", "--help")
    package_cli = run("-m", "sophon.cli", "--help")
    assert standalone.stdout == package_cli.stdout
    assert "update" in standalone.stdout
    assert "init" in standalone.stdout
    assert "setup" in standalone.stdout
    assert "config" in standalone.stdout
    assert "sophon --help" in standalone.stdout
    assert "Usage: sophon [run|update|init|setup|config|doctor|agent]" in standalone.stdout
    assert "Flags: --version, --help" in standalone.stdout
    assert "agent — Chat (-m MESSAGE, -t TASK)" in standalone.stdout
    assert "-p SYSTEM_PROMPT -m MESSAGE" in standalone.stdout
    assert "╭" in standalone.stdout


def test_help_command_rows_fit_one_line_at_standard_width():
    result = run(ROOT / "sophon", "--help")
    help_rows = [line for line in result.stdout.splitlines() if "agent — Chat" in line]
    assert len(help_rows) == 1
    assert "-m MESSAGE, -t TASK" in help_rows[0]


def test_double_dash_version_alias_is_supported():
    result = run(ROOT / "sophon", "--v")
    assert result.returncode == 0
    assert result.stdout == f"sophon {VERSION}\n"


def test_setup_progress_uses_lowercase_of_and_white_text():
    from sophon.setup_wizard import _choose_provider

    class TerminalBuffer(StringIO):
        def isatty(self):
            return True

    output = TerminalBuffer()
    fake_input = type("TerminalInput", (), {"isatty": lambda self: True})()
    with patch("sys.stdout", output), patch("sys.stdin", fake_input), patch(
        "sophon.setup_wizard._read_key", return_value="CANCEL"
    ):
        assert _choose_provider() is None
    text = output.getvalue()
    assert "1 of 4" in text
    assert "1 OF 4" not in text
    assert "\033[38;5;255m" in text


def test_previous_next_hint_is_limited_to_numbered_setup_steps():
    from sophon.setup_wizard import _choose_existing_action, _choose_provider

    class TerminalBuffer(StringIO):
        def isatty(self):
            return True

    fake_input = type("TerminalInput", (), {"isatty": lambda self: True})()
    output = TerminalBuffer()
    with (
        patch("sys.stdout", output),
        patch("sys.stdin", fake_input),
        patch("sophon.setup_wizard.clear_terminal"),
        patch("sophon.setup_wizard._read_key", return_value="CANCEL"),
    ):
        assert _choose_provider() is None
    assert "1 of 4" in output.getvalue()
    assert "←/→ previous/next" in output.getvalue()

    output = TerminalBuffer()
    with (
        patch("sys.stdout", output),
        patch("sys.stdin", fake_input),
        patch("sophon.setup_wizard.clear_terminal"),
        patch("sophon.setup_wizard._read_key", return_value="CANCEL"),
    ):
        assert _choose_existing_action({"provider": "openrouter", "model": "test"}) is None
    assert "Current Provider" in output.getvalue()
    assert "←/→ previous/next" not in output.getvalue()


def test_review_and_complete_card_titles_and_provider_labels_are_white(tmp_path):
    from sophon.setup_wizard import run_setup_wizard

    class TerminalBuffer(StringIO):
        def isatty(self):
            return True

    output = TerminalBuffer()
    fake_input = type("TerminalInput", (), {"isatty": lambda self: True})()
    with (
        patch("sys.stdout", output),
        patch("sys.stdin", fake_input),
        patch("sophon.setup_wizard.read_config", return_value=None),
        patch("sophon.setup_wizard._choose_provider", return_value="openrouter"),
        patch("sophon.setup_wizard._choose_model", return_value="test-model"),
        patch("sophon.agent._detect_reasoning_support", return_value=False),
        patch("sophon.setup_wizard._read_hidden_credential", return_value="test-secret"),
        patch("sophon.setup_wizard._read_key", return_value="ENTER"),
        patch("sophon.setup_wizard.write_config", return_value=tmp_path / "config.json"),
        patch("sophon.setup_wizard.ensure_agent_instructions_file"),
    ):
        assert run_setup_wizard() == 0
    text = output.getvalue()
    assert re.search(r"\033\[38;5;255m\s*Setup Complete", text)
    assert "Review Setup" not in text
    assert "4 of 4" not in text
    assert text.count("Setup Complete") == 1
    assert "\033[38;5;255mProvider:\033[0m" in text
    assert "test-secret" not in text


def test_setup_credentials_card_contains_the_hidden_api_key_field(tmp_path):
    from sophon.setup_wizard import run_setup_wizard

    class TerminalBuffer(StringIO):
        def isatty(self):
            return True

    output = TerminalBuffer()
    fake_input = type("TerminalInput", (), {"isatty": lambda self: True})()
    with (
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}),
        patch("sys.stdout", output),
        patch("sys.stdin", fake_input),
        patch("sophon.setup_wizard.read_config", return_value=None),
        patch("sophon.setup_wizard._choose_provider", return_value="openrouter"),
        patch("sophon.setup_wizard._choose_model", return_value="test-model"),
        patch("sophon.agent._detect_reasoning_support", return_value=False),
        patch("sophon.setup_wizard._read_hidden_credential", return_value="test-secret") as credential_reader,
        patch("sophon.setup_wizard._read_key", return_value="ENTER"),
        patch("sophon.setup_wizard.write_config", return_value=tmp_path / "config.json"),
        patch("sophon.setup_wizard.ensure_agent_instructions_file"),
    ):
        assert run_setup_wizard() == 0
    text = output.getvalue()
    credential_reader.assert_called_once_with("API key", "OpenRouter", "test-model", saved_key_exists=False, saved_key="")
    assert "test-secret" not in text


def test_hidden_credential_is_masked_inside_the_setup_card():
    import pty
    import threading
    import time

    from sophon.setup_wizard import _read_hidden_credential

    class TerminalBuffer(StringIO):
        def isatty(self):
            return True

    master, slave = pty.openpty()
    terminal_input = os.fdopen(slave, "r", encoding="utf-8", closefd=False)
    output = TerminalBuffer()
    try:
        writer = threading.Thread(target=lambda: (time.sleep(0.05), os.write(master, b"secret\n")), daemon=True)
        writer.start()
        with patch("sys.stdin", terminal_input), patch("sys.stdout", output):
            assert _read_hidden_credential("API key", "OpenRouter", "test-model", saved_key_exists=False) == "secret"
        writer.join(timeout=1)
    finally:
        terminal_input.close()
        os.close(master)
        os.close(slave)
    rendered = output.getvalue()
    assert "Setup of Credentials" in rendered
    assert "API key: [hidden input]" not in rendered
    assert "API key:" in rendered
    plain_rendered = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
    assert "API key: •" in plain_rendered
    assert "secret" not in rendered



def test_main_interactive_completion_covers_commands_and_arguments():
    from sophon.core import _main_completion_choices

    assert "gent" in _main_completion_choices("/a", 2)
    assert _main_completion_choices("/auto-update ", 13) == ["on", "off"]
    assert _main_completion_choices("/auto-backup ", 13) == ["on", "off", "frequency ", "amount "]
    assert _main_completion_choices("/auto-backup amount ", 20) == [str(value) for value in range(1, 11)]


def test_saved_credential_can_be_kept_or_replaced_with_arrow_selection():
    import pty
    import threading
    import time

    from sophon.setup_wizard import _read_hidden_credential

    def run_with_input(payload):
        master, slave = pty.openpty()
        terminal_input = os.fdopen(slave, "r", encoding="utf-8", closefd=False)
        output = StringIO()
        writer = threading.Thread(target=lambda: (time.sleep(0.05), os.write(master, payload)), daemon=True)
        writer.start()
        try:
            with patch("sys.stdin", terminal_input), patch("sys.stdout", output):
                value = _read_hidden_credential(
                    "API key", "OpenRouter", "test-model", saved_key_exists=True, saved_key="saved-secret"
                )
            writer.join(timeout=1)
            return value, re.sub(r"\x1b\[[0-9;]*m", "", output.getvalue())
        finally:
            terminal_input.close()
            os.close(master)
            os.close(slave)

    kept, keep_render = run_with_input(b"\r")
    assert kept == "saved-secret"
    assert "Keep saved key" in keep_render and "New key" in keep_render
    replaced, _ = run_with_input(b"\x1b[B\rnew-secret\r")
    assert replaced == "new-secret"


def test_automatic_backup_status_uses_top_right_on_off_badge():
    from sophon import core

    cards = []
    with (
        patch("sophon.core._backup_preferences", return_value=({}, {
            "enabled": True, "frequency": "Every 2 days", "backup_amount": 2, "last_backup_at": ""
        })),
        patch("sophon.core.card", side_effect=lambda title, lines, **kwargs: cards.append((title, lines, kwargs))),
    ):
        assert core.auto_backup_command("") == 0
    title, lines, kwargs = cards[-1]
    assert title == "Automatic backups"
    assert "Status:" not in " ".join(lines)
    assert kwargs["top_right"] == "ON"


def test_plain_words_are_not_commands():
    result = run(ROOT / "sophon", input_text="exit\n/exit\n")
    assert result.returncode == 0
    assert "Commands must start with /." in result.stdout
    assert "Interactive closed. See you soon." in result.stdout


def test_cwd_and_pwd_are_not_terminal_ui_commands():
    with tempfile.TemporaryDirectory() as temp_dir:
        result = run(
            ROOT / "sophon",
            input_text="/cwd\n/pwd\n/exit\n",
            env_overrides={"XDG_CONFIG_HOME": temp_dir},
        )
    assert result.returncode == 0
    assert "not a Sophon command" in result.stdout
    assert "Current directory" not in result.stdout
    assert "Project workspace" not in result.stdout


def test_agent_single_dash_prompt_argument_is_accepted():
    with tempfile.TemporaryDirectory() as temp_dir:
        result = run(
            ROOT / "sophon",
            "agent",
            "-p",
            "hello",
            env_overrides={"XDG_CONFIG_HOME": temp_dir},
        )
    assert result.returncode == 1
    assert "No provider is configured" in result.stdout
    assert "Unknown command" not in result.stdout + result.stderr


def test_card_puts_a_blank_row_after_its_title():
    output = StringIO()
    with patch("sys.stdout", output):
        card("Heading", ["Body text"])
    rows = output.getvalue().splitlines()
    assert rows[2].split("│", 1)[1].rsplit("│", 1)[0].strip() == ""


def test_setup_refuses_to_read_credentials_without_a_tty():
    result = run(ROOT / "sophon", "setup", input_text="do-not-read-this-secret\n")
    assert result.returncode == 1
    assert "interactive terminal" in result.stdout
    assert "do-not-read-this-secret" not in result.stdout + result.stderr


def test_unicode_display_width_and_word_wrap():
    assert display_width("界") == 2
    assert display_width("e\u0301") == 1
    lines = wrap_text("alpha 界界 beta", 7)
    assert lines == ["alpha", "界界", "beta"]
    assert all(display_width(line) <= 7 for line in lines)


def test_long_unbroken_words_are_split_by_display_width():
    parts = wrap_text("界" * 7, 5)
    assert parts == ["界" * 2, "界" * 2, "界" * 2, "界"]
    assert all(display_width(part) <= 5 for part in parts)


def test_unicode_card_border_rows_stay_aligned():
    output = StringIO()
    with patch("sys.stdout", output):
        card(None, ["Latin words wrap correctly", "漢字 and e\u0301 stay aligned"], width=12)
    rows = output.getvalue().splitlines()
    widths = {display_width(row) for row in rows}
    assert len(widths) == 1


def test_config_hides_credentials_and_uses_private_file_permissions():
    with tempfile.TemporaryDirectory() as temp_dir:
        env = {"XDG_CONFIG_HOME": temp_dir}
        with patch.dict(os.environ, env):
            path = write_config({
                "schema_version": 1,
                "provider": "openrouter",
                "model": "~openai/gpt-sol-latest",
                "api_key": "secret-openrouter-key",
                "endpoint": "https://openrouter.ai/api/v1/chat/completions",
            })
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        result = run(ROOT / "sophon", "config", env_overrides=env)
        assert result.returncode == 0
        assert "secret-openrouter-key" not in result.stdout
        assert "API key: hidden" in result.stdout
        assert "Config:" in result.stdout
        assert "Config file:" not in result.stdout
        assert "Endpoint:" not in result.stdout


def test_auto_update_setting_defaults_off_and_is_persisted_in_private_config(tmp_path):
    from sophon import core

    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
        assert core.auto_update_enabled() is False
        core.set_auto_update(True)
        assert core.auto_update_enabled() is True
        path = config_path()
        assert path.stat().st_mode & 0o777 == 0o600
        assert not core.settings_path().exists()
        core.set_auto_update(False)
        assert core.auto_update_enabled() is False


def test_legacy_auto_update_migrates_to_config_without_provider(tmp_path):
    import json

    from sophon import core

    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
        legacy = core.settings_path()
        legacy.parent.mkdir(parents=True)
        legacy.write_text('{"auto_update": true}\n', encoding="utf-8")
        assert core.auto_update_enabled() is True
        saved = json.loads(config_path().read_text(encoding="utf-8"))
        assert not legacy.exists()
    assert saved == {"schema_version": 1, "auto_update": True}


def test_auto_update_preference_is_written_into_provider_config(tmp_path):
    import json

    from sophon import core

    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
        write_config({
            "schema_version": 1,
            "provider": "openrouter",
            "model": "test-model",
            "api_key": "test-key",
        })
        core.set_auto_update(True)
        saved = json.loads(config_path().read_text(encoding="utf-8"))
    assert saved["auto_update"] is True
    assert saved["reasoning"] == {"is_support": False, "enabled": False, "effort": "medium"}
    assert saved["agent_instructions_path"].endswith("/agent/AGENT.md")


def test_legacy_auto_update_preference_migrates_into_config(tmp_path):
    import json

    from sophon.config import read_config

    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
        settings = config_path().parent.parent / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"auto_update": true}\n', encoding="utf-8")
        write_config({
            "schema_version": 1,
            "provider": "openrouter",
            "model": "test-model",
            "api_key": "test-key",
        })
        # Remove fields that write_config intentionally preserves as opaque data.
        path = config_path()
        legacy_shape = json.loads(path.read_text(encoding="utf-8"))
        legacy_shape.pop("auto_update", None)
        legacy_shape.pop("reasoning", None)
        legacy_shape.pop("agent_instructions_path", None)
        path.write_text(json.dumps(legacy_shape), encoding="utf-8")
        migrated = read_config()
        persisted = json.loads(path.read_text(encoding="utf-8"))
        assert not settings.exists()
    assert migrated["auto_update"] is True
    assert persisted["auto_update"] is True


def test_terminal_auto_update_command_can_be_toggled(tmp_path):
    result = run(
        ROOT / "sophon",
        input_text="/auto-update on\n/auto-update\n/auto-update off\n/exit\n",
        env_overrides={"XDG_CONFIG_HOME": str(tmp_path)},
    )
    assert result.returncode == 0
    assert "Auto-update is on." in result.stdout
    assert "Auto-update is off." in result.stdout


def test_terminal_restart_command_restarts_current_invocation():
    from sophon import core

    with patch("sophon.core.read_prompt", side_effect=["/restart", "/exit"]), patch(
        "sophon.core.restart_sophon"
    ) as restart:
        assert core.run_session() == 0
    restart.assert_called_once_with()


def test_auto_update_checks_on_startup_only_when_enabled():
    from sophon import core

    latest = "31.10.2026 – #9999"
    with (
        patch("sophon.core.auto_update_enabled", return_value=True),
        patch("sophon.core.latest_version", return_value=latest),
        patch("sophon.core.update") as update,
    ):
        core.auto_update_on_start()
    update.assert_called_once_with(known_version=latest, restart_after=True)


def test_main_does_not_run_startup_update_check_for_maintenance_commands():
    from sophon import core

    with (
        patch("sophon.core.auto_update_on_start") as startup_update,
        patch("sophon.core.ensure_environment_config"),
        patch("sophon.core.doctor", return_value=0),
        patch("sophon.core.sys.argv", ["sophon", "doctor"]),
    ):
        assert core.main() == 0
    startup_update.assert_not_called()


def test_run_session_shows_welcome_before_deferred_update_check():
    from sophon import core

    events = []
    with (
        patch("sophon.core.clear_terminal", side_effect=lambda: events.append("clear")),
        patch("sophon.core.show_welcome", side_effect=lambda **kwargs: events.append("welcome")),
        patch("sophon.core.auto_update_enabled", return_value=True),
        patch("sophon.core.auto_update_on_start", side_effect=lambda: events.append("update")),
        patch("sophon.core.read_prompt", return_value="/exit"),
        patch.object(core, "RESTART_REQUESTED", False),
    ):
        assert core.run_session() == 0
    assert events[:3] == ["clear", "welcome", "update"]


def test_automatic_update_status_card_is_one_line():
    from sophon import core

    cards = []
    with (
        patch("sophon.core.clear_terminal"),
        patch("sophon.core.show_welcome"),
        patch("sophon.core.auto_update_enabled", return_value=False),
        patch("sophon.core.set_auto_update") as set_update,
        patch("sophon.core.read_prompt", side_effect=["/auto-update on", "/exit"]),
        patch("sophon.core.card", side_effect=lambda title, lines, **kwargs: cards.append((title, lines))),
        patch.object(core, "RESTART_REQUESTED", False),
    ):
        assert core.run_session() == 0
    update_cards = [lines for title, lines in cards if title == "Automatic updates"]
    assert update_cards == [["Auto-update is on.", "Use /auto-update off to off."]]
    set_update.assert_called_once_with(True)


def test_agent_exposes_required_builtin_tools_on_every_provider_request():
    import json

    from sophon.agent import BUILTIN_TOOLS, _request

    names = {tool["function"]["name"] for tool in BUILTIN_TOOLS}
    assert names == {
        "get_version", "is_update", "update", "system_health", "workspace_list",
        "session_list", "session_rename", "get_time", "get_date",
    }

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":12,"completion_tokens":3,"total_tokens":15}}'

    captured = {}

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    config = {
        "provider": "openrouter",
        "model": "test-model",
        "api_key": "test-key",
        "endpoint": "https://openrouter.ai/api/v1/chat/completions",
    }
    usage = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "estimated": False}
    with patch("sophon.agent.urlopen", side_effect=fake_urlopen):
        assert _request(config, [{"role": "user", "content": "hello"}], usage_out=usage) == "ok"
    sent_names = {tool["function"]["name"] for tool in captured["payload"]["tools"]}
    assert names <= sent_names
    assert {"list_files", "read_file", "write_file"} <= sent_names
    assert usage == {"requests": 1, "prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15, "estimated": False}


def test_openrouter_user_not_found_401_has_actionable_auth_guidance():
    from io import BytesIO
    from urllib.error import HTTPError
    from sophon.agent import _request

    captured = {}

    def reject(request, timeout):
        captured["authorization"] = request.get_header("Authorization")
        raise HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            hdrs=None,
            fp=BytesIO(b'{"error":{"message":"User not found.","code":401}}'),
        )

    config = {
        "provider": "openrouter",
        "model": "test-model",
        "api_key": "Bearer valid-looking-key",
        "endpoint": "https://openrouter.ai/api/v1/chat/completions",
    }
    try:
        with patch("sophon.agent.urlopen", side_effect=reject):
            _request(config, [{"role": "user", "content": "hello"}])
    except RuntimeError as error:
        assert "OpenRouter rejected the key/account" in str(error)
        assert "sophon setup" in str(error)
        assert "User not found." in str(error)
    else:
        raise AssertionError("The provider's 401 response must be surfaced")
    assert captured["authorization"] == "Bearer valid-looking-key"


def test_agent_executes_builtin_tool_calls_and_returns_results_to_the_model():
    from sophon.agent import _complete_turn

    tool_messages = []
    requests = 0

    def fake_request(config, messages, on_chunk=None, tool_calls_out=None, **kwargs):
        nonlocal requests
        requests += 1
        if requests == 1:
            tool_calls_out.append({
                "id": "call-version",
                "type": "function",
                "function": {"name": "get_version", "arguments": "{}"},
            })
            return ""
        tool_messages.extend(message for message in messages if message.get("role") == "tool")
        return "The installed Sophon version is available."

    config = {"provider": "openrouter", "model": "test", "api_key": "test"}
    with patch("sophon.agent._request", side_effect=fake_request):
        result = _complete_turn(config, [{"role": "user", "content": "What version?"}])
    assert result == "The installed Sophon version is available."
    assert len(tool_messages) == 1
    assert tool_messages[0]["name"] == "get_version"
    assert "Sophon CLI version:" in tool_messages[0]["content"]


def test_builtin_tool_dispatch_returns_version_update_health_and_workspace_results(tmp_path):
    from sophon import core
    from sophon.agent import _execute_builtin_tool

    with patch("sophon.core.latest_version", return_value=VERSION):
        assert VERSION in _execute_builtin_tool("get_version", {})
        assert "Update available: no" in _execute_builtin_tool("is_update", {})

    with patch("sophon.agent.subprocess.run") as run_doctor:
        run_doctor.return_value.stdout = "3 passed · 0 warnings · 0 errors"
        run_doctor.return_value.stderr = ""
        run_doctor.return_value.returncode = 0
        assert "3 passed" in _execute_builtin_tool("system_health", {})
        assert run_doctor.call_args.args[0][-1] == "doctor"

    with patch("sophon.agent.WORKSPACE_ROOT", tmp_path):
        (tmp_path / "project-a").mkdir()
        (tmp_path / "notes.txt").write_text("not a project folder")
        assert _execute_builtin_tool("workspace_list", {}) == "project-a"

    with (
        patch("sophon.core.update", return_value=0),
        patch.object(core, "RESTART_REQUESTED", True),
        patch.object(core, "RESTART_ARGS", None),
    ):
        assert "restart after this agent response" in _execute_builtin_tool("update", {})


def test_session_tools_list_and_rename_saved_sessions(tmp_path):
    from sophon import agent

    session_path = tmp_path / "sessions" / "sessions.json"
    with (
        patch("sophon.agent.SESSION_FILE", session_path),
        patch("sophon.agent.LEGACY_SESSION_FILE", tmp_path / "legacy.json"),
    ):
        agent._write_sessions([
            {
                "id": "ab12cd34ef",
                "title": "Old title",
                "created_at": "2026-10-31T10:00:00+03:00",
                "updated_at": "2026-10-31T10:00:00+03:00",
                "messages": [{"role": "user", "content": "hello"}],
            },
            {
                "id": "ab99cd34ef",
                "title": "Another session",
                "created_at": "2026-10-31T09:00:00+03:00",
                "updated_at": "2026-10-31T09:00:00+03:00",
                "messages": [],
            },
        ])
        listed = agent._execute_builtin_tool("session_list", {})
        renamed = agent._execute_builtin_tool(
            "session_rename", {"session_id": "ab12", "title": "New title"}
        )
        sessions = agent._load_sessions()
    assert "ab12cd34ef | Old title | 1 messages" in listed
    assert "New title" in renamed
    assert sessions[0]["title"] == "New title"
    assert sessions[0]["updated_at"] != "2026-10-31T10:00:00+03:00"
    with (
        patch("sophon.agent.SESSION_FILE", session_path),
        patch("sophon.agent.LEGACY_SESSION_FILE", tmp_path / "legacy.json"),
    ):
        assert "ambiguous" in str(_raises_runtime_error(
            lambda: agent._execute_builtin_tool("session_rename", {"session_id": "a", "title": "x"})
        ))


def _raises_runtime_error(call):
    try:
        call()
    except RuntimeError as error:
        return error
    raise AssertionError("Expected a RuntimeError")


def test_get_time_and_date_builtins_return_local_values():
    from sophon.agent import _execute_builtin_tool

    assert re.fullmatch(r"\d{2}:\d{2}:\d{2} .* \(UTC[+-]\d{4}\)", _execute_builtin_tool("get_time", {}))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", _execute_builtin_tool("get_date", {}))


def test_forked_session_has_new_identity_and_independent_message_objects():
    from sophon.agent import _fork_session

    source = {
        "id": "source1234",
        "title": "Debug session",
        "messages": [{"role": "assistant", "content": "Answer", "reasoning": "Thought"}],
    }
    forked = _fork_session(source)
    assert forked["id"] != source["id"]
    assert forked["title"] == "Fork of Debug session"
    assert forked["messages"] == source["messages"]
    assert forked["messages"][0] is not source["messages"][0]


def test_fork_command_persists_a_new_session_from_the_selected_session(tmp_path):
    from sophon import agent

    session_path = tmp_path / "sessions" / "sessions.json"
    source = {
        "id": "source1234",
        "title": "Original project",
        "created_at": "2026-10-31T10:00:00+03:00",
        "updated_at": "2026-10-31T10:00:00+03:00",
        "messages": [{"role": "user", "content": "first message"}],
    }
    with (
        patch("sophon.agent.SESSION_FILE", session_path),
        patch("sophon.agent.LEGACY_SESSION_FILE", tmp_path / "legacy.json"),
    ):
        agent._write_sessions([source.copy()])
        commands = iter(["/fork", "/exit"])
        with (
            patch("sophon.agent._input_bar", side_effect=lambda *args, **kwargs: next(commands)),
            patch("sophon.agent._pick_item", return_value={"session": source}),
        ):
            assert agent._interactive({"provider": "openrouter", "model": "test", "context_window": 1024}) == 0
        sessions = agent._load_sessions()
    forked = next(item for item in sessions if item["id"] != "source1234")
    assert forked["title"] == "Fork of Original project"
    assert forked["messages"] == source["messages"]


def test_agent_and_workspace_markdown_guides_are_separate_and_used(tmp_path, monkeypatch):
    import json

    from sophon import agent
    from sophon.config import ensure_agent_instructions_file

    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
        agent_path = ensure_agent_instructions_file()
        agent_path.write_text("# Custom Agent Guidance\nAlways explain tradeoffs.\n", encoding="utf-8")
        write_config({
            "schema_version": 1,
            "provider": "openrouter",
            "model": "test-model",
            "api_key": "test-key",
            "agent_instructions_path": str(agent_path),
        })
        workspace_root = tmp_path / "sophon" / "workspace"
        project = workspace_root / "project-x"
        monkeypatch.setattr(agent, "WORKSPACE_ROOT", workspace_root)
        monkeypatch.setattr(agent, "PROJECT_DIR", project)
        agent._ensure_workspace()
        prompt = agent._system_message({"agent_instructions_path": str(agent_path)})
        workspace_file = workspace_root / "WORKSPACE.md"
        config = json.loads(config_path().read_text(encoding="utf-8"))
    assert "Custom Agent Guidance" in prompt
    assert "Workspace guidance (WORKSPACE.md)" in prompt
    assert workspace_file.exists()
    assert "top-level folder" in workspace_file.read_text(encoding="utf-8")
    assert config["agent_instructions_path"] == str(agent_path)


def test_answering_indicator_dot_pulses_more_slowly():
    from sophon.agent import _answering_rows

    class TerminalBuffer(StringIO):
        def isatty(self):
            return True

    with patch("sys.stdout", TerminalBuffer()):
        first = _answering_rows(0)[0]
        same_phase = _answering_rows(1)[0]
        next_phase = _answering_rows(2)[0]
    dot = re.compile(r"\x1b\[38;5;\d+m•\x1b\[0m")
    assert dot.search(first).group() == dot.search(same_phase).group()
    assert dot.search(first).group() != dot.search(next_phase).group()


def test_reasoning_support_detection_and_openrouter_request_payload():
    import json

    from sophon.agent import _model_supports_reasoning, _request

    assert _model_supports_reasoning({"supported_parameters": ["tools", "reasoning"]})
    assert _model_supports_reasoning({"capabilities": {"reasoning": True}})
    assert not _model_supports_reasoning({"supported_parameters": ["tools"]})

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"answer","reasoning":"think carefully"}}]}'

    captured = {}

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    config = {
        "provider": "openrouter",
        "model": "reasoning-model",
        "api_key": "test-key",
        "reasoning": {"is_support": True, "enabled": True, "effort": "high"},
    }
    reasoning_chunks = []
    with patch("sophon.agent.urlopen", side_effect=fake_urlopen):
        answer = _request(
            config,
            [{"role": "user", "content": "why?"}],
            on_reasoning=reasoning_chunks.append,
        )
    assert answer == "answer"
    assert captured["payload"]["reasoning"] == {"effort": "high"}
    assert captured["payload"]["include_reasoning"] is True
    assert reasoning_chunks == ["think carefully"]


def test_reasoning_display_is_muted_and_precedes_the_agent_answer():
    from sophon.agent import _message_rows

    class TerminalBuffer(StringIO):
        def isatty(self):
            return True

    with patch("sys.stdout", TerminalBuffer()):
        rows = _message_rows(
            [{"role": "assistant", "content": "Final answer", "reasoning": "Reasoning details", "reasoning_expanded": True}],
            80,
        )
    rendered = "\n".join(rows)
    assert rendered.index("Reasoning details") < rendered.index("Final answer")
    assert "\033[38;5;245m" in rendered


def test_reasoning_commands_persist_support_enabled_and_effort(tmp_path):
    import json

    from sophon import agent

    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
        initial = {
            "schema_version": 1,
            "provider": "openrouter",
            "model": "reasoning-model",
            "api_key": "test-key",
            "agent_instructions_path": str(tmp_path / "sophon" / "agent" / "AGENT.md"),
            "auto_update": False,
            "reasoning": {"is_support": False, "enabled": False, "effort": "medium"},
            "context_window": 1024,
        }
        write_config(initial)
        commands = iter(["/reasoning on", "/reasoning-effort high", "/reasoning off", "/exit"])
        with (
            patch("sophon.agent._load_sessions", return_value=[]),
            patch("sophon.agent._input_bar", side_effect=lambda *args, **kwargs: next(commands)),
            patch("sophon.agent._detect_reasoning_support", return_value=True),
        ):
            assert agent._interactive(initial.copy()) == 0
        saved = json.loads(config_path().read_text(encoding="utf-8"))
    assert saved["reasoning"] == {"is_support": True, "enabled": False, "effort": "high"}


def test_cwd_is_the_only_directory_command_and_is_agent_only(tmp_path, monkeypatch):
    from sophon import agent

    project = tmp_path / "workspace" / "project"
    project.mkdir(parents=True)
    monkeypatch.chdir(project)
    monkeypatch.setattr(agent, "WORKSPACE_ROOT", project.parent)
    monkeypatch.setattr(agent, "PROJECT_DIR", project)
    cwd_title, cwd_path, cwd_error = agent._workspace_location("cwd")
    pwd_title, pwd_path, pwd_error = agent._workspace_location("pwd")
    assert (cwd_title, cwd_path, cwd_error) == ("Current directory", str(project), False)
    assert pwd_title == "Unknown Agent command"
    assert "Use /cwd" in pwd_path
    assert pwd_error is True


def test_agent_argument_forms_support_custom_prompt_message_and_task():
    from sophon.agent import _parse_agent_args

    assert _parse_agent_args(["-p", "Be a reviewer", "-m", "Review this"]) == (
        "Review this", "Be a reviewer", None
    )
    assert _parse_agent_args(["-m", "Hello"]) == ("Hello", None, None)
    assert _parse_agent_args(["-t", "Fix the bug"]) == ("Fix the bug", None, None)
    assert _parse_agent_args(["-p", "Legacy prompt"]) == ("Legacy prompt", None, None)
    assert _parse_agent_args(["-p", "system", "-t", "task"])[2] is not None


def test_custom_system_prompt_is_included_in_the_provider_system_message():
    from sophon.agent import _complete_turn

    captured = {}

    def fake_request(config, messages, **kwargs):
        captured["messages"] = messages
        return "Done"

    with patch("sophon.agent._request", side_effect=fake_request):
        answer = _complete_turn(
            {"provider": "openrouter", "model": "test", "api_key": "key"},
            [{"role": "user", "content": "Do the task"}],
            custom_system_prompt="Act as a careful reviewer.",
        )
    assert answer == "Done"
    assert "User-provided system prompt:\nAct as a careful reviewer." in captured["messages"][0]["content"]


def test_auto_compression_defaults_on_and_validates_persisted_threshold(tmp_path):
    from sophon.core import auto_compression_settings

    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
        assert auto_compression_settings() == (True, 80)
        write_config({
            "schema_version": 1,
            "auto_compression": False,
            "auto_compression_threshold": 65,
        })
        assert auto_compression_settings() == (False, 65)
        from sophon.config import read_config

        assert read_config() is None


def test_config_migrates_auto_compression_defaults_without_showing_them(tmp_path):
    import json

    from sophon.config import read_config

    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
        write_config({
            "schema_version": 1,
            "provider": "openrouter",
            "model": "test-model",
            "api_key": "secret-key",
        })
        loaded = read_config()
        persisted = json.loads(config_path().read_text(encoding="utf-8"))
    assert loaded["auto_compression"] is True
    assert loaded["auto_compression_threshold"] == 80
    assert persisted["auto_compression"] is True
    assert persisted["auto_compression_threshold"] == 80


def test_auto_compression_commands_persist_toggle_and_threshold(tmp_path):
    import json

    from sophon import agent

    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
        initial = {
            "schema_version": 1,
            "provider": "openrouter",
            "model": "test-model",
            "api_key": "test-key",
            "auto_update": False,
            "auto_compression": True,
            "auto_compression_threshold": 80,
            "context_window": 1024,
        }
        write_config(initial.copy())
        commands = iter(["/auto-compression off", "/threshold 65", "/exit"])
        with (
            patch("sophon.agent._load_sessions", return_value=[]),
            patch("sophon.agent._input_bar", side_effect=lambda *args, **kwargs: next(commands)),
        ):
            assert agent._interactive(initial.copy()) == 0
        saved = json.loads(config_path().read_text(encoding="utf-8"))
    assert saved["auto_compression"] is False
    assert saved["auto_compression_threshold"] == 65


def test_compact_history_summarizes_old_turns_and_keeps_recent_messages():
    from sophon.agent import _compact_history

    messages = [
        {"role": "user", "content": f"Question {index}"}
        if index % 2 == 0 else {"role": "assistant", "content": f"Answer {index}"}
        for index in range(8)
    ]
    captured = {}

    def fake_request(config, request_messages, **kwargs):
        captured["config"] = config
        captured["messages"] = request_messages
        captured["kwargs"] = kwargs
        return "Goal and next steps summary."

    config = {
        "provider": "openrouter",
        "model": "test-model",
        "api_key": "key",
        "reasoning": {"is_support": True, "enabled": True, "effort": "high"},
    }
    with patch("sophon.agent._request", side_effect=fake_request):
        compacted, summary = _compact_history(config, messages, 8000)
    assert summary == "Goal and next steps summary."
    assert compacted[0]["summary"] is True
    assert compacted[0]["content"].endswith(summary)
    assert compacted[1:] == messages[-6:]
    assert captured["config"]["reasoning"]["enabled"] is False
    assert captured["kwargs"]["tools_enabled"] is False


def test_delete_session_removes_and_persists_only_the_selected_session(tmp_path, monkeypatch):
    from sophon import agent

    session_path = tmp_path / "sessions" / "sessions.json"
    monkeypatch.setattr(agent, "SESSION_FILE", session_path)
    sessions = [
        {"id": "one", "title": "First", "messages": []},
        {"id": "two", "title": "Second", "messages": []},
    ]
    agent._write_sessions(sessions)
    assert agent._delete_session(sessions, "one") is True
    assert [item["id"] for item in sessions] == ["two"]
    assert [item["id"] for item in agent._load_sessions()] == ["two"]
    assert agent._delete_session(sessions, "missing") is False


def test_delete_command_requires_confirmation_and_removes_selected_session(tmp_path, monkeypatch):
    import json

    from sophon import agent

    session_path = tmp_path / "sessions" / "sessions.json"
    saved_session = {
        "id": "delete12345",
        "title": "Remove this",
        "created_at": "2026-10-31T10:00:00+03:00",
        "updated_at": "2026-10-31T10:00:00+03:00",
        "messages": [{"role": "user", "content": "history"}],
    }
    with (
        patch("sophon.agent.SESSION_FILE", session_path),
        patch("sophon.agent.LEGACY_SESSION_FILE", tmp_path / "legacy.json"),
    ):
        agent._write_sessions([saved_session])
        commands = iter(["/delete", "DELETE", "/exit"])
        with (
            patch("sophon.agent._input_bar", side_effect=lambda *args, **kwargs: next(commands)),
            patch("sophon.agent._pick_item", side_effect=lambda session, draw, items, **kwargs: items[0]),
        ):
            assert agent._interactive({"provider": "openrouter", "model": "test", "context_window": 1024}) == 0
        assert json.loads(session_path.read_text(encoding="utf-8")) == []


def test_manual_compact_requires_threshold_and_saves_summary(tmp_path):
    import json

    from sophon import agent

    session_path = tmp_path / "sessions" / "sessions.json"
    saved_session = {
        "id": "compact1234",
        "title": "Long context",
        "created_at": "2026-10-31T10:00:00+03:00",
        "updated_at": "2026-10-31T10:00:00+03:00",
        "messages": [
            {"role": "user", "content": "U" * 1800},
            {"role": "assistant", "content": "A" * 1800},
            {"role": "user", "content": "U" * 1800},
            {"role": "assistant", "content": "A" * 1800},
        ],
    }
    with (
        patch("sophon.agent.SESSION_FILE", session_path),
        patch("sophon.agent.LEGACY_SESSION_FILE", tmp_path / "legacy.json"),
    ):
        agent._write_sessions([saved_session])
        commands = iter(["/resume", "/compact", "/exit"])
        def compacted(config, messages, window, usage_out=None):
            return ([{"role": "assistant", "content": "Earlier conversation summary:\nSummary", "summary": True}, *messages[-2:]], "Summary")
        with (
            patch("sophon.agent._input_bar", side_effect=lambda *args, **kwargs: next(commands)),
            patch("sophon.agent._pick_item", side_effect=lambda session, draw, items, **kwargs: items[0]),
            patch("sophon.agent._compact_history", side_effect=compacted),
        ):
            assert agent._interactive({
                "provider": "openrouter",
                "model": "test",
                "context_window": 512,
                "auto_compression": False,
                "auto_compression_threshold": 80,
            }) == 0
        stored = json.loads(session_path.read_text(encoding="utf-8"))[0]
    assert stored["messages"][0]["summary"] is True
    assert len(stored["messages"]) == 3


def test_changelog_shows_release_and_last_updated_date():
    from sophon import agent

    commands = iter(["/changelog", "/help", "/exit"])
    captured = []

    def fake_draw(session, text, cursor, **kwargs):
        captured.extend(kwargs.get("notice_rows") or [])

    def fake_input(draw, *args, **kwargs):
        draw("", 0)
        return next(commands)

    with (
        patch("sophon.agent._draw_screen", side_effect=fake_draw),
        patch("sophon.agent._input_bar", side_effect=fake_input),
        patch("sophon.agent._load_sessions", return_value=[]),
    ):
        assert agent._interactive({"provider": "openrouter", "model": "test", "context_window": 1024}) == 0
    rendered = "\n".join(captured)
    assert "Sophon Changelog" in rendered
    assert "Last updated:" in rendered
    assert "/sessions" in rendered
    assert "/undo" in rendered and "/review" in rendered and "/usage" in rendered
    assert "/history" in rendered and "five levels deep" in rendered
    assert "double-Esc" in rendered
    assert "30–95%" in rendered
    assert "argument-aware command completion" in rendered


def test_config_reasoning_labels_only_show_effort_when_supported_and_on(tmp_path):
    from sophon.core import show_config

    base = {
        "schema_version": 1,
        "provider": "openrouter",
        "model": "test-model",
        "api_key": "secret",
        "auto_compression": True,
        "auto_compression_threshold": 80,
    }
    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
        output = StringIO()
        with patch("sys.stdout", output):
            write_config({**base, "reasoning": {"is_support": False, "enabled": False, "effort": "high"}})
            show_config()
        assert "Reasoning: not supported" in output.getvalue()
        assert "Reasoning effort:" not in output.getvalue()
        assert "Auto-compression" not in output.getvalue()
        config_text = re.sub(r"\x1b\[[0-9;]*m", "", output.getvalue())
        assert config_text.index("API key: hidden") < config_text.index("Environment:")
        output = StringIO()
        with patch("sys.stdout", output):
            write_config({**base, "reasoning": {"is_support": True, "enabled": False, "effort": "high"}})
            show_config()
        assert "Reasoning: off" in output.getvalue()
        assert "Reasoning effort:" not in output.getvalue()
        output = StringIO()
        with patch("sys.stdout", output):
            write_config({**base, "reasoning": {"is_support": True, "enabled": True, "effort": "high"}})
            show_config()
        assert "Reasoning: on" in output.getvalue()
        assert "Reasoning effort: high" in output.getvalue()


def test_doctor_shows_check_progress_and_extra_workspace_checks(tmp_path):
    from sophon import agent, core

    config_root = tmp_path / "config"
    config_root.mkdir()
    session_root = tmp_path / "sessions"
    workspace = tmp_path / "workspace"
    project = workspace / "my-project"
    project.mkdir(parents=True)
    session_file = session_root / "sessions.json"
    output = StringIO()
    with (
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_root)}),
        patch("sophon.core.read_config", return_value=None),
        patch("sophon.agent.SESSION_FILE", session_file),
        patch("sophon.agent.WORKSPACE_ROOT", workspace),
        patch("sophon.agent.PROJECT_DIR", project),
        patch("sophon.agent._load_sessions", return_value=[]),
        patch("sys.stdout", output),
    ):
        result = core.doctor()
    text = output.getvalue()
    assert result == 1
    assert re.search(r"\d+ of \d+", text)
    assert "Python executable" in text
    assert "Workspace storage" in text
    assert "Project workspace exists" in text
    assert "Agent instructions file is missing" in text


def test_termux_home_paths_are_displayed_as_tilde():
    from sophon.core import display_home_paths

    home = Path("/data/data/com.termux/files/home")
    with patch("sophon.core.Path.home", return_value=home):
        assert display_home_paths("/data/data/com.termux/files/home/project/file.py") == "~/project/file.py"
        assert display_home_paths("Located at /data/data/com.termux/files/home") == "Located at ~"


def test_auto_compression_runs_before_sending_when_threshold_is_reached(tmp_path):
    import json

    from sophon import agent

    session_path = tmp_path / "sessions" / "sessions.json"
    saved_session = {
        "id": "auto123456",
        "title": "Large context",
        "created_at": "2026-10-31T10:00:00+03:00",
        "updated_at": "2026-10-31T10:00:00+03:00",
        "messages": [
            {"role": "user", "content": "U" * 30000},
            {"role": "assistant", "content": "A" * 30000},
            {"role": "user", "content": "U" * 30000},
            {"role": "assistant", "content": "A" * 30000},
        ],
    }
    captured = {}
    with (
        patch("sophon.agent.SESSION_FILE", session_path),
        patch("sophon.agent.LEGACY_SESSION_FILE", tmp_path / "legacy.json"),
    ):
        agent._write_sessions([saved_session])
        commands = iter(["/resume", "Continue", "/exit"])

        def compacted(config, messages, window, usage_out=None):
            captured["compact_count"] = captured.get("compact_count", 0) + 1
            return (
                [{"role": "assistant", "content": "Earlier conversation summary:\nSummary", "summary": True}, *messages[-2:]],
                "Summary",
            )

        def complete(config, messages, **kwargs):
            captured["request_messages"] = messages
            return "Done"

        with (
            patch("sophon.agent._input_bar", side_effect=lambda *args, **kwargs: next(commands)),
            patch("sophon.agent._pick_item", side_effect=lambda session, draw, items, **kwargs: items[0]),
            patch("sophon.agent._compact_history", side_effect=compacted),
            patch("sophon.agent._complete_turn", side_effect=complete),
            patch("sophon.agent._draw_screen"),
        ):
            assert agent._interactive({
                "provider": "openrouter",
                "model": "test",
                "context_window": 8192,
                "auto_compression": True,
                "auto_compression_threshold": 80,
            }) == 0
        stored = json.loads(session_path.read_text(encoding="utf-8"))[0]
    assert captured["compact_count"] >= 1
    assert captured["request_messages"][0]["summary"] is True
    assert any(message.get("content") == "Done" for message in stored["messages"])


def test_manual_compact_is_rejected_below_threshold():
    from sophon import agent

    saved_session = {
        "id": "small12345",
        "title": "Short context",
        "created_at": "2026-10-31T10:00:00+03:00",
        "updated_at": "2026-10-31T10:00:00+03:00",
        "messages": [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
        ],
    }
    commands = iter(["/resume", "/compact", "/exit"])
    captured = []

    def fake_draw(session, text, cursor, **kwargs):
        captured.extend(kwargs.get("notice_rows") or [])

    def fake_input(draw, *args, **kwargs):
        draw("", 0)
        return next(commands)

    with (
        patch("sophon.agent._load_sessions", return_value=[saved_session]),
        patch("sophon.agent._pick_item", side_effect=lambda session, draw, items, **kwargs: items[0]),
        patch("sophon.agent._compact_history", side_effect=AssertionError("must not compact")),
        patch("sophon.agent._input_bar", side_effect=fake_input),
        patch("sophon.agent._draw_screen", side_effect=fake_draw),
    ):
        assert agent._interactive({
            "provider": "openrouter",
            "model": "test",
            "context_window": 8192,
            "auto_compression": False,
            "auto_compression_threshold": 80,
        }) == 0
    assert "Context is below the limit" in "\n".join(captured)


def test_environment_detection_and_config_persistence(tmp_path):
    import json

    from sophon.config import detect_environment, ensure_environment_config

    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path), "TERMUX_VERSION": "0.118"}):
        assert detect_environment() == "Termux (Android)"
        assert ensure_environment_config() == "Termux (Android)"
        saved = json.loads(config_path().read_text(encoding="utf-8"))
    assert saved["Environment"] == "Termux (Android)"


def test_plan_mode_is_read_only_and_prompts_for_explicit_build_approval():
    from sophon.agent import _complete_turn, _tools_for_mode

    names = {tool["function"]["name"] for tool in _tools_for_mode("plan")}
    assert {"list_files", "read_file"} <= names
    assert "write_file" not in names
    assert "update" not in names
    assert "session_rename" not in names
    captured = {}

    def fake_request(config, messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return "Here is the plan."

    with patch("sophon.agent._request", side_effect=fake_request):
        answer = _complete_turn(
            {"provider": "openrouter", "model": "test", "api_key": "key"},
            [{"role": "user", "content": "Plan this"}],
            mode="plan",
        )
    assert answer == "Here is the plan."
    assert "read-only" in captured["messages"][0]["content"]
    assert captured["kwargs"]["mode"] == "plan"


def test_new_and_forked_agent_sessions_default_to_chat_mode():
    from sophon.agent import _fork_session, _new_session

    assert _new_session()["mode"] == "chat"
    forked = _fork_session({"id": "session123", "title": "Plan", "mode": "build", "messages": []})
    assert forked["mode"] == "chat"


def test_footer_reasoning_mode_and_context_share_one_line_below_model():
    from sophon.agent import _footer_rows

    rendered = "\n".join(_footer_rows("test/model", [], 4096, 100, mode="build", reasoning="high"))
    plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
    assert plain.index("Model:") < plain.index("Reasoning: high") < plain.index("Mode: build")
    status_line = next(line for line in plain.splitlines() if "Reasoning:" in line)
    assert "Mode:" in status_line and "Context:" in status_line


def test_reasoning_transcript_uses_a_vertical_connector_to_the_answer():
    from sophon.agent import _message_rows

    rendered = "\n".join(_message_rows([
        {"role": "assistant", "content": "The answer", "reasoning": "First inspect the request.", "reasoning_expanded": True},
    ], 80))
    plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
    assert plain.index("Reasoning") < plain.index("│") < plain.index("The answer")


def test_reasoning_transcript_is_collapsed_by_default():
    from sophon.agent import _message_rows

    rendered = "\n".join(_message_rows([
        {"role": "assistant", "content": "The answer", "reasoning": "Private thought details."},
    ], 80))
    plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
    assert "Reasoning" in plain
    assert "Reasoning >" not in plain and "Reasoning <" not in plain
    assert "Private thought details." not in plain
    assert "The answer" in plain


def test_slash_completion_is_a_suffix_and_only_completes_at_input_end():
    from sophon.agent import (
        _accept_slash_completion,
        _slash_command_completion,
        _slash_completion_choices,
    )

    assert _slash_command_completion("/sess", 5) == "ions"
    assert _slash_command_completion("/sess", 3) == ""
    assert _slash_command_completion("/sessions ", 10) == ""
    assert _slash_completion_choices("/mo", 3) == ["de ", "del "]
    assert _slash_command_completion("/", 1) == "auto-compression "
    assert _slash_completion_choices("/mode ", 6) == ["build", "chat", "plan"]
    assert _slash_command_completion("/reasoning o", 12, 1) == "n"
    assert _slash_command_completion("/model", 6) == " "
    assert _slash_command_completion("/model ", 7) == ""
    completed, cursor, did_complete = _accept_slash_completion("/mode ", 6)
    assert (completed, cursor, did_complete) == ("/mode build", 11, True)
    submitted, cursor, did_complete = _accept_slash_completion(completed, cursor)
    assert (submitted, cursor, did_complete) == ("/mode build", 11, False)


def test_undo_restores_agent_file_changes_and_refuses_external_overwrites(tmp_path):
    from sophon.agent import _execute_workspace_tool, _restore_workspace_changes

    project = tmp_path / "project"
    project.mkdir()
    target = project / "notes.txt"
    target.write_text("before\n", encoding="utf-8")
    changes = []
    with patch("sophon.agent.PROJECT_DIR", project):
        _execute_workspace_tool("write_file", {"path": "notes.txt", "content": "after\n"}, change_log=changes)
        assert target.read_text(encoding="utf-8") == "after\n"
        assert "1 file" in _restore_workspace_changes(changes)
        assert target.read_text(encoding="utf-8") == "before\n"

        changes = []
        _execute_workspace_tool("write_file", {"path": "notes.txt", "content": "agent edit\n"}, change_log=changes)
        target.write_text("newer manual work\n", encoding="utf-8")
        try:
            _restore_workspace_changes(changes)
        except RuntimeError as error:
            assert "changed after the Agent edit" in str(error)
        else:
            raise AssertionError("Undo must not overwrite changes made after the Agent edit")
        assert target.read_text(encoding="utf-8") == "newer manual work\n"


def test_review_sends_only_a_bounded_diff_without_tools():
    from sophon.agent import _review_changes

    changes = [{
        "path": "app.py",
        "before_exists": True,
        "before": b"def value():\n    return 1\n",
        "after": b"def value():\n    return 2\n",
    }]
    with patch("sophon.agent._request", return_value="No actionable issue found.") as request:
        result = _review_changes({"provider": "openrouter", "model": "reviewer"}, "change value", changes)
    assert result == "No actionable issue found."
    assert request.call_args.kwargs["tools_enabled"] is False
    review_prompt = request.call_args.args[1][1]["content"]
    assert "Original task:\nchange value" in review_prompt
    assert "-    return 1" in review_prompt and "+    return 2" in review_prompt


def test_usage_panel_reports_session_totals_and_openrouter_remaining_limits():
    import json
    from io import BytesIO
    from sophon.agent import _provider_limit_rows, _usage_panel_rows

    payload = {"data": {
        "limit": 20,
        "limit_remaining": 12.5,
        "effective_budget": {"remaining_usd": 4.25, "resets_at": "2026-11-02T00:00:00Z"},
        "free_model_daily_requests": {"remaining": 8, "limit": 50},
    }}
    def fake_urlopen(request, timeout):
        assert request.full_url == "https://openrouter.ai/api/v1/key"
        assert request.get_header("Authorization") == "Bearer key-value"
        return BytesIO(json.dumps(payload).encode("utf-8"))

    session = {"usage": {"requests": 3, "prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42, "estimated": False}}
    with patch("sophon.agent.urlopen", side_effect=fake_urlopen):
        rows = _usage_panel_rows(session, {"provider": "openrouter", "api_key": "Bearer key-value"})
    joined = "\n".join(rows)
    assert "3" in rows[0] and "42 total" in rows[1]
    assert "$12.50" in joined and "$4.25" in joined and "8 of 50" in joined
    cloudflare_rows = _provider_limit_rows({"provider": "cloudflare"})
    assert any("10,000 free Neurons/day" in row for row in cloudflare_rows)
    assert any("not available from the inference token" in row for row in cloudflare_rows)


def test_agent_undo_and_review_commands_follow_the_last_workspace_edit(tmp_path):
    import json
    from sophon import agent

    project = tmp_path / "project"
    project.mkdir()
    sessions_file = tmp_path / "sessions" / "sessions.json"
    commands = iter(["Create a file", "/review", "/undo", "/exit"])
    review_calls = []

    def complete(config, messages, **kwargs):
        agent._execute_workspace_tool(
            "write_file",
            {"path": "created.txt", "content": "hello\n"},
            change_log=kwargs["change_log"],
        )
        kwargs["activity_out"].append({
            "timestamp": "2026-11-01T12:00:00+03:00",
            "tool": "write_file",
            "arguments": {"path": "created.txt", "content": "[omitted]"},
            "status": "ok",
            "summary": "Wrote created.txt.",
            "duration_ms": 1,
            "changes": [{"path": "created.txt", "operation": "created", "added_lines": 1}],
        })
        return "Created the file."

    def fake_review(config, task, changes, usage_out):
        review_calls.append((task, changes))
        usage_out["requests"] = 1
        return "No actionable issue found."

    def fake_input(draw, *args, **kwargs):
        draw("", 0)
        return next(commands)

    with (
        patch("sophon.agent._load_sessions", return_value=[]),
        patch("sophon.agent.SESSION_FILE", sessions_file),
        patch("sophon.agent.PROJECT_DIR", project),
        patch("sophon.agent._input_bar", side_effect=fake_input),
        patch("sophon.agent._complete_turn", side_effect=complete),
        patch("sophon.agent._review_changes", side_effect=fake_review),
        patch("sophon.agent._draw_screen"),
    ):
        assert agent._interactive({
            "provider": "openrouter",
            "model": "test",
            "context_window": 4096,
            "auto_compression": False,
        }) == 0

    assert len(review_calls) == 1
    assert review_calls[0][0] == "Create a file"
    assert review_calls[0][1][0]["path"] == "created.txt"
    assert not (project / "created.txt").exists()
    stored = json.loads(sessions_file.read_text(encoding="utf-8"))[0]
    assert stored["activity_log"][0]["tool"] == "write_file"
    assert stored["activity_log"][0]["summary"] == "Wrote created.txt."


def test_sessions_panel_sorts_by_title_and_shows_context_and_date():
    from sophon.agent import _sessions_panel_rows

    sessions = [
        {"title": "Zeta", "updated_at": "2026-11-01T10:00:00+03:00", "context_window": 1000,
         "messages": [{"role": "user", "content": "hello"}]},
        {"title": "Alpha", "updated_at": "2026-10-30T10:00:00+03:00", "context_window": 1000,
         "messages": [{"role": "user", "content": "hi"}]},
    ]
    rows = _sessions_panel_rows(sessions, None)
    assert rows[0].startswith("Alpha")
    assert rows[1].startswith("Zeta")
    assert all("Context" in row and "2026-" in row for row in rows)


def test_auto_compression_threshold_accepts_30_and_rejects_out_of_range(tmp_path):
    from sophon.core import auto_compression_settings

    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
        write_config({"schema_version": 1, "auto_compression": True, "auto_compression_threshold": 30})
        assert auto_compression_settings() == (True, 30)
        try:
            write_config({"schema_version": 1, "auto_compression_threshold": 29})
        except ValueError as error:
            assert "30 and 95" in str(error)
        else:
            raise AssertionError("Threshold below 30 must be rejected")


def test_clear_keeps_the_current_session_and_hides_old_transcript():
    from sophon import agent

    saved = {
        "id": "keep123456", "title": "Keep me", "created_at": "2026-11-01T10:00:00+03:00",
        "updated_at": "2026-11-01T10:00:00+03:00", "messages": [
            {"role": "user", "content": "Earlier question"},
            {"role": "assistant", "content": "Earlier answer"},
        ],
    }
    commands = iter(["/resume", "/clear", "/exit"])
    draws = []

    def fake_draw(session, text, cursor, **kwargs):
        draws.append((session["id"], kwargs.get("display_message_start", 0)))

    def fake_input(draw, *args, **kwargs):
        draw("", 0)
        return next(commands)

    with (
        patch("sophon.agent._load_sessions", return_value=[saved]),
        patch("sophon.agent._pick_item", side_effect=lambda session, draw, items, **kwargs: items[0]),
        patch("sophon.agent._input_bar", side_effect=fake_input),
        patch("sophon.agent._draw_screen", side_effect=fake_draw),
    ):
        assert agent._interactive({"provider": "openrouter", "model": "test", "context_window": 1024}) == 0
    assert any(session_id == "keep123456" and offset == 2 for session_id, offset in draws)
    assert saved["id"] == "keep123456"


def test_double_escape_cancels_active_response_and_shows_hint(tmp_path, monkeypatch):
    import time

    from sophon import agent

    class FakeTTY:
        def isatty(self):
            return True

    class CapturedTTY(StringIO):
        def isatty(self):
            return True

    commands = iter(["hello", "/exit"])
    captured = CapturedTTY()

    def fake_complete(config, messages, **kwargs):
        cancellation = kwargs["cancellation"]
        while not cancellation.cancelled:
            time.sleep(0.005)
        raise agent.AgentInterrupted("Response interrupted by user.")

    def fake_input(draw, *args, **kwargs):
        draw("", 0)
        return next(commands)

    def fake_key_reader(events, stop_event):
        events.put("CANCEL")
        events.put("CANCEL")

    with (
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}),
        patch("sophon.agent.SESSION_FILE", tmp_path / "sessions.json"),
        patch("sophon.agent.LEGACY_SESSION_FILE", tmp_path / "legacy.json"),
        patch("sophon.agent._load_sessions", return_value=[]),
        patch("sophon.agent._input_bar", side_effect=fake_input),
        patch("sophon.agent._complete_turn", side_effect=fake_complete),
        patch("sophon.agent._input_key_reader", side_effect=fake_key_reader),
        patch("sys.stdin", FakeTTY()),
        patch("sys.stdout", captured),
    ):
        assert agent._interactive({"provider": "openrouter", "model": "test", "context_window": 1024}) == 0
    assert "İptal etmek için bir kez daha basın" in captured.getvalue()
    assert "Response interrupted." in captured.getvalue()
    stored = (tmp_path / "sessions.json").read_text(encoding="utf-8")
    assert '"cancelled": true' in stored


def test_plan_mode_blocks_unexpected_mutating_tool_calls():
    from sophon.agent import _complete_turn

    captured = {"calls": 0}

    def fake_request(config, messages, *, tool_calls_out, **kwargs):
        captured["calls"] += 1
        if captured["calls"] == 1:
            tool_calls_out.append({
                "id": "call-write",
                "type": "function",
                "function": {"name": "write_file", "arguments": '{"path":"blocked.txt","content":"no"}'},
            })
            return ""
        captured["messages"] = messages
        return "Plan only."

    with patch("sophon.agent._request", side_effect=fake_request), patch(
        "sophon.agent._execute_workspace_tool", side_effect=AssertionError("plan mode must not write")
    ):
        answer = _complete_turn(
            {"provider": "openrouter", "model": "test", "api_key": "key"},
            [{"role": "user", "content": "Plan a change"}],
            mode="plan",
        )
    assert answer == "Plan only."
    assert "disabled in plan mode" in captured["messages"][-1]["content"]


def test_agent_cd_browses_current_directory_and_persists_safe_root(tmp_path):
    from sophon import agent

    workspace = tmp_path / "workspace"
    current = workspace / "my-project"
    browser_root = tmp_path / "checkout"
    target = browser_root / "src" / "CaseSensitiveProject"
    outside = tmp_path / "outside"
    current.mkdir(parents=True)
    target.mkdir(parents=True)
    browser_root.mkdir(exist_ok=True)
    outside.mkdir()
    sessions_file = tmp_path / "sessions" / "sessions.json"
    commands = iter([f"/cd {target}", "/rename switched", f"/cd {outside}", "/new", "hello", "/exit"])

    with (
        patch("sophon.agent.WORKSPACE_ROOT", workspace),
        patch("sophon.agent.PROJECT_DIR", current),
        patch("sophon.agent.Path.cwd", return_value=browser_root),
        patch("sophon.agent.SESSION_FILE", sessions_file),
        patch("sophon.agent.LEGACY_SESSION_FILE", tmp_path / "legacy.json"),
        patch("sophon.agent._input_bar", side_effect=lambda *args, **kwargs: next(commands)),
        patch("sophon.agent._complete_turn", return_value="Done."),
        patch("sophon.agent._draw_screen"),
    ):
        assert agent._interactive({"provider": "openrouter", "model": "test", "context_window": 1024}) == 0
        saved = agent._load_sessions()
        assert agent.PROJECT_DIR == target

    created = next(item for item in saved if item["title"] == "hello")
    assert created["workspace_dir"] == str(target.resolve())
    assert created["workspace_root"] == str(browser_root.resolve())
    assert created["messages"][-1]["content"] == "Done."


def test_sessions_picker_uses_fork_style_and_opens_selected_workspace(tmp_path):
    from sophon import agent

    workspace = tmp_path / "workspace"
    current = workspace / "current"
    selected_directory = workspace / "selected"
    current.mkdir(parents=True)
    selected_directory.mkdir()
    sessions_file = tmp_path / "sessions" / "sessions.json"
    saved_session = {
        "id": "saved123456",
        "title": "Alpha project",
        "created_at": "2026-10-30T10:00:00+03:00",
        "updated_at": "2026-10-31T10:00:00+03:00",
        "messages": [{"role": "user", "content": "continue"}],
        "context_window": 4096,
        "workspace_dir": str(selected_directory),
    }
    prompts = iter(["/sessions", "/exit"])
    picked = {}

    def choose(*args, **kwargs):
        picked["title"] = kwargs["title"]
        picked["items"] = args[2]
        return {"session": saved_session}

    def read_prompt(*args, **kwargs):
        prompt = next(prompts)
        if prompt == "/exit":
            picked["directory"] = agent.PROJECT_DIR
        return prompt

    with (
        patch("sophon.agent.WORKSPACE_ROOT", workspace),
        patch("sophon.agent.PROJECT_DIR", current),
        patch("sophon.agent.SESSION_FILE", sessions_file),
        patch("sophon.agent.LEGACY_SESSION_FILE", tmp_path / "legacy.json"),
        patch("sophon.agent._input_bar", side_effect=read_prompt),
        patch("sophon.agent._pick_item", side_effect=choose),
        patch("sophon.agent._draw_screen"),
    ):
        agent._write_sessions([saved_session.copy()])
        assert agent._interactive({"provider": "openrouter", "model": "test", "context_window": 4096}) == 0

    assert picked["title"] == "Saved Sessions"
    assert "Alpha project" in picked["items"][0]["label"]
    assert "Context" in picked["items"][0]["label"]
    assert "2026-10-31" in picked["items"][0]["label"]
    assert picked["directory"] == selected_directory


def test_idle_agent_escape_returns_to_sophon_tui():
    from sophon import agent

    with (
        patch("sophon.agent._load_sessions", return_value=[]),
        patch("sophon.agent._input_bar", side_effect=agent.AgentExitToTUI),
        patch("sophon.agent._draw_screen"),
    ):
        assert agent._interactive({"provider": "openrouter", "model": "test", "context_window": 1024}) == agent.EXIT_TO_TUI


def test_interactive_agent_command_returns_to_welcome_after_agent_exit():
    from sophon import core
    from sophon.agent import EXIT_TO_TUI

    with (
        patch("sophon.core.clear_terminal"),
        patch("sophon.core.show_welcome") as welcome,
        patch("sophon.core.auto_update_enabled", return_value=False),
        patch("sophon.core.read_prompt", side_effect=["/agent", "/exit"]),
        patch("sophon.agent.main", return_value=EXIT_TO_TUI) as agent_main,
        patch("sophon.core.card"),
        patch.object(core, "RESTART_REQUESTED", False),
    ):
        assert core.run_session() == 0
    agent_main.assert_called_once_with([])
    assert welcome.call_count == 2


def test_agent_escape_from_cli_enters_interactive_tui():
    from sophon import core
    from sophon.agent import EXIT_TO_TUI

    with (
        patch("sophon.core.ensure_environment_config"),
        patch("sophon.core.sys.argv", ["sophon", "agent"]),
        patch("sophon.agent.main", return_value=EXIT_TO_TUI),
        patch("sophon.core.run_session", return_value=0) as run_session,
        patch.object(core, "RESTART_REQUESTED", False),
    ):
        assert core.main() == 0
    run_session.assert_called_once_with()


def test_system_status_is_detailed_and_does_not_show_provider_secrets(tmp_path):
    from sophon import agent, core

    workspace = tmp_path / "workspace"
    project = workspace / "project"
    project.mkdir(parents=True)
    identity = tmp_path / "AGENT.md"
    identity.write_text("# Instructions\n", encoding="utf-8")
    session_file = tmp_path / "sessions.json"
    legacy_session_file = tmp_path / "legacy-sessions.json"
    output = StringIO()
    config = {
        "schema_version": 1,
        "provider": "openrouter",
        "model": "test-model",
        "api_key": "must-not-appear",
        "agent_instructions_path": str(identity),
        "reasoning": {"is_support": True, "enabled": False, "effort": "medium"},
    }
    with (
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path / "config-home")}),
        patch("sophon.core.detect_environment", return_value="Test Linux"),
        patch("sophon.core.read_config", return_value=config),
        patch("sophon.agent.WORKSPACE_ROOT", workspace),
        patch("sophon.agent.PROJECT_DIR", project),
        patch("sophon.agent.SESSION_FILE", session_file),
        patch("sophon.agent.LEGACY_SESSION_FILE", legacy_session_file),
        patch("sys.stdout", output),
    ):
        assert core.system_status() == 0
    rendered = output.getvalue()
    assert "Sophon System Status" in rendered
    assert "Environment: Test Linux" in rendered
    assert "Active project:" in rendered
    assert "Home storage free:" in rendered
    assert "Utilities:" in rendered
    assert "must-not-appear" not in rendered
    assert "Sophon Doctor" not in rendered
    assert not session_file.exists() and not legacy_session_file.exists()


def test_identity_command_displays_and_saves_agent_instructions(tmp_path):
    from sophon import core
    from sophon.config import agent_instructions_path

    keys = iter(["DOWN", "CHAR:Please ", "SAVE", "CHAR:y"])
    cards = []
    instructions = "# Instructions\nBe concise.\n"

    with (
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}),
        patch("sophon.core.read_config", return_value=None),
        patch("sophon.core.clear_terminal"),
        patch("sophon.core.card", side_effect=lambda *args, **kwargs: cards.append((args, kwargs))),
        patch("sophon.agent._read_key", side_effect=lambda *args, **kwargs: next(keys)),
    ):
        identity_path = agent_instructions_path()
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        identity_path.write_text(instructions, encoding="utf-8")
        assert core.manage_identity() == 0
        path = agent_instructions_path()
    assert path.read_text(encoding="utf-8") == "# Instructions\nPlease Be concise.\n"
    rendered = "\n".join(str(call) for call in cards)
    assert "Agent Identity" in rendered
    assert "ctrl+x to save" in rendered
    assert "esc to cancel" in rendered
    assert "E — edit" not in rendered


def test_directory_tree_picker_is_current_directory_relative_and_depth_limited(tmp_path):
    from sophon.agent import _directory_tree_nodes, _pick_directory_tree

    root = tmp_path / "checkout"
    nested = root / "src" / "components" / "ui"
    nested.mkdir(parents=True)
    (nested / "Button.tsx").write_text("export {}\n", encoding="utf-8")
    deep = root
    for index in range(1, 7):
        deep = deep / f"level{index}"
        deep.mkdir()
    nodes = _directory_tree_nodes(root, max_depth=5)
    paths = {Path(str(node.get("path", ""))) for node in nodes if node.get("directory") is True}
    assert (root / "src").resolve() in paths
    assert (root / "src" / "components" / "ui").resolve() in paths
    assert (root / "level1" / "level2" / "level3" / "level4" / "level5").resolve() in paths
    assert (root / "level1" / "level2" / "level3" / "level4" / "level5" / "level6").resolve() not in paths
    assert any("Button.tsx" in str(node.get("tree")) for node in nodes)

    rendered = []
    with (
        patch("sophon.agent._read_key", side_effect=["DOWN", "ENTER"]),
        patch("sophon.agent.shutil.get_terminal_size", return_value=os.terminal_size((100, 30))),
    ):
        selected = _pick_directory_tree(
            {"workspace_root": str(root)},
            lambda *args, **kwargs: rendered.append(kwargs["panel_rows"]),
            root,
            tmp_path / "other-project",
        )
    assert selected == (root / "level1" / "level2").resolve()
    assert "[ENTER] select · [ARROW KEYS] navigate · [ESC] back" in "\n".join(rendered[-1])
    assert any("change directory" in row for row in rendered[-1])


def test_complete_turn_records_redacted_tool_activity_and_file_change_details(tmp_path):
    import json
    from sophon.agent import _complete_turn

    project = tmp_path / "project"
    project.mkdir()
    activity = []
    changes = []
    calls = 0

    def fake_request(config, messages, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            kwargs["tool_calls_out"].append({
                "id": "write-1",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "secret.txt", "content": "private\nvalue\n"}),
                },
            })
            return ""
        return "Done."

    with patch("sophon.agent.PROJECT_DIR", project), patch("sophon.agent._request", side_effect=fake_request):
        answer = _complete_turn(
            {"provider": "openrouter", "model": "test", "api_key": "key"},
            [{"role": "user", "content": "write a file"}],
            change_log=changes,
            activity_out=activity,
        )
    assert answer == "Done."
    assert len(activity) == 1
    event = activity[0]
    assert event["tool"] == "write_file" and event["status"] == "ok"
    assert "private" not in str(event["arguments"])
    assert event["arguments"]["content"].startswith("[omitted")
    assert event["changes"][0]["path"] == "secret.txt"
    assert event["changes"][0]["added_lines"] == 2
    assert "private" not in event["summary"]
    assert (project / "secret.txt").read_text(encoding="utf-8") == "private\nvalue\n"


def test_activity_history_redacts_credential_key_spellings():
    from sophon.agent import _history_safe_arguments

    safe = _history_safe_arguments({
        "apiKey": "camel-secret",
        "api-key": "dash-secret",
        "Authorization": "Bearer secret",
        "private_key": "private-secret",
        "ordinary": "visible",
    })
    assert safe == {
        "apiKey": "[redacted]",
        "api-key": "[redacted]",
        "Authorization": "[redacted]",
        "private_key": "[redacted]",
        "ordinary": "visible",
    }


def test_history_panel_shows_latest_tool_details_and_navigates_to_older_events():
    from sophon.agent import _history_panel

    session = {"activity_log": [
        {
            "timestamp": "2026-11-01T12:00:00+03:00",
            "tool": "read_file",
            "arguments": {"path": "src/app.py"},
            "status": "ok",
            "summary": "Read file · 40 bytes · 2 lines.",
            "duration_ms": 2,
            "changes": [],
        },
        {
            "timestamp": "2026-11-01T12:01:00+03:00",
            "tool": "write_file",
            "arguments": {"path": "src/app.py", "content": "[omitted · 3 bytes · 1 lines]"},
            "status": "ok",
            "summary": "Wrote src/app.py.",
            "duration_ms": 4,
            "changes": [{"path": "src/app.py", "operation": "updated", "added_lines": 1, "removed_lines": 1, "before_bytes": 40, "after_bytes": 3}],
        },
    ]}
    rendered = []
    with (
        patch("sophon.agent._read_key", side_effect=["UP", "CANCEL"]),
        patch("sophon.agent.shutil.get_terminal_size", return_value=os.terminal_size((100, 30))),
    ):
        _history_panel(session, lambda *args, **kwargs: rendered.append(kwargs["panel_rows"]))
    latest = "\n".join(rendered[0])
    earlier = "\n".join(rendered[1])
    assert "write_file" in latest and "src/app.py" in latest and "+1/-1 lines" in latest
    assert "read_file" in earlier and "40 bytes" in earlier
    assert "Press enter" not in latest


def test_config_backup_and_restore_require_confirmation_and_preserve_all_fields(tmp_path):
    import json
    from sophon import core

    backup_path = tmp_path / ".sophon" / "backup" / "config_backup.json"
    cards = []
    initial = {
        "schema_version": 1,
        "provider": "openrouter",
        "model": "saved-model",
        "api_key": "saved-secret",
        "auto_update": True,
        "custom_metadata": {"keep": "me"},
    }
    with (
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path / "xdg")} ),
        patch("sophon.core.CONFIG_BACKUP_PATH", backup_path),
        patch("sophon.core.card", side_effect=lambda *args, **kwargs: cards.append((args, kwargs))),
        patch("sophon.core.read_prompt", side_effect=["no", "RESTORE"]),
    ):
        write_config(initial)
        assert core.backup_config() == 0
        assert json.loads(backup_path.read_text(encoding="utf-8")) == initial
        assert backup_path.stat().st_mode & 0o777 == 0o600
        write_config({**initial, "model": "new-model", "api_key": "new-secret", "auto_update": False})
        assert core.restore_config() == 0
        assert json.loads(config_path().read_text(encoding="utf-8"))["model"] == "new-model"
        assert core.restore_config() == 0
        restored = json.loads(config_path().read_text(encoding="utf-8"))
    assert restored["model"] == "saved-model"
    assert restored["api_key"] == "saved-secret"
    assert restored["auto_update"] is True
    assert restored["custom_metadata"] == {"keep": "me"}
    assert "new-secret" not in "\n".join(str(card_call) for card_call in cards)


def test_reconfiguration_starts_at_provider_picker_without_existing_provider_prompt(tmp_path):
    from sophon.setup_wizard import run_setup_wizard

    existing = {"provider": "cloudflare", "model": "old-model", "api_key": "old-key"}
    selected = {}
    with (
        patch("sophon.setup_wizard.sys.stdin.isatty", return_value=True),
        patch("sophon.setup_wizard.sys.stdout.isatty", return_value=True),
        patch("sophon.setup_wizard.read_config", return_value=existing),
        patch("sophon.setup_wizard._choose_existing_action", side_effect=AssertionError("must be skipped")),
        patch("sophon.setup_wizard._choose_provider", side_effect=lambda *args, **kwargs: selected.update(args=args, kwargs=kwargs) or "cloudflare"),
        patch("sophon.setup_wizard._choose_model", return_value="new-model"),
        patch("sophon.setup_wizard._read_hidden_credential", return_value="new-key"),
        patch("sophon.agent._detect_reasoning_support", return_value=False),
        patch("sophon.setup_wizard.auto_compression_settings", return_value=(True, 80)),
        patch("sophon.setup_wizard.auto_update_enabled", return_value=False),
        patch("builtins.input", return_value="a" * 32),
        patch("sophon.setup_wizard.card"),
        patch("sophon.setup_wizard.clear_terminal"),
        patch("sophon.setup_wizard._read_key", return_value="ENTER"),
        patch("sophon.setup_wizard.ensure_agent_instructions_file"),
        patch("sophon.setup_wizard.agent_instructions_path", return_value=tmp_path / "AGENT.md"),
        patch("sophon.setup_wizard.write_config", return_value=tmp_path / "config.json"),
    ):
        assert run_setup_wizard(reconfigure=True) == 0
    assert selected["args"] == ("cloudflare",)
    assert selected["kwargs"]["title"] == "Reconfiguration"


def test_system_prompt_requests_plain_unfenced_workspace_paths():
    from sophon.agent import _system_message

    prompt = _system_message({})
    assert "plain text using `~`" in prompt
    assert "never wrap a directory path in a fenced code block" in prompt


def test_interactive_tui_routes_status_and_identity_commands():
    from sophon import core

    with (
        patch("sophon.core.clear_terminal"),
        patch("sophon.core.show_welcome"),
        patch("sophon.core.auto_update_enabled", return_value=False),
        patch("sophon.core.read_prompt", side_effect=["/status", "/identity", "/exit"]),
        patch("sophon.core.system_status", return_value=0) as status,
        patch("sophon.core.manage_identity", return_value=0) as identity,
        patch("sophon.core.card"),
        patch.object(core, "RESTART_REQUESTED", False),
    ):
        assert core.run_session() == 0
    status.assert_called_once_with()
    identity.assert_called_once_with()


def test_interactive_tui_routes_backup_restore_reconfig_and_documents_them():
    from sophon import core

    prompts = iter(["/backup", "/restore", "/reconfig", "/help", "/exit"])
    cards = []
    with (
        patch("sophon.core.clear_terminal"),
        patch("sophon.core.show_welcome"),
        patch("sophon.core.auto_update_enabled", return_value=False),
        patch("sophon.core.read_prompt", side_effect=lambda *args, **kwargs: next(prompts)),
        patch("sophon.core.backup_config", return_value=0) as backup,
        patch("sophon.core.restore_config", return_value=0) as restore,
        patch("sophon.setup_wizard.run_setup_wizard", return_value=0) as setup,
        patch("sophon.core.card", side_effect=lambda *args, **kwargs: cards.append((args, kwargs))),
        patch.object(core, "RESTART_REQUESTED", False),
    ):
        assert core.run_session() == 0
    backup.assert_called_once_with()
    restore.assert_called_once_with()
    setup.assert_called_once_with(reconfigure=True)
    help_rows = "\n".join(str(call) for call in cards)
    assert "/backup —" in help_rows and "/restore —" in help_rows and "/reconfig —" in help_rows


def test_ctrl_r_key_maps_to_reasoning_disclosure_toggle():
    from sophon.agent import _read_key_raw

    read_fd, write_fd = os.pipe()
    terminal_input = os.fdopen(read_fd, "r", encoding="utf-8", closefd=False)
    try:
        os.write(write_fd, b"\x12")
        with patch("sys.stdin", terminal_input):
            assert _read_key_raw() == "TOGGLE_REASONING"
    finally:
        terminal_input.close()
        os.close(read_fd)
        os.close(write_fd)



def test_sgr_mouse_click_is_recognized_only_on_reasoning_label():
    from sophon import agent

    read_fd, write_fd = os.pipe()
    terminal_input = os.fdopen(read_fd, "r", encoding="utf-8", closefd=False)
    try:
        os.write(write_fd, b"\x1b[<0;12;4M")
        with patch("sys.stdin", terminal_input):
            key = agent._read_key_raw()
        assert key == "MOUSE:0,12,4"
    finally:
        terminal_input.close()
        os.close(read_fd)
        os.close(write_fd)

    with patch.object(agent, "REASONING_HITBOX", (4, 8, 17)):
        assert agent._reasoning_mouse_click("MOUSE:0,12,4") is True
        assert agent._reasoning_mouse_click("MOUSE:0,20,4") is False
        assert agent._reasoning_mouse_click("MOUSE:1,12,4") is False


def test_identity_editor_keys_map_to_line_insert_delete_and_delete_key():
    from sophon.agent import _read_key_raw

    for payload, expected in [(b"\x0e", "INSERT_LINE"), (b"\x0b", "DELETE_LINE"), (b"\x1b[3~", "DELETE")]:
        read_fd, write_fd = os.pipe()
        terminal_input = os.fdopen(read_fd, "r", encoding="utf-8", closefd=False)
        try:
            os.write(write_fd, payload)
            with patch("sys.stdin", terminal_input):
                assert _read_key_raw() == expected
        finally:
            terminal_input.close()
            os.close(read_fd)
            os.close(write_fd)


def test_escape_from_nested_agent_prompt_returns_to_tui():
    from sophon import agent

    calls = 0

    def input_bar(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "/rename"
        raise agent.AgentExitToTUI

    with (
        patch("sophon.agent._load_sessions", return_value=[]),
        patch("sophon.agent._input_bar", side_effect=input_bar),
        patch("sophon.agent._draw_screen"),
    ):
        assert agent._interactive({"provider": "openrouter", "model": "test", "context_window": 1024}) == agent.EXIT_TO_TUI
    assert calls == 2


def test_config_persists_allowed_path_and_empty_mode_tool_lists(tmp_path):
    import json

    config = {
        "schema_version": 1,
        "provider": "openrouter",
        "model": "test-model",
        "api_key": "secret",
        "allowed_path": ["~/shared", "~/shared"],
        "mode": {"plan": [], "build": ["file_read", "file_edit"], "chat": []},
    }
    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
        write_config(config)
        persisted = json.loads(config_path().read_text(encoding="utf-8"))
    assert persisted["allowed_path"] == ["~/shared"]
    assert persisted["mode"] == {"plan": [], "build": ["file_read", "file_edit"], "chat": []}


def test_mode_tool_registry_includes_ask_planning_and_file_manage_but_respects_explicit_allowlist():
    from sophon.agent import FILE_MANAGE_TOOL_NAMES, _tools_for_mode

    plan_names = {tool["function"]["name"] for tool in _tools_for_mode("plan")}
    build_names = {tool["function"]["name"] for tool in _tools_for_mode("build")}
    restricted_names = {tool["function"]["name"] for tool in _tools_for_mode("build", {"mode": {"build": ["file_read"]}})}
    assert {"ask", "planning", "tree_view"} <= plan_names
    assert "file_edit" not in plan_names
    assert {"ask", "planning", "file_create", "file_edit", "extract"} <= build_names
    assert len(FILE_MANAGE_TOOL_NAMES) >= 15
    assert restricted_names == {"file_read"}


def test_file_manage_allowed_path_rejects_escape_and_symlink_and_supports_absolute_roots(tmp_path):
    from sophon.agent import _execute_workspace_tool

    project = tmp_path / "project"
    project.mkdir()
    allowed = tmp_path / "shared"
    allowed.mkdir()
    (allowed / "notes.txt").write_text("shared data", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (project / "link.txt").symlink_to(outside)
    with patch("sophon.agent.PROJECT_DIR", project):
        assert _execute_workspace_tool("file_read", {"path": str(allowed / "notes.txt")}, config={"allowed_path": [str(allowed)]}) == "shared data"
        for path in ("../secret.txt", "link.txt", str(outside)):
            try:
                _execute_workspace_tool("file_read", {"path": path})
            except RuntimeError as error:
                assert "File Manage paths" in str(error) or "Symbolic links" in str(error)
            else:
                raise AssertionError(f"Unsafe path unexpectedly allowed: {path}")


def test_file_manage_operations_are_bounded_safe_and_undoable(tmp_path):
    import zipfile
    from sophon.agent import _execute_workspace_tool, _restore_workspace_changes, _refresh_undo_digests

    project = tmp_path / "project"
    project.mkdir()
    changes = []
    with patch("sophon.agent.PROJECT_DIR", project):
        assert "Created" in _execute_workspace_tool("file_create", {"path": "docs/a.txt", "content": "alpha\n"}, change_log=changes)
        assert _execute_workspace_tool("file_read", {"path": "docs/a.txt"}) == "alpha\n"
        _execute_workspace_tool("file_edit", {"path": "docs/a.txt", "new_content": "beta\n"}, change_log=changes)
        _execute_workspace_tool("file_rename", {"path": "docs/a.txt", "new_name": "b.txt"}, change_log=changes)
        _execute_workspace_tool("file_copy", {"source_path": "docs/b.txt", "destination_path": "docs/c.txt"}, change_log=changes)
        _execute_workspace_tool("zip", {"path": "docs", "archive_path": "docs.zip"}, change_log=changes)
        _execute_workspace_tool("extract", {"path": "docs.zip", "destination_path": "unzipped"}, change_log=changes)
        assert "b.txt" in _execute_workspace_tool("tree_view", {"path": "."})
        _refresh_undo_digests(changes)
        summary = _restore_workspace_changes(changes)
        assert "Undid" in summary
        assert not (project / "docs").exists()
        assert not (project / "docs.zip").exists()
        assert not (project / "unzipped").exists()

        malicious = project / "unsafe.zip"
        with zipfile.ZipFile(malicious, "w") as archive:
            archive.writestr("../escape.txt", "not safe")
        try:
            _execute_workspace_tool("extract", {"path": "unsafe.zip", "destination_path": "bad"})
        except RuntimeError as error:
            assert "unsafe path" in str(error)
        else:
            raise AssertionError("ZIP traversal must be rejected")
        assert not (tmp_path / "escape.txt").exists()


def test_file_manage_copy_refuses_existing_destinations_and_folder_self_copy(tmp_path):
    from sophon.agent import _execute_workspace_tool

    project = tmp_path / "project"
    (project / "folder").mkdir(parents=True)
    (project / "folder" / "a.txt").write_text("data", encoding="utf-8")
    (project / "existing.txt").write_text("keep", encoding="utf-8")
    with patch("sophon.agent.PROJECT_DIR", project):
        for name, args in [
            ("file_copy", {"source_path": "folder/a.txt", "destination_path": "existing.txt"}),
            ("folder_copy", {"source_path": "folder", "destination_path": "folder/child"}),
        ]:
            try:
                _execute_workspace_tool(name, args)
            except RuntimeError as error:
                assert "destination" in str(error).lower() or "itself" in str(error).lower()
            else:
                raise AssertionError(f"Unsafe copy unexpectedly succeeded: {name}")
    assert (project / "existing.txt").read_text(encoding="utf-8") == "keep"


def test_planning_tool_updates_and_completes_todo_checklist(tmp_path):
    from sophon.agent import _execute_workspace_tool, _restore_workspace_changes

    project = tmp_path / "project"
    project.mkdir()
    changes = []
    with patch("sophon.agent.PROJECT_DIR", project):
        result = _execute_workspace_tool("planning", {
            "action": "create", "goal": "Ship support", "steps": ["Inspect code", "Add tests", "Verify"],
        }, change_log=changes)
        assert "3 tracked steps" in result
        _execute_workspace_tool("planning", {"action": "complete", "step": 1}, change_log=changes)
        content = (project / "TODO.md").read_text(encoding="utf-8")
        assert "- [x] 1. Inspect code" in content
        assert "- [ ] 2. Add tests" in content
        assert "TODO.md" in str(changes)
        _restore_workspace_changes(changes)
        assert not (project / "TODO.md").exists()


def test_question_card_renders_sequential_presets_skip_and_final_submit(tmp_path):
    import queue
    from sophon.agent import _questions_panel

    events = queue.Queue()
    for key in ("CHAR:a", "ENTER", "CHAR:s", "ENTER", "ENTER"):
        events.put(key)
    rendered = []
    with patch("sophon.agent.shutil.get_terminal_size", return_value=os.terminal_size((100, 32))):
        answer = _questions_panel({"questions": [
            {"question": "Which framework?", "options": ["Flask", "FastAPI"]},
            {"question": "Proceed now?", "options": ["Yes", "No"]},
        ]}, lambda text, cursor, **kwargs: rendered.append((text, kwargs.get("panel_rows"))), events, {"frame": 0})
    assert answer is not None
    assert '"answer": "Flask"' in answer
    assert '"skipped": true' in answer
    assert any("Questions" in "\n".join(rows or []) and "1 of 2" in "\n".join(rows or []) for _, rows in rendered)
    assert any("2 of 2" in "\n".join(rows or []) for _, rows in rendered)
    assert any("Press Enter to submit all answers" in "\n".join(rows or []) for _, rows in rendered)


def test_questions_validate_count_and_preset_limits():
    from sophon.agent import _validated_questions

    for payload in (
        {"questions": []},
        {"questions": [{"question": "Q", "options": ["a", "b", "c", "d"]}]},
        {"questions": [{"question": "Q"}] * 7},
    ):
        try:
            _validated_questions(payload)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Invalid question payload was accepted")


def test_doctor_reports_agent_tool_registry_and_provider_model_catalog_failure(tmp_path):
    from sophon import core

    config = {"provider": "openrouter", "model": "test/model", "api_key": "private-token"}
    output = StringIO()
    with (
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}),
        patch("sophon.core.read_config", return_value=config),
        patch("sophon.agent._load_sessions", return_value=[]),
        patch("sophon.agent._list_models", side_effect=RuntimeError("HTTP 401 private-token")),
        patch("sys.stdout", output),
    ):
        core.doctor()
    rendered = re.sub(r"\x1b\[[0-9;]*m", "", output.getvalue())
    assert "Agent tool registry" in rendered
    assert "Agent provider check failed" in rendered
    assert "private-token" not in rendered
    assert re.search(r"\d+ of \d+", rendered)


def test_footer_moves_context_to_second_line_when_status_is_too_wide():
    from sophon.agent import _footer_rows

    rendered = "\n".join(_footer_rows("a-very-long-model-name/with-a-long-provider-id", [], 4096, 42, mode="build", reasoning="high"))
    plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
    lines = plain.splitlines()
    status_index = next(index for index, line in enumerate(lines) if "Reasoning:" in line)
    context_index = next(index for index, line in enumerate(lines) if "Context:" in line)
    assert context_index == status_index + 1


def test_undo_delete_recreate_move_restores_original_path_and_removes_replacement(tmp_path):
    from sophon.agent import _execute_workspace_tool, _refresh_undo_digests, _restore_workspace_changes

    project = tmp_path / "project"
    project.mkdir()
    original = project / "a.txt"
    original.write_text("original content\n", encoding="utf-8")
    changes = []
    with patch("sophon.agent.PROJECT_DIR", project):
        _execute_workspace_tool("file_delete", {"path": "a.txt"}, change_log=changes)
        _execute_workspace_tool("file_create", {"path": "a.txt", "content": "replacement\n"}, change_log=changes)
        _execute_workspace_tool("file_move", {"source_path": "a.txt", "destination_path": "b.txt"}, change_log=changes)
        _refresh_undo_digests(changes)
        _restore_workspace_changes(changes)
    assert original.read_text(encoding="utf-8") == "original content\n"
    assert not (project / "b.txt").exists()


def test_complete_turn_enforces_nonempty_mode_allowlist_at_tool_dispatch(tmp_path):
    import json
    from sophon.agent import _complete_turn

    project = tmp_path / "project"
    project.mkdir()
    target = project / "notes.txt"
    target.write_text("original\n", encoding="utf-8")
    calls = 0
    captured = []

    def fake_request(config, messages, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            kwargs["tool_calls_out"].append({
                "id": "blocked-edit",
                "type": "function",
                "function": {"name": "file_edit", "arguments": json.dumps({"path": "notes.txt", "new_content": "changed\n"})},
            })
            return ""
        captured.extend(messages)
        return "I did not edit the file."

    with patch("sophon.agent.PROJECT_DIR", project), patch("sophon.agent._request", side_effect=fake_request):
        answer = _complete_turn(
            {"provider": "openrouter", "model": "test", "api_key": "key", "mode": {"build": ["file_read"]}},
            [{"role": "user", "content": "Change this file"}],
            mode="build",
        )
    assert answer == "I did not edit the file."
    assert target.read_text(encoding="utf-8") == "original\n"
    tool_result = next(message for message in captured if message.get("role") == "tool")
    assert "not enabled for build mode" in tool_result["content"]


def test_planning_animation_reports_failure_instead_of_success(tmp_path):
    import json
    from sophon.agent import _complete_turn

    project = tmp_path / "project"
    project.mkdir()
    calls = 0
    statuses = []

    def fake_request(config, messages, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            kwargs["tool_calls_out"].append({
                "id": "bad-plan",
                "type": "function",
                "function": {"name": "planning", "arguments": json.dumps({"action": "complete", "step": 999})},
            })
            return ""
        return "There is no such step."

    with patch("sophon.agent.PROJECT_DIR", project), patch("sophon.agent._request", side_effect=fake_request):
        _complete_turn(
            {"provider": "openrouter", "model": "test", "api_key": "key"},
            [{"role": "user", "content": "Complete step 999"}],
            mode="build",
            on_planning=statuses.append,
        )
    assert statuses == ["Updating TODO.md", "TODO.md update failed"]
    assert not (project / "TODO.md").exists()


def test_questions_allow_custom_answers_starting_with_preset_letter_and_double_escape_cancels_turn():
    import queue
    from sophon.agent import AgentInterrupted, RequestCancellation, _questions_panel

    custom_events = queue.Queue()
    for key in ("CHAR:A", "CHAR:p", "CHAR:p", "CHAR:l", "CHAR:e", "ENTER", "ENTER"):
        custom_events.put(key)
    with patch("sophon.agent.shutil.get_terminal_size", return_value=os.terminal_size((100, 32))):
        answer = _questions_panel(
            {"questions": [{"question": "What should it be called?", "options": ["Alpha", "Beta"]}]},
            lambda *args, **kwargs: None,
            custom_events,
            {"frame": 0},
        )
    assert '"answer": "Apple"' in answer

    cancel_events = queue.Queue()
    cancel_events.put("DOUBLE_CANCEL")
    cancellation = RequestCancellation()
    with patch("sophon.agent.shutil.get_terminal_size", return_value=os.terminal_size((100, 32))):
        try:
            _questions_panel(
                {"questions": [{"question": "Continue?", "options": ["Yes", "No"]}]},
                lambda *args, **kwargs: None,
                cancel_events,
                {"frame": 0},
                cancellation,
            )
        except AgentInterrupted:
            pass
        else:
            raise AssertionError("Double Escape must cancel the active request")
    assert cancellation.cancelled


def test_setup_preserves_providerless_access_settings_on_first_provider_setup(tmp_path):
    import json
    from sophon.setup_wizard import run_setup_wizard

    preferences = {
        "schema_version": 1,
        "auto_update": True,
        "allowed_path": ["~/shared"],
        "mode": {"plan": [], "build": ["file_read"], "chat": []},
    }
    preference_path = tmp_path / "config.json"
    preference_path.write_text(json.dumps(preferences), encoding="utf-8")
    saved = {}
    with (
        patch("sophon.setup_wizard.sys.stdin.isatty", return_value=True),
        patch("sophon.setup_wizard.sys.stdout.isatty", return_value=True),
        patch("sophon.setup_wizard.read_config", return_value=None),
        patch("sophon.setup_wizard.config_path", return_value=preference_path),
        patch("sophon.setup_wizard._choose_existing_action", side_effect=AssertionError("no provider is configured yet")),
        patch("sophon.setup_wizard._choose_provider", return_value="openrouter"),
        patch("sophon.setup_wizard._choose_model", return_value="test-model"),
        patch("sophon.setup_wizard._read_hidden_credential", return_value="new-key"),
        patch("sophon.agent._detect_reasoning_support", return_value=False),
        patch("sophon.setup_wizard.auto_compression_settings", return_value=(True, 80)),
        patch("sophon.setup_wizard.auto_update_enabled", return_value=True),
        patch("sophon.setup_wizard.card"),
        patch("sophon.setup_wizard.clear_terminal"),
        patch("sophon.setup_wizard._read_key", return_value="ENTER"),
        patch("sophon.setup_wizard.ensure_agent_instructions_file"),
        patch("sophon.setup_wizard.agent_instructions_path", return_value=tmp_path / "AGENT.md"),
        patch("sophon.setup_wizard.config_display_path", return_value="~/.sophon/config/config.json"),
        patch("sophon.setup_wizard.write_config", side_effect=lambda value: saved.update(value) or preference_path),
    ):
        assert run_setup_wizard() == 0
    assert saved["allowed_path"] == ["~/shared"]
    assert saved["mode"] == preferences["mode"]
    assert saved["auto_update"] is True


def test_first_interactive_start_runs_setup_before_opening_tui():
    from sophon import core

    with (
        patch("sophon.core.read_config", return_value=None),
        patch("sophon.core.card"),
        patch("sophon.setup_wizard.run_setup_wizard", return_value=0) as setup,
        patch("sophon.core._mark_interactive_initialized") as mark_initialized,
        patch("sophon.core.run_session", return_value=0) as run_session,
    ):
        assert core._start_interactive() == 0
    setup.assert_called_once_with()
    mark_initialized.assert_called_once_with()
    run_session.assert_called_once_with()


def test_existing_config_offers_reconfigure_once_and_persists_choice(tmp_path):
    from sophon import core

    marker = tmp_path / "state" / "interactive_initialized"
    with (
        patch("sophon.core.FIRST_INTERACTIVE_MARKER", marker),
        patch("sophon.core.read_config", return_value={"provider": "openrouter", "model": "test"}),
        patch("sophon.core.sys.stdin.isatty", return_value=True),
        patch("sophon.core.sys.stdout.isatty", return_value=True),
        patch("sophon.core.read_prompt", return_value="r"),
        patch("sophon.core.card"),
        patch("sophon.setup_wizard.run_setup_wizard", return_value=0) as setup,
        patch("sophon.core._mark_interactive_initialized") as mark_initialized,
        patch("sophon.core.run_session", return_value=0) as run_session,
    ):
        assert core._start_interactive() == 0
    setup.assert_called_once_with(reconfigure=True)
    mark_initialized.assert_called_once_with()
    run_session.assert_called_once_with()


def test_existing_config_with_startup_marker_skips_prompt(tmp_path):
    from sophon import core

    marker = tmp_path / "state" / "interactive_initialized"
    marker.parent.mkdir()
    marker.write_text("initialized\n", encoding="utf-8")
    with (
        patch("sophon.core.FIRST_INTERACTIVE_MARKER", marker),
        patch("sophon.core.read_config", return_value={"provider": "openrouter", "model": "test"}),
        patch("sophon.core.read_prompt", side_effect=AssertionError("startup choice should only be asked once")),
        patch("sophon.core.run_session", return_value=0) as run_session,
    ):
        assert core._start_interactive() == 0
    run_session.assert_called_once_with()


def test_primary_slash_completion_omits_setup_and_completes_subcommands():
    from sophon.core import _main_completion_choices

    assert _main_completion_choices("/s", 2) == ["tatus"]
    assert _main_completion_choices("/auto-backup", len("/auto-backup")) == [" "]
    schedules = _main_completion_choices("/auto-backup frequency ", len("/auto-backup frequency "))
    assert "Every 2 days" in schedules
    assert "Every day at 09:00" in schedules
