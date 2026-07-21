"""Tests for frontmatter / status parsing."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from frontmatter import get_status, get_status_from_text, normalize_status, parse_frontmatter


class NormalizeStatusTests(unittest.TestCase):
    def test_basic(self) -> None:
        self.assertEqual(normalize_status("ready-for-review"), "ready-for-review")
        self.assertEqual(normalize_status(" Ready-For-Review "), "ready-for-review")
        self.assertEqual(normalize_status('"ready-for-review"'), "ready-for-review")
        self.assertEqual(normalize_status("'done'"), "done")
        self.assertIsNone(normalize_status(None))
        self.assertIsNone(normalize_status("  "))


class ParseFrontmatterTests(unittest.TestCase):
    def test_standard(self) -> None:
        text = "---\nstatus: draft\nlang: es\n---\n\n# Title\n"
        self.assertEqual(parse_frontmatter(text)["status"], "draft")
        self.assertEqual(get_status_from_text(text), "draft")

    def test_quoted(self) -> None:
        text = '---\nstatus: "ready-for-review"\n---\nbody\n'
        self.assertEqual(get_status_from_text(text), "ready-for-review")

    def test_crlf(self) -> None:
        text = "---\r\nstatus: ready-for-review\r\n---\r\nbody\r\n"
        self.assertEqual(get_status_from_text(text), "ready-for-review")

    def test_spaces_around_colon(self) -> None:
        text = "---\nstatus : done\n---\n"
        self.assertEqual(get_status_from_text(text), "done")

    def test_incomplete_frontmatter(self) -> None:
        text = "---\nstatus: ready-for-review\nlang: es\n"
        # Mid-write: no closing ---. Line scan still finds status.
        self.assertEqual(get_status_from_text(text), "ready-for-review")

    def test_no_frontmatter(self) -> None:
        self.assertIsNone(get_status_from_text("# just a note\n"))

    def test_broken_yaml_fallback(self) -> None:
        text = "---\nstatus: ready-for-review\nbad: [unterminated\n---\n"
        self.assertEqual(get_status_from_text(text), "ready-for-review")

    def test_read_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "note.md"
            path.write_text("---\nstatus: draft\n---\n\nx\n", encoding="utf-8")
            self.assertEqual(get_status(path), "draft")


if __name__ == "__main__":
    unittest.main()
