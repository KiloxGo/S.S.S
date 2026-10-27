import io
import json
import re
import stat
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import py7zr
import pytest
import requests

from sss.canvas import Canvas, group_courses, srt
from sss.communities import Reviews, Share, free_material, paginated, teacher_bucket
from sss.http import Http, SourceError
from sss.storage import Store, extract_archive, safe_name
from sss.sync import Sync, validate_plan


def course(cid, code, term=30, name="电气工程基础"):
    return {"id": cid, "name": name, "course_code": f"本-(2026-2027-1)-{code}-03-{name}",
            "teachers": [{"display_name": "徐潇源"}],
            "term": {"id": term, "name": "2026-2027 Fall", "start_at": "2026-08-01T00:00:00Z", "end_at": "2027-02-01T00:00:00Z"}}


def plan():
    return {"schema_version": 1, **group_courses([course(1, "EE3316")], now=datetime(2026, 10, 27, tzinfo=timezone.utc))[0]}


def test_group_multiple_spaces_and_restrict_terms():
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    older = course(3, "EE3316", term=27)
    older["term"]["end_at"] = "2026-07-01T00:00:00Z"
    groups = group_courses([course(1, "EE3316"), course(2, "EE3316"), older, {"id": 4}], now=now)
    assert len(groups) == 1 and groups[0]["canvas_ids"] == [1, 2]
    assert groups[0]["teachers"] == ["徐潇源"]
    assert group_courses([older], term="27", now=now)[0]["canvas_ids"] == [3]


def test_observer_space_downloaded_but_is_not_own_teacher():
    own = course(1, "EE3316")
    own["enrollments"] = [{"type": "student"}]
    observer = course(2, "EE3316")
    observer["enrollments"] = [{"type": "observer"}]
    observer["teachers"] = [{"display_name": "其他教师"}]
    group = group_courses([own, observer], now=datetime(2026, 10, 27, tzinfo=timezone.utc))[0]
    assert group["canvas_ids"] == [1, 2]
    assert group["teachers"] == ["徐潇源"]
    assert group["canvas_spaces"][1]["roles"] == ["observer"]


@pytest.mark.parametrize("value,expected", [(0, True), ("0.00", True), (0.01, False), (-1, False), (None, False), (False, False), ("free", False), ("NaN", False)])
def test_only_explicit_zero_price_is_free(value, expected):
    assert free_material({"points": value}) is expected


def test_paid_price_change_never_calls_purchase():
    api = Share("fake-token")
    calls = []
    api.material = lambda mid: {"id": mid, "points": 1}
    api.call = lambda *args, **kw: calls.append(args)
    with pytest.raises(SourceError, match="not explicitly zero"):
        api.acquire_free(7)
    assert calls == []


def test_free_and_owned_material_acquisition():
    api = Share("fake-token")
    assert api.http.session.get_adapter("https://api-v2.share.dyweb.sjtu.cn/api/v1/material/purchase/7").max_retries.total == 0
    calls = []
    api.material = lambda mid: {"id": mid, "points": 0}
    def call(method, path):
        calls.append(path)
        return "https://storage.example/material.zip?signature=secret" if "download" in path else None
    api.call = call
    _, url = api.acquire_free(7)
    assert calls == ["/api/v1/material/purchase/7", "/api/v1/material/download/7"]
    assert url.startswith("https://")
    api.material = lambda mid: {"id": mid, "points": 0, "has_purchased": True}
    calls.clear()
    api.acquire_free(7)
    assert len(calls) == 1


def test_share_includes_every_teacher_and_unarchived():
    api = Share("fake-token")
    api.call = lambda *a, **kw: {"classes": [{"id": 1, "teacher": {"name": "甲"}, "materials": [{"id": 10}]}, {"id": 2, "teacher": {"name": "乙"}, "materials": [{"id": 20}]}], "unarchived": [{"id": 30}]}
    materials = api.materials(7)
    assert [m["id"] for m in materials] == [30, 10, 20]
    assert materials[2]["class"]["teacher"]["name"] == "乙"


def test_review_classification():
    assert teacher_bucket([{"name": " 徐 潇源 "}], ["徐潇源"]) == "own-teachers"
    assert teacher_bucket([{"name": "其他"}], ["徐潇源"]) == "other-teachers"
    assert teacher_bucket([], ["徐潇源"]) == "unknown-teachers"
    assert teacher_bucket([{"name": "其他"}], []) == "unknown-teachers"


