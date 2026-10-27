import importlib.util
from pathlib import Path
from types import SimpleNamespace


def test_notices_include_licenses_without_runtime_code_or_bytecode(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("sss_desktop_build", Path(__file__).parents[1] / "scripts/build_desktop.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    files = [Path(name) for name in (
        "packaging.dist-info/licenses/LICENSE",
        "packaging/licenses/__init__.py",
        "packaging/licenses/__pycache__/__init__.cpython-314.pyc",
        "packaging/licenses/LICENSE.pyc",
    )]
    source = tmp_path / "installed"
    for item in files:
        path = source / item
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("package license" if item.name == "LICENSE" else "runtime code must not be distributed as a notice")
    package = SimpleNamespace(metadata={"Name": "packaging", "License-Expression": "MIT"}, version="1.0", files=files,
                              locate_file=lambda item: source / item)
    monkeypatch.setattr(build.importlib.metadata, "distributions", lambda: [package])
    output = tmp_path / "output"
    output.mkdir()
    build.third_party_notices(output)
    copied = list((output / "ThirdPartyNotices").rglob("*"))
    assert any(path.name == "LICENSE" and path.read_text() == "package license" for path in copied if path.is_file())
    assert not any(path.suffix in {".py", ".pyc"} or "__pycache__" in path.parts for path in copied)
