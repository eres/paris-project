#!/usr/bin/env python3
"""Publish completed Notre Paris French notes to Notion, without changing the originals.

The local vault determines eligibility. A local ledger preserves page identity and
the last seen remote snapshot; edits made directly in Notion are never overwritten.
"""
from __future__ import annotations

import argparse
import hashlib
import fcntl
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

from watch_vault import WATCH_DIRS, get_note_status
from translate_note_fr import split_frontmatter

NOTION_VERSION = "2026-03-11"
STATE_FILE = Path(__file__).parent / ".notion-sync-state.json"
ALIASES_FILE = Path(__file__).parent / ".notion-sync-aliases.json"
TITLE_RE = re.compile(r"^(\d+)\.\s+(.+)$")
DATE_KEYS = {"date", "created", "updated", "fecha"}
_PUNCTUATION = str.maketrans({
    "\u2019": "'",
    "\u2018": "'",
    "\u02bc": "'",
    "`": "'",
    "\u2013": "-",
    "\u2014": "-",
})
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class PermanentSyncError(ValueError):
    """A conflict that will not succeed until a person resolves it."""


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_notion_id(value: str) -> str:
    raw = value.strip().lower().replace("-", "")
    if len(raw) == 32 and all(character in "0123456789abcdef" for character in raw):
        return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"
    return value.strip()


def comparable(title: str) -> str:
    title = unicodedata.normalize("NFKD", title)
    title = "".join(character for character in title if not unicodedata.combining(character))
    title = title.translate(_PUNCTUATION)
    return " ".join(title.casefold().split())


def normalized_body(text: str) -> str:
    text = unicodedata.normalize("NFKC", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = text.translate(_PUNCTUATION)
    lines = [" ".join(line.split()) for line in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines).strip())
    return text


def iso_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def file_facts(path: Path) -> dict:
    if not path.is_file():
        return {"exists": False}
    stat = path.stat()
    facts = {"exists": True, "mtime_utc": iso_timestamp(stat.st_mtime)}
    birth = getattr(stat, "st_birthtime", None)
    if birth:
        facts["birthtime_utc"] = iso_timestamp(birth)
    return facts


def title_parts(title: str) -> tuple[int, str] | None:
    match = TITLE_RE.match(title.strip())
    return (int(match.group(1)), match.group(2).strip()) if match else None


def vault_for(path: Path) -> Path:
    for folder in [path.parent, *path.parents]:
        if (folder / ".obsidian").is_dir():
            return folder
    raise ValueError("The source note is not inside an Obsidian vault")


def french_path(vault: Path, source: Path) -> Path:
    return vault / "12 French" / f"{source.stem}.fr.md"


def french_body(vault: Path, source: Path) -> str:
    translated = french_path(vault, source)
    if not translated.is_file():
        raise FileNotFoundError(f"French translation is absent: {translated}")
    _, body = split_frontmatter(translated.read_text(encoding="utf-8"))
    body = body.strip()
    if not body:
        raise ValueError(f"French translation is empty: {translated}")
    return body


def _markdown_files(vault: Path) -> list[Path]:
    result = []
    for folder in sorted(WATCH_DIRS):
        root = vault / folder
        if root.is_dir():
            result.extend(path for path in root.rglob("*.md") if path.is_file())
    return sorted(result, key=lambda path: path.relative_to(vault).as_posix())


def sources(vault: Path) -> list[Path]:
    result = [path for path in _markdown_files(vault) if get_note_status(path) == "done"]
    stems = {}
    for path in result:
        previous = stems.get(comparable(path.stem))
        if previous is not None:
            raise PermanentSyncError(f"Ambiguous French filename for {previous} and {path}")
        stems[comparable(path.stem)] = path
    return result


def frontmatter_dates(path: Path) -> list[dict]:
    raw, _body = split_frontmatter(path.read_text(encoding="utf-8"))
    if not raw:
        return []
    found = []
    for line in raw.splitlines():
        key, separator, value = line.partition(":")
        if not separator or key.strip().lower() not in DATE_KEYS:
            continue
        quoted = value.strip().strip("'\"")
        found.append({
            "key": key.strip(),
            "value": quoted,
            "narrative_date_ignored": bool(re.search(r"\b200[45]\b", quoted)),
        })
    return found


