"""Stage a release payload and write the archives users download.

Everything a machine needs to RUN YBM, with the admin console already built.
The console is the point: a source checkout has none until someone installs
Node.js 22.22+ and builds it, which is the one prerequisite a non-developer
cannot reasonably be asked to satisfy. Shipping
backend/src/agent_control/static/admin inside the payload removes it.

Python rather than PowerShell so one implementation serves both release jobs,
and because only tarfile/zipfile let a Windows host set the Unix executable bit
on ybm.sh. A tarball whose launcher is not executable is a broken download that
looks fine until the user runs it.

    python scripts/package_release.py --version 0.1.0

Writes dist/YBM-windows.zip, dist/YBM-unix.tar.gz, and the staged tree at
dist/payload that the MSI build consumes. Stable asset names make the latest
release directly downloadable without first discovering its version number.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import shutil
import sys
import tarfile
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[1]

# Single files copied to the payload root.
ROOT_FILES = ("YBM.bat", "Install-YBM.bat", "ybm.sh", "README.md", "LICENSE", "CHANGELOG.md")

# Directory trees. backend/tests and the VS Code extension are not runtime.
TREES = ("backend/src", "scripts", "whatsapp-bridge/src")

# Manifests that sit at a directory root, copied individually so the rest of
# that directory does not come along.
ROOT_OF_TREE_FILES = (
    "backend/pyproject.toml",
    "backend/uv.lock",
    "config/config.example.yaml",
    "whatsapp-bridge/package.json",
    "whatsapp-bridge/package-lock.json",
)

# Per-machine state that must never be baked into a package handed to someone
# else. config.yaml and .env are the important ones: they can hold live tokens.
EXCLUDE_DIRS = {".venv", "node_modules", "__pycache__", ".agent_control", ".pytest_cache", ".ruff_cache"}
EXCLUDE_FILES = {"config.yaml", ".env", "agent_control.db"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}

# Needs the executable bit in the tarball, and in the zip for anyone who
# unpacks it on a Unix machine.
EXECUTABLE = {"ybm.sh"}

_PYPROJECT_VERSION = re.compile(r'^version\s*=\s*"[^"]*"', re.MULTILINE)


def _stamp_pyproject_version(path: Path, version: str) -> None:
    """`ybm check-updates` reads the installed package's own metadata
    (`importlib.metadata.version`), which `uv sync`/pip derive from this
    file's `[project] version` at install time - not from the release tag
    that named the archive. Left as committed (dev-time, bumped
    infrequently), an install from this exact payload would forever report
    that stale dev version instead of the tag a human just downloaded, so
    `check-updates` would claim an update is available on the version it is
    currently running. Stamp the packaged copy with the version this
    archive is actually named after; the git-tracked source file is
    untouched.
    """
    text = path.read_text(encoding="utf-8")
    stamped, count = _PYPROJECT_VERSION.subn(f'version = "{version}"', text, count=1)
    if count != 1:
        raise ValueError(f"expected exactly one top-level version field in {path}, found {count}")
    path.write_text(stamped, encoding="utf-8")


def _ignore(_dir: str, names: list[str]) -> set[str]:
    dropped = set()
    for name in names:
        if name in EXCLUDE_DIRS or name in EXCLUDE_FILES:
            dropped.add(name)
        elif Path(name).suffix in EXCLUDE_SUFFIXES:
            dropped.add(name)
    return dropped


def stage(version: str, stage_dir: Path) -> None:
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True)

    for name in ROOT_FILES:
        source = REPO_ROOT / name
        if source.exists():
            shutil.copy2(source, stage_dir / name)

    for tree in TREES:
        source = REPO_ROOT / tree
        if not source.exists():
            continue
        shutil.copytree(source, stage_dir / tree, ignore=_ignore, dirs_exist_ok=True)

    for rel in ROOT_OF_TREE_FILES:
        source = REPO_ROOT / rel
        if not source.exists():
            continue
        target = stage_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    pyproject = stage_dir / "backend/pyproject.toml"
    if pyproject.exists():
        _stamp_pyproject_version(pyproject, version)

    # Gives `ybm check-updates` a baseline in an installed copy that has no .git.
    (stage_dir / ".ybm-release-version").write_text(version, encoding="utf-8")


def _sorted_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def write_zip(stage_dir: Path, out: Path, prefix: str) -> None:
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in _sorted_files(stage_dir):
            rel = path.relative_to(stage_dir).as_posix()
            info = zipfile.ZipInfo(f"{prefix}/{rel}")
            info.compress_type = zipfile.ZIP_DEFLATED
            # Upper 16 bits carry the Unix mode. Without this, ybm.sh arrives
            # without its executable bit for anyone unzipping on macOS/Linux.
            mode = 0o755 if rel in EXECUTABLE else 0o644
            info.external_attr = (mode & 0xFFFF) << 16
            zf.writestr(info, path.read_bytes())


def write_tar(stage_dir: Path, out: Path, prefix: str) -> None:
    with tarfile.open(out, "w:gz") as tf:
        for path in _sorted_files(stage_dir):
            rel = path.relative_to(stage_dir).as_posix()
            info = tf.gettarinfo(str(path), arcname=f"{prefix}/{rel}")
            info.mode = 0o755 if rel in EXECUTABLE else 0o644
            # Reproducible-ish: the packaging host's uid/gid and name mean
            # nothing on the machine that unpacks this.
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            with path.open("rb") as fh:
                tf.addfile(info, fh)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Version for the archive names; no 'v' prefix.")
    parser.add_argument("--output-dir", default="dist")
    parser.add_argument("--stage-dir", default="dist/payload")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    if not out_dir.is_absolute():
        out_dir = REPO_ROOT / out_dir
    stage_dir = Path(args.stage_dir)
    if not stage_dir.is_absolute():
        stage_dir = REPO_ROOT / stage_dir

    console = REPO_ROOT / "backend/src/agent_control/static/admin/index.html"
    # Fail loudly rather than shipping the exact problem this packaging exists
    # to solve. A release without the console is worse than no release: it
    # looks complete and serves build instructions.
    if not console.exists():
        print("ERROR: the admin console is not built.", file=sys.stderr)
        print(f"  Expected: {console}", file=sys.stderr)
        print("  Run 'npm ci && npm run build' in frontend/ first.", file=sys.stderr)
        return 1

    print(f"Staging YBM {args.version}")
    stage(args.version, stage_dir)

    staged_console = stage_dir / "backend/src/agent_control/static/admin/index.html"
    if not staged_console.exists():
        print(f"ERROR: the console did not survive staging - expected {staged_console}", file=sys.stderr)
        return 1
    if not (stage_dir / "ybm.sh").exists():
        print("ERROR: ybm.sh is missing from the payload - the Unix launcher would not ship", file=sys.stderr)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    # One top-level directory inside each archive, so unpacking in a Downloads
    # folder produces `YBM-1.2.3/` rather than scattering backend/, scripts/,
    # and a loose ybm.sh across whatever was already there.
    prefix = f"YBM-{args.version}"
    zip_path = out_dir / "YBM-windows.zip"
    tar_path = out_dir / "YBM-unix.tar.gz"
    write_zip(stage_dir, zip_path, prefix)
    write_tar(stage_dir, tar_path, prefix)

    print(f"\nStaged:  {stage_dir}")
    for path in (zip_path, tar_path):
        print(f"{path.name}")
        print(f"  {path.stat().st_size / 1024 / 1024:.1f} MB   SHA256 {sha256(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
