"""Acceptance tests for the hybrid vault watcher."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from status_cache import StatusCache
from watch_vault import NoteProcessor, WatchService, is_note_path


NOTE_TEMPLATE = """---
lang: es
status: {status}
---

# Fixture note

Body line.
"""


def write_note(path: Path, status: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(NOTE_TEMPLATE.format(status=status), encoding="utf-8")


def set_status(path: Path, new_status: str) -> None:
    text = path.read_text(encoding="utf-8")
    for old in ("draft", "ready-for-review", "done"):
        text = text.replace(f"status: {old}", f"status: {new_status}")
    path.write_text(text, encoding="utf-8")


def markers(path: Path) -> set[str]:
    found = set()
    for suffix in (
        ".md.editorial_review.ran",
        ".md.translate_fr.ran",
        ".md.literary_map.ran",
    ):
        if Path(str(path) + suffix.replace(".md", "")).exists() or path.with_suffix(
            path.suffix + suffix[3:]
        ).exists():
            # Prefer explicit with_suffix style used by action scripts.
            pass
    for p in path.parent.glob(path.name + ".*"):
        if p.name.endswith(".editorial_review.ran"):
            found.add("editorial_review")
        if p.name.endswith(".translate_fr.ran"):
            found.add("translate_fr")
        if p.name.endswith(".literary_map.ran"):
            found.add("literary_map")
    return found


def clear_markers(path: Path) -> None:
    for p in path.parent.glob(path.name + ".*"):
        if p.name.endswith(".ran"):
            p.unlink()


class HelperTests(unittest.TestCase):
    def test_is_note_path(self) -> None:
        self.assertTrue(is_note_path(Path("01 Places/foo.md")))
        self.assertFalse(is_note_path(Path("01 Places/.foo.md")))
        self.assertFalse(is_note_path(Path("01 Places/foo.tmp")))
        self.assertFalse(is_note_path(Path(".obsidian/workspace.md")))


class AcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="notre-paris-test-"))
        self.vault = self.tmp / "vault"
        self.watch = self.vault / "01 Places"
        self.watch.mkdir(parents=True)
        for name in ("02 People", "03 Scenes", "04 Themes", "05 Fragments"):
            (self.vault / name).mkdir()
        self.cache_path = self.tmp / "status_cache.json"
        self.note = self.watch / "probe-note.md"
        self.processor = NoteProcessor(
            self.vault,
            StatusCache(self.cache_path),
            debounce_s=0.05,
            stable_checks=2,
            stable_interval_s=0.02,
            python=sys.executable,
        )
        write_note(self.note, "draft")
        self.processor.bootstrap([self.watch])

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_case_a_draft_to_ready_for_review(self) -> None:
        clear_markers(self.note)
        set_status(self.note, "ready-for-review")
        ran = self.processor.process_path(self.note, source="test")
        self.assertTrue(ran)
        self.assertEqual(markers(self.note), {"editorial_review"})

    def test_case_b_ready_to_done(self) -> None:
        set_status(self.note, "ready-for-review")
        self.processor.process_path(self.note, source="seed")
        clear_markers(self.note)
        set_status(self.note, "done")
        ran = self.processor.process_path(self.note, source="test")
        self.assertTrue(ran)
        self.assertEqual(markers(self.note), {"translate_fr", "literary_map"})

    def test_case_c_body_edit_no_transition(self) -> None:
        clear_markers(self.note)
        text = self.note.read_text(encoding="utf-8") + "\nExtra line.\n"
        self.note.write_text(text, encoding="utf-8")
        ran = self.processor.process_path(self.note, source="test")
        self.assertFalse(ran)
        self.assertEqual(markers(self.note), set())

    def test_case_d_rapid_saves_no_duplicate(self) -> None:
        clear_markers(self.note)
        set_status(self.note, "ready-for-review")
        self.assertTrue(self.processor.process_path(self.note, source="first"))
        # Same content / same transition again.
        self.assertFalse(self.processor.process_path(self.note, source="second"))
        self.assertEqual(markers(self.note), {"editorial_review"})
        # Only one marker file should exist.
        marker_files = list(self.note.parent.glob(self.note.name + ".editorial_review.ran"))
        self.assertEqual(len(marker_files), 1)

    def test_case_e_terminal_write_with_events(self) -> None:
        """Simulate terminal edit while WatchService is running."""
        clear_markers(self.note)
        service = WatchService(
            vault=self.vault,
            watch_dirs=["01 Places"],
            cache_path=self.tmp / "svc-cache.json",
            reconcile_every_s=0.4,
            debounce_s=0.08,
            bootstrap=True,
            python=sys.executable,
        )
        # Re-bootstrap from draft.
        write_note(self.note, "draft")
        thread = threading.Thread(target=service.start, daemon=True)
        thread.start()
        time.sleep(0.6)
        set_status(self.note, "ready-for-review")
        deadline = time.time() + 8
        while time.time() < deadline:
            if "editorial_review" in markers(self.note):
                break
            time.sleep(0.1)
        service.stop()
        thread.join(timeout=5)
        self.assertIn("editorial_review", markers(self.note))
        self.assertNotIn("translate_fr", markers(self.note))
        self.assertNotIn("literary_map", markers(self.note))

    def test_case_f_restart_reconciles_offline_transition(self) -> None:
        # Bootstrap as draft, "stop" processor, change status offline, reconcile.
        write_note(self.note, "draft")
        cache = StatusCache(self.tmp / "offline-cache.json")
        processor = NoteProcessor(
            self.vault,
            cache,
            debounce_s=0.05,
            stable_checks=2,
            stable_interval_s=0.02,
            python=sys.executable,
        )
        processor.bootstrap([self.watch])
        clear_markers(self.note)
        set_status(self.note, "ready-for-review")
        # New processor instance = restart.
        processor2 = NoteProcessor(
            self.vault,
            StatusCache(self.tmp / "offline-cache.json"),
            debounce_s=0.05,
            stable_checks=2,
            stable_interval_s=0.02,
            python=sys.executable,
        )
        ran = processor2.reconcile([self.watch])
        self.assertGreaterEqual(ran, 1)
        self.assertEqual(markers(self.note), {"editorial_review"})

    def test_obsidian_atomic_rename_pattern(self) -> None:
        """Obsidian-like save: write temp then os.replace onto target."""
        clear_markers(self.note)
        # Seed cache as draft.
        write_note(self.note, "draft")
        self.processor.bootstrap([self.watch])
        clear_markers(self.note)

        tmp = self.note.with_suffix(".md.tmp")
        tmp.write_text(NOTE_TEMPLATE.format(status="ready-for-review"), encoding="utf-8")
        os.replace(tmp, self.note)
        # Event path: schedule as moved dest.
        self.processor.schedule(self.note, source="fs:moved:dest")
        deadline = time.time() + 5
        while time.time() < deadline:
            if "editorial_review" in markers(self.note):
                break
            time.sleep(0.05)
        self.assertEqual(markers(self.note), {"editorial_review"})


if __name__ == "__main__":
    unittest.main()
