"""Build the generated app's web bundle and check the committed copy (design SP5 6.5).

Usage (from any directory):

    python scripts/appgen_web.py          # rebuild into backend/src/shifde/appgen/templates/web/
    python scripts/appgen_web.py --check  # rebuild into a temporary directory and compare

The bundle is `frontend/appgen-web` built by Vite. `BUILD_DIGEST` holds the sha256 hex of the
UTF-8 text of one line `<relative posix path> <sha256 hex of the file bytes>\\n` per bundle file
(`BUILD_DIGEST` excluded), sorted by path, followed by a newline. `--check` fails when the rebuild
differs from the committed files or when the committed files do not match `BUILD_DIGEST`.
Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
PACKAGE = "@shifde/appgen-web"
TARGET = ROOT / "backend" / "src" / "shifde" / "appgen" / "templates" / "web"
DIGEST_FILE = "BUILD_DIGEST"


def file_hashes(root: Path) -> dict[str, str]:
    """Relative posix path -> sha256 hex of every file under `root`, `BUILD_DIGEST` excluded."""
    hashes: dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == DIGEST_FILE:
            continue
        hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def digest_of(hashes: dict[str, str]) -> str:
    text = "".join(f"{rel} {hashes[rel]}\n" for rel in sorted(hashes))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest(root: Path) -> str:
    return digest_of(file_hashes(root))


def compare(committed: Path, rebuilt: Path) -> list[str]:
    """One line per differing, missing or extra path; empty when the committed bundle is current."""
    old, new = file_hashes(committed), file_hashes(rebuilt)
    problems: list[str] = []
    for rel in sorted(old.keys() | new.keys()):
        if rel not in old:
            problems.append(f"missing: {rel}")
        elif rel not in new:
            problems.append(f"extra: {rel}")
        elif old[rel] != new[rel]:
            problems.append(f"differs: {rel}")
    stamp = committed / DIGEST_FILE
    if not stamp.is_file():
        problems.append(f"missing: {DIGEST_FILE}")
    elif stamp.read_text(encoding="utf-8") != digest_of(old) + "\n":
        problems.append(f"stale: {DIGEST_FILE}")
    return problems


def build(out_dir: Path | None) -> None:
    """Run the package's Vite build, into `out_dir` when given, otherwise into its `dist/`."""
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        raise SystemExit("pnpm not found on PATH")
    argv = [pnpm, "--filter", PACKAGE, "build"]
    if out_dir is not None:
        argv = [pnpm, "--filter", PACKAGE, "exec", "vite", "build", "--outDir", str(out_dir), "--emptyOutDir"]
    proc = subprocess.run(argv, cwd=FRONTEND, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        print((proc.stdout or "") + (proc.stderr or ""), end="")
        raise SystemExit(proc.returncode)


def write() -> int:
    build(None)
    dist = FRONTEND / "appgen-web" / "dist"
    if TARGET.exists():
        shutil.rmtree(TARGET)
    shutil.copytree(dist, TARGET)
    value = digest(TARGET)
    (TARGET / DIGEST_FILE).write_text(value + "\n", encoding="utf-8", newline="\n")
    print(f"appgen-web bundle written to {TARGET.relative_to(ROOT).as_posix()}: {value}")
    return 0


def check() -> int:
    with tempfile.TemporaryDirectory(prefix="appgen-web-") as tmp:
        out = Path(tmp) / "web"
        build(out)
        problems = compare(TARGET, out)
        value = digest(out)
    if problems:
        for line in problems:
            print(line)
        return 1
    print(f"appgen-web bundle OK {value}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build or check the generated app's web bundle.")
    parser.add_argument("--check", action="store_true", help="rebuild into a temporary directory and compare")
    args = parser.parse_args(argv)
    return check() if args.check else write()


if __name__ == "__main__":
    sys.exit(main())
