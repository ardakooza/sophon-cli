import os
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


def test_plain_words_are_not_commands():
    result = run(ROOT / "sophon", input_text="exit\n/exit\n")
    assert result.returncode == 0
    assert "Commands must start with /." in result.stdout


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


def test_config_masks_credentials_and_uses_private_file_permissions():
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
        assert "••••••••-key" in result.stdout
