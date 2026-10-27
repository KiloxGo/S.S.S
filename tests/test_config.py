import json
import os
from pathlib import Path

import pytest

from sss.config import load_config


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("SSS_CANVAS_TOKEN", "SSS_SHARE_TOKEN", "SSS_REVIEWS_COOKIE"):
        monkeypatch.delenv(name, raising=False)


def test_missing_files_allow_environment_only(monkeypatch):
    monkeypatch.setenv("SSS_CANVAS_TOKEN", "environment-token")
    assert load_config(Path("missing.json")) == {
        "canvas_token": "environment-token", "share_token": "", "reviews_cookie": "",
    }


def test_each_field_uses_environment_then_dotenv_then_json(monkeypatch):
    path = Path("credentials.json")
    path.write_text(json.dumps({
        "canvas_token": "json-canvas", "share_token": "json-share", "reviews_cookie": "jcourse_session=json",
    }), encoding="utf-8")
    Path(".env").write_text("SSS_CANVAS_TOKEN='file-canvas'\nSSS_SHARE_TOKEN='file-share'\n", encoding="utf-8")
    monkeypatch.setenv("SSS_CANVAS_TOKEN", "environment-canvas")
    assert load_config(path) == {
        "canvas_token": "environment-canvas", "share_token": "file-share", "reviews_cookie": "jcourse_session=json",
    }
    assert "SSS_SHARE_TOKEN" not in os.environ


def test_explicit_file_preserves_cookie_and_literal_dollar_expression():
    Path(".env").write_text("SSS_CANVAS_TOKEN=wrong-file\n", encoding="utf-8")
    Path("chosen.env").write_text(
        "\ufeff# UTF-8 BOM and quoted cookies\n"
        "SSS_CANVAS_TOKEN='token-${UNCHANGED}'\n"
        "SSS_REVIEWS_COOKIE='jcourse_session=abc==; csrftoken=def; acw_tc=ghi'\n",
        encoding="utf-8",
    )
    config = load_config(Path("missing.json"), Path("chosen.env"))
    assert config["canvas_token"] == "token-${UNCHANGED}"
    assert config["reviews_cookie"] == "jcourse_session=abc==; csrftoken=def; acw_tc=ghi"


def test_empty_values_override_lower_priority_credentials(monkeypatch):
    Path("credentials.json").write_text('{"share_token": "json-share"}', encoding="utf-8")
    Path(".env").write_text("SSS_CANVAS_TOKEN=file-canvas\nSSS_SHARE_TOKEN=''\n", encoding="utf-8")
    monkeypatch.setenv("SSS_CANVAS_TOKEN", "")
    config = load_config(Path("credentials.json"))
    assert config["canvas_token"] == ""
    assert config["share_token"] == ""