class NotionAPI:
    def __init__(self, token: str, page_id: str, client: httpx.Client | None = None):
        if not token:
            raise ValueError("NOTION_API_KEY is not configured")
        if not page_id:
            raise ValueError("NOTION_NOTES_PAGE_ID is not configured")
        self.page_id = canonical_notion_id(page_id)
        self.client = client or httpx.Client(
            base_url="https://api.notion.com",
            timeout=25,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Notion-Version": NOTION_VERSION,
            },
        )

    def request(self, method: str, path: str, **kwargs) -> dict:
        # A timeout on POST is not retried: the page may already exist and the
        # next run rediscovers it by title. A rejected POST (429 or 5xx) did
        # not create a page, so it is safe to retry.
        for attempt in range(4):
            try:
                response = self.client.request(method, path, **kwargs)
            except httpx.TimeoutException:
                if method == "POST" or attempt == 3:
                    raise
                time.sleep(2 ** attempt)
                continue
            except httpx.TransportError:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
                continue
            if response.status_code in RETRYABLE_STATUS and attempt < 3:
                retry_after = response.headers.get("Retry-After", "")
                delay = float(retry_after) if retry_after.isdigit() else 2 ** attempt
                time.sleep(min(delay, 30))
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("Notion request retries exhausted")

    def child_pages(self) -> list[dict]:
        pages = []
        cursor = None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            response = self.request(
                "GET", f"/v1/blocks/{self.page_id}/children", params=params
            )
            for item in response["results"]:
                if item.get("type") == "child_page" and not item.get("archived"):
                    pages.append({
                        "id": item["id"],
                        "title": item["child_page"]["title"],
                    })
            cursor = response.get("next_cursor")
            if not response.get("has_more"):
                return pages

    def markdown(self, page_id: str) -> str:
        response = self.request(
            "GET", f"/v1/pages/{page_id}/markdown",
            params={"markdown_version": "v2"},
        )
        if response.get("truncated") or response.get("unknown_block_ids"):
            raise PermanentSyncError(f"Notion page has unrendered blocks: {page_id}")
        return response["markdown"]

    def create(self, title: str, body: str) -> str:
        response = self.request("POST", "/v1/pages", json={
            "parent": {"type": "page_id", "page_id": self.page_id},
            "properties": {
                "title": {"type": "title", "title": [{"type": "text", "text": {"content": title}}]}
            },
            "markdown_version": "v2",
            "markdown": body,
        })
        return response["id"]

    def replace(self, page_id: str, body: str) -> list:
        result = self.request("PATCH", f"/v1/pages/{page_id}/markdown", json={
            "markdown_version": "v2",
            "type": "replace_content",
            "replace_content": {"new_str": body},
        })
        if result.get("truncated") or result.get("unknown_block_ids"):
            raise PermanentSyncError(f"Notion reported an incomplete update: {page_id}")
        return list(result.get("warnings") or [])


