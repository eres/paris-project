import tempfile
import unittest
from unittest import mock
from pathlib import Path

import httpx

from sync_notion import NotionAPI, NotionSync


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

    def sync(self, **kwargs):
        return NotionSync(self.root, self.api, state_path=self.state_path, **kwargs)

    def test_creates_number_twelve_and_is_idempotent(self):
        note = self.add("Chez Emmanuel")
        sync = self.sync(allow_unlinked_history=True)
        self.assertEqual(sync.sync_one(note), "created: 12. Chez Emmanuel")
        self.assertEqual(sync.sync_one(note), "unchanged: 12. Chez Emmanuel")
        self.assertEqual(len(self.api.created), 1)

    def test_updates_existing_translation_without_creating_duplicate(self):
        note = self.add("Chez Emmanuel")
        self.sync(allow_unlinked_history=True).sync_one(note)
        (self.root / "12 French" / "Chez Emmanuel.fr.md").write_text(
            "---\nstatus: Done\n---\n\nLe nouveau texte.\n", encoding="utf-8"
        )
        self.assertEqual(self.sync().sync_one(note), "updated: 12. Chez Emmanuel")
        self.assertEqual(len(self.api.created), 1)
        self.assertEqual(self.api.updated, ["new-1"])

    def test_never_overwrites_edits_made_in_notion(self):
        note = self.add("Chez Emmanuel")
        self.sync(allow_unlinked_history=True).sync_one(note)
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
        sync = self.sync(allow_unlinked_history=True)
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
        self.assertIn("linked without overwrite", sync.sync_one(note))
        self.assertEqual(len(self.api.created), 0)
        self.assertEqual(self.api.updated, [])
        self.assertEqual(self.api.content["human"], "Edited by author")

    def test_legacy_alias_prevents_duplicate_when_french_title_differs(self):
        note = self.add("La sala", "La salle était vaste.")
        self.api.pages[8]["title"] = "9. La salle"
        self.api.content["old-9"] = "La salle était vaste."
        # Use a temporary alias fixture rather than writing to the real repo.
        with mock.patch("sync_notion.ALIASES_FILE", self.root / "aliases.json"):
            (self.root / "aliases.json").write_text(
                '{"01 Places/La sala.md": "old-9"}', encoding="utf-8"
            )
            sync = self.sync()
            self.assertEqual(sync.pending(), [])
            self.assertIn("adopted matching page", sync.sync_one(note))
            self.assertEqual(len(self.api.created), 0)

    def test_missing_translation_is_reported(self):
        source = self.add("Chez Emmanuel")
        (self.root / "12 French" / "Chez Emmanuel.fr.md").unlink()
        report = self.sync().audit()
        self.assertEqual(report["writing_order"], "not_inferred")
        self.assertIsNone(report["notes"][0]["proposed_number"])
        self.assertEqual(report["notes"][0]["status"], "missing_translation")
        self.assertGreaterEqual(len(report["notion_only"]), 11)
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

    def test_unlinked_history_blocks_a_new_number(self):
        note = self.add("Chez Emmanuel")
        with self.assertRaisesRegex(ValueError, "not linked"):
            self.sync().sync_one(note)
        self.assertEqual(self.api.created, [])

    def test_later_source_edit_updates_the_linked_page(self):
        note = self.add("Chez Emmanuel", "Texte local.")
        self.api.pages.append({"id": "human", "title": "12. Chez Emmanuel"})
        self.api.content["human"] = "Texte déjà dans Notion."
        self.assertIn("linked without overwrite", self.sync().sync_one(note))
        (self.root / "12 French" / "Chez Emmanuel.fr.md").write_text(
            "---\nstatus: Done\n---\n\nTexte révisé.\n", encoding="utf-8"
        )
        self.assertEqual(self.sync().sync_one(note), "updated: 12. Chez Emmanuel")
        self.assertEqual(self.api.updated, ["human"])
        self.assertEqual(len(self.api.created), 0)

    def test_notion_edit_after_link_is_preserved(self):
        note = self.add("Chez Emmanuel", "Texte local.")
        self.api.pages.append({"id": "human", "title": "12. Chez Emmanuel"})
        self.api.content["human"] = "Texte déjà dans Notion."
        self.sync().sync_one(note)
        self.api.content["human"] += "\n\nNote humaine."
        (self.root / "12 French" / "Chez Emmanuel.fr.md").write_text(
            "---\nstatus: Done\n---\n\nTexte révisé.\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "human edits"):
            self.sync().sync_one(note)
        self.assertEqual(self.api.updated, [])

    def test_curly_apostrophe_and_undashed_id_do_not_duplicate(self):
        note = self.add("L'édifice", "Le bâtiment.")
        dashed = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        page = self.api.pages[6]
        page["id"] = dashed
        page["title"] = "7. L’édifice"
        self.api.content[dashed] = "Le bâtiment.\n"
        with mock.patch("sync_notion.ALIASES_FILE", self.root / "aliases.json"):
            (self.root / "aliases.json").write_text(
                '{"01 Places/L\'édifice.md": "' + dashed.replace("-", "") + '"}',
                encoding="utf-8",
            )
            sync = self.sync()
            self.assertEqual(sync.pending(), [])
            self.assertIn("adopted matching page", sync.sync_one(note))
            self.assertEqual(self.api.created, [])
            self.assertEqual(self.api.updated, [])

    def test_audit_marks_narrative_years_and_assigns_no_number(self):
        path = self.add("Chez Emmanuel")
        path.write_text(
            "---\nstatus: Done\ndate: 2004-11-02\n---\n\nOriginal\n",
            encoding="utf-8",
        )
        row = self.sync().audit()["notes"][0]
        self.assertEqual(row["frontmatter_dates"][0]["value"], "2004-11-02")
        self.assertTrue(row["frontmatter_dates"][0]["narrative_date_ignored"])
        self.assertIsNone(row["proposed_number"])


class NotionRequestTests(unittest.TestCase):
    def test_post_retries_rate_limit_but_not_timeout(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow"})
            return httpx.Response(200, json={"id": "created"})

        client = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://api.notion.com",
        )
        api = NotionAPI("token", "11111111222243338444555555555555", client=client)
        with mock.patch("sync_notion.time.sleep"):
            self.assertEqual(api.create("12. Titre", "Corps"), "created")
        self.assertEqual(calls["n"], 2)

        def timeout(_request):
            raise httpx.TimeoutException("timed out")

        api.client = httpx.Client(
            transport=httpx.MockTransport(timeout),
            base_url="https://api.notion.com",
        )
        with self.assertRaises(httpx.TimeoutException):
            api.create("12. Titre", "Corps")


if __name__ == "__main__":
    unittest.main()
