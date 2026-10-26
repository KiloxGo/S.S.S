import json
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from .canvas import Canvas, srt
from .communities import Reviews, Share, free_material, teacher_bucket
from .config import credential_values
from .http import SourceError
from .storage import Store, extract_archive, safe_name


def validate_plan(plan):
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise SourceError("Plan must be an object with schema_version = 1")
    for key in ("code", "name"):
        if not isinstance(plan.get(key), str) or not plan[key].strip():
            raise SourceError(f"Plan requires a nonempty {key}")
    if not isinstance(plan.get("term"), dict) or not plan["term"].get("id"):
        raise SourceError("Plan requires term.id to prevent cross-semester collisions")
    if not isinstance(plan.get("teachers"), list) or not all(isinstance(n, str) for n in plan["teachers"]):
        raise SourceError("Plan teachers must be a list of names")
    for key in ("canvas_ids", "reviews_ids", "share_ids"):
        ids = plan.get(key, [])
        if not isinstance(ids, list) or any(type(i) is not int or i <= 0 for i in ids) or len(ids) != len(set(ids)):
            raise SourceError(f"Plan {key} must contain unique positive integer IDs")
    if not plan.get("canvas_ids"):
        raise SourceError("Plan requires at least one Canvas ID")
    if not isinstance(plan.get("aliases", []), list) or any(not isinstance(a, str) for a in plan.get("aliases", [])):
        raise SourceError("Plan aliases must be a list of course codes")
    return plan


