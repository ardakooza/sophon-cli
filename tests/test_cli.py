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
        patch("sophon.setup_wizard.getpass.getpass", return_value="test-secret"),
        patch("sophon.setup_wizard._read_key", return_value="ENTER"),
        patch("sophon.setup_wizard.write_config", return_value=tmp_path / "config.json"),
        patch("sophon.setup_wizard.ensure_agent_instructions_file"),
    ):
        assert run_setup_wizard() == 0
    text = output.getvalue()
    assert re.search(r"\033\[38;5;255m\s*Review Setup", text)
    assert re.search(r"\033\[38;5;255m\s*Setup Complete", text)
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
        patch("sophon.setup_wizard.getpass.getpass", return_value="test-secret"),
        patch("sophon.setup_wizard._read_key", return_value="ENTER"),
        patch("sophon.setup_wizard.write_config", return_value=tmp_path / "config.json"),
        patch("sophon.setup_wizard.ensure_agent_instructions_file"),
    ):
        assert run_setup_wizard() == 0
    text = output.getvalue()
    assert "Setup of Credentials" in text
    assert "API key: [hidden input]" in text
    assert "test-secret" not in text


def test_plain_words_are_not_commands():
    result = run(ROOT / "sophon", input_text="exit\n/exit\n")
    assert result.returncode == 0
    assert "Commands must start with /." in result.stdout
    assert "Session closed. See you soon." in result.stdout


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
    assert update_cards == [["Auto-update is on. Use /auto-update on|off."]]
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
        same_phase = _answering_rows(5)[0]
        next_phase = _answering_rows(6)[0]
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
            [{"role": "assistant", "content": "Final answer", "reasoning": "Reasoning details"}],
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

    commands = iter(["/changelog", "/exit"])
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
        {"role": "assistant", "content": "The answer", "reasoning": "First inspect the request."},
    ], 80))
    plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
    assert plain.index("Reasoning") < plain.index("│") < plain.index("The answer")


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
