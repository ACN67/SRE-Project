"""Check A/B/C -> E consumers in a disposable source overlay, without merging.

Uses already-fetched refs; never downloads models, calls APIs or builds real data.
This is a compatibility rehearsal, not a Git merge-conflict or full E2E check.
"""
import io
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    selections = [
        ("origin/A", []),
        ("origin/B", ["src/dynamic_memory/memory"]),
        ("origin/C", ["src/dynamic_memory/retrieval"]),
    ]
    # Fail before constructing the overlay if any branch is missing.
    for ref, _ in selections:
        commit = subprocess.check_output(["git", "rev-parse", "--verify", ref], cwd=ROOT, text=True).strip()
        print(f"{ref}: {commit}", flush=True)
    with tempfile.TemporaryDirectory(prefix="e-abc-handoff-") as target:
        stage = Path(target)
        for ref, paths in selections:
            data = subprocess.check_output(["git", "archive", "--format=zip", ref, *paths], cwd=ROOT)
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for name in archive.namelist():
                    path = PurePosixPath(name)
                    if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
                        raise ValueError("Unsafe archive path")
                archive.extractall(stage)
        # Keep A's public contracts, SourceRepository and configuration. Only
        # overlay B/C-owned directories, then E's current consumer and tests.
        for relative in ("src/dynamic_memory/web", "tests/web"):
            shutil.copytree(ROOT / relative, stage / relative, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(ROOT / "streamlit_app.py", stage / "streamlit_app.py")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(stage / "src")
        env.pop("DYNAMIC_MEMORY_WEB_FACTORY", None)
        return subprocess.run([sys.executable, "-m", "pytest", "tests/web", "-q"], cwd=stage, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