class NotionSync:
    def __init__(
        self,
        vault: Path,
        api: NotionAPI,
        state_path: Path = STATE_FILE,
        *,
        allow_unlinked_history: bool = False,
    ):
        self.vault = vault.resolve()
        self.api = api
        self.state_path = state_path
        self.allow_unlinked_history = allow_unlinked_history
        self.state = self._load()
        self.aliases = self._load_aliases()
        self.remote = self.api.child_pages()
        self._check_remote()

    def _load(self) -> dict:
        if not self.state_path.exists():
            return {"version": 1, "pages": {}}
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("pages"), dict):
            raise PermanentSyncError("Unrecognized Notion sync ledger format")
        return data

    def _load_aliases(self) -> dict[str, str]:
        # Historical translations may have Spanish source filenames but a
        # French title in Notion. Link those by exact page ID, never by guess.
        if not ALIASES_FILE.exists():
            return {}
        aliases = json.loads(ALIASES_FILE.read_text(encoding="utf-8"))
        if not isinstance(aliases, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in aliases.items()
        ):
            raise PermanentSyncError("Notion aliases must map vault-relative paths to page IDs")
        if len(aliases.values()) != len(set(canonical_notion_id(value) for value in aliases.values())):
            raise PermanentSyncError("Two source notes cannot refer to one Notion page")
        return aliases

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            os.chmod(tmp, 0o600)
            json.dump(self.state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.state_path)

    def _check_remote(self) -> None:
        numbers = {}
        names = {}
        for page in self.remote:
            parts = title_parts(page["title"])
            if parts is None:
                continue
            number, title = parts
            if number in numbers:
                raise PermanentSyncError(f"Duplicate Notion sequence #{number}")
            if comparable(title) in names:
                raise PermanentSyncError(f"Duplicate Notion title {title}")
            numbers[number] = page["id"]
            names[comparable(title)] = page

    def key(self, path: Path) -> str:
        return path.resolve().relative_to(self.vault).as_posix()

    def match(self, path: Path) -> dict | None:
        key = self.key(path)
        page_id = self.state["pages"].get(key, {}).get("page_id") or self.aliases.get(key)
        if page_id:
            wanted = canonical_notion_id(page_id)
            found_by_id = [
                page for page in self.remote
                if canonical_notion_id(page["id"]) == wanted
            ]
            if len(found_by_id) != 1:
                raise PermanentSyncError(f"Notion page ID for {key} is not a child of Notes")
            return found_by_id[0]
        matches = []
        for page in self.remote:
            parts = title_parts(page["title"])
            if parts and comparable(parts[1]) == comparable(path.stem):
                matches.append(page)
        if len(matches) > 1:
            raise PermanentSyncError(f"Ambiguous Notion title: {path.stem}")
        return matches[0] if matches else None

    def _safe_match(self, path: Path) -> tuple[dict | None, str | None]:
        try:
            return self.match(path), None
        except PermanentSyncError as error:
            return None, str(error)

    def unlinked_numbered_pages(self) -> list[dict]:
        linked = set()
        for path in _markdown_files(self.vault):
            found, _error = self._safe_match(path)
            if found:
                linked.add(canonical_notion_id(found["id"]))
        for record in self.state["pages"].values():
            if record.get("page_id"):
                linked.add(canonical_notion_id(record["page_id"]))
        for page_id in self.aliases.values():
            linked.add(canonical_notion_id(page_id))
        unlinked = []
        for page in self.remote:
            if title_parts(page["title"]) is None:
                continue
            if canonical_notion_id(page["id"]) not in linked:
                unlinked.append(page)
        return unlinked

    def pending(self) -> list[Path]:
        return [
            path for path in sources(self.vault)
            if french_path(self.vault, path).is_file() and not self.match(path)
        ]

    def audit(self) -> dict:
        # Ambiguous Done filenames are a hard stop; do not invent a row for them.
        sources(self.vault)
        notes = []
        for path in _markdown_files(self.vault):
            key = self.key(path)
            note_status = get_note_status(path) or "missing"
            translated = french_path(self.vault, path)
            has_translation = translated.is_file()
            remote, error = self._safe_match(path)
            content_match = None
            if remote and has_translation and error is None:
                try:
                    content_match = normalized_body(self.api.markdown(remote["id"])) == normalized_body(
                        french_body(self.vault, path)
                    )
                except (PermanentSyncError, OSError, httpx.HTTPError, KeyError, ValueError):
                    content_match = None
            if error:
                status = "blocked"
            elif note_status != "done":
                status = "not_eligible"
            elif not has_translation:
                status = "missing_translation"
            elif remote is None:
                status = "pending"
            elif content_match is False:
                status = "divergent"
            elif content_match is None:
                status = "unreadable"
            else:
                status = "published"
            notes.append({
                "source": key,
                "note_status": note_status,
                "translation": has_translation,
                "french_file": translated.relative_to(self.vault).as_posix() if has_translation else None,
                "notion": remote["title"] if remote else None,
                "notion_page_id": remote["id"] if remote else None,
                "tracked": key in self.state["pages"],
                "status": status,
                "content_match": content_match,
                "proposed_number": None,
                "filesystem": file_facts(path),
                "translation_filesystem": file_facts(translated),
                "frontmatter_dates": frontmatter_dates(path),
                "error": error,
            })
        return {
            "writing_order": "not_inferred",
            "notes": notes,
            "notion_only": [
                {"id": page["id"], "title": page["title"]}
                for page in self.unlinked_numbered_pages()
            ],
        }

    def sync_one(self, path: Path, *, ordered_backfill: bool = False) -> str:
        path = path.resolve()
        key = self.key(path)
        if get_note_status(path) != "done":
            raise PermanentSyncError(f"Not Done: {key}")
        body = french_body(self.vault, path)
        source_hash = digest(body)
        found = self.match(path)
        record = self.state["pages"].get(key)

        if record:
            if not found or canonical_notion_id(found["id"]) != canonical_notion_id(record["page_id"]):
                raise PermanentSyncError(f"Tracked Notion page was moved or renamed: {key}")
            remote_md = self.api.markdown(found["id"])
            if digest(remote_md) != record.get("remote_hash"):
                raise PermanentSyncError(f"Notion has human edits: {found['title']}; preserved")
            if source_hash == record.get("source_hash"):
                return f"unchanged: {found['title']}"
            warnings = self.api.replace(found["id"], body) or []
            updated_md = self.api.markdown(found["id"])
            record.update(source_hash=source_hash, remote_hash=digest(updated_md))
            if warnings and normalized_body(updated_md) != normalized_body(body):
                record["lossy"] = True
                self._save()
                raise PermanentSyncError(f"Notion applied a lossy update: {found['title']}")
            record.pop("lossy", None)
            self._save()
            return f"updated: {found['title']}"

        if found:
            # Remember the existing page without writing. A later change of the
            # French source updates this same page unless Notion itself changes.
            remote_md = self.api.markdown(found["id"])
            self.state["pages"][key] = {
                "page_id": found["id"],
                "source_hash": source_hash,
                "remote_hash": digest(remote_md),
            }
            self._save()
            if normalized_body(remote_md) == normalized_body(body):
                return f"adopted matching page: {found['title']}"
            return f"linked without overwrite: {found['title']}"

        other_pending = [p for p in self.pending() if p != path]
        if other_pending and not ordered_backfill:
            names = ", ".join(self.key(p) for p in other_pending)
            raise PermanentSyncError(
                "Unnumbered completed notes require an ordered backfill first: " + names
            )
        unlinked = self.unlinked_numbered_pages()
        if unlinked and not self.allow_unlinked_history:
            titles = ", ".join(page["title"] for page in unlinked)
            raise PermanentSyncError(
                "Numbered Notion pages are not linked to a vault note; "
                "map them in .notion-sync-aliases.json before creating pages: " + titles
            )
        numbers = [
            parts[0] for page in self.remote
            if (parts := title_parts(page["title"])) is not None
        ]
        number = max(numbers, default=0) + 1
        title = f"{number}. {path.stem}"
        page_id = self.api.create(title, body)
        self.remote.append({"id": page_id, "title": title})
        remote_md = self.api.markdown(page_id)
        self.state["pages"][key] = {
            "page_id": page_id,
            "source_hash": source_hash,
            "remote_hash": digest(remote_md),
        }
        self._save()
        return f"created: {title}"

    def backfill(self, order_file: Path | None) -> list[str]:
        missing = self.pending()
        if not missing:
            return ["No pending translated Done notes"]
        if len(missing) > 1 and order_file is None:
            raise PermanentSyncError(
                "Multiple pending notes need explicit writing order. "
                "Use --order-file with one vault-relative source-note path per line:\n" +
                "\n".join(self.key(p) for p in missing)
            )
        if order_file:
            ordered_keys = [
                line.strip() for line in order_file.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            expected = {self.key(p) for p in missing}
            if len(ordered_keys) != len(set(ordered_keys)) or set(ordered_keys) != expected:
                raise PermanentSyncError("Order file must list each pending source note exactly once")
            paths = [self.vault / key for key in ordered_keys]
        else:
            paths = missing
        return [self.sync_one(path, ordered_backfill=True) for path in paths]

    def reconcile(self) -> list[str]:
        results = []
        for path in sources(self.vault):
            if not french_path(self.vault, path).is_file():
                continue
            found, error = self._safe_match(path)
            if error:
                results.append(f"blocked: {self.key(path)}: {error}")
                continue
            if found or self.key(path) in self.state["pages"]:
                try:
                    results.append(self.sync_one(path))
                except PermanentSyncError as error:
                    results.append(f"blocked: {self.key(path)}: {error}")
        # Recover a single completed note missed during a restart. Never
        # guess the order when several unpublished French notes coexist.
        # Creation still stops when numbered pages are not linked to the vault.
        awaiting = self.pending()
        if len(awaiting) == 1:
            try:
                results.append(self.sync_one(awaiting[0]))
            except PermanentSyncError as error:
                results.append(f"blocked: {self.key(awaiting[0])}: {error}")
        elif awaiting:
            results.append(
                "Backfill required (writing order unknown): " +
                ", ".join(self.key(path) for path in awaiting)
            )
        return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish completed French notes into Notre Paris / Notes")
    parser.add_argument("source", type=Path, nargs="?", help="One original Done note")
    parser.add_argument("--vault", type=Path)
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--order-file", type=Path)
    parser.add_argument("--reconcile", action="store_true")
    args = parser.parse_args()
    try:
        if args.source:
            vault = vault_for(args.source.expanduser().resolve())
        elif args.vault:
            vault = args.vault.expanduser().resolve()
        else:
            parser.error("Provide a source note or --vault")
        api = NotionAPI(
            os.getenv("NOTION_API_KEY", ""),
            os.getenv("NOTION_NOTES_PAGE_ID", ""),
        )
        # Serialize watcher/manual invocations to prevent competing sequence numbers.
        lock_path = STATE_FILE.with_suffix(".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            sync = NotionSync(vault, api)
            if args.audit:
                print(json.dumps(sync.audit(), ensure_ascii=False, indent=2))
            elif args.backfill:
                for outcome in sync.backfill(args.order_file):
                    print(outcome)
            elif args.reconcile:
                for outcome in sync.reconcile():
                    print(outcome)
            elif args.source:
                print(sync.sync_one(args.source))
            else:
                parser.error("Choose --audit, --backfill, --reconcile or a source note")
            return 0
        finally:
            os.close(fd)
    except PermanentSyncError as error:
        print(f"[NOTION] Sync blocked (permanent): {error}", file=sys.stderr)
        return 2
    except FileNotFoundError as error:
        print(f"[NOTION] Sync blocked (permanent): {error}", file=sys.stderr)
        return 2
    except (ValueError, OSError, httpx.HTTPError, KeyError) as error:
        print(f"[NOTION] Sync blocked: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
