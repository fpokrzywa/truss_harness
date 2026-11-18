"""Tests for scripts/traceability.py (run: python -m unittest discover -s scripts/tests)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "traceability", Path(__file__).resolve().parents[1] / "traceability.py"
)
assert _SPEC and _SPEC.loader
tr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tr)


class TempRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for e in tr.ENTRIES:
            for t in e.tests:
                (self.root / t).parent.mkdir(parents=True, exist_ok=True)
                (self.root / t).write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_generated_matrix_passes_the_check(self) -> None:
        path = tr.write(self.root)
        self.assertEqual(path.read_bytes(), tr.generate().encode("utf-8"))
        self.assertNotIn(b"\r\n", path.read_bytes())
        self.assertEqual(tr.check(self.root), [])

    def test_a_missing_test_path_fails_and_is_named(self) -> None:
        missing = "backend/tests/nope/test_missing.py"
        entries = (*tr.ENTRIES[:-1], tr.Entry("F-5.3-13", "x", "covered", (missing,)))
        tr.write(self.root, entries)
        problems = tr.check(self.root, entries)
        self.assertEqual(len(problems), 1)
        self.assertIn(missing, problems[0])

    def test_a_drifted_matrix_fails(self) -> None:
        path = tr.write(self.root)
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write("edited by hand\n")
        self.assertEqual(
            tr.check(self.root),
            [f"{tr.MATRIX} is out of date: run python scripts/traceability.py"],
        )

    def test_a_missing_matrix_fails(self) -> None:
        problems = tr.check(self.root)
        self.assertEqual(len(problems), 1)
        self.assertIn(tr.MATRIX, problems[0])
        self.assertIn("missing", problems[0])

    def test_main_check_exits_1_and_prints_to_stderr(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(tr.main(["--check"], root=self.root), 1)
        self.assertIn(tr.MATRIX, err.getvalue())
        self.assertFalse((self.root / tr.MATRIX).exists())


class RealRepoTests(unittest.TestCase):
    def test_the_committed_matrix_is_current_and_every_test_file_exists(self) -> None:
        self.assertEqual(tr.check(tr.ROOT), [])

    def test_mapping_has_13_unique_ids_and_covered_entries_list_tests(self) -> None:
        ids = [e.id for e in tr.ENTRIES]
        self.assertEqual(ids, [f"F-5.3-{n:02d}" for n in range(1, 14)])
        self.assertEqual(len(set(ids)), 13)
        for e in tr.ENTRIES:
            self.assertRegex(e.id, re.compile(r"^F-5\.3-\d{2}$"))
            self.assertIn(e.status, ("covered", "deferred"))
            if e.status == "covered":
                self.assertTrue(e.tests, e.id)
            else:
                self.assertEqual(e.tests, (), e.id)
                self.assertTrue(e.note, e.id)


if __name__ == "__main__":
    unittest.main()
