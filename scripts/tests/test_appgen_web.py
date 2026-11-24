"""Tests for scripts/appgen_web.py (digest and comparison only; building needs pnpm)."""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import appgen_web  # noqa: E402


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_tree(root: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


FILES = {"index.html": b"<html></html>\n", "assets/index.js": b"console.log(1);\n", "assets/index.css": b"a{}"}


class DigestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_digest_is_sha256_of_sorted_path_hash_lines(self) -> None:
        tree = make_tree(self.tmp / "a", FILES)
        lines = "".join(f"{rel} {sha(FILES[rel])}\n" for rel in sorted(FILES))
        self.assertEqual(appgen_web.digest(tree), sha(lines.encode("utf-8")))
        self.assertEqual(lines.splitlines()[0], f"assets/index.css {sha(b'a{}')}")

    def test_digest_does_not_depend_on_write_order(self) -> None:
        first = make_tree(self.tmp / "a", FILES)
        second = make_tree(self.tmp / "b", dict(reversed(list(FILES.items()))))
        self.assertEqual(appgen_web.digest(first), appgen_web.digest(second))

    def test_digest_excludes_build_digest(self) -> None:
        tree = make_tree(self.tmp / "a", FILES)
        before = appgen_web.digest(tree)
        (tree / "BUILD_DIGEST").write_text("anything\n", encoding="utf-8")
        self.assertEqual(appgen_web.digest(tree), before)
        self.assertNotIn("BUILD_DIGEST", appgen_web.file_hashes(tree))


class CompareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.committed = make_tree(self.tmp / "committed", FILES)
        (self.committed / "BUILD_DIGEST").write_text(appgen_web.digest(self.committed) + "\n", encoding="utf-8")
        self.rebuilt = make_tree(self.tmp / "rebuilt", FILES)

    def test_identical_trees_pass(self) -> None:
        self.assertEqual(appgen_web.compare(self.committed, self.rebuilt), [])

    def test_changed_byte_fails_and_names_the_path(self) -> None:
        (self.rebuilt / "assets/index.js").write_bytes(b"console.log(2);\n")
        self.assertEqual(appgen_web.compare(self.committed, self.rebuilt), ["differs: assets/index.js"])

    def test_hand_edited_committed_file_fails(self) -> None:
        (self.committed / "assets/index.js").write_bytes(b"console.log(2);\n")
        problems = appgen_web.compare(self.committed, self.rebuilt)
        self.assertIn("differs: assets/index.js", problems)
        self.assertIn("stale: BUILD_DIGEST", problems)

    def test_missing_file_fails_and_names_the_path(self) -> None:
        (self.rebuilt / "assets/extra.js").write_bytes(b"x")
        self.assertEqual(appgen_web.compare(self.committed, self.rebuilt), ["missing: assets/extra.js"])

    def test_extra_file_fails_and_names_the_path(self) -> None:
        (self.rebuilt / "assets/index.css").unlink()
        problems = appgen_web.compare(self.committed, self.rebuilt)
        self.assertEqual(problems, ["extra: assets/index.css"])

    def test_stale_build_digest_fails(self) -> None:
        (self.committed / "BUILD_DIGEST").write_text("0" * 64 + "\n", encoding="utf-8")
        self.assertEqual(appgen_web.compare(self.committed, self.rebuilt), ["stale: BUILD_DIGEST"])

    def test_missing_build_digest_fails(self) -> None:
        (self.committed / "BUILD_DIGEST").unlink()
        self.assertEqual(appgen_web.compare(self.committed, self.rebuilt), ["missing: BUILD_DIGEST"])


if __name__ == "__main__":
    unittest.main()
