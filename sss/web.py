"""Loopback-only UI for the existing collector. Secrets never leave the backend."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, closing
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from dotenv import set_key
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .canvas import Canvas, group_courses
from .communities import Reviews, Share
from .config import credential_values, load_config
from .http import SourceError
from .storage import Store, safe_name
from .sync import Sync, course_directory, validate_plan
from .study import prompt_catalog

FIELDS = {"canvas_token": "canvas", "share_token": "share", "reviews_cookie": "reviews"}
STATIC_DIR = Path(__file__).parent / "web_static"


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    canvas_token: str | None = Field(default=None, max_length=16384)
    share_token: str | None = Field(default=None, max_length=16384)
    reviews_cookie: str | None = Field(default=None, max_length=16384)

    @field_validator("canvas_token", "share_token", "reviews_cookie")
    @classmethod
    def single_line(cls, value):
        if value is not None and any(c in value for c in ("\r", "\n", "\0")):
            raise ValueError("Credentials must fit on one line")
        return value.strip() if value is not None else None


class ClipboardText(BaseModel):
    text: str = Field(min_length=1, max_length=32768)


def public_report(report):
    return {
        "status": report.get("status", "idle"),
        "active_source": report.get("active_source"),
        "sources": report.get("sources", {}),
        "errors": report.get("errors", []),
        "finished_at": report.get("finished_at"),
        "files_received": len(report.get("artifacts", [])),
    }


def open_folder(path):
    if sys.platform == "win32":
        os.startfile(str(path))
    else:
        command = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.run([command, str(path)], check=True, capture_output=True, timeout=10)


class CourseService:
    def __init__(self, config_path, env_path, output):
        self.config_path = Path(config_path).resolve()
        self.env_path = Path(env_path).resolve()
        self.output = Path(output).resolve()
        self.lock = threading.RLock()
        self.credentials_lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sss-sync")
        self.courses = {}
        self.jobs = {}
        self.checks = {}

    def config(self):
        with self.credentials_lock:
            return load_config(self.config_path, self.env_path)

    def settings(self):
        config = self.config()
        with self.lock:
            return {source: {
                "configured": bool(config.get(key)),
                "managed": "SSS_" + key.upper() in os.environ,
                **self.checks.get(source, {}),
            } for key, source in FIELDS.items()}

    def save_credentials(self, values):
        updates = {key: value for key, value in values.items() if value is not None}
        if not updates:
            return
        for key in updates:
            if "SSS_" + key.upper() in os.environ:
                raise HTTPException(409, "该凭据由进程环境变量提供，请在启动服务前修改。")
        with self.credentials_lock:
            self.env_path.parent.mkdir(parents=True, exist_ok=True)
            contents = self.env_path.read_text(encoding="utf-8-sig") if self.env_path.exists() else ""
            # Atomic replacement preserves other settings/comments and restricts file access.
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.env_path.parent,
                                             prefix=".sss-env-", delete=False) as f:
                temporary = Path(f.name)
                f.write(contents)
            try:
                for key, value in updates.items():
                    set_key(temporary, "SSS_" + key.upper(), value, quote_mode="always")
                temporary.chmod(0o600)
                os.replace(temporary, self.env_path)
            finally:
                temporary.unlink(missing_ok=True)
        with self.lock:
            self.checks = {}
            self.courses = {}

    def authenticate(self):
        config = self.config()
        checks = {
            "canvas": lambda: Canvas(config["canvas_token"]).http.json("GET", "/api/v1/users/self/profile"),
            "share": lambda: Share(config["share_token"]).call("GET", "/api/v1/user/profile"),
            "reviews": lambda: Reviews(config["reviews_cookie"]).http.json("GET", "/api/auth/me"),
        }

        def check(item):
            key, source = item
            if not config.get(key):
                return source, {"status": "missing", "message": "未配置"}
            try:
                if not checks[source]():
                    raise SourceError("登录状态无效")
                return source, {"status": "ok", "message": "已连接"}
            except SourceError as exc:
                clean = Store(self.output, credential_values(config)).clean(str(exc))
                return source, {"status": "error", "message": clean}

        with ThreadPoolExecutor(max_workers=3) as pool:
            result = dict(pool.map(check, FIELDS.items()))
        with self.lock:
            self.checks = result
        return result

    def load_courses(self):
        config = self.config()
        courses = group_courses(Canvas(config.get("canvas_token")).courses())
        with self.lock:
            self.courses = {str(c["canvas_ids"][0]): c for c in courses}
        return self.snapshot()

    def course(self, key):
        with self.lock:
            course = self.courses.get(key)
            if course is None:
                raise HTTPException(404, "未找到课程，请重新加载课程列表。")
            return deepcopy(course)

    def previous(self, course):
        root = self.directory(course)
        digest = hashlib.sha256(str(root).encode()).hexdigest()[:16]
        db = self.output.parent / ".local" / "state" / digest / "state.sqlite"
        if not db.is_file():
            return {}
        try:
            with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as connection:
                row = connection.execute("SELECT value FROM records WHERE key = 'report'").fetchone()
            return public_report(json.loads(row[0])) if row else {}
        except (sqlite3.Error, ValueError, TypeError):
            return {}

    def snapshot(self):
        with self.lock:
            courses, jobs = deepcopy(self.courses), deepcopy(self.jobs)
        results = []
        for key, course in courses.items():
            previous = self.previous(course)
            results.append({
                **course, "key": key,
                "local": self.directory(course).is_dir(),
                "sync": {**previous, **jobs.get(key, {})},
            })
        return {"courses": results, "output": str(self.output)}

    def plan(self, course):
        path = self.output.parent / ".local" / (safe_name(course["code"]) + ".json")
        if path.is_file():
            saved = validate_plan(json.loads(path.read_text(encoding="utf-8")))
            if (saved["code"] == course["code"] and saved["term"]["id"] == course["term"]["id"]
                    and set(saved["canvas_ids"]) == set(course["canvas_ids"])):
                return saved
        return {"schema_version": 1, **course, "aliases": [], "share_ids": [], "reviews_ids": []}

    def directory(self, course):
        return course_directory(self.plan(course), self.output)

    def start_sync(self, key):
        course = self.course(key)
        config = self.config()
        if not config.get("canvas_token"):
            raise HTTPException(400, "请先配置 Canvas Token。")
        with self.lock:
            if self.jobs.get(key, {}).get("status") in ("queued", "running"):
                raise HTTPException(409, "这门课程已经在同步队列中。")
            self.jobs[key] = {"status": "queued", "errors": [], "sources": {}}
            self.executor.submit(self.run_sync, key, course, config)

    def run_sync(self, key, course, config):
        def progress(report):
            clean = Store(self.output, credential_values(config)).clean(public_report(report))
            with self.lock:
                self.jobs[key] = {**clean, "status": "running"}

        with self.lock:
            self.jobs[key]["status"] = "running"
        job = None
        try:
            sources = ["canvas", "subtitles"]
            sources += [source for field, source in FIELDS.items() if source != "canvas" and config.get(field)]
            job = Sync(config, self.plan(course), self.output, on_progress=progress)
            report = job.run(sources)
            with self.lock:
                self.jobs[key] = job.store.clean(public_report(report))
        except Exception as exc:
            message = str(exc) if isinstance(exc, SourceError) else "同步中断，请重试；已完成的本地资料会保留。"
            clean = Store(self.output, credential_values(config)).clean(message)
            with self.lock:
                self.jobs[key] = {"status": "failed", "errors": [{"scope": "sync", "message": clean}],
                                  "finished_at": datetime.now(timezone.utc).isoformat()}
        finally:
            if job is not None:
                job.state.db.close()


def create_app(config_path=Path(".local/credentials.json"), env_path=Path(".env"),
               output=Path("data"), static_dir=None, desktop=None, clipboard=None):
    service = CourseService(config_path, env_path, output)
    static = Path(static_dir) if static_dir is not None else STATIC_DIR

    @asynccontextmanager
    async def lifespan(app):
        yield
        service.executor.shutdown(wait=True, cancel_futures=True)

    app = FastAPI(title="S.S.S", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.service = service
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        origin = request.headers.get("origin")
        if (request.headers.get("sec-fetch-site") == "cross-site"
                or (origin and origin != str(request.base_url).rstrip("/"))):
            return JSONResponse({"detail": "只允许从本地 S.S.S 页面访问。"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        # Pydantic's default response includes the supplied value, which may be a token.
        return JSONResponse({"detail": "输入格式有误；每项凭据应为一行文本。"}, status_code=422)

    @app.exception_handler(SourceError)
    async def source_error(request, exc):
        clean = Store(service.output, credential_values(service.config())).clean(str(exc))
        return JSONResponse({"detail": clean}, status_code=502)

    @app.exception_handler(OSError)
    @app.exception_handler(ValueError)
    async def local_error(request, exc):
        return JSONResponse({"detail": "本地配置或文件处理失败，请检查配置文件和目录权限。"}, status_code=500)

    @app.get("/api/state")
    def state():
        return {"settings": service.settings(), "desktop": desktop, **service.snapshot()}

    @app.post("/api/desktop/copy")
    def native_copy(payload: ClipboardText):
        if desktop is None or clipboard is None:
            raise HTTPException(404, "桌面功能不可用。")
        try:
            clipboard(payload.text)
        except Exception:
            raise HTTPException(503, "无法访问系统剪贴板，请展开提示词后手动复制。") from None
        return {"copied": True}

    @app.post("/api/desktop/open-data")
    def data_folder():
        if desktop is None:
            raise HTTPException(404, "桌面功能不可用。")
        try:
            open_folder(service.output.parent)
        except (OSError, subprocess.SubprocessError):
            raise HTTPException(503, "无法打开数据文件夹。") from None
        return {"opened": True}

    @app.post("/api/connect")
    def connect(credentials: Credentials):
        service.save_credentials(credentials.model_dump())
        checks = service.authenticate()
        if checks["canvas"]["status"] == "ok":
            service.load_courses()
        else:
            with service.lock:
                service.courses = {}
        return {"settings": service.settings(), **service.snapshot()}

    @app.get("/api/courses")
    def courses():
        return service.load_courses()

    @app.get("/api/prompts")
    def prompts(course: str | None = None):
        if course is None:
            return {"prompts": prompt_catalog()}
        selected = service.course(course)
        return {"prompts": prompt_catalog(service.plan(selected), service.directory(selected))}

    @app.post("/api/courses/{key}/sync", status_code=202)
    def sync(key: str):
        service.start_sync(key)
        return {"accepted": True}

    @app.post("/api/courses/{key}/open")
    def folder(key: str):
        path = service.directory(service.course(key))
        if not path.is_dir():
            raise HTTPException(409, "请先同步课程，完成后即可打开资料文件夹。")
        try:
            open_folder(path)
        except (OSError, subprocess.SubprocessError):
            raise HTTPException(503, "无法打开文件管理器，请检查桌面环境。") from None
        return {"opened": True}

    @app.get("/")
    def index():
        path = static / "index.html"
        if not path.is_file():
            raise HTTPException(503, "前端尚未构建，请运行 npm --prefix web install 和 npm --prefix web run build。")
        return FileResponse(path)

    @app.get("/favicon.svg")
    def favicon():
        path = static / "favicon.svg"
        if not path.is_file():
            raise HTTPException(404, "Not found")
        return FileResponse(path, media_type="image/svg+xml")

    app.mount("/assets", StaticFiles(directory=static / "assets", check_dir=False), name="assets")
    return app


def serve(config_path, env_path, output, port):
    import uvicorn

    if not 1 <= port <= 65535:
        raise SourceError("端口应在 1–65535 之间。")
    print(f"S.S.S: http://127.0.0.1:{port}", flush=True)
    uvicorn.run(create_app(config_path, env_path, output), host="127.0.0.1", port=port,
                access_log=False, log_level="warning")