@pytest.mark.parametrize("value,expected", [
    ("raw-session", {"jcourse_session": "raw-session"}),
    ("raw-session==", {"jcourse_session": "raw-session=="}),
    ("  raw-session  ", {"jcourse_session": "raw-session"}),
    ("jcourse_session=raw-session==", {"jcourse_session": "raw-session=="}),
    ("jcourse_session=raw-session; csrftoken=csrf; acw_tc=edge; cdn_sec_tc=cdn; other=ignored",
     {"jcourse_session": "raw-session", "csrftoken": "csrf", "acw_tc": "edge", "cdn_sec_tc": "cdn"}),
    ("", {}),
    ("   ", {}),
])
def test_reviews_accepts_raw_session_and_existing_cookie_headers(value, expected):
    api = Reviews(value)
    assert api.http.session.cookies.get_dict() == expected
    for cookie in api.http.session.cookies:
        assert cookie.domain == "course.sjtu.plus"
        assert cookie.secure


def test_pagination_continues_when_server_caps_page_size():
    class Fake:
        def json(self, method, path, params):
            page = params["page"]
            return {"items": [{"id": page}], "total": 3, "page_size": 1}
    assert [r["id"] for r in paginated(Fake(), "/reviews")] == [1, 2, 3]


def test_broken_pagination_is_not_silent_success():
    class Fake:
        def json(self, *args, **kwargs):
            return {"items": [{"id": 1}], "total": 3}
    with pytest.raises(SourceError, match="repeated"):
        paginated(Fake(), "/reviews")


def test_srt_milliseconds_and_last_end():
    text = srt([{"bg": 1234, "ed": 2500, "res": "你好"}, {"bg": 60000, "ed": 62000, "res": "世界"}])
    assert "00:00:01,234 --> 00:00:02,500" in text
    assert "00:01:00,000 --> 00:01:02,000" in text


def test_assignment_links_only_trust_canvas():
    api = Canvas("fake-token")
    assert api.linked_files('<a href="/courses/42/files/100/download?verifier=private">a</a><img src="/api/v1/files/200/preview"><a href="https://evil.example/files/300">bad</a>') == [100, 200]


def test_redaction_removes_secrets_and_signed_urls(tmp_path):
    store = Store(tmp_path, ["super-private-value"])
    clean = store.clean({"token": "abc", "text": "super-private-value", "url": "https://example.com/file?jwt_token=abc#private", "description": '<a href="https://example.com/f?verifier=123">file</a>'})
    text = json.dumps(clean)
    assert "super-private-value" not in text and "verifier" not in text and "jwt_token" not in text
    assert clean["url"] == "https://example.com/file"


def test_same_size_corruption_is_repaired_from_verified_cache(tmp_path):
    class Fake:
        calls = 0
        def request(self, *args, **kwargs):
            self.calls += 1
            r = requests.Response()
            r.status_code = 200
            r.raw = io.BytesIO(b"good")
            return r
    store, http = Store(tmp_path), Fake()
    target = tmp_path / "file.pdf"
    store.download(http, "https://example.com/f", target, 4, "v1")
    assert store.download(http, "https://example.com/f", target, 4, "v1")["status"] == "cached"
    target.write_bytes(b"evil")
    store.download(http, "https://example.com/f", target, 4, "v1")
    assert target.read_bytes() == b"good" and http.calls == 1
    blob = store.state.blobs / store.state.get("download:https://example.com/f")["sha256"]
    blob.write_bytes(b"evil")
    store.download(http, "https://example.com/f", target, 4, "v1")
    assert target.read_bytes() == b"good" and http.calls == 2


def test_failed_download_preserves_previous_file(tmp_path):
    class Fake:
        def request(self, *args, **kwargs):
            r = requests.Response()
            r.status_code = 200
            r.raw = io.BytesIO(b"bad")
            return r
    target = tmp_path / "file.pdf"
    target.write_bytes(b"old")
    with pytest.raises(SourceError, match="size mismatch"):
        Store(tmp_path).download(Fake(), "https://example.com/f", target, 10)
    assert target.read_bytes() == b"old" and not (tmp_path / "file.pdf.part").exists()


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/escape", "dir/../../escape", "dir\\..\\escape", "file:stream", "NUL", "dir/ending."])
def test_zip_unsafe_paths_rejected(tmp_path, name):
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, "payload")
    with pytest.raises(SourceError):
        extract_archive(path)
    assert not path.with_name("bad.zip.extracted").exists()


