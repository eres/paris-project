import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from proofread_note import proofread_output_path
from watch_vault import VaultHandler, get_editorial_output_path, normalize_note_status


class VaultHandlerTests(unittest.TestCase):
    def make_handler(self, root: Path) -> VaultHandler:
        placeholder = root / "unused.py"
        return VaultHandler(
            root,
            placeholder,
            placeholder,
            placeholder,
            placeholder,
            literary_map_enabled=True,
            debounce_seconds=0,
        )

    def make_note(self, root: Path, status: str, body: str = "Texto de prueba.") -> Path:
        (root / ".obsidian").mkdir(exist_ok=True)
        notes_dir = root / "01 Places"
        notes_dir.mkdir(exist_ok=True)
        note = notes_dir / "Watchdog Fixture.md"
        self.write_note(note, status, body)
        return note

    def write_note(self, note: Path, status: str, body: str) -> None:
        note.write_text(f"---\nstatus: {status}\n---\n\n{body}\n", encoding="utf-8")

    def install_fake_steps(self, handler: VaultHandler, *, failing_tag: str | None = None):
        calls = []
        translated_contents = []

        def fake_step(tag, _script, note_path):
            calls.append(tag)
            if tag == failing_tag:
                return False
            if tag == "PROOFREAD":
                output = proofread_output_path(note_path, None)
                output.write_text(note_path.read_text(encoding="utf-8"), encoding="utf-8")
            elif tag == "TRANSLATE":
                translated_contents.append(note_path.read_text(encoding="utf-8"))
            elif tag == "EDITORIAL":
                output = get_editorial_output_path(note_path)
                output.write_text("# Editorial Review\n", encoding="utf-8")
            return True

        handler.run_step = Mock(side_effect=fake_step)
        return calls, translated_contents

    def test_status_variants_remain_two_distinct_internal_states(self):
        for value in ("Ready-for-review", "ready-for-review", "ready_for_review"):
            self.assertEqual(normalize_note_status(value), "ready_for_review")
        for value in ("Done", "done"):
            self.assertEqual(normalize_note_status(value), "done")
        self.assertNotEqual(normalize_note_status("Ready-for-review"), normalize_note_status("Done"))

    def test_ready_for_review_runs_only_proofread_and_editorial(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            note = self.make_note(root, "Ready-for-review")
            handler = self.make_handler(root)
            calls, _ = self.install_fake_steps(handler)

            self.assertTrue(handler.run_pipeline(note))

            self.assertEqual(calls, ["PROOFREAD", "EDITORIAL"])
            self.assertNotIn("TRANSLATE", calls)
            self.assertNotIn("LITERARY_MAP", calls)

    def test_done_runs_translation_then_literary_map_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            note = self.make_note(root, "Done")
            handler = self.make_handler(root)
            calls, _ = self.install_fake_steps(handler)

            self.assertTrue(handler.run_pipeline(note))

            self.assertEqual(calls, ["TRANSLATE", "LITERARY_MAP"])
            self.assertNotIn("PROOFREAD", calls)
            self.assertNotIn("EDITORIAL", calls)

    def test_translation_failure_prevents_literary_map(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            note = self.make_note(root, "done")
            handler = self.make_handler(root)
            calls, _ = self.install_fake_steps(handler, failing_tag="TRANSLATE")

            self.assertFalse(handler.run_pipeline(note))

            self.assertEqual(calls, ["TRANSLATE"])
            self.assertNotIn("LITERARY_MAP", calls)

    def test_same_version_and_state_are_processed_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            note = self.make_note(root, "done")
            handler = self.make_handler(root)
            calls, _ = self.install_fake_steps(handler)

            self.assertTrue(handler.run_pipeline(note))
            self.assertTrue(handler.run_pipeline(note))

            self.assertEqual(calls, ["TRANSLATE", "LITERARY_MAP"])

    def test_changed_content_runs_again_after_status_is_reestablished(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            note = self.make_note(root, "Ready-for-review", "Versión uno.")
            handler = self.make_handler(root)
            calls, _ = self.install_fake_steps(handler)

            self.assertTrue(handler.run_pipeline(note))
            self.write_note(note, "draft", "Versión dos.")
            self.assertTrue(handler.run_pipeline(note))
            self.write_note(note, "ready_for_review", "Versión dos.")
            self.assertTrue(handler.run_pipeline(note))

            self.assertEqual(calls, ["PROOFREAD", "EDITORIAL", "PROOFREAD", "EDITORIAL"])

    def test_changed_done_content_reprocesses_without_reestablishing_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            note = self.make_note(root, "done", "Versión final uno.")
            handler = self.make_handler(root)
            calls, _ = self.install_fake_steps(handler)

            self.assertTrue(handler.run_pipeline(note))
            self.write_note(note, "done", "Edición posterior sin cambiar estado.")
            self.assertTrue(handler.run_pipeline(note))

            self.assertEqual(calls, [
                "TRANSLATE", "LITERARY_MAP",
                "TRANSLATE", "LITERARY_MAP",
            ])

    def test_full_editorial_transition_translates_only_edited_final_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            note = self.make_note(root, "draft", "Borrador.")
            handler = self.make_handler(root)
            calls, translated_contents = self.install_fake_steps(handler)

            self.assertTrue(handler.run_pipeline(note))
            self.write_note(note, "Ready-for-review", "Versión para revisar.")
            self.assertTrue(handler.run_pipeline(note))
            self.write_note(note, "ready_for_review", "Versión editada por humanos.")
            self.assertTrue(handler.run_pipeline(note))
            self.write_note(note, "Done", "Versión final editada por humanos.")
            self.assertTrue(handler.run_pipeline(note))

            self.assertEqual(calls, ["PROOFREAD", "EDITORIAL", "TRANSLATE", "LITERARY_MAP"])
            self.assertEqual(len(translated_contents), 1)
            self.assertIn("Versión final editada por humanos.", translated_contents[0])
            self.assertNotIn("Versión para revisar.", translated_contents[0])

    def test_event_arriving_during_processing_is_not_discarded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            note = self.make_note(root, "done")
            handler = self.make_handler(root)
            handler.pending[note] = 0

            def requeue(_path):
                handler.pending[note] = time.time()
                return True

            handler.run_pipeline = Mock(side_effect=requeue)
            handler.process_pending()

            self.assertIn(note, handler.pending)

    def test_done_edits_are_retranslated_and_resynced_without_reestablishing_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            note = self.make_note(root, "Done", "Version un.")
            handler = self.make_handler(root)
            handler.notion_sync_enabled = True
            calls, _ = self.install_fake_steps(handler)

            self.assertTrue(handler.run_pipeline(note))
            self.write_note(note, "Done", "Version deux.")
            self.assertTrue(handler.run_pipeline(note))

            self.assertEqual(calls, [
                "TRANSLATE", "NOTION", "LITERARY_MAP",
                "TRANSLATE", "NOTION", "LITERARY_MAP",
            ])

    def test_notion_failure_retries_only_sync_not_translation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            note = self.make_note(root, "Done")
            handler = self.make_handler(root)
            handler.notion_sync_enabled = True
            calls = []
            attempts = [0]

            def fake_step(tag, _script, _note):
                calls.append(tag)
                if tag == "NOTION":
                    attempts[0] += 1
                    return attempts[0] > 1
                return True

            handler.run_step = Mock(side_effect=fake_step)
            self.assertTrue(handler.run_pipeline(note))
            self.assertIn(note, handler.pending_notion)
            handler.pending_notion[note] = 0
            handler.process_pending()
            self.assertNotIn(note, handler.pending_notion)
            self.assertEqual(calls, [
                "TRANSLATE", "NOTION", "LITERARY_MAP", "NOTION",
            ])

    def test_permanent_notion_conflict_is_not_retried(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            note = self.make_note(root, "Done")
            handler = self.make_handler(root)
            handler.notion_sync_enabled = True
            calls = []

            def fake_step(tag, _script, _note):
                calls.append(tag)
                if tag == "NOTION":
                    handler.last_step_code = 2
                    return False
                handler.last_step_code = 0
                return True

            handler.run_step = Mock(side_effect=fake_step)
            self.assertTrue(handler.run_pipeline(note))
            self.assertNotIn(note, handler.pending_notion)
            handler.process_pending()
            self.assertEqual(calls, ["TRANSLATE", "NOTION", "LITERARY_MAP"])



if __name__ == "__main__":
    unittest.main()
