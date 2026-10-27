import hashlib
import json
import re
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit

from .http import SourceError


class State:
    """Private bookkeeping, stored outside the study-material directory."""
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.blobs = self.directory / "cache"
        self.blobs.mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.directory / "state.sqlite")
        self.db.execute("CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM records WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO records VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))
        self.db.commit()

    def import_legacy(self, root):
        for receipt in root.rglob("*.receipt.json"):
            path = receipt.with_name(receipt.name.removesuffix(".receipt.json"))
            if not path.is_file():
                continue
            old = json.loads(receipt.read_text(encoding="utf-8"))
            with path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            if digest != old.get("sha256"):
                continue
            rel = path.relative_to(root)
            if rel.parts[0] == "canvas":
                match = re.match(r"(\d+)-", path.name)
                key = "canvas:file:" + match[1] if match else None
            elif rel.parts[0] == "share" and path.parent.name.isdigit():
                key = "share:material:" + path.parent.name
            else:
                key = None
            if key:
                blob = self.blobs / digest
                if not blob.exists():
                    shutil.copyfile(path, blob)
                self.put("download:" + key, {"sha256": digest, "size": path.stat().st_size, "updated": old.get("updated")})


def state_directory(root):
    root = Path(root).resolve()
    return root.parent.parent / ".local" / "state" / hashlib.sha256(str(root).encode()).hexdigest()[:16]


def safe_name(value: str, limit=90) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value)).strip(" .")
    if not value or value.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]}:
        value = "_" + value
    if len(value) > limit:
        p = Path(value)
        suffix = p.suffix[:12]
        value = value[:limit - len(suffix) - 9] + "-" + hashlib.sha256(value.encode()).hexdigest()[:8] + suffix
    return value


def public_url(url):
    u = urlsplit(url)
    return urlunsplit((u.scheme, u.netloc, u.path, "", ""))


