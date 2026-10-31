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
    assert "Usage: sophon" in standalone.stdout
    assert "agent -p MESSAGE for one-shot" in standalone.stdout
    assert "╭" in standalone.stdout


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
        patch("sophon.setup_wizard.getpass.getpass", return_value="test-secret"),
        patch("sophon.setup_wizard._read_key", return_value="ENTER"),
        patch("sophon.setup_wizard.write_config", return_value=tmp_path / "config.json"),
    ):
        assert run_setup_wizard() == 0
    text = output.getvalue()
    assert re.search(r"\033\[38;5;255m\s*Review Setup", text)
    assert re.search(r"\033\[38;5;255m\s*Setup Complete", text)
    assert "\033[38;5;255mProvider:\033[0m" in text
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


def test_auto_update_setting_defaults_off_and_is_persisted_privately(tmp_path):
    from sophon import core

    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
        assert core.auto_update_enabled() is False
        core.set_auto_update(True)
        assert core.auto_update_enabled() is True
        path = core.settings_path()
        assert path.stat().st_mode & 0o777 == 0o600
        core.set_auto_update(False)
        assert core.auto_update_enabled() is False


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


def test_main_checks_auto_update_before_normal_cli_command():
    from sophon import core

    with (
        patch("sophon.core.auto_update_enabled", return_value=True),
        patch("sophon.core.auto_update_on_start") as startup_update,
        patch("sophon.core.doctor", return_value=0),
        patch("sophon.core.sys.argv", ["sophon", "doctor"]),
    ):
        assert core.main() == 0
    startup_update.assert_called_once_with()


def test_agent_exposes_required_builtin_tools_on_every_provider_request():
    import json

    from sophon.agent import BUILTIN_TOOLS, _request

    names = {tool["function"]["name"] for tool in BUILTIN_TOOLS}
    assert names == {"get_version", "is_update", "update", "system_health", "workspace_list"}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"ok"}}]}'

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
    with patch("sophon.agent.urlopen", side_effect=fake_urlopen):
        assert _request(config, [{"role": "user", "content": "hello"}]) == "ok"
    sent_names = {tool["function"]["name"] for tool in captured["payload"]["tools"]}
    assert names <= sent_names
    assert {"list_files", "read_file", "write_file"} <= sent_names


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


def test_cwd_and_pwd_commands_are_available_inside_agent_only(tmp_path, monkeypatch):
    from sophon import agent

    project = tmp_path / "workspace" / "project"
    project.mkdir(parents=True)
    monkeypatch.chdir(project)
    monkeypatch.setattr(agent, "WORKSPACE_ROOT", project.parent)
    monkeypatch.setattr(agent, "PROJECT_DIR", project)
    cwd_title, cwd_path, cwd_error = agent._workspace_location("cwd")
    pwd_title, pwd_path, pwd_error = agent._workspace_location("pwd")
    assert (cwd_title, cwd_path, cwd_error) == ("Current directory", str(project), False)
    assert (pwd_title, pwd_path, pwd_error) == ("Project workspace", str(project), False)
