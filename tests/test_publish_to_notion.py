import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from publish_to_notion import (
    NotionAPI,
    PublishError,
    identity_title,
    markdown_blocks,
    publish,
    rich_text,
    select_destination,
)
from watch_vault import VaultHandler


class FakeNotion:
    def __init__(self, titles=()):
        self.pages = [
            {"id": f"page-{index}", "type": "child_page", "child_page": {"title": title}}
            for index, title in enumerate(titles, 1)
        ]
        self.created = []
        self.updated = []

    def children(self, _parent_id):
        return list(self.pages)

    def create_page(self, _parent_id, title, blocks):
        self.created.append((title, blocks))
        page_id = f"new-{len(self.created)}"
        self.pages.append({"id": page_id, "type": "child_page", "child_page": {"title": title}})
        return page_id

    def replace_body(self, page_id, blocks):
        self.updated.append((page_id, blocks))


class PublicationTests(unittest.TestCase):
    def make_vault(self, root, name="La musique dans la fête"):
        vault = root / "vault"
        vault.mkdir()
        (vault / ".obsidian").mkdir()
        (vault / "01 Places").mkdir()
        (vault / "12 French").mkdir()
        source = vault / "01 Places" / f"{name}.md"
        source.write_text("---\nstatus: Done\n---\n\nTexto original.\n", encoding="utf-8")
        translated = vault / "12 French" / f"{name}.fr.md"
        translated.write_text("---\nstatus: Done\n---\n\nLa musique résonne.\n\n**L'amitié** demeure.\n", encoding="utf-8")
        return source, translated

    def environment(self):
        return patch.dict(os.environ, {
            "NOTION_NOTES_PAGE_ID": "3ccf9bd4a7ae8157b497fb287db20c8a",
            "NOTION_API_KEY": "test-token",
        })

    def test_new_note_number_uses_maximum_not_count(self):
        with tempfile.TemporaryDirectory() as d, self.environment():
            source, _ = self.make_vault(Path(d))
            api = FakeNotion(["1. Rue Blainville", "11. La fête chez nous", "8. L'appartement"])
            action, title, page_id = publish(source, api=api)
            self.assertEqual((action, title, page_id), ("create", "12. La musique dans la fête", "new-1"))
            self.assertEqual(len(api.created), 1)
            self.assertIn("La musique résonne.", str(api.created[0][1]))
            self.assertNotIn("Texto original.", str(api.created[0][1]))

    def test_republication_updates_in_place_and_preserves_number(self):
        with tempfile.TemporaryDirectory() as d, self.environment():
            source, translated = self.make_vault(Path(d))
            api = FakeNotion(["11. La fête chez nous"])
            self.assertEqual(publish(source, api=api)[1], "12. La musique dans la fête")
            translated.write_text("La version renouvelée.\n", encoding="utf-8")
            operation, title, page_id = publish(source, api=api)
            self.assertEqual((operation, title, page_id), ("update", "12. La musique dans la fête", "new-1"))
            self.assertEqual(len(api.created), 1)
            self.assertEqual(len(api.updated), 1)
            self.assertIn("La version renouvelée.", str(api.updated[0][1]))

    def test_existing_manual_page_is_adopted(self):
        with tempfile.TemporaryDirectory() as d, self.environment():
            source, _ = self.make_vault(Path(d), "La fête chez nous")
            api = FakeNotion(["1. Rue Blainville", "11. La fête chez nous"])
            operation, title, page_id = publish(source, api=api)
            self.assertEqual((operation, title, page_id), ("update", "11. La fête chez nous", "page-2"))
            self.assertEqual(len(api.created), 0)

    def test_dry_run_does_not_create_or_save_state(self):
        with tempfile.TemporaryDirectory() as d, self.environment():
            source, _ = self.make_vault(Path(d))
            api = FakeNotion(["11. La fête chez nous"])
            operation, title, _ = publish(source, api=api, dry_run=True)
            self.assertEqual((operation, title), ("create", "12. La musique dans la fête"))
            self.assertEqual(api.created, [])
            self.assertFalse((source.parent.parent / ".notre-paris-notion-state.json").exists())

    def test_ambiguous_titles_refuse_overwrite(self):
        pages = [
            {"id": "one", "type": "child_page", "child_page": {"title": "10. La salle"}},
            {"id": "two", "type": "child_page", "child_page": {"title": "11. La salle"}},
        ]
        with self.assertRaises(PublishError):
            select_destination(pages, "La salle", None)

    def test_missing_saved_page_refuses_duplicate(self):
        with self.assertRaises(PublishError):
            select_destination([], "La salle", "missing-id")

    def test_long_text_chunked_and_frontmatter_excluded(self):
        chunks = rich_text("é" * 4200)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(c["text"]["content"]) <= 1800 for c in chunks))
        blocks = markdown_blocks("Bonjour **ami**.\n\nDeuxième paragraphe.")
        self.assertEqual([block["type"] for block in blocks], ["paragraph", "paragraph"])
        self.assertTrue(any(part.get("annotations", {}).get("bold")
                            for part in blocks[0]["paragraph"]["rich_text"]))
        self.assertEqual(identity_title("11. La fête chez nous"), identity_title("La fête chez nous"))


class WatchdogNotionTests(unittest.TestCase):
    def make_handler(self, root):
        (root / ".obsidian").mkdir()
        notes = root / "01 Places"
        notes.mkdir()
        path = notes / "Note.md"
        path.write_text("---\nstatus: Done\n---\n\nTexte.", encoding="utf-8")
        placeholder = root / "unused.py"
        handler = VaultHandler(
            root, placeholder, placeholder, placeholder, placeholder,
            notion_publish_enabled=True, debounce_seconds=0,
        )
        return path, handler

    def test_notion_failure_retries_without_rerunning_translation(self):
        with tempfile.TemporaryDirectory() as d:
            source, handler = self.make_handler(Path(d))
            calls = []
            def fake_step(tag, _script, _source):
                calls.append(tag)
                return not (tag == "NOTION" and calls.count("NOTION") == 1)
            handler.run_step = Mock(side_effect=fake_step)
            self.assertFalse(handler.run_pipeline(source))
            self.assertTrue(handler.run_pipeline(source))
            self.assertEqual(calls, ["TRANSLATE", "LITERARY_MAP", "NOTION", "NOTION"])

    def test_reenter_done_even_with_identical_content_republishes(self):
        with tempfile.TemporaryDirectory() as d:
            source, handler = self.make_handler(Path(d))
            calls = []
            handler.run_step = Mock(side_effect=lambda tag, *_: calls.append(tag) or True)
            self.assertTrue(handler.run_pipeline(source))
            source.write_text("---\nstatus: draft\n---\n\nTexte.", encoding="utf-8")
            self.assertTrue(handler.run_pipeline(source))
            source.write_text("---\nstatus: Done\n---\n\nTexte.", encoding="utf-8")
            self.assertTrue(handler.run_pipeline(source))
            self.assertEqual(calls, [
                "TRANSLATE", "LITERARY_MAP", "NOTION",
                "TRANSLATE", "LITERARY_MAP", "NOTION",
            ])


if __name__ == "__main__":
    unittest.main()
