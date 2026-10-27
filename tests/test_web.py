import json
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sss import web
from sss.http import SourceError
from sss.sync import Sync, course_directory


@pytest.fixture
def app(tmp_path, monkeypatch):
    for key in web.FIELDS:
        monkeypatch.delenv("SSS_" + key.upper(), raising=False)
    env = tmp_path / ".env"
    env.write_text("# preserved comment\nUNRELATED='keep'\nSSS_CANVAS_TOKEN='private-canvas-value'\n", encoding="utf-8")
    course = {"id": 93540, "name": "高电压技术", "course_code": "本-EE3325-高电压技术",
              "enrollments": [{"type": "student"}], "teachers": [{"display_name": "吴建东"}],
              "term": {"id": 30, "name": "2026-2027 Fall", "start_at": "2026-01-01T00:00:00Z", "end_at": "2099-01-01T00:00:00Z"}}

    class FakeCanvas:
        def __init__(self, token):
            if not token:
                raise SourceError("Canvas token missing")
            self.http = SimpleNamespace(json=lambda *a: {"id": 1})

        def courses(self):
            return [course]

    monkeypatch.setattr(web, "Canvas", FakeCanvas)
    monkeypatch.setattr(web, "Share", lambda token: SimpleNamespace(call=lambda *a: {"id": 2}))
    monkeypatch.setattr(web, "Reviews", lambda token: SimpleNamespace(http=SimpleNamespace(json=lambda *a: {"id": 3})))
    monkeypatch.setattr(Sync, "canvas", lambda self: {"files": 2, "assignments": 1})
    monkeypatch.setattr(Sync, "subtitles", lambda self: {"recordings": 3, "subtitles": 2, "unavailable": 1})
    static = tmp_path / "static"
    static.mkdir()
    (static / "assets").mkdir()
    (static / "index.html").write_text("<!doctype html><title>S.S.S</title>", encoding="utf-8")
    return web.create_app(tmp_path / "missing.json", env, tmp_path / "data", static_dir=static)


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client


def wait_for_sync(client):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = client.get("/api/state").json()["courses"][0]
        if row["sync"]["status"] not in ("queued", "running"):
            return row
        time.sleep(.01)
    pytest.fail("Sync did not finish")


def test_initial_state_never_returns_secrets_and_serves_page(client):
    response = client.get("/api/state")
    assert response.status_code == 200
    assert response.json()["settings"]["canvas"]["configured"] is True
    assert "private-canvas-value" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.get("/").status_code == 200


def test_prompt_list_is_generic_or_bound_to_a_loaded_course_without_secrets(client, app):
    generic = client.get("/api/prompts")
    assert generic.status_code == 200 and len(generic.json()["prompts"]) == 6
    assert client.get("/api/prompts?course=unknown").status_code == 404
    course = client.get("/api/courses").json()["courses"][0]
    response = client.get("/api/prompts?course=93540")
    assert response.status_code == 200
    assert str(app.state.service.directory(course)) in response.json()["prompts"][0]["text"]
    assert "吴建东" in response.text and "private-canvas-value" not in response.text
    assert not app.state.service.directory(course).exists()


def test_connect_saves_raw_values_preserves_settings_and_does_not_echo(client, app):
    value = "private-review's-${literal}=="
    response = client.post("/api/connect", json={"share_token": "private-share-value", "reviews_cookie": value})
    assert response.status_code == 200
    assert response.json()["settings"]["reviews"]["status"] == "ok"
    assert response.json()["courses"][0]["name"] == "高电压技术"
    assert value not in response.text and "private-share-value" not in response.text
    contents = app.state.service.env_path.read_text(encoding="utf-8")
    assert "# preserved comment" in contents and "UNRELATED='keep'" in contents
    config = app.state.service.config()
    assert config["reviews_cookie"] == value
    assert config["canvas_token"] == "private-canvas-value"


def test_optional_platforms_do_not_prevent_course_loading(client):
    response = client.post("/api/connect", json={})
    data = response.json()
    assert data["settings"]["share"]["status"] == "missing"
    assert data["settings"]["reviews"]["status"] == "missing"
    assert len(data["courses"]) == 1


def test_empty_value_clears_saved_credential(client, app):
    response = client.post("/api/connect", json={"canvas_token": ""})
    assert response.status_code == 200
    assert response.json()["courses"] == []
    assert response.json()["settings"]["canvas"]["configured"] is False
    assert app.state.service.config()["canvas_token"] == ""


def test_environment_credentials_are_reported_as_managed(client, monkeypatch):
    monkeypatch.setenv("SSS_CANVAS_TOKEN", "private-environment-value")
    response = client.get("/api/state")
    assert response.json()["settings"]["canvas"]["managed"] is True
    response = client.post("/api/connect", json={"canvas_token": "replacement-value"})
    assert response.status_code == 409
    assert "private-environment-value" not in response.text


