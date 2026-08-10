"""Vendor Monaco into the repo so the editor works offline (design 4.2).

Run: ``python tools/vendor_monaco.py``  (needs npm on PATH, once)

Until this ran, the editor loaded Monaco from a CDN — which meant no internet, no editor
at all, plus a standing bet that cdnjs keeps serving one pinned version forever. Neither
is acceptable for something that ships as a frozen desktop build.

**Everything in ``min/vs`` is kept, deliberately.** The obvious trim is
``language/typescript`` at 5.6 MB, and it would be a mistake: KubeJS scripting is
JavaScript, so that directory is the difference between real IntelliSense and coloured
text. Any curation list written today rots the first time someone opens a file type we
didn't anticipate, and the whole payload is ~4.5% of the QtWebEngine binaries the app
already carries (Qt6WebEngineCore.dll alone is 202 MB). Not a size decision.
"""
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

MONACO_VERSION = "0.52.2"
TARGET = Path(__file__).resolve().parent.parent / "packsmith" / "gui" / "editor" / "vendor"


def main() -> int:
    npm = shutil.which("npm")
    if npm is None:
        print("npm not found on PATH — install Node, or copy monaco-editor's min/vs "
              f"into {TARGET / 'monaco' / 'vs'} by hand.")
        return 1

    with tempfile.TemporaryDirectory() as work_dir:
        work = Path(work_dir)
        print(f"fetching monaco-editor@{MONACO_VERSION}…")
        result = subprocess.run(
            [npm, "pack", f"monaco-editor@{MONACO_VERSION}", "--silent"],
            cwd=work, capture_output=True, text=True)
        if result.returncode != 0:
            print(result.stderr.strip() or "npm pack failed")
            return 1

        tarballs = list(work.glob("*.tgz"))
        if not tarballs:
            print("npm pack produced no tarball")
            return 1
        with tarfile.open(tarballs[0]) as tar:
            tar.extractall(work, filter="data")

        source = work / "package" / "min" / "vs"
        if not source.is_dir():
            print(f"no min/vs in the package — layout changed upstream?")
            return 1

        destination = TARGET / "monaco" / "vs"
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)

        (TARGET / "monaco" / "VERSION").write_text(MONACO_VERSION + "\n", encoding="utf-8")

    total = sum(f.stat().st_size for f in destination.rglob("*") if f.is_file())
    print(f"vendored monaco {MONACO_VERSION} -> {destination}  ({total / 1_048_576:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
