"""Build on the target operating system; bundle only explicitly selected assets."""

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent.parent


def run(*command, **kwargs):
    subprocess.run(command, cwd=ROOT, check=True, **kwargs)


def create_icon(directory):
    from PIL import Image, ImageDraw
    image = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((32, 32, 992, 992), radius=230, fill="#edf2e7", outline="#c3d3b8", width=14)
    draw.line([(512, 740), (512, 320)], fill="#426a4d", width=35)
    left = [(512, 345), (435, 295), (260, 290), (240, 306), (240, 710), (380, 708), (460, 730), (512, 770)]
    right = [(1024 - x, y) for x, y in left]
    draw.line(left, fill="#426a4d", width=35, joint="curve")
    draw.line(right, fill="#426a4d", width=35, joint="curve")
    icon = directory / ("icon.icns" if sys.platform == "darwin" else "icon.ico")
    image.save(icon)
    return icon


def third_party_notices(directory):
    notices = directory / "ThirdPartyNotices"
    notices.mkdir()
    inventory = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name", "unknown")
        if name.lower() in {"sjtu-sss", "pytest", "pip"}:
            continue
        inventory.append({"name": name, "version": distribution.version,
                          "license": distribution.metadata.get("License-Expression") or distribution.metadata.get("License") or "see package notices"})
        for item in distribution.files or []:
            # A runtime package can itself be named "licenses" (e.g. SPDX
            # helpers). Its Python caches embed build-machine paths and are not
            # license documents. Include only notice files or dist-info notices.
            if "__pycache__" in item.parts or item.suffix.lower() in {".py", ".pyc", ".pyo", ".so", ".dll", ".dylib"}:
                continue
            filename = item.name.lower()
            named_notice = filename in {"license", "licence", "copying", "notice"} or filename.startswith(("license.", "licence.", "copying.", "notice."))
            metadata_notices = any(part.endswith(".dist-info") for part in item.parts) and "licenses" in item.parts
            if named_notice or metadata_notices:
                source = Path(distribution.locate_file(item))
                if source.is_file():
                    target = notices / name / Path(*item.parts[-2:])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
    (notices / "packages.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
    import sysconfig
    python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copyfile(python_license, notices / "Python-LICENSE.txt")
    (notices / "README.txt").write_text(
        "Python and third-party component notices. packages.json inventories the build environment; not every build dependency is part of the runtime.\n"
        "Windows: the included, unmodified 7-Zip decoder is a separate executable/library. License: ../7-Zip-License.txt; source: https://www.7-zip.org/download.html\n",
        encoding="utf-8")


def windows_decoder():
    candidates = [shutil.which("7z"), r"C:\Program Files\7-Zip\7z.exe"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and Path(candidate).with_name("7z.dll").is_file() and Path(candidate).with_name("License.txt").is_file():
            return Path(candidate)
    raise RuntimeError("Windows 构建机需要安装完整的 7-Zip（7z.exe 与 7z.dll），以便随应用携带 RAR 解码器。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-web-build", action="store_true", help="使用已构建的前端")
    parser.add_argument("--window-test", action="store_true", help="构建后打开原生窗口，自动检查配置与复制功能")
    args = parser.parse_args()
    if sys.platform not in ("darwin", "win32"):
        raise RuntimeError("此构建脚本支持 Windows 和 macOS，请在目标系统构建。")
    if not args.skip_web_build:
        npm = shutil.which("npm")
        if not npm:
            raise RuntimeError("构建机需要安装 Node.js 和 npm。")
        run(npm, "--prefix", "web", "ci")
        run(npm, "--prefix", "web", "run", "build")
    static = ROOT / "sss" / "web_static"
    if not (static / "index.html").is_file():
        raise RuntimeError("前端尚未构建。")
    work = ROOT / "build" / "desktop"
    work.mkdir(parents=True, exist_ok=True)
    icon = create_icon(work)
    version = importlib.metadata.version("sjtu-sss")
    architecture = {"aarch64": "arm64", "amd64": "x64", "x86_64": "x64"}.get(platform.machine().lower(), platform.machine().lower())
    target = "macos" if sys.platform == "darwin" else "windows"
    label = f"SSS-{version}-{target}-{architecture}"
    output = ROOT / "dist" / label
    archive = ROOT / "dist" / (label + ".zip")
    if output.exists() or archive.exists():
        raise RuntimeError(f"输出已存在：{output}；请先移走上次构建，避免覆盖。")
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
               "--name", "SSS", "--paths", str(ROOT), "--distpath", str(work / "frozen"),
               "--workpath", str(work / "pyinstaller"), "--specpath", str(work), "--icon", str(icon),
               "--add-data", f"{static}{os.pathsep}sss/web_static",
               "--add-data", f"{ROOT / 'sss' / 'study_resources'}{os.pathsep}sss/study_resources",
               "--collect-data", "webview", "--collect-data", "certifi",
               "--hidden-import", "uvicorn.logging", "--hidden-import", "uvicorn.loops.asyncio",
               "--hidden-import", "uvicorn.protocols.http.h11_impl", "--hidden-import", "uvicorn.lifespan.on"]
    for module in ("PyQt5", "PyQt6", "PySide2", "PySide6", "cefpython3", "gi", "qtpy", "tkinter"):
        command += ["--exclude-module", module]
    if sys.platform == "darwin":
        command += ["--osx-bundle-identifier", "org.sss.study", "--hidden-import", "webview.platforms.cocoa"]
    else:
        decoder = windows_decoder()
        command += ["--hidden-import", "webview.platforms.winforms", "--hidden-import", "webview.platforms.edgechromium",
                    "--add-binary", f"{decoder}{os.pathsep}tools",
                    "--add-binary", f"{decoder.with_name('7z.dll')}{os.pathsep}tools"]
    command.append(str(ROOT / "desktop" / "launcher.py"))
    run(*command)
    output.mkdir(parents=True)
    if sys.platform == "darwin":
        bundle = work / "frozen" / "SSS.app"
        shutil.copytree(bundle, output / "SSS.app", symlinks=True)
        executable = output / "SSS.app" / "Contents" / "MacOS" / "SSS"
    else:
        shutil.copytree(work / "frozen" / "SSS", output / "SSS", symlinks=True)
        executable = output / "SSS" / "SSS.exe"
        for name in ("License.txt", "readme.txt"):
            source = decoder.parent / name
            if source.exists():
                shutil.copyfile(source, output / ("7-Zip-" + name))
    marker = executable.parents[3] / "portable.flag" if sys.platform == "darwin" else executable.parent / "portable.flag"
    marker.write_text("Keep this file beside the application to store data in SSS-Data.\n", encoding="utf-8")
    shutil.copyfile(ROOT / "desktop" / "使用说明.txt", output / "使用说明.txt")
    third_party_notices(output)
    with tempfile.TemporaryDirectory(prefix="sss-bundle-check-") as temporary:
        result = Path(temporary) / "result.json"
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("SSS_") and k not in {"PYTHONPATH", "PYTHONHOME"}}
        check_command = [str(executable), "--data-dir", str(Path(temporary) / "data"), "--self-test", str(result)]
        if args.window_test:
            check_command.append("--self-test-window")
        checked = subprocess.run(check_command, cwd=ROOT, env=clean_env, timeout=120)
        if checked.returncode or not result.exists():
            log = Path(temporary) / "data" / ".local" / "app.log"
            if log.exists():
                shutil.copyfile(log, work / "self-test-failure.log")
            raise RuntimeError(f"打包自检失败（退出码 {checked.returncode}），诊断日志：{work / 'self-test-failure.log'}")
        report = json.loads(result.read_text(encoding="utf-8"))
        if not report.get("ok") or not report.get("frozen"):
            raise RuntimeError(f"打包自检失败：{report}")
        report.pop("data_dir", None)
        (output / "build-check.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if sys.platform == "darwin":
        run("ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(output), str(archive))
    else:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
            for file in output.rglob("*"):
                if file.is_file():
                    zipped.write(file, file.relative_to(output.parent))
    import hashlib
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    print(f"Built: {archive}\nSize: {archive.stat().st_size / 1024**2:.1f} MiB")


if __name__ == "__main__":
    main()
