import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests
from bs4 import BeautifulSoup

from sss.canvas import Canvas
from sss.content import html_markdown
from sss.content import write_pair
from sss.storage import Store, extract_archive
from sss.sync import Sync


def assert_local_links(root):
    checked = 0
    for page in [*root.rglob("*.md"), *root.rglob("*.html")]:
        text = page.read_text(encoding="utf-8")
        targets = re.findall(r'\]\(([^\s)]+)(?:\s+"[^"]*")?\)', text) if page.suffix == ".md" else [n.get("href") or n.get("src") for n in BeautifulSoup(text, "html.parser").select("[href], [src]")]
        for target in targets:
            if not target or urlsplit(target).scheme or target.startswith("#"):
                continue
            path = (page.parent / unquote(target.split("#")[0])).resolve()
            assert path.is_relative_to(root.resolve()), (page, target)
            assert path.exists(), (page, target)
            checked += 1
    return checked


def test_readable_course_export_separates_questions_solutions_and_deduplicates(tmp_path, monkeypatch):
    payloads = {10: b"same document", 11: b"same document", 12: b"image bytes"}
    files = {fid: {"id": fid, "display_name": f"{fid}.pdf" if fid != 12 else "figure.png", "url": f"https://files.example/{fid}", "size": len(data), "updated_at": "v1"} for fid, data in payloads.items()}
    files[10]["hidden_for_user"] = True
    class FakeHttp:
        calls = 0
        def request(self, method, url, **kwargs):
            self.calls += 1
            response = requests.Response()
            response.status_code = 200
            response.raw = io.BytesIO(payloads[int(url.rsplit("/", 1)[1])])
            return response
    http = FakeHttp()
    class FakeCanvas(Canvas):
        def __init__(self, token): self.http = http
        def file(self, fid): return files[fid]
        def items(self, cid, kind, **kwargs):
            if kind == "folders": return []
            if kind == "files": return [files[10]]
            if cid == 2: return []
            return [
                {"id": 101, "name": "附件题", "description": '<a href="/courses/1/files/10" title="原题附件">题目文件</a>', "attachments": [files[10]], "submission": {"attachments": [files[11]]}},
                {"id": 102, "name": "页面题", "description": '<h2>证明</h2><p>请证明 x &gt; 0。</p><table><tr><th>变量</th><th>值</th></tr><tr><td>x</td><td>1</td></tr></table><img src="/courses/1/files/12/preview"><script>alert(1)</script>',
                 "submission": {"body": "<p>这是我的在线解答。</p>"}},
                {"id": 103, "name": "未提交", "description": "<p>仅有题面。</p>"},
                {"id": 104, "name": "无描述附件题", "description": "", "attachments": [files[10]]},
            ]
        def videos(self, cid): return http, [{"id": str(cid), "courBeginTime": "2026-10-27 08:00"}]
        @staticmethod
        def subtitles(http, vid): return {"beforeAssemblyList": [{"bg": 0, "ed": 1000, "res": "相同字幕"}]}
    monkeypatch.setattr("sss.sync.Canvas", FakeCanvas)
    plan = {"schema_version": 1, "code": "TEST100", "name": "测试课程", "term": {"id": 30}, "canvas_ids": [1, 2], "teachers": ["甲"], "canvas_spaces": [{"id": 1, "teachers": ["甲"]}, {"id": 2, "teachers": ["乙"]}]}
    job = Sync({}, plan, tmp_path / "data")
    report = job.run(["canvas", "subtitles"])
    assert report["status"] == "complete"
    assert not list(job.root.rglob("*.json"))
    assert (job.root / "canvas/1-甲").is_dir() and (job.root / "canvas/2-乙").is_dir()
    assert len(list(job.root.rglob("*.pdf"))) == 1
    assert len(list(job.root.rglob("*.srt"))) == 1
    for folder in (job.root / "canvas").glob("*/subtitles"):
        assert all(p.suffix == ".srt" for p in folder.iterdir())
    question = job.root / "canvas/1-甲/assignments/附件题-101/question/README.md"
    assert "题目文件" in question.read_text(encoding="utf-8")
    assert question.with_name("index.html").is_file()
    assert "题目见下方附件" in (job.root / "canvas/1-甲/assignments/无描述附件题-104/question/README.md").read_text(encoding="utf-8")
    solution = job.root / "canvas/1-甲/assignments/附件题-101/solution/README.md"
    assert "我的提交" in solution.read_text(encoding="utf-8")
    inline = job.root / "canvas/1-甲/assignments/页面题-102/question"
    assert "请证明" in (inline / "README.md").read_text(encoding="utf-8")
    assert "| 变量 |" in (inline / "README.md").read_text(encoding="utf-8")
    assert "<table>" in (inline / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in (inline / "index.html").read_text(encoding="utf-8")
    assert "在线解答" in (inline.parent / "solution/README.md").read_text(encoding="utf-8")
    assert not (job.root / "canvas/1-甲/assignments/未提交-103/solution").exists()
    assert assert_local_links(job.root) > 20
    assert "README.md)" in (job.root / "README.md").read_text(encoding="utf-8")
    assert 'index.html"' in (job.root / "index.html").read_text(encoding="utf-8")
    original_requests = http.calls
    second = Sync({}, plan, tmp_path / "data")
    second.run(["canvas", "subtitles"])
    assert http.calls == original_requests
    assert len(list(second.root.rglob("*.pdf"))) == 1
    assert assert_local_links(second.root) > 20
    third = Sync({}, plan, tmp_path / "data")
    third.run(["canvas"])
    assert http.calls == original_requests
    assert ".srt)" in (third.root / "canvas/2-乙/README.md").read_text(encoding="utf-8")
    assert len(list(third.root.rglob("*.srt"))) == 1
    assert assert_local_links(third.root) > 20


def test_legacy_receipts_are_imported_and_original_folder_is_preserved(tmp_path):
    plan = {"schema_version": 1, "code": "TEST100", "name": "Course", "term": {"id": 30}, "canvas_ids": [1], "teachers": ["Teacher"]}
    job = Sync({}, plan, tmp_path / "data")
    old = job.root / "canvas/1/files/10-notes.pdf"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old notes")
    digest = hashlib.sha256(b"old notes").hexdigest()
    old.with_name(old.name + ".receipt.json").write_text(json.dumps({"sha256": digest, "updated": "v1"}), encoding="utf-8")
    (job.root / "manifest.json").write_text("{}", encoding="utf-8")
    def render():
        class Offline:
            def request(self, *a, **kw): raise AssertionError("Legacy cache must avoid network download")
        job.store.download(Offline(), "https://example.com/file", job.root / "notes.pdf", 9, "v1", key="canvas:file:10")
        return {"files": 1}
    job.canvas = render
    job.run(["canvas"])
    assert not list(job.root.rglob("*.json"))
    assert (job.root / "notes.pdf").read_bytes() == b"old notes"
    assert (job.state.directory / "legacy/canvas/1/files/10-notes.pdf").read_bytes() == b"old notes"


def test_gbk_zip_filenames_are_decoded_without_exporting_marker_json(tmp_path):
    name = "归纳总结.pdf"
    encoded = name.encode("gb18030")
    placeholder = "X" * len(encoded)
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        archive.writestr(placeholder, b"content")
    zipped = tmp_path / "notes.zip"
    zipped.write_bytes(raw.getvalue().replace(placeholder.encode(), encoded))
    folders = extract_archive(zipped)
    assert (Path(folders[0]) / name).read_bytes() == b"content"
    assert not list(Path(folders[0]).rglob("*.json"))


def test_html_to_markdown_keeps_lists_math_and_safe_links():
    text = html_markdown('<h2>题目</h2><ol><li>求 $x^2$</li><li><a href="notes.pdf">参考附件</a></li></ol><script>secret()</script>')
    assert "## 题目" in text and "$x^2$" in text and "[参考附件](notes.pdf)" in text
    assert "secret" not in text


def test_partial_refresh_preserves_content_referenced_by_unselected_source(tmp_path):
    plan = {"schema_version": 1, "code": "TEST100", "name": "Course", "term": {"id": 30}, "canvas_ids": [1], "teachers": ["Teacher"]}
    job = Sync({}, plan, tmp_path / "data")
    original = job.root / "canvas/1-Teacher/files/shared.pdf"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"shared")
    page = job.root / "share/README.md"
    page.parent.mkdir(parents=True)
    page.write_text("[仍需要的资料](../canvas/1-Teacher/files/shared.pdf)", encoding="utf-8")
    def refresh():
        job.clear(job.root / "canvas/1-Teacher/files")
        return {"files": 0}
    job.canvas = refresh
    job.run(["canvas"])
    assert original.read_bytes() == b"shared"
    assert assert_local_links(job.root) > 0


