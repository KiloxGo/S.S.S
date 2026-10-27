from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sss import desktop, web


def test_portable_data_location_and_readonly_fallback(tmp_path, monkeypatch):
    beside = tmp_path / "application"
    beside.mkdir()
    fallback = tmp_path / "profile"
    monkeypatch.setattr(desktop, "application_directory", lambda: beside)
    monkeypatch.setattr(desktop, "user_data_directory", lambda: fallback)
    assert desktop.data_directory() == fallback
    (beside / "portable.flag").touch()
    assert desktop.data_directory() == beside / "SSS-Data"
    original = desktop.writable_directory

    def readonly(path):
        if path == beside / "SSS-Data":
            raise PermissionError("read-only application volume")
        return original(path)

    monkeypatch.setattr(desktop, "writable_directory", readonly)
    assert desktop.data_directory() == fallback
    assert desktop.data_directory(tmp_path / "chosen") == tmp_path / "chosen"


def test_frozen_application_directory_is_beside_mac_bundle(tmp_path, monkeypatch):
    executable = tmp_path / "SSS.app" / "Contents" / "MacOS" / "SSS"
    monkeypatch.setattr(desktop.sys, "frozen", True, raising=False)
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    monkeypatch.setattr(desktop.sys, "executable", str(executable))
    assert desktop.application_directory() == tmp_path


def test_instance_lock_rejects_duplicate_then_allows_reopening(tmp_path):
    path = tmp_path / "desktop.lock"
    with desktop.InstanceLock(path):
        with pytest.raises(RuntimeError, match="已在使用"):
            with desktop.InstanceLock(path):
                pytest.fail("duplicate lock acquired")
    with desktop.InstanceLock(path):
        pass


def test_desktop_uses_only_its_data_directory_and_limits_native_actions(tmp_path, monkeypatch):
    for key in web.FIELDS:
        monkeypatch.delenv("SSS_" + key.upper(), raising=False)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".env").write_text("SSS_CANVAS_TOKEN=private-checkout-token\n")
    monkeypatch.chdir(checkout)
    root = tmp_path / "SSS-Data"
    root.mkdir()
    copied, opened = [], []
    monkeypatch.setattr(web, "open_folder", opened.append)
    app = web.create_app(root / ".local/credentials.json", root / ".env", root / "data",
                         desktop={"data_dir": str(root)}, clipboard=copied.append)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        state = client.get("/api/state").json()
        assert state["desktop"]["data_dir"] == str(root)
        assert not any(item["configured"] for item in state["settings"].values())
        assert client.post("/api/desktop/copy", json={"text": "考试范围，引用字幕时间戳"}).status_code == 200
        assert copied == ["考试范围，引用字幕时间戳"]
        assert client.post("/api/desktop/copy", json={"text": "blocked"}, headers={"Origin": "https://other.example"}).status_code == 403
        assert len(copied) == 1
        assert client.post("/api/desktop/open-data").status_code == 200
        assert opened == [root]
        assert client.post("/api/desktop/copy", json={"text": ""}).status_code == 422
    browser = web.create_app(root / ".local/credentials.json", root / ".env", root / "data")
    with TestClient(browser, base_url="http://127.0.0.1") as client:
        assert client.post("/api/desktop/copy", json={"text": "test"}).status_code == 404
        assert client.post("/api/desktop/open-data").status_code == 404