@pytest.mark.parametrize("payload", [
    {"canvas_token": "sensitive-value\ninvalid"},
    {"reviews_cookie": {"secret": "sensitive-value"}},
    {"canvas_token": "sensitive-value", "unknown_field": True},
])
def test_validation_errors_do_not_echo_submitted_credentials(client, payload):
    response = client.post("/api/connect", json=payload)
    assert response.status_code == 422
    assert "sensitive-value" not in response.text


def test_rejects_cross_origin_requests_and_untrusted_hosts(client, app):
    before = app.state.service.env_path.read_text()
    response = client.post("/api/connect", json={"canvas_token": "attacker-value"}, headers={"Origin": "https://example.com"})
    assert response.status_code == 403
    assert client.get("/api/state", headers={"Host": "attacker.example"}).status_code == 400
    assert client.get("/api/state", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert app.state.service.env_path.read_text() == before
    assert client.get("/api/state", headers={"Origin": "http://127.0.0.1"}).status_code == 200


def test_course_sync_finishes_and_status_survives_service_restart(client, app):
    client.get("/api/courses")
    response = client.post("/api/courses/93540/sync")
    assert response.status_code == 202
    course = wait_for_sync(client)
    assert course["sync"]["status"] == "complete" and course["local"]
    assert course["sync"]["sources"]["subtitles"]["summary"]["subtitles"] == 2
    service = app.state.service
    other = web.create_app(service.config_path, service.env_path, service.output)
    with TestClient(other, base_url="http://127.0.0.1") as restored:
        course = restored.get("/api/courses").json()["courses"][0]
        assert course["sync"]["status"] == "complete"
        assert course["sync"]["finished_at"]


def test_duplicate_sync_is_rejected_while_running(client, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def canvas(self):
        started.set()
        assert release.wait(5)
        return {"files": 1}

    monkeypatch.setattr(Sync, "canvas", canvas)
    client.get("/api/courses")
    try:
        assert client.post("/api/courses/93540/sync").status_code == 202
        assert started.wait(3)
        assert client.post("/api/courses/93540/sync").status_code == 409
        course = client.get("/api/state").json()["courses"][0]
        assert course["sync"]["active_source"] == "canvas"
    finally:
        release.set()
    assert wait_for_sync(client)["sync"]["status"] == "complete"


def test_partial_source_failure_is_visible_and_keeps_course_output(client, monkeypatch):
    def subtitles(self):
        raise SourceError("Subtitle service unavailable")

    monkeypatch.setattr(Sync, "subtitles", subtitles)
    client.get("/api/courses")
    client.post("/api/courses/93540/sync")
    row = wait_for_sync(client)
    assert row["sync"]["status"] == "partial" and row["local"]
    assert row["sync"]["errors"][0]["message"] == "Subtitle service unavailable"


def test_open_folder_uses_only_a_loaded_courses_generated_directory(client, app, monkeypatch):
    opened = []
    monkeypatch.setattr(web, "open_folder", opened.append)
    course = client.get("/api/courses").json()["courses"][0]
    assert client.post("/api/courses/93540/open").status_code == 409
    assert client.post("/api/courses/unknown/open").status_code == 404
    path = course_directory(course, app.state.service.output)
    path.mkdir(parents=True)
    assert client.post("/api/courses/93540/open").status_code == 200
    assert opened == [path]


def test_existing_plan_aliases_are_kept(client, app):
    course = client.get("/api/courses").json()["courses"][0]
    plan = {"schema_version": 1, **course, "aliases": ["EE3315"], "share_ids": [], "reviews_ids": []}
    path = app.state.service.output.parent / ".local" / "EE3325.json"
    path.parent.mkdir()
    path.write_text(json.dumps(plan), encoding="utf-8")
    assert app.state.service.plan(course)["aliases"] == ["EE3315"]


def test_custom_plan_directory_is_used_for_status_and_folder_opening(client, app, monkeypatch):
    course = client.get("/api/courses").json()["courses"][0]
    plan = {"schema_version": 1, **course, "name": "高电压技术-自定义", "aliases": [], "share_ids": [], "reviews_ids": []}
    path = app.state.service.output.parent / ".local" / "EE3325.json"
    path.parent.mkdir()
    path.write_text(json.dumps(plan), encoding="utf-8")
    directory = course_directory(plan, app.state.service.output)
    directory.mkdir(parents=True)
    opened = []
    monkeypatch.setattr(web, "open_folder", opened.append)
    assert client.get("/api/state").json()["courses"][0]["local"] is True
    assert client.post("/api/courses/93540/open").status_code == 200
    assert opened == [directory]