def test_markdown_source_files_are_deduplicated_but_reading_views_remain(tmp_path):
    store = Store(tmp_path)
    first, second = tmp_path / "lecture-a.md", tmp_path / "lecture-b.md"
    first.write_text("# Original course notes", encoding="utf-8")
    second.write_text("# Original course notes", encoding="utf-8")
    write_pair(store, tmp_path, "Index", '[Notes](lecture-b.md "Original title")')
    aliases = store.deduplicate()
    assert aliases == {"lecture-b.md": "lecture-a.md"}
    assert (tmp_path / "README.md").exists() and (tmp_path / "index.html").exists()
    assert "lecture-a.md" in (tmp_path / "README.md").read_text(encoding="utf-8")
    assert assert_local_links(tmp_path) == 2


def test_review_exports_focus_on_content_and_teacher_without_api_fields(tmp_path, monkeypatch):
    class FakeReviews:
        def __init__(self, *args): pass
        def courses(self, plan): return [{"id": 1}, {"id": 2}]
        def detail(self, cid): return {"main_teacher": {"name": "甲" if cid == 1 else "乙"}}
        def reviews(self, cid):
            return [{"id": cid, "content": f"正文 {cid}：考核占 70%。\n\n- 课程内容\n- 学习建议", "score": 0.123456, "created_at": "internal-timestamp", "user_id": "internal-user", "vote": {"like_count": 999}}]
    monkeypatch.setattr("sss.sync.Reviews", FakeReviews)
    plan = {"schema_version": 1, "code": "TEST100", "name": "Course", "term": {"id": 30}, "canvas_ids": [1], "teachers": ["甲"]}
    job = Sync({}, plan, tmp_path / "data")
    report = job.run(["reviews"])
    assert report["sources"]["reviews"]["summary"]["own-teachers"] == 1
    assert report["sources"]["reviews"]["summary"]["other-teachers"] == 1
    own = (job.root / "reviews/own-teachers/README.md").read_text(encoding="utf-8")
    other = (job.root / "reviews/other-teachers/README.md").read_text(encoding="utf-8")
    assert "正文 1" in own and "正文 2" in other and "70%" in own
    assert all(value not in own + other for value in ("0.123456", "internal-timestamp", "internal-user", "like_count"))
    assert not list(job.root.rglob("*.json"))
    assert assert_local_links(job.root) > 0