def test_zip_symlink_and_case_collision_rejected(tmp_path):
    path = tmp_path / "bad.zip"
    info = zipfile.ZipInfo("link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(info, "../../escape")
    with pytest.raises(SourceError, match="links"):
        extract_archive(path)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("A.txt", "one")
        archive.writestr("a.txt", "two")
    with pytest.raises(SourceError, match="duplicate"):
        extract_archive(path)


def test_tar_links_rejected(tmp_path):
    path = tmp_path / "bad.tar"
    with tarfile.open(path, "w") as archive:
        item = tarfile.TarInfo("link")
        item.type = tarfile.SYMTYPE
        item.linkname = "../../escape"
        archive.addfile(item)
    with pytest.raises(SourceError):
        extract_archive(path)


def test_real_7z_and_zip_and_tar_extraction(tmp_path):
    payload = tmp_path / "notes.txt"
    payload.write_text("study notes", encoding="utf-8")
    seven = tmp_path / "notes.7z"
    with py7zr.SevenZipFile(seven, "w") as archive:
        archive.write(payload, "notes.txt")
    tar = tmp_path / "notes.tar.gz"
    with tarfile.open(tar, "w:gz") as archive:
        archive.add(payload, arcname="notes.txt")
    zipped = tmp_path / "notes.zip"
    with zipfile.ZipFile(zipped, "w") as archive:
        archive.write(seven, "notes.7z")
    assert len(extract_archive(zipped)) == 2
    assert (tmp_path / "notes.zip.extracted/notes.7z.extracted/notes.txt").read_text() == "study notes"
    assert len(extract_archive(zipped)) == 2
    assert Path(extract_archive(tar)[0], "notes.txt").read_text() == "study notes"


def test_archive_limits_leave_no_partial_extraction(tmp_path):
    path = tmp_path / "big.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("payload", b"x" * 1000)
    with pytest.raises(SourceError, match="limits"):
        extract_archive(path, max_bytes=100)
    assert not path.with_name("big.zip.extracted").exists()


def test_nested_archive_shares_one_size_budget(tmp_path):
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("payload", b"x" * 1000)
    path = tmp_path / "nested.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("inside.zip", inner.getvalue())
    with pytest.raises(SourceError, match="limits"):
        extract_archive(path, max_bytes=1100)
    assert not (tmp_path / "nested.zip.extracted/inside.zip.extracted").exists()


def test_rar_hardlink_rejected_before_decoder(tmp_path, monkeypatch):
    import rarfile
    class Member:
        filename, file_size, file_redir = "hardlink", 0, (4, 0, "../../target")
        def is_symlink(self): return False
        def is_file(self): return True
        def is_dir(self): return False
    class Archive:
        def __init__(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def infolist(self): return [Member()]
        def extractall(self, *a): pytest.fail("Unsafe RAR must not reach decoder")
    monkeypatch.setattr(rarfile, "RarFile", Archive)
    path = tmp_path / "unsafe.rar"
    path.write_bytes(b"fixture")
    with pytest.raises(SourceError, match="links"):
        extract_archive(path)


@pytest.mark.parametrize("version", [3, 5])
def test_rar_regular_files_use_real_library_metadata_api(tmp_path, monkeypatch, version):
    import rarfile
    # Use real metadata objects so a removed library method cannot be hidden by a stub.
    if version == 3:
        member = rarfile.Rar3Info()
        member.type = rarfile.RAR_BLOCK_FILE
        member.flags, member.host_os, member.mode = 0, rarfile.RAR_OS_WIN32, 0
    else:
        member = rarfile.Rar5FileInfo()
        member.file_flags = 0
    member.filename, member.file_size = "notes.txt", len(b"study notes")

    class Archive:
        def __init__(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def infolist(self): return [member]
        def extractall(self, stage): (stage / "notes.txt").write_bytes(b"study notes")

    monkeypatch.setattr(rarfile, "RarFile", Archive)
    path = tmp_path / "notes.rar"
    path.write_bytes(b"fixture")
    assert Path(extract_archive(path)[0], "notes.txt").read_text() == "study notes"


@pytest.mark.parametrize("exception,message", [
    ("NeedFirstVolume", "缺少首卷"),
    ("RarCannotExec", "解压工具"),
    ("PasswordRequired", "需要密码"),
    ("AttributeError", "RAR archive extraction failed (AttributeError)"),
])
def test_rar_failure_reports_actual_cause(tmp_path, monkeypatch, exception, message):
    import rarfile
    error_type = AttributeError if exception == "AttributeError" else getattr(rarfile, exception)
    def fail(*args):
        if exception == "NeedFirstVolume":
            raise error_type("internal diagnostic", 1)
        raise error_type("internal diagnostic")
    monkeypatch.setattr(rarfile, "RarFile", fail)
    path = tmp_path / "notes.rar"
    path.write_bytes(b"fixture")
    with pytest.raises(SourceError, match=re.escape(message)):
        extract_archive(path)


@pytest.mark.parametrize("payload,error", [(b"abc", None), (b"abcd", "超过声明大小"), (b"ab", "内容不完整"), (b"abd", "校验失败")])
def test_bsdtar_fallback_preserves_argument_order_and_checks_output(tmp_path, monkeypatch, payload, error):
    import rarfile
    import zlib
    from types import SimpleNamespace
    member = SimpleNamespace(filename="notes.txt", file_size=3, file_redir=None, CRC=zlib.crc32(b"abc"),
                             is_file=lambda: True, is_dir=lambda: False, is_symlink=lambda: False)
    class Archive:
        def __init__(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def infolist(self): return [member]
        def extractall(self, stage): raise rarfile.BadRarFile("decoder argument failure")
    path = tmp_path / "notes.rar"
    path.write_bytes(b"fixture")
    class Process:
        def __init__(self, command, **kwargs):
            assert command[command.index("-f") + 1] == str(path.resolve())
            self.stdout = io.BytesIO(payload)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def wait(self, **kwargs): return 0
        def kill(self): pass
    monkeypatch.setattr(rarfile, "RarFile", Archive)
    monkeypatch.setattr(rarfile, "tool_setup", lambda: SimpleNamespace(setup={"open_cmd": ("BSDTAR_TOOL",)}))
    monkeypatch.setattr("sss.storage.subprocess.Popen", Process)
    if error:
        with pytest.raises(SourceError, match=error):
            extract_archive(path)
        assert not path.with_name("notes.rar.extracted").exists()
    else:
        assert Path(extract_archive(path)[0], "notes.txt").read_bytes() == b"abc"


def test_filename_keeps_extension_and_reserved_names():
    assert safe_name("CON.pdf").startswith("_")
    assert safe_name("x" * 200 + ".zip").endswith(".zip")


def test_plan_rejects_path_like_ids():
    p = plan()
    p["canvas_ids"] = ["../../elsewhere"]
    with pytest.raises(SourceError, match="positive integer"):
        validate_plan(p)


def test_source_failure_continues_other_sources_and_reports_partial(tmp_path):
    job = Sync({}, plan(), tmp_path)
    def fail():
        raise SourceError("credentials missing")
    job.share = fail
    job.reviews = lambda: {"reviews": 1}
    report = job.run(["share", "reviews"])
    assert report["status"] == "partial"
    assert report["sources"]["reviews"]["status"] == "complete"
    assert report["errors"][0]["scope"] == "share"


def test_redirect_strips_custom_credentials(monkeypatch):
    http = Http("https://api.example", {"Auth": "secret", "Authorization": "Bearer secret", "jwt-token": "secret"}, delay=0)
    calls = []
    def request(method, url, **kw):
        calls.append(kw["headers"].copy())
        r = requests.Response()
        r.url = url
        r.status_code = 302 if len(calls) == 1 else 200
        r.headers["Location"] = "https://storage.example/file"
        r._content = b""
        r._content_consumed = True
        return r
    monkeypatch.setattr(http.session, "request", request)
    http.request("GET", "/download")
    assert calls[0]["Auth"] == "secret" and calls[1] == {}


def test_cross_origin_canvas_pagination_is_refused(monkeypatch):
    http = Http("https://canvas.example", {"Authorization": "Bearer secret"}, delay=0)
    r = requests.Response()
    r.status_code = 200
    r._content = b'[{"id":1}]'
    r.headers["Link"] = '<https://evil.example/next>; rel="next"'
    monkeypatch.setattr(http, "request", lambda *a, **kw: r)
    with pytest.raises(SourceError, match="pagination"):
        list(http.pages("/courses"))
