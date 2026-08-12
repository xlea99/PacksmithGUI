"""Where user data lives, and whether a fresh clone can start — design 9.0/9.1.

Both failures here are invisible in development and total in the field: a frozen build
loses every profile on exit, and a clean checkout dies before `main()`. Neither shows up
in a dev run, because dev is never frozen and the developer's `userdata/` already exists.
"""
import sys
from pathlib import Path

import pytest

from packsmith.common.setup import ProjectPaths, ensure_directory, load_config


class _Frozen:
    """Pretend to be a PyInstaller build without being one."""

    def __init__(self, monkeypatch, executable, platform="win32", env=None):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(executable))
        monkeypatch.setattr(sys, "platform", platform)
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)


def test_a_frozen_build_does_not_put_user_data_in_the_extraction_dir(monkeypatch, tmp_path):
    """`_MEIPASS` is a temp dir recreated per launch. Rooting userdata there means every
    profile, tag and blueprint is destroyed on exit — silently, looking like a fresh start."""
    meipass = tmp_path / "_MEI12345"
    meipass.mkdir()
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    _Frozen(monkeypatch, executable=meipass / "packsmith.exe",
            env={"APPDATA": str(tmp_path / "roaming")})

    root = ProjectPaths.data_root()

    assert meipass not in root.parents and root != meipass, \
        f"user data would live in the extraction dir: {root}"
    assert root == tmp_path / "roaming" / "Packsmith"


def test_a_userdata_folder_beside_the_exe_wins(monkeypatch, tmp_path):
    """Portable mode: modpack devs move tools between drives with their instances."""
    app = tmp_path / "app"
    (app / "userdata").mkdir(parents=True)
    _Frozen(monkeypatch, executable=app / "packsmith.exe",
            env={"APPDATA": str(tmp_path / "roaming")})

    assert ProjectPaths.data_root() == app


@pytest.mark.parametrize("platform, env, expected", [
    ("win32", {"APPDATA": "APPDATA_DIR"}, ("APPDATA_DIR", "Packsmith")),
    ("linux", {"XDG_DATA_HOME": "XDG_DIR"}, ("XDG_DIR", "Packsmith")),
])
def test_each_platform_uses_its_own_per_user_location(monkeypatch, tmp_path,
                                                      platform, env, expected):
    base = tmp_path / expected[0]
    base.mkdir()
    _Frozen(monkeypatch, executable=tmp_path / "app" / "packsmith.exe", platform=platform,
            env={k: str(base) for k in env})
    assert ProjectPaths.data_root() == base / expected[1]


def test_a_source_checkout_still_uses_the_repo_root():
    """Unchanged on purpose — every existing dev profile lives there."""
    assert not getattr(sys, "frozen", False), "test env assumption"
    assert (ProjectPaths.data_root() / "packsmith" / "common" / "setup.py").is_file()


# --- a fresh clone has to start ----------------------------------------------------------

def test_a_missing_config_directory_is_created_not_fatal(tmp_path):
    """It was `must_exist=True`, so a clone without `userdata/config/` raised at IMPORT
    time — before any code could explain what to create."""
    created = ensure_directory(tmp_path / "userdata" / "config")
    assert created.is_dir()


def test_a_missing_main_toml_means_defaults_not_a_crash(tmp_path):
    """The import-time `open()` had no guard at all, so a clone without the file could not
    reach `main()`."""
    assert load_config(tmp_path / "userdata" / "config" / "main.toml") == {}


def test_a_present_config_is_actually_read(tmp_path):
    (tmp_path / "main.toml").write_text('theme = "dark"', encoding="utf-8")
    assert load_config(tmp_path / "main.toml") == {"theme": "dark"}


def test_a_malformed_config_still_fails_loudly(tmp_path):
    """Absent means defaults; *broken* means the user wrote something and meant it, and
    quietly applying defaults would discard an override they made on purpose."""
    (tmp_path / "main.toml").write_text("this is not = = toml", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid TOML"):
        load_config(tmp_path / "main.toml")


def test_building_paths_creates_everything_a_fresh_clone_lacks(tmp_path, monkeypatch):
    """End to end: point the app at an empty directory and it should come up, not die."""
    monkeypatch.setattr(ProjectPaths, "data_root", staticmethod(lambda: tmp_path))
    paths = ProjectPaths.build()
    for directory in (paths.userdata, paths.config, paths.logs, paths.profiles):
        assert directory.is_dir(), f"{directory} was not created"


# --- the dependency manifest exists ------------------------------------------------------

def test_every_third_party_import_is_declared():
    """There was no manifest of any kind — no pyproject, no requirements.txt. A checkout
    could only be run by someone who already knew what to install."""
    import re
    import tomllib

    root = Path(__file__).resolve().parents[1]
    with open(root / "pyproject.toml", "rb") as f:
        declared = {re.split(r"[=<>~\[]", d)[0].lower()
                    for d in tomllib.load(f)["project"]["dependencies"]}

    # Parsed, not grepped: a regex over source text also matches prose in docstrings
    # ("...read from the registry..." declares a dependency on `the`).
    import ast

    imported = set()
    for path in (root / "packsmith").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    third_party = {m.lower() for m in imported
                   if m not in sys.stdlib_module_names and m != "packsmith"}
    # `starlark` is the import name of the `starlark-pyo3` distribution.
    third_party = {"starlark-pyo3" if m == "starlark" else m for m in third_party}

    assert third_party <= declared, f"undeclared dependencies: {sorted(third_party - declared)}"
