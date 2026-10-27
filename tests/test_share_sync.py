import io
import json
import zipfile
import pytest
import requests

from sss.communities import Share
from sss.sync import Sync


@pytest.mark.parametrize("wrong_size", [False, True])
def test_share_sync_related_courses_dedup_free_purchase_download_and_extract(tmp_path, monkeypatch, wrong_size):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("notes.txt", "study notes")
    payloads = {11: archive.getvalue(), 12: b"%PDF-example"}
    purchases, downloads = [], []
    courses = {
        1: {"id": 1, "code": "EE3316", "deprecated_courses": [{"id": 2, "code": "EE3310"}]},
        2: {"id": 2, "code": "EE3310", "latest_course": {"id": 1, "code": "EE3316"}},
    }
    materials = {
        11: {"id": 11, "name": "Notes", "file_name": "notes.zip", "points": 0, "size": len(payloads[11]), "updated_at": "v1", "class": {"id": 5, "teacher": None}},
        12: {"id": 12, "file_name": "reference.pdf", "points": 0, "size": len(payloads[12]) + int(wrong_size), "updated_at": "v1", "has_purchased": True},
        13: {"id": 13, "points": 5},
        14: {"id": 14, "points": 0, "blocked": True},
    }

    class DownloadHttp:
        def request(self, method, url, **kwargs):
            mid = int(url.split("/")[-1].split("?")[0])
            downloads.append(mid)
            response = requests.Response()
            response.status_code = 200
            response.raw = io.BytesIO(payloads[mid])
            return response

    class FakeShare(Share):
        def __init__(self, token):
            self.http = DownloadHttp()

        def call(self, method, path, **kwargs):
            if path.endswith("/search"):
                return [courses[1]]
            if "/course/get/" in path:
                return courses[int(path.split("/")[-1])]
            if path == "/api/v1/material":
                return {"unarchived": [materials[12]], "classes": [{"id": 5, "teacher": {"name": "Teacher"}, "materials": [materials[11], materials[13], materials[14]]}]}
            mid = int(path.split("/")[-1])
            if "/material/get/" in path:
                return materials[mid]
            if "/purchase/" in path:
                assert materials[mid]["points"] == 0
                purchases.append(mid)
                materials[mid]["has_purchased"] = True
                return None
            if "/download/" in path:
                return f"https://storage.example/{mid}?signature=private"
            pytest.fail("Unexpected API call")

    monkeypatch.setattr("sss.sync.Share", FakeShare)
    plan = {"schema_version": 1, "code": "EE3316", "name": "Course", "term": {"id": 30}, "canvas_ids": [1], "teachers": ["Teacher"]}
    job = Sync({"share_token": "private-credential"}, plan, tmp_path)
    report = job.run(["share"])
    summary = report["sources"]["share"]["summary"]
    assert summary == {"courses": 2, "downloaded": 1 if wrong_size else 2, "skipped_paid": 1, "skipped_blocked": 1}
    assert purchases == [11] and sorted(downloads) == [11, 12]
    assert (job.root / "share/Notes-11/notes.zip.extracted/notes.txt").read_text() == "study notes"
    assert (job.root / "share/Notes-11/index.html").is_file()
    assert not list(job.root.rglob("*.json"))
    assert not list(job.root.rglob("*.zip"))
    exported = job.state.get("report")
    assert "private-credential" not in json.dumps(exported)
    if wrong_size:
        assert exported["status"] == "partial"
        assert exported["errors"][0]["message"] == "Download size mismatch"
        assert not list(job.root.rglob("reference.pdf"))
    else:
        assert exported["status"] == "complete"
        again = Sync({"share_token": "private-credential"}, plan, tmp_path).run(["share"])
        assert again["status"] == "complete"
        assert purchases == [11] and sorted(downloads) == [11, 12]
        assert all(a["status"] == "cached" for a in again["artifacts"])
