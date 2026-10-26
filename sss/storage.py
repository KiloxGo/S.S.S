import hashlib
import json
import re
import shutil
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit

from .http import SourceError


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
    def __init__(self, root: Path, secrets=()):
        self.root = root.resolve()
        self.secrets = secrets

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

    def download(self, http, url, path, expected_size=None, updated=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        receipt = path.with_name(path.name + ".receipt.json")
        if path.is_file() and receipt.is_file():
            old = json.loads(receipt.read_text(encoding="utf-8"))
            if old.get("updated") == updated and old.get("size") == path.stat().st_size and (expected_size is None or expected_size == path.stat().st_size):
                with path.open("rb") as source:
                    if hashlib.file_digest(source, "sha256").hexdigest() == old.get("sha256"):
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
            self.json(receipt, record)
            return record
        finally:
            part.unlink(missing_ok=True)


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


def extract_archive(path: Path, *, max_bytes=2 * 1024**3, max_files=10000, depth=0, budget=None):
    kind = archive_kind(path)
    if not kind:
        return []
    if depth >= 3:
        raise SourceError("Nested archive depth limit reached")
    budget = budget if budget is not None else {"bytes": max_bytes, "files": max_files}
    target = path.with_name(path.name + ".extracted")
    marker = target / ".sss-extracted.json"
    if target.is_symlink() or (target.exists() and any(p.is_symlink() for p in target.rglob("*"))):
        raise SourceError("Extraction folder contains a symbolic link")
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    cached = marker.exists() and json.loads(marker.read_text(encoding="utf-8")).get("sha256") == digest
    if not cached:
        if target.exists():
            raise SourceError("Existing extraction folder differs; move it aside before re-extracting")
        with tempfile.TemporaryDirectory(prefix=".sss-extract-", dir=path.parent) as temp:
            stage = Path(temp) / "content"
            stage.mkdir()
            try:
                if kind == "zip":
                    with zipfile.ZipFile(path) as archive:
                        entries = [(i.filename, i.file_size, stat.S_ISLNK(i.external_attr >> 16) or (stat.S_IFMT(i.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR))) for i in archive.infolist()]
                        validate_members(entries, budget["bytes"], budget["files"])
                        archive.extractall(stage)
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
                        entries = [(i.filename, i.file_size, bool(i.file_redir) or i.is_symlink() or not (i.isfile() or i.isdir())) for i in archive.infolist()]
                        validate_members(entries, budget["bytes"], budget["files"])
                        archive.extractall(stage)
                files = list(stage.rglob("*"))
                if any(p.is_symlink() or p.resolve().is_relative_to(stage.resolve()) is False for p in files):
                    raise SourceError("Unsafe extracted file")
                total = sum(p.stat().st_size for p in files if p.is_file())
                if total > budget["bytes"] or len(files) > budget["files"]:
                    raise SourceError("Extracted size exceeds limits")
                budget["bytes"] -= total
                budget["files"] -= len(files)
                (stage / ".sss-extracted.json").write_text(json.dumps({"sha256": digest}), encoding="utf-8")
                shutil.move(str(stage), str(target))
            except SourceError:
                raise
            except Exception as exc:
                raise SourceError(f"Archive extraction failed ({type(exc).__name__}); RAR may require unrar/7-Zip") from None
    else:
        files = [p for p in target.rglob("*") if ".extracted" not in str(p.relative_to(target).parent)]
        budget["bytes"] -= sum(p.stat().st_size for p in files if p.is_file())
        budget["files"] -= len(files)
        if min(budget.values()) < 0:
            raise SourceError("Cached archive exceeds extraction limits")
    extracted = [str(target)]
    for child in sorted(target.rglob("*")):
        if child.is_file() and archive_kind(child) and not any(p.name.endswith(".extracted") for p in child.relative_to(target).parents if p != Path(".")):
            extracted += extract_archive(child, depth=depth + 1, budget=budget)
    return extracted