class Store:
    def __init__(self, root: Path, secrets=(), state=None):
        self.root = root.resolve()
        self.secrets = secrets
        self._state = state

    @property
    def state(self):
        if self._state is None:
            self._state = State(state_directory(self.root))
        return self._state

    def clean(self, obj):
        if isinstance(obj, dict):
            return {k: ("[REDACTED]" if re.search(r"token|cookie|authorization|signature|session_url|verifier", k, re.I) else self.clean(v)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.clean(v) for v in obj]
        if isinstance(obj, str):
            for secret in self.secrets:
                obj = obj.replace(secret, "[REDACTED]")
            # Persist provenance without signed URL queries or LTI launch fragments.
            return re.sub(r'https?://[^\s<>"\x27]+', lambda m: public_url(m[0]), obj)
        return obj

    def json(self, path: Path, obj):
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        part.write_text(json.dumps(self.clean(obj), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        part.replace(path)

    def text(self, path: Path, text: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        part.write_text(self.clean(text), encoding="utf-8")
        part.replace(path)

    def download(self, http, url, path, expected_size=None, updated=None, key=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        key = "download:" + (key or public_url(url))
        old = self.state.get(key)
        if old and old.get("updated") == updated and (expected_size is None or expected_size == old.get("size")):
            blob = self.state.blobs / old["sha256"]
            if blob.is_file() and blob.stat().st_size == old.get("size"):
                with blob.open("rb") as source:
                    if hashlib.file_digest(source, "sha256").hexdigest() == old.get("sha256"):
                        # Also verify the exported copy; same-size local corruption must be repaired.
                        shutil.copyfile(blob, path)
                        return {**old, "path": str(path.relative_to(self.root)).replace("\\", "/"), "status": "cached"}
        part = path.with_name(path.name + ".part")
        try:
            with http.request("GET", url, stream=True) as response, part.open("wb") as out:
                if "text/html" in response.headers.get("Content-Type", "") and path.suffix.lower() not in (".html", ".htm"):
                    raise SourceError("Download returned an HTML login/error page")
                size = 0
                sha = hashlib.sha256()
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        size += len(chunk)
                        if size > 4 * 1024**3:
                            raise SourceError("Download exceeds the 4 GiB per-file limit")
                        out.write(chunk)
                        sha.update(chunk)
                if expected_size is not None and size != expected_size:
                    raise SourceError("Download size mismatch")
            part.replace(path)
            record = {"path": str(path.relative_to(self.root)).replace("\\", "/"), "size": size,
                      "sha256": sha.hexdigest(), "updated": updated, "status": "downloaded"}
            blob = self.state.blobs / record["sha256"]
            shutil.copyfile(path, blob)
            self.state.put(key, record)
            return record
        finally:
            part.unlink(missing_ok=True)

    def deduplicate(self):
        """Keep one copy of each content file and repair every generated local link."""
        from urllib.parse import quote, unquote
        import os
        groups, aliases = {}, {}
        def reading_page(path):
            relative = path.relative_to(self.root)
            if relative.parts[0] == "notes" or (path.parent == self.root and path.name in {"AGENTS.md", "CLAUDE.md", "SCHEMA.md", "PROMPTS.md"}):
                return True
            return (path.name == "README.md" and path.with_name("index.html").exists()) or (path.name == "index.html" and path.with_name("README.md").exists())
        paths = sorted((p for p in self.root.rglob("*") if p.is_file() and not reading_page(p)),
                       key=lambda p: ("solution" in p.parts, len(str(p)), str(p)))
        for path in paths:
            with path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            if digest in groups:
                aliases[path] = groups[digest]
            else:
                groups[digest] = path
        for page in [*self.root.rglob("*.md"), *self.root.rglob("*.html")]:
            text = page.read_text(encoding="utf-8")
            def replace(match):
                target = match[1]
                if urlsplit(target).scheme or target.startswith("#"):
                    return match[0]
                absolute = (page.parent / unquote(target.split("#")[0])).resolve()
                if absolute in aliases:
                    relative = quote(os.path.relpath(aliases[absolute], page.parent).replace("\\", "/"), safe="/.")
                    return match[0].replace(target, relative)
                return match[0]
            if page.suffix == ".md":
                text = re.sub(r'\]\(([^\s)]+)(?:\s+"[^"]*")?\)', replace, text)
            else:
                text = re.sub(r'(?:href|src)="([^"]+)"', replace, text)
            page.write_text(text, encoding="utf-8")
        for duplicate in aliases:
            duplicate.unlink()
        return {str(k.relative_to(self.root)).replace("\\", "/"): str(v.relative_to(self.root)).replace("\\", "/") for k, v in aliases.items()}

    def restore_referenced_files(self, previous_root):
        """An unselected source may still link to a shared file retired by the selected source."""
        from urllib.parse import unquote
        from bs4 import BeautifulSoup
        for page in [*self.root.rglob("*.md"), *self.root.rglob("*.html")]:
            text = page.read_text(encoding="utf-8")
            if page.suffix == ".md":
                targets = re.findall(r'\]\(([^\s)]+)(?:\s+"[^"]*")?\)', text)
            else:
                targets = [n.get("href") or n.get("src") for n in BeautifulSoup(text, "html.parser").select("[href], [src]")]
            for target in targets:
                if not target or urlsplit(target).scheme or target.startswith("#"):
                    continue
                path = (page.parent / unquote(target.split("#")[0])).resolve()
                if not path.is_relative_to(self.root) or path.exists():
                    continue
                previous = previous_root / path.relative_to(self.root)
                if previous.is_file():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(previous, path)


def archive_kind(path):
    name = path.name.lower()
    if name.endswith(".zip"): return "zip"
    if name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")): return "tar"
    if name.endswith(".7z"): return "7z"
    if name.endswith(".rar"): return "rar"
    return None


def validate_members(entries, max_bytes, max_files):
    if len(entries) > max_files or sum(size for _, size, _ in entries) > max_bytes:
        raise SourceError("Archive exceeds extraction limits")
    seen = set()
    for name, size, link in entries:
        p = PurePosixPath(name.replace("\\", "/"))
        if p.is_absolute() or ".." in p.parts or not p.parts or any(":" in x or x.endswith((".", " ")) or safe_name(x, 255) != x for x in p.parts):
            raise SourceError("Archive contains an unsafe path")
        key = str(p).casefold()
        if key in seen or link or size < 0:
            raise SourceError("Archive contains duplicate paths, links, or special files")
        seen.add(key)


def extract_rar(archive, path, stage):
    import rarfile
    try:
        archive.extractall(stage)
        return
    except rarfile.BadRarFile:
        # rarfile 4.5 constructs `bsdtar -f -- archive`, treating '--' as the
        # archive filename. Use stdout-only decoding with explicit argument order.
        if rarfile.tool_setup().setup["open_cmd"][0] != "BSDTAR_TOOL":
            raise
    for member in archive.infolist():
        destination = stage / member.filename
        if member.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        command = [rarfile.BSDTAR_TOOL, "-x", "--to-stdout", "-f", str(path.resolve()), "--", member.filename]
        with destination.open("wb") as output, tempfile.TemporaryFile() as diagnostics:
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=diagnostics) as process:
                size, crc = 0, 0
                try:
                    while chunk := process.stdout.read(min(256 * 1024, member.file_size - size + 1)):
                        size += len(chunk)
                        if size > member.file_size:
                            raise SourceError("RAR 解压内容超过声明大小，已停止解压。")
                        crc = zlib.crc32(chunk, crc)
                        output.write(chunk)
                    if process.wait(timeout=90) != 0 or size != member.file_size:
                        raise SourceError("RAR 解码失败或内容不完整，原始压缩包已保留。")
                    if member.CRC is not None and crc != member.CRC:
                        raise SourceError("RAR 解压文件校验失败，原始压缩包已保留。")
                except BaseException:
                    process.kill()
                    process.wait()
                    raise


def extract_archive(path: Path, *, max_bytes=2 * 1024**3, max_files=10000, depth=0, budget=None, state=None):
    kind = archive_kind(path)
    if not kind:
        return []
    if depth >= 3:
        raise SourceError("Nested archive depth limit reached")
    budget = budget if budget is not None else {"bytes": max_bytes, "files": max_files}
    state = state or State(state_directory(path.parent))
    target = path.with_name(path.name + ".extracted")
    if target.is_symlink() or (target.exists() and any(p.is_symlink() for p in target.rglob("*"))):
        raise SourceError("Extraction folder contains a symbolic link")
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    cache_key = "extraction:" + str(path.resolve())
    cached = target.is_dir() and state.get(cache_key) == digest
    if not cached:
        if target.exists():
            raise SourceError("Existing extraction folder differs; move it aside before re-extracting")
        with tempfile.TemporaryDirectory(prefix=".sss-extract-", dir=path.parent) as temp:
            stage = Path(temp) / "content"
            stage.mkdir()
            try:
                if kind == "zip":
                    with zipfile.ZipFile(path) as archive:
                        names = {}
                        for info in archive.infolist():
                            name = info.filename
                            if not info.flag_bits & 0x800:
                                try:
                                    candidate = name.encode("cp437").decode("gb18030")
                                    if re.search(r"[\u4e00-\u9fff]", candidate):
                                        name = candidate
                                except (UnicodeEncodeError, UnicodeDecodeError):
                                    pass
                            names[info.filename] = name
                        entries = [(names[i.filename], i.file_size, stat.S_ISLNK(i.external_attr >> 16) or (stat.S_IFMT(i.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR))) for i in archive.infolist()]
                        validate_members(entries, budget["bytes"], budget["files"])
                        for info in archive.infolist():
                            destination = stage / names[info.filename]
                            if info.is_dir():
                                destination.mkdir(parents=True, exist_ok=True)
                            else:
                                destination.parent.mkdir(parents=True, exist_ok=True)
                                with archive.open(info) as source, destination.open("wb") as out:
                                    shutil.copyfileobj(source, out)
                elif kind == "tar":
                    with tarfile.open(path) as archive:
                        entries = [(i.name, i.size, not (i.isfile() or i.isdir())) for i in archive.getmembers()]
                        validate_members(entries, budget["bytes"], budget["files"])
                        archive.extractall(stage, filter="data")
                elif kind == "7z":
                    import py7zr
                    with py7zr.SevenZipFile(path) as archive:
                        entries = [(i.filename, i.uncompressed or 0, i.is_symlink or not (i.is_file or i.is_directory)) for i in archive.list()]
                        validate_members(entries, budget["bytes"], budget["files"])
                        archive.extractall(stage)
                else:
                    import rarfile
                    with rarfile.RarFile(path) as archive:
                        entries = [(i.filename, i.file_size, bool(i.file_redir) or i.is_symlink() or not (i.is_file() or i.is_dir())) for i in archive.infolist()]
                        validate_members(entries, budget["bytes"], budget["files"])
                        extract_rar(archive, path, stage)
                files = list(stage.rglob("*"))
                if any(p.is_symlink() or p.resolve().is_relative_to(stage.resolve()) is False for p in files):
                    raise SourceError("Unsafe extracted file")
                total = sum(p.stat().st_size for p in files if p.is_file())
                if total > budget["bytes"] or len(files) > budget["files"]:
                    raise SourceError("Extracted size exceeds limits")
                budget["bytes"] -= total
                budget["files"] -= len(files)
                shutil.move(str(stage), str(target))
                state.put(cache_key, digest)
            except SourceError:
                raise
            except Exception as exc:
                if kind == "rar":
                    import rarfile
                    if isinstance(exc, rarfile.NeedFirstVolume):
                        raise SourceError("RAR 分卷包缺少首卷，原始压缩包已保留。") from None
                    if isinstance(exc, rarfile.RarCannotExec):
                        raise SourceError("未找到可用的 RAR 解压工具，请安装 unrar、unar 或 7-Zip 并加入 PATH。") from None
                    if isinstance(exc, (rarfile.PasswordRequired, rarfile.RarWrongPassword)):
                        raise SourceError("RAR 压缩包需要密码，原始压缩包已保留。") from None
                raise SourceError(f"{kind.upper()} archive extraction failed ({type(exc).__name__})") from None
    else:
        files = [p for p in target.rglob("*") if ".extracted" not in str(p.relative_to(target).parent)]
        budget["bytes"] -= sum(p.stat().st_size for p in files if p.is_file())
        budget["files"] -= len(files)
        if min(budget.values()) < 0:
            raise SourceError("Cached archive exceeds extraction limits")
    extracted = [str(target)]
    for child in sorted(target.rglob("*")):
        if child.is_file() and archive_kind(child) and not any(p.name.endswith(".extracted") for p in child.relative_to(target).parents if p != Path(".")):
            extracted += extract_archive(child, depth=depth + 1, budget=budget, state=state)
    return extracted
