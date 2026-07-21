"""Tests for transition semantics."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from transitions import Action, detect_transition


class TransitionTests(unittest.TestCase):
    def test_draft_to_ready(self) -> None:
        t = detect_transition("draft", "ready-for-review")
        assert t is not None
        self.assertEqual(t.actions, (Action.EDITORIAL_REVIEW,))

    def test_ready_to_done(self) -> None:
        t = detect_transition("ready-for-review", "done")
        assert t is not None
        self.assertEqual(t.actions, (Action.TRANSLATE_FR, Action.LITERARY_MAP))

    def test_ready_does_not_translate(self) -> None:
        t = detect_transition("draft", "ready-for-review")
        assert t is not None
        self.assertNotIn(Action.TRANSLATE_FR, t.actions)
        self.assertNotIn(Action.LITERARY_MAP, t.actions)

    def test_same_status(self) -> None:
        self.assertIsNone(detect_transition("draft", "draft"))
        self.assertIsNone(detect_transition("ready-for-review", "ready-for-review"))

    def test_text_edit_equivalent(self) -> None:
        # No transition when status unchanged (body edits are out of band).
        self.assertIsNone(detect_transition("draft", "draft"))

    def test_unknown_transition(self) -> None:
        self.assertIsNone(detect_transition("draft", "done"))
        self.assertIsNone(detect_transition(None, "ready-for-review"))
        self.assertIsNone(detect_transition("done", "draft"))


if __name__ == "__main__":
    unittest.main()
