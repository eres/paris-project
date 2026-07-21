Private bilingual writing + photography project (Notre Paris).

## Structure

- `notes/es/` original notes in Spanish
- `notes/en/` English translations
- `notes/fr/` French translations
- `prompts/` translation prompts
- `scripts/` vault watcher and automation helpers
- `tests/` unit and acceptance tests

## Note workflow (Obsidian vault)

Canonical statuses for vault notes under `01 Places` … `05 Fragments`:

1. Write as `status: draft`
2. When ready for editorial pass: `status: ready-for-review`
   - triggers **only** `scripts/editorial_review.py`
   - does **not** translate
   - does **not** run literary map
3. When finished: `status: done`
   - triggers `scripts/translate_note_fr.py`
   - then `scripts/literary_map.py` as the final step

## Vault watcher

See [`scripts/WATCHER.md`](scripts/WATCHER.md) for hybrid event+polling design,
launchd setup, macOS permissions, and diagnostics.

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
python3 scripts/diagnose_watcher.py
python3 scripts/watch_vault.py --vault /path/to/Notre-Paris --bootstrap --verbose
```

## Repo note translations (legacy)

The `notes/` tree still uses translation frontmatter (`ready-for-translation`).
That path is separate from the Obsidian vault status workflow above.
