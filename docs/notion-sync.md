# Notre Paris → Notion (French publication)

The literary source of truth remains the Obsidian vault. Notion contains **copies of
completed French translations only**, as numbered child pages under **Notre Paris / Notes**.
The original Spanish notes, reviews and local French files are never modified by this sync.

## What is implemented

- After a successful Done translation, the existing Python watchdog calls
  sync_notion.py. The Literary Map stage is retained.
- New pages are named N. Title, where N is one above the highest existing
  number in the Notes parent. Existing pages are not renumbered.
- Source identity is vault-relative original note path, recorded with the
  remote Notion page ID in a local, git-ignored ledger. Title collisions
  are detected before creating anything.
- Repeating the operation is idempotent. Editing a Done source causes a
  new French translation and, if the remote page was not manually edited,
  updates the existing Notion page.
- An edit made directly in Notion blocks automatic replacement rather than
  being lost. Untracked existing pages are adopted only when their Markdown
  exactly matches the current French content.
- Failed publication is retried by the watchdog without rerunning translation,
  with exponential delay between 5 and 60 minutes. At service startup,
  previously tracked pages are reconciled.
- The watcher **refuses to allocate a new number** if other translated Done
  notes are missing from Notion. This avoids racing past old unpublished work.

## One-time configuration on the Mac mini

The ChatGPT Notion connection is separate from the local Python process.
Create or select a Notion integration that has content read, insert and update
permissions and share the **Notes** page with it. Store the integration token
locally, never in git:

    NOTION_API_KEY=secret_...
    NOTION_NOTES_PAGE_ID=<private Notes page UUID>
    NOTION_SYNC_ENABLED=1

These belong in the project's .env, loaded by watch_vault.py. Do not paste
the token into a public issue, PR, terminal output or chat.

Install dependencies if needed, and run the read-only audit first:

    .venv/bin/pip install -r requirements.txt
    .venv/bin/python sync_notion.py --vault "/absolute/path/to/Notre-Paris" --audit

Check the audit against the real vault. The notes numbered 1–11 in Notion
should remain untouched.

## Backfill existing translated Done notes

When multiple completed French notes are unpublished, **writing order cannot
safely be deduced from the fictional date (2004–2005), alphabetic filename
or filesystem modification timestamp**. Create a private plain-text file
listing the original Spanish note's vault-relative paths in the order they
were written/finalized, e.g.:

    02 People/Étienne.md
    01 Places/Chez Emmanuel.md

The example is illustrative only; verify the actual order first. Then:

    .venv/bin/python sync_notion.py --vault "/absolute/path/to/Notre-Paris" --backfill --order-file "/private/path/notion-order.txt"

**Important for the first eleven pages:** an older source filename may be in
Spanish while the title in Notion is French (e.g., a Spanish filename for
La salle). If the audit calls an already-published legacy note "pending",
do **not** backfill it as a new page. In the private, git-ignored file
.notion-sync-aliases.json, map its actual vault-relative source path to the
existing Notion page UUID, for example:

    {
      "01 Places/Nombre original.md": "UUID-of-existing-Notion-page"
    }

This is a manually verified identity map, not a numbering map. The script
validates every alias against a child page in Notes and never writes an
untracked page whose body differs from the local French text.

The tool requires the list to contain every currently unpublished translated
Done note **exactly once**, before it changes Notion. It publishes in that
order, without changing previously numbered pages. If only one note is pending,
the order file is optional.

If a page with a matching title already exists but differs from local French,
the sync blocks and requests a human comparison. It never silently overwrites
that page. Resolve such conflicts before another backfill attempt.

## Start and verify automatic sync

    ./scripts/watchdog-restart.sh
    ./scripts/watchdog-status.sh
    ./scripts/watchdog-logs.sh

When a source note enters Done, look for TRANSLATE, NOTION and LITERARY_MAP
steps in the logs, then confirm the resulting numbered French page in Notion.
For a safe end-to-end test, make a *new test note* in an eligible source folder
and move it to Done **only after the backlog is reconciled**. Check it has
the next sequence number and that repeating the same status does not duplicate
the page. Editing the completed source should update the same page.

Run isolated tests with:

    .venv/bin/python -m unittest discover -s tests -v

To disable remote publication without affecting editorial automation, set
NOTION_SYNC_ENABLED=0 and restart the watchdog, or run it with --no-notion-sync.

## Failure and recovery

- A missing token, revoked permissions or inaccessible Notes parent causes
  explicit log errors; no automatic numbering occurs.
- The local ledger is .notion-sync-state.json (private, git-ignored). Back it
  up with the Mac mini's local project data. It is not the author's manuscript.
- On network failure during page creation, **do not retry by hand blindly**.
  First run --audit; the next invocation searches the Notion child pages by
  exact normalized title to avoid creating duplicates.
- A missing/renamed ledger page, conflicting title, changed remote Markdown,
  or multiple unordered pending notes is a stop condition requiring review.
- Startup reconciliation updates tracked notes; it does not guess the writing
  order of untracked backlog notes.
- Never delete/recreate the first eleven Notion pages, renumber them, or
  overwrite a divergent Notion page to force the pipeline green.
