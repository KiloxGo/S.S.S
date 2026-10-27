"""Native launcher for source checkouts and self-contained desktop bundles."""

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import multiprocessing
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import urllib.request

APP_NAME = "S.S.S"


def application_directory():
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        if sys.platform == "darwin":
            for parent in executable.parents:
                if parent.suffix == ".app":
                    return parent.parent
        return executable.parent
    return Path(__file__).resolve().parent.parent


def user_data_directory():
    if sys.platform == "win32":
        return Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "SSS"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "S.S.S"
    return Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "sss"


def writable_directory(path):
    path.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=path):
        pass
    return path.resolve()


def data_directory(explicit=None):
    if explicit is not None:
        return writable_directory(Path(explicit).expanduser())
    beside = application_directory()
    if (beside / "portable.flag").is_file():
        try:
            return writable_directory(beside / "SSS-Data")
        except OSError:
            pass
    return writable_directory(user_data_directory())


class InstanceLock:
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.file = self.path.open("a+b")
            self.file.seek(0)
            if not self.file.read(1):
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if self.file is not None:
                self.file.close()
            self.file = None
            raise RuntimeError("S.S.S 已在使用这个数据目录，请切换到已打开的应用窗口。") from None
        return self

    def __exit__(self, *args):
        if self.file is not None:
            if sys.platform == "win32":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()
            self.file = None


def configure_archives():
    """Use the bundled Windows decoder; macOS also has the system bsdtar."""
    import rarfile
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    decoder = root / "tools" / "7z.exe"
    if decoder.is_file():
        rarfile.SEVENZIP_TOOL = str(decoder)
        rarfile.tool_setup(unrar=False, unar=False, bsdtar=False, sevenzip=True, force=True)


def configure_logging(root):
    directory = root / ".local"
    directory.mkdir(exist_ok=True)
    handler = RotatingFileHandler(directory / "app.log", maxBytes=2 * 1024**2, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.WARNING)
    # Windowed executables have no console streams on Windows. Libraries still
    # need file-like streams, including print() in the collector.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


class DesktopServer:
    def __init__(self, root):
        import uvicorn
        from .web import create_app
        self.root = Path(root).resolve()
        self.app = create_app(self.root / ".local" / "credentials.json", self.root / ".env", self.root / "data",
                              desktop={"data_dir": str(self.root)}, clipboard=self.copy_text)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(128)
        self.url = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
        config = uvicorn.Config(self.app, log_config=None, access_log=False, loop="asyncio", http="h11", ws="none")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, name="sss-local-server")

    @staticmethod
    def copy_text(text):
        import pyperclip
        pyperclip.copy(text)

    def start(self):
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                self.stop()
                raise RuntimeError("本地服务未能启动，请查看数据目录中的 .local/app.log。")
            time.sleep(.02)

    def stop(self):
        self.server.should_exit = True
        if self.thread.is_alive():
            self.thread.join()
        self.socket.close()


