import tempfile
import unittest
from pathlib import Path

from sync_notion import NotionSync


class FakeNotion:
    def __init__(self):
        self.pages = [{"id": f"old-{n}", "title": f"{n}. Existing {n}"} for n in range(1, 12)]
        self.content = {f"old-{n}": f"Prior {n}" for n in range(1, 12)}
        self.created = []
        self.updated = []

    def child_pages(self):
        return [dict(page) for page in self.pages]

    def markdown(self, page_id):
        return self.content[page_id]

    def create(self, title, body):
        page_id = f"new-{len(self.created) + 1}"
        self.created.append((title, body))
        self.pages.append({"id": page_id, "title": title})
        self.content[page_id] = body
        return page_id

    def replace(self, page_id, body):
        self.updated.append(page_id)
        self.content[page_id] = body


class NotionSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / ".obsidian").mkdir()
        (self.root / "01 Places").mkdir()
        (self.root / "12 French").mkdir()
        self.api = FakeNotion()
        self.state_path = self.root / "private-state.json"

    def add(self, name, body="Un texte en français.", status="Done"):
        path = self.root / "01 Places" / (name + ".md")
        path.write_text(f"---\nstatus: {status}\n---\n\nOriginal\n", encoding="utf-8")
        (self.root / "12 French" / (name + ".fr.md")).write_text(
            f"---\nstatus: Done\n---\n\n{body}\n", encoding="utf-8"
        )
        return path

    def sync(self):
        return NotionSync(self.root, self.api, state_path=self.state_path)

    def test_creates_number_twelve_and_is_idempotent(self):
        note = self.add("Chez Emmanuel")
        sync = self.sync()
        self.assertEqual(sync.sync_one(note), "created: 12. Chez Emmanuel")
        self.assertEqual(sync.sync_one(note), "unchanged: 12. Chez Emmanuel")
        self.assertEqual(len(self.api.created), 1)

    def test_updates_existing_translation_without_creating_duplicate(self):
        note = self.add("Chez Emmanuel")
        self.sync().sync_one(note)
        (self.root / "12 French" / "Chez Emmanuel.fr.md").write_text(
            "---\nstatus: Done\n---\n\nLe nouveau texte.\n", encoding="utf-8"
        )
        self.assertEqual(self.sync().sync_one(note), "updated: 12. Chez Emmanuel")
        self.assertEqual(len(self.api.created), 1)
        self.assertEqual(self.api.updated, ["new-1"])

    def test_never_overwrites_edits_made_in_notion(self):
        note = self.add("Chez Emmanuel")
        self.sync().sync_one(note)
        self.api.content["new-1"] += "\n\nA human note"
        (self.root / "12 French" / "Chez Emmanuel.fr.md").write_text(
            "Nouvelle traduction.\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "human edits"):
            self.sync().sync_one(note)
        self.assertEqual(self.api.updated, [])

    def test_backfill_requires_explicit_order(self):
        a = self.add("Chez Emmanuel")
        b = self.add("Étienne")
        sync = self.sync()
        with self.assertRaisesRegex(ValueError, "order"):
            sync.backfill(None)
        with self.assertRaisesRegex(ValueError, "ordered backfill"):
            sync.sync_one(a)
        order_file = self.root / "order.txt"
        order_file.write_text("01 Places/Étienne.md\n01 Places/Chez Emmanuel.md\n")
        self.assertEqual(sync.backfill(order_file), [
            "created: 12. Étienne",
            "created: 13. Chez Emmanuel",
        ])

    def test_existing_human_page_is_never_modified(self):
        note = self.add("Chez Emmanuel")
        self.api.pages.append({"id": "human", "title": "12. Chez Emmanuel"})
        self.api.content["human"] = "Edited by author"
        sync = self.sync()
        self.assertIn("not overwritten", sync.sync_one(note))
        self.assertEqual(len(self.api.created), 0)
        self.assertEqual(self.api.updated, [])

    def test_missing_translation_is_reported(self):
        source = self.add("Chez Emmanuel")
        (self.root / "12 French" / "Chez Emmanuel.fr.md").unlink()
        self.assertEqual(self.sync().audit()[0]["status"], "missing_translation")
        with self.assertRaises(FileNotFoundError):
            self.sync().sync_one(source)

    def test_duplicate_source_stems_are_blocked(self):
        self.add("Chez Emmanuel")
        (self.root / "02 People").mkdir()
        (self.root / "02 People" / "Chez Emmanuel.md").write_text(
            "---\nstatus: Done\n---\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            self.sync().audit()


if __name__ == "__main__":
    unittest.main()
