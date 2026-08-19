import unittest

from translate_note_fr import compose_translated_note, split_frontmatter, strip_translated_frontmatter


class TranslateNoteFrontmatterTests(unittest.TestCase):
    def test_split_keeps_status_line_verbatim(self):
        source = (
            "---\n"
            "type: place\n"
            "status: Done\n"
            "themes: Wait\n"
            "---\n"
            "\n"
            "El jardín se presenta como otro reino.\n"
        )

        frontmatter, body = split_frontmatter(source)

        self.assertEqual(
            frontmatter,
            "---\ntype: place\nstatus: Done\nthemes: Wait\n---",
        )
        self.assertEqual(body, "El jardín se presenta como otro reino.\n")

    def test_compose_reuses_source_status_instead_of_translated_yaml(self):
        frontmatter = "---\ntype: place\nstatus: Done\n---"
        model_output = (
            "---\n"
            "type: lieu\n"
            "status: Terminé\n"
            "---\n"
            "\n"
            "Le jardin se présente comme un autre royaume.\n"
        )

        composed = compose_translated_note(frontmatter, model_output)

        self.assertTrue(composed.startswith("---\ntype: place\nstatus: Done\n---\n"))
        self.assertIn("Le jardin se présente comme un autre royaume.", composed)
        self.assertNotIn("status: Terminé", composed)
        self.assertNotIn("type: lieu", composed)

    def test_compose_strips_list_status_that_becomes_an_obsidian_dropdown(self):
        frontmatter = "---\nstatus: Done\n---"
        model_output = "---\nstatus:\n  - Terminé\n---\n\nRue étroite.\n"

        composed = compose_translated_note(frontmatter, model_output)

        self.assertEqual(composed, "---\nstatus: Done\n---\n\nRue étroite.\n")

    def test_notes_without_frontmatter_keep_the_translated_body(self):
        composed = compose_translated_note(None, "Un jardin.\n")
        self.assertEqual(composed, "Un jardin.\n")

    def test_strip_leaves_body_only_notes_unchanged(self):
        self.assertEqual(strip_translated_frontmatter("  Un jardin.  "), "Un jardin.")


if __name__ == "__main__":
    unittest.main()