class Sync:
    def __init__(self, config, plan, output):
        self.config = config
        self.plan = validate_plan(plan)
        term = safe_name(str(plan["term"].get("name") or plan["term"]["id"]))
        self.root = Path(output).resolve() / term / safe_name(plan["code"] + "-" + plan["name"], 70)
        self.store = Store(self.root, credential_values(config))
        self.manifest = {"schema_version": 1, "course": plan, "started_at": datetime.now(timezone.utc).isoformat(),
                         "status": "running", "sources": {}, "artifacts": [], "errors": []}

    def checkpoint(self):
        self.store.json(self.root / "course.json", self.plan)
        self.store.json(self.root / "manifest.json", self.manifest)

    def attempt(self, scope, fn):
        try:
            return fn()
        except (SourceError, OSError, ValueError, KeyError, TypeError) as exc:
            # Do not persist exception strings from third-party libraries or response bodies.
            message = str(exc) if isinstance(exc, SourceError) else f"Local processing failed ({type(exc).__name__})"
            self.manifest["errors"].append({"scope": scope, "message": message})
            self.checkpoint()
            return None

    def artifact(self, http, url, path, *, size=None, updated=None):
        record = self.store.download(http, url, path, size, updated)
        self.manifest["artifacts"].append(record)
        self.checkpoint()
        folders = extract_archive(path)
        record["extracted"] = [str(Path(p).relative_to(self.root)).replace("\\", "/") for p in folders]
        return record

    def canvas(self):
        api = Canvas(self.config.get("canvas_token"))
        counts = {"files": 0, "assignments": 0}
        for cid in self.plan["canvas_ids"]:
            base = self.root / "canvas" / str(cid)
            folders = self.attempt(f"canvas/{cid}/folders", lambda: api.items(cid, "folders")) or []
            folder_map = {f["id"]: f for f in folders}

            def folder_path(fid, visited=None):
                visited = visited or set()
                if fid in visited:
                    raise SourceError("Cyclic Canvas folder hierarchy")
                f = folder_map.get(fid)
                if not f:
                    return Path()
                visited.add(fid)
                if not f.get("parent_folder_id"):
                    return Path()
                return folder_path(f["parent_folder_id"], visited) / safe_name(f"{fid}-{f['name']}", 45)

            files = self.attempt(f"canvas/{cid}/files", lambda: api.items(cid, "files"))
            self.store.json(base / "folders.json", folders)
            if files is not None:
                self.store.json(base / "files.json", files)
                for f in files:
                    def fetch(f=f):
                        if f.get("locked_for_user") or f.get("hidden_for_user"):
                            raise SourceError("Canvas file is locked or hidden for this user")
                        path = base / "files" / folder_path(f.get("folder_id")) / safe_name(f"{f['id']}-{f.get('display_name') or f['filename']}")
                        return self.artifact(api.http, f["url"], path, size=f.get("size"), updated=f.get("updated_at"))
                    if self.attempt(f"canvas/{cid}/file/{f['id']}", fetch):
                        counts["files"] += 1
            assignments = self.attempt(f"canvas/{cid}/assignments", lambda: api.items(cid, "assignments", **{"include[]": "submission"}))
            if assignments is not None:
                self.store.json(base / "assignments.json", assignments)
                for a in assignments:
                    directory = base / "assignments" / safe_name(f"{a['id']}-{a['name']}", 65)
                    self.store.json(directory / "assignment.json", a)
                    self.store.text(directory / "description.html", a.get("description") or "")
                    text = BeautifulSoup(a.get("description") or "", "html.parser").get_text("\n", strip=True)
                    self.store.text(directory / "README.md", f"# {a['name']}\n\n截止时间：{a.get('due_at') or '未设置'}\n\n{text}\n")
                    attachments = {f["id"]: f for f in (a.get("submission") or {}).get("attachments", [])}
                    for fid in api.linked_files(a.get("description")):
                        f = self.attempt(f"canvas/{cid}/assignment/{a['id']}/file/{fid}", lambda fid=fid: api.file(fid))
                        if f:
                            attachments[fid] = f
                    for fid, f in attachments.items():
                        path = directory / "attachments" / safe_name(f"{fid}-{f.get('display_name') or f.get('filename') or 'attachment'}")
                        self.attempt(f"canvas/{cid}/assignment/{a['id']}/download/{fid}",
                                     lambda f=f, path=path: self.artifact(api.http, f["url"], path, size=f.get("size"), updated=f.get("updated_at")))
                    counts["assignments"] += 1
            self.checkpoint()
        return counts

    def subtitles(self):
        api = Canvas(self.config.get("canvas_token"))
        counts = {"recordings": 0, "subtitles": 0, "unavailable": 0}
        for cid in self.plan["canvas_ids"]:
            launched = self.attempt(f"subtitles/{cid}/launch", lambda: api.videos(cid))
            if launched is None:
                continue
            http, videos = launched
            base = self.root / "canvas" / str(cid) / "subtitles"
            self.store.json(base / "recordings.json", videos)
            counts["recordings"] += len(videos)
            for video in videos:
                vid = str(video.get("id") or video.get("courseId") or video.get("courId"))
                def fetch(video=video, vid=vid):
                    data = api.subtitles(http, vid)
                    lines = data.get("beforeAssemblyList") or data.get("afterAssemblyList") or []
                    name = safe_name(f"{vid}-{video.get('courBeginTime') or video.get('subjName') or 'recording'}", 70)
                    self.store.json(base / (name + ".json"), data)
                    if not lines:
                        counts["unavailable"] += 1
                        return
                    self.store.text(base / (name + ".srt"), srt(lines))
                    self.store.text(base / (name + ".txt"), "\n".join(line.get("res", "") for line in lines) + "\n")
                    counts["subtitles"] += 1
                self.attempt(f"subtitles/{cid}/{vid}", fetch)
            self.checkpoint()
        return counts

    def reviews(self):
        api = Reviews(self.config.get("reviews_cookie", ""))
        courses = api.courses(self.plan)
        self.store.json(self.root / "reviews" / "courses.json", courses)
        if not courses:
            raise SourceError("No exact course code match in 选课社区; add aliases or reviews_ids to the plan")
        buckets = {key: [] for key in ("own-teachers", "other-teachers", "unknown-teachers")}
        count = 0
        for course in courses:
            cid = course["id"]
            def fetch(cid=cid):
                detail = api.detail(cid)
                reviews = api.reviews(cid)
                teachers = detail.get("teacher_group") or [detail.get("main_teacher")]
                bucket = teacher_bucket(teachers, self.plan["teachers"])
                self.store.json(self.root / "reviews" / bucket / f"{cid}-course.json", detail)
                self.store.json(self.root / "reviews" / bucket / f"{cid}-reviews.json", reviews)
                for review in reviews:
                    buckets[bucket].append({"course_id": cid, "teachers": teachers, "review": review})
                return len(reviews)
            count += self.attempt(f"reviews/{cid}", fetch) or 0
        for bucket, records in buckets.items():
            base = self.root / "reviews" / bucket
            self.store.json(base / "all.json", records)
            sections = [f"# {bucket}\n"]
            for record in records:
                review = record["review"]
                teachers = "、".join(t["name"] for t in record["teachers"] if t)
                content = review.get("content") or review.get("comment") or review.get("text") or ""
                sections.append(f"## {teachers} · 评价 {review['id']}\n\n来源：https://course.sjtu.plus/review/{review['id']}\n\n{content}\n")
            self.store.text(base / "README.md", "\n".join(sections))
        return {"courses": len(courses), "reviews": count, **{k: len(v) for k, v in buckets.items()}}

    def share(self):
        api = Share(self.config.get("share_token"))
        courses = api.courses(self.plan)
        self.store.json(self.root / "share" / "courses.json", courses)
        if not courses:
            raise SourceError("No exact course code match in 传承交大; add aliases or share_ids to the plan")
        counts = {"courses": len(courses), "downloaded": 0, "skipped_paid": 0, "skipped_blocked": 0}
        seen = set()
        for course in courses:
            cid = course["id"]
            materials = self.attempt(f"share/{cid}/materials", lambda: api.materials(cid))
            if materials is None:
                continue
            self.store.json(self.root / "share" / f"{cid}-materials.json", materials)
            for material in materials:
                mid = material["id"]
                if mid in seen:
                    continue
                seen.add(mid)
                if any(material.get(k) for k in ("block", "blocked", "is_blocked")):
                    counts["skipped_blocked"] += 1
                    continue
                if not free_material(material):
                    counts["skipped_paid"] += 1
                    continue
                def fetch(mid=mid, material=material):
                    detail, url = api.acquire_free(mid)
                    group = detail.get("class") or material.get("class") or {}
                    teacher = (group.get("teacher") or {}).get("name") or "unclassified"
                    directory = self.root / "share" / safe_name(f"{group.get('id') or 'none'}-{teacher}", 50) / str(mid)
                    self.store.json(directory / "material.json", detail)
                    # file_name carries the actual archive extension; display name may not.
                    filename = detail.get("file_name") or material.get("file_name") or detail.get("name") or str(mid)
                    return self.artifact(api.http, url, directory / safe_name(filename), updated=detail.get("updated_at"))
                if self.attempt(f"share/{cid}/material/{mid}", fetch):
                    counts["downloaded"] += 1
        return counts

    def run(self, sources):
        self.checkpoint()
        for source in sources:
            print(f"[{source}] collecting...", flush=True)
            before = len(self.manifest["errors"])
            summary = self.attempt(source, getattr(self, source))
            status = "complete" if len(self.manifest["errors"]) == before else "partial"
            self.manifest["sources"][source] = {"status": status, "summary": summary}
            self.checkpoint()
            print(f"[{source}] {status}: {json.dumps(summary, ensure_ascii=False)}", flush=True)
        self.manifest["status"] = "partial" if self.manifest["errors"] else "complete"
        self.manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        self.checkpoint()
        return self.manifest