def self_test(root, result_path, native_window=False):
    """Run from the frozen binary as well as source, without starting a GUI."""
    from .study import prompt_catalog
    from .storage import extract_archive
    import zipfile
    import py7zr
    import webview  # Verify the window library was included in the bundle.
    server = DesktopServer(root)
    try:
        server.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(server.url + "/") as response:
            html = response.read().decode("utf-8")
        with opener.open(server.url + "/api/state") as response:
            state = json.load(response)
        with opener.open(server.url + "/api/prompts") as response:
            prompts = json.load(response)["prompts"]
        import re
        for asset in re.findall(r'(?:src|href)="(/assets/[^\"]+)"', html):
            with opener.open(server.url + asset) as response:
                assert response.status == 200
        test_archive = root / "smoke.zip"
        with zipfile.ZipFile(test_archive, "w") as archive:
            archive.writestr("notes.txt", "portable archive check")
        extracted = extract_archive(test_archive)
        assert (Path(extracted[0]) / "notes.txt").read_text() == "portable archive check"
        seven = root / "smoke.7z"
        with py7zr.SevenZipFile(seven, "w") as archive:
            archive.write(Path(extracted[0]) / "notes.txt", "notes.txt")
        extracted_seven = extract_archive(seven)
        assert (Path(extracted_seven[0]) / "notes.txt").read_text() == "portable archive check"
        assert len(prompts) == len(prompt_catalog()) == 6
        assert "<title>S.S.S" in html
        assert not any(item["configured"] for item in state["settings"].values())
        window_check = check_native_window(server, root) if native_window else None
        result = {"ok": True, "platform": sys.platform, "frozen": bool(getattr(sys, "frozen", False)),
                  "data_dir": str(root), "frontend": "<title>S.S.S" in html, "prompts": len(prompts),
                  "credential_free": True, "archive": True}
        if window_check:
            result["window"] = window_check
        Path(result_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        server.stop()


def check_native_window(server, root):
    """Optional interactive smoke check; the reporting route exists only here."""
    import pyperclip
    import webview
    result = {}
    finished = threading.Event()
    original_clipboard = pyperclip.paste()

    @server.app.post("/api/_window-check")
    def receive_report(payload: dict):
        result.update(payload)
        finished.set()
        return {"received": True}

    script = """(async () => {
      const wait = async fn => { for (let i=0; i<100; i++) { if (fn()) return; await new Promise(r=>setTimeout(r,100)); } throw Error('DOM timeout'); };
      const button = name => [...document.querySelectorAll('button')].find(b=>b.textContent.trim()===name);
      try {
        await wait(()=>button('平台配置'));
        button('平台配置').click();
        await wait(()=>document.querySelector('dialog[open]'));
        if (!document.querySelector('dialog[open]').textContent.includes('数据目录')) throw Error('Missing desktop settings');
        document.querySelector('[aria-label="关闭平台配置"]').click();
        button('学习提示词').click();
        await wait(()=>document.querySelector('[aria-label="复制课程学习指南"]'));
        document.querySelector('[aria-label="复制课程学习指南"]').click();
        await wait(()=>document.querySelector('dialog[open]').textContent.includes('已复制'));
        await fetch('/api/_window-check', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ok:true, settings:true, prompts:true, native_copy:true})});
      } catch(e) {
        await fetch('/api/_window-check', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ok:false, error:String(e)})});
      }
    })();"""
    window = create_window(server, root)
    window.events.loaded += lambda: window.run_js(script)

    def close_after_check():
        if not finished.wait(45):
            result.update(ok=False, error="Native window timed out")
        if result.get("ok"):
            result["clipboard_verified"] = "SCHEMA.md" in pyperclip.paste()
        window.destroy()

    try:
        webview.start(close_after_check, gui="edgechromium" if sys.platform == "win32" else None,
                      private_mode=True, storage_path=str(root / ".local" / "webview"))
    finally:
        pyperclip.copy(original_clipboard)
    if not result.get("ok") or not result.get("clipboard_verified"):
        raise RuntimeError(f"原生窗口自检失败：{result}")
    return result


def create_window(server, root):
    import webview
    webview.settings["ALLOW_FILE_URLS"] = False
    window = webview.create_window(APP_NAME, server.url, width=1080, height=780,
                                   min_size=(580, 480), text_select=True, background_color="#f6f7f3")

    def before_close():
        service = server.app.state.service
        with service.lock:
            busy = any(job.get("status") in ("queued", "running") for job in service.jobs.values())
        if busy:
            return window.create_confirmation_dialog(APP_NAME, "仍有课程正在同步。关闭后会等待当前任务安全结束，尚未开始的任务会取消。是否关闭？")

    window.events.closing += before_close
    return window


def show_error(message):
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)
    elif sys.platform == "darwin":
        import subprocess
        script = 'display alert "S.S.S" message ' + json.dumps(message, ensure_ascii=False) + ' as critical'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=30)
    elif sys.stderr:
        print(message, file=sys.stderr)


def main(argv=None):
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="S.S.S 桌面应用")
    parser.add_argument("--data-dir", type=Path, help="指定数据目录；不会读取当前工作目录的凭据")
    parser.add_argument("--self-test", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--self-test-window", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = None
    try:
        root = data_directory(args.data_dir)
        configure_logging(root)
        configure_archives()
        with InstanceLock(root / ".local" / "desktop.lock"):
            if args.self_test:
                self_test(root, args.self_test, args.self_test_window)
                return 0
            import webview
            server = DesktopServer(root)
            try:
                server.start()
                create_window(server, root)
                webview.start(gui="edgechromium" if sys.platform == "win32" else None, private_mode=True,
                              storage_path=str(root / ".local" / "webview"))
            finally:
                server.stop()
        return 0
    except Exception as exc:
        logging.exception("Desktop application failed")
        if args.self_test:
            args.self_test.write_text(json.dumps({"ok": False, "error": type(exc).__name__}), encoding="utf-8")
        else:
            message = str(exc) if isinstance(exc, RuntimeError) else "应用启动失败。"
            if root:
                message += f"\n数据目录：{root}\n诊断日志：.local/app.log"
            show_error(message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
