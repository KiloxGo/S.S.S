import hashlib
import json
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .canvas import CANVAS_BASE, Canvas, srt
from .communities import Reviews, Share, free_material, teacher_bucket
from .config import credential_values
from .content import html_markdown, link, sanitize_html, write_pair
from .http import SourceError
from .storage import State, Store, archive_kind, extract_archive, safe_name
from .study import write_study_files


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


def course_directory(plan, output):
    term = safe_name(str(plan["term"].get("name") or plan["term"]["id"]))
    return Path(output).resolve() / term / safe_name(plan["code"] + "-" + plan["name"], 70)


class Sync:
    def __init__(self, config, plan, output, on_progress=None):
        self.config = config
        self.plan = validate_plan(plan)
        self.output = Path(output).resolve()
        self.root = course_directory(plan, self.output)
        self.on_progress = on_progress
        key = hashlib.sha256(str(self.root).encode()).hexdigest()[:16]
        self.state = State(self.output.parent / ".local" / "state" / key)
        self.store = Store(self.root, credential_values(config), self.state)
        self.manifest = {"schema_version": 2, "course": plan, "started_at": datetime.now(timezone.utc).isoformat(),
                         "status": "running", "sources": {}, "artifacts": [], "errors": []}
        self.current_source = ""

    def checkpoint(self):
        self.state.put("running-report", self.store.clean(self.manifest))
        if self.on_progress:
            self.on_progress(self.manifest)

    def attempt(self, scope, fn):
        try:
            return fn()
        except (SourceError, OSError, ValueError, KeyError, TypeError) as exc:
            message = str(exc) if isinstance(exc, SourceError) else f"Local processing failed ({type(exc).__name__})"
            self.manifest["errors"].append({"scope": scope, "message": message})
            self.checkpoint()
            return None

    def clear(self, path):
        assert path.resolve().is_relative_to(self.root.resolve())
        if path.exists():
            shutil.rmtree(path)

    def artifact(self, http, url, path, *, size=None, updated=None, key=None):
        record = self.store.download(http, url, path, size, updated, key=key)
        record["source"] = self.current_source
        self.manifest["artifacts"].append(record)
        self.checkpoint()
        folders = extract_archive(path, state=self.state)
        if folders:
            contents = [p for p in Path(folders[0]).rglob("*") if p.is_file() and not archive_kind(p)]
            record["contents"] = [str(p.relative_to(self.root)).replace("\\", "/") for p in contents]
            path.unlink()
            for child in Path(folders[0]).rglob("*"):
                if child.is_file() and archive_kind(child) and child.with_name(child.name + ".extracted").is_dir():
                    child.unlink()
        else:
            record["contents"] = [record["path"]]
        return record

    def canvas_base(self, cid):
        space = next((s for s in self.plan.get("canvas_spaces", []) if s["id"] == cid), {})
        names = space.get("teachers") or self.plan.get("teachers") or ["教师未标注"]
        target = self.root / "canvas" / safe_name(f"{cid}-{'、'.join(names)}", 65)
        for old in (self.root / "canvas").glob(f"{cid}*"):
            if old.is_dir() and old != target and not target.exists():
                old.rename(target)
        return target

    def file_artifact(self, api, f, directory):
        # Canvas hides attachment-only files from Files navigation while allowing assignment links.
        if f.get("locked_for_user"):
            raise SourceError(f"文件尚未发布或不可访问：{f.get('display_name') or f.get('filename') or f['id']}")
        filename = safe_name(f"{f['id']}-{f.get('display_name') or f.get('filename') or 'attachment'}")
        return self.artifact(api.http, f["url"], directory / filename, size=f.get("size"), updated=f.get("updated_at"), key=f"canvas:file:{f['id']}")

    def file_links(self, directory, record):
        return [link(directory / "README.md", self.root / path) for path in record["contents"]]

    def assignment(self, api, cid, a, base):
        directory = base / "assignments" / safe_name(f"{a['name']}-{a['id']}", 65)
        question = directory / "question"
        question.mkdir(parents=True, exist_ok=True)
        description = a.get("description") or ""
        question_files = {f["id"]: f for f in a.get("attachments") or []}
        for fid in api.linked_files(description):
            f = self.attempt(f"canvas/{cid}/assignment/{a['id']}/question/{fid}", lambda fid=fid: api.file(fid))
            if f:
                question_files[fid] = f
        records, file_paths = [], {}
        for fid, f in question_files.items():
            record = self.attempt(f"canvas/{cid}/assignment/{a['id']}/question/{fid}", lambda f=f: self.file_artifact(api, f, question))
            if record:
                records.append(record)
                if record["contents"]:
                    file_paths[fid] = self.root / record["contents"][0]
        soup = BeautifulSoup(sanitize_html(description), "html.parser")
        for node in soup.select("[href], [src]"):
            for attr in ("href", "src"):
                if not node.get(attr):
                    continue
                url = urljoin(CANVAS_BASE, node[attr])
                if urlsplit(url).hostname != urlsplit(CANVAS_BASE).hostname:
                    continue
                match = re.search(r"/files/(\d+)(?:/|$)", urlsplit(url).path)
                if match and int(match[1]) in file_paths:
                    node[attr] = link(question / "README.md", file_paths[int(match[1])], "file").partition("](")[2][:-1]
        body = html_markdown(str(soup))
        if not body:
            body = "平台未提供题面正文。" + ("题目见下方附件。" if records else "暂无可用题目附件。")
        text = f"# {a['name']}\n\n{body}\n"
        if records:
            text += "\n## 题目附件\n\n" + "\n".join("- " + item for record in records for item in self.file_links(question, record)) + "\n"
        submission = a.get("submission") or {}
        submitted_files = submission.get("attachments") or []
        submitted_body, submitted_url = submission.get("body") or "", submission.get("url") or ""
        if submitted_files or submitted_body.strip() or submitted_url.strip():
            solution = directory / "solution"
            solution_text = f"# {a['name']} · 我的提交\n\n{html_markdown(submitted_body)}\n"
            if submitted_url:
                solution_text += f"\n提交链接：{submitted_url}\n"
            for f in submitted_files:
                record = self.attempt(f"canvas/{cid}/assignment/{a['id']}/solution/{f['id']}", lambda f=f: self.file_artifact(api, f, solution))
                if record:
                    solution_text += "\n" + "\n".join("- " + item for item in self.file_links(solution, record)) + "\n"
            write_pair(self.store, solution, a["name"] + " · 我的提交", solution_text)
            text += "\n" + link(question / "README.md", solution / "index.html", "查看我的提交（solution）") + "\n"
        write_pair(self.store, question, a["name"], text)
        return question / "index.html"

    def canvas(self):
        api = Canvas(self.config.get("canvas_token"))
        counts = {"files": 0, "assignments": 0}
        for cid in self.plan["canvas_ids"]:
            base = self.canvas_base(cid)
            folders = self.attempt(f"canvas/{cid}/folders", lambda: api.items(cid, "folders")) or []
            folder_map = {f["id"]: f for f in folders}
            def folder_path(fid, visited=None):
                visited = visited or set()
                if fid in visited:
                    raise SourceError("Cyclic Canvas folder hierarchy")
                f = folder_map.get(fid)
                if not f or not f.get("parent_folder_id"):
                    return Path()
                visited.add(fid)
                return folder_path(f["parent_folder_id"], visited) / safe_name(f["name"], 45)
            files = self.attempt(f"canvas/{cid}/files", lambda: api.items(cid, "files"))
            if files is not None:
                self.clear(base / "files")
                entries = []
                for f in files:
                    record = self.attempt(f"canvas/{cid}/file/{f['id']}", lambda f=f: self.file_artifact(api, f, base / "files" / folder_path(f.get("folder_id"))))
                    if record:
                        counts["files"] += 1
                        entries.extend(self.file_links(base / "files", record))
                write_pair(self.store, base / "files", "课程文件", "# 课程文件\n\n" + "\n".join("- " + e for e in entries))
            assignments = self.attempt(f"canvas/{cid}/assignments", lambda: api.items(cid, "assignments", **{"include[]": "submission"}))
            if assignments is not None:
                self.clear(base / "assignments")
                entries = []
                for a in assignments:
                    page = self.attempt(f"canvas/{cid}/assignment/{a['id']}", lambda a=a: self.assignment(api, cid, a, base))
                    if page:
                        counts["assignments"] += 1
                        entries.append(link(base / "assignments/README.md", page, a["name"]))
                write_pair(self.store, base / "assignments", "作业", "# 作业\n\n题面与我的提交分别位于 question 和 solution。\n\n" + "\n".join("- " + e for e in entries))
        return counts

    def subtitles(self):
        api = Canvas(self.config.get("canvas_token"))
        counts = {"recordings": 0, "subtitles": 0, "unavailable": 0}
        for cid in self.plan["canvas_ids"]:
            launched = self.attempt(f"subtitles/{cid}/launch", lambda: api.videos(cid))
            if launched is None:
                continue
            http, videos = launched
            base = self.canvas_base(cid) / "subtitles"
            self.clear(base)
            base.mkdir(parents=True, exist_ok=True)
            entries = self.manifest.setdefault("subtitle_entries", {})[str(cid)] = []
            counts["recordings"] += len(videos)
            for video in videos:
                vid = str(video.get("id") or video.get("courseId") or video.get("courId"))
                def fetch(video=video, vid=vid):
                    data = api.subtitles(http, vid)
                    lines = data.get("beforeAssemblyList") or data.get("afterAssemblyList") or []
                    if not lines:
                        counts["unavailable"] += 1
                        return
                    name = safe_name(f"{video.get('courBeginTime') or video.get('subjName') or '录播'}-{vid}", 70)
                    self.store.text(base / (name + ".srt"), srt(lines))
                    entries.append({"path": str((base / (name + ".srt")).relative_to(self.root)).replace("\\", "/"), "label": name})
                    counts["subtitles"] += 1
                self.attempt(f"subtitles/{cid}/{vid}", fetch)
        return counts

    def reviews(self):
        api = Reviews(self.config.get("reviews_cookie", ""))
        courses = api.courses(self.plan)
        if not courses:
            raise SourceError("No exact course code match in 选课社区; add aliases or reviews_ids to the plan")
        self.clear(self.root / "reviews")
        buckets = {key: [] for key in ("own-teachers", "other-teachers", "unknown-teachers")}
        count = 0
        for course in courses:
            cid = course["id"]
            def fetch(cid=cid):
                detail = api.detail(cid)
                teachers = detail.get("teacher_group") or [detail.get("main_teacher")]
                bucket = teacher_bucket(teachers, self.plan["teachers"])
                names = "、".join(t.get("name", "") for t in teachers if t) or "教师未标注"
                reviews = api.reviews(cid)
                for review in reviews:
                    content = review.get("content") or review.get("comment") or review.get("text") or ""
                    buckets[bucket].append(f"## {names}\n\n{content}\n\n[原评价](https://course.sjtu.plus/review/{review['id']})\n")
                return len(reviews)
            count += self.attempt(f"reviews/{cid}", fetch) or 0
        labels = {"own-teachers": "本班教师评价", "other-teachers": "其他教师评价", "unknown-teachers": "教师未标注的评价"}
        for bucket, sections in buckets.items():
            write_pair(self.store, self.root / "reviews" / bucket, labels[bucket], f"# {labels[bucket]}\n\n" + ("\n---\n\n".join(sections) if sections else "暂无评价。"))
        return {"courses": len(courses), "reviews": count, **{k: len(v) for k, v in buckets.items()}}

    def share(self):
        api = Share(self.config.get("share_token"))
        courses = api.courses(self.plan)
        if not courses:
            raise SourceError("No exact course code match in 传承交大; add aliases or share_ids to the plan")
        self.clear(self.root / "share")
        counts = {"courses": len(courses), "downloaded": 0, "skipped_paid": 0, "skipped_blocked": 0}
        seen, entries = set(), []
        for course in courses:
            cid = course["id"]
            materials = self.attempt(f"share/{cid}/materials", lambda: api.materials(cid))
            if materials is None:
                continue
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
                    title = detail.get("name") or material.get("name") or detail.get("file_name") or str(mid)
                    directory = self.root / "share" / safe_name(f"{title}-{mid}", 75)
                    filename = detail.get("file_name") or material.get("file_name") or title
                    if filename.casefold() in ("readme.md", "index.html"):
                        filename = "asset-" + filename
                    record = self.artifact(api.http, url, directory / safe_name(filename), size=detail.get("size"), updated=detail.get("updated_at"), key=f"share:material:{mid}")
                    text = f"# {title}\n\n{detail.get('description') or ''}\n\n" + "\n".join("- " + item for item in self.file_links(directory, record))
                    text += f"\n\n[原始上传页面](https://share.dyweb.sjtu.cn/course/{cid}/material/{mid})\n"
                    write_pair(self.store, directory, title, text)
                    entries.append(link(self.root / "share/README.md", directory / "index.html", title))
                    return record
                if self.attempt(f"share/{cid}/material/{mid}", fetch):
                    counts["downloaded"] += 1
        write_pair(self.store, self.root / "share", "传承交大参考资料", "# 传承交大参考资料\n\n" + "\n".join("- " + e for e in entries))
        return counts

    def index(self):
        write_study_files(self.store, self.root, self.plan)
        text = f"# {self.plan['code']} · {self.plan['name']}\n\n## AI 学习入口\n\n先读 [AGENTS.md](AGENTS.md) 了解学习约定，再读本页的来源与同步结果；目录结构见 [SCHEMA.md](SCHEMA.md)，具体任务见 [PROMPTS.md](PROMPTS.md)。Claude 可从 [CLAUDE.md](CLAUDE.md) 开始。\n\n个人与 AI 学习笔记放在 `notes/`，后续同步会保留。\n\n## Canvas\n\n"
        for base in sorted((self.root / "canvas").glob("*")):
            if not base.is_dir():
                continue
            section = f"# {base.name}\n\n"
            for label, subdir in (("课件与课程文件", "files"), ("作业题目与我的提交", "assignments")):
                page = base / subdir / "index.html"
                if page.exists():
                    section += "- " + link(base / "README.md", page, label) + "\n"
            entries = self.manifest.get("subtitle_entries", {}).get(base.name.split("-", 1)[0])
            if entries is None:
                entries = [{"path": str(p.relative_to(self.root)), "label": p.stem} for p in sorted((base / "subtitles").glob("*.srt"))]
            for entry in entries:
                section += "- " + link(base / "README.md", self.root / entry["path"], entry["label"]) + "\n"
            write_pair(self.store, base, base.name, section)
            text += "- " + link(self.root / "README.md", base / "index.html", base.name) + "\n"
        text += "\n## 参考资料与评价\n\n"
        for label, relative in (("传承交大资料", "share/index.html"), ("本班教师评价", "reviews/own-teachers/index.html"), ("其他教师评价", "reviews/other-teachers/index.html"), ("教师未标注的评价", "reviews/unknown-teachers/index.html")):
            if (self.root / relative).exists():
                text += "- " + link(self.root / "README.md", self.root / relative, label) + "\n"
        text += "\n## 本次同步\n\n"
        labels = {"canvas": "Canvas 课程资料", "subtitles": "录播字幕", "share": "传承交大资料", "reviews": "选课社区评价"}
        fields = {"files": "份文件", "assignments": "份作业", "recordings": "次录播", "subtitles": "份字幕", "unavailable": "次尚无字幕", "courses": "个课程条目", "downloaded": "份免费资料", "skipped_paid": "份收费资料已跳过", "skipped_blocked": "份屏蔽资料已跳过", "reviews": "条评价", "own-teachers": "条本班教师评价", "other-teachers": "条其他教师评价", "unknown-teachers": "条教师未标注的评价"}
        for source, result in self.manifest["sources"].items():
            summary = "，".join(f"{value} {fields.get(key, key)}" for key, value in (result.get("summary") or {}).items() if value)
            status = "已完成" if result["status"] == "complete" else "部分完成"
            text += f"- {labels.get(source, source)}：{status} · {summary or '暂无结果'}\n"
        if self.manifest["errors"]:
            text += "\n未能取得的内容：\n\n" + "\n".join(f"- {e['message']}（{e['scope']}）" for e in self.manifest["errors"]) + "\n"
        write_pair(self.store, self.root, self.plan["name"], text)

    def run(self, sources):
        final_root = self.root
        legacy = (final_root / "manifest.json").exists()
        if legacy:
            self.state.import_legacy(final_root)
        previous = self.state.get("report", {})
        self.manifest["subtitle_entries"] = previous.get("subtitle_entries", {})
        self.manifest["sources"] = {k: v for k, v in previous.get("sources", {}).items() if k not in sources}
        self.manifest["errors"] = [e for e in previous.get("errors", []) if e["scope"].split("/")[0] not in sources]
        self.manifest["artifacts"] = [a for a in previous.get("artifacts", []) if a.get("source") not in sources]
        with tempfile.TemporaryDirectory(prefix="render-", dir=self.state.directory) as temp:
            self.root = Path(temp) / final_root.name
            if final_root.exists() and not legacy:
                shutil.copytree(final_root, self.root)
            else:
                self.root.mkdir()
            self.store = Store(self.root, credential_values(self.config), self.state)
            for source in sources:
                self.current_source = source
                self.manifest["active_source"] = source
                self.checkpoint()
                print(f"[{source}] collecting...", flush=True)
                before = len(self.manifest["errors"])
                summary = self.attempt(source, getattr(self, source))
                status = "complete" if len(self.manifest["errors"]) == before else "partial"
                self.manifest["sources"][source] = {"status": status, "summary": summary}
                self.checkpoint()
                print(f"[{source}] {status}: {json.dumps(summary, ensure_ascii=False)}", flush=True)
            self.manifest["status"] = "partial" if self.manifest["errors"] else "complete"
            self.manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
            self.index()
            if final_root.exists() and not legacy:
                self.store.restore_referenced_files(final_root)
            self.manifest["duplicates"] = self.store.deduplicate()
            for record in self.manifest["artifacts"]:
                record["contents"] = [self.manifest["duplicates"].get(path, path) for path in record.get("contents", [])]
            for entries in self.manifest["subtitle_entries"].values():
                for entry in entries:
                    entry["path"] = self.manifest["duplicates"].get(entry["path"], entry["path"])
            backup = self.state.directory / ("legacy" if legacy else "previous")
            assert backup.resolve().is_relative_to(self.state.directory)
            assert final_root.resolve().is_relative_to(self.output)
            assert self.root.resolve().is_relative_to(self.state.directory)
            if backup.exists():
                shutil.rmtree(backup)
            final_root.parent.mkdir(parents=True, exist_ok=True)
            if final_root.exists():
                shutil.move(str(final_root), str(backup))
            try:
                shutil.move(str(self.root), str(final_root))
            except OSError:
                if backup.exists() and not final_root.exists():
                    shutil.move(str(backup), str(final_root))
                raise
        self.root = final_root
        self.store = Store(self.root, credential_values(self.config), self.state)
        self.state.put("report", self.store.clean(self.manifest))
        return self.manifest
