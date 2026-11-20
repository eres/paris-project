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
DEFAULT_NOTES_PAGE = "3ccf9bd4-a7ae-8157-b497-fb287db20c8a"
STATE_FILE = Path(__file__).parent / ".notion-sync-state.json"
TITLE_RE = re.compile(r"^(\d+)\.\s+(.+)$")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def comparable(title: str) -> str:
    title = unicodedata.normalize("NFKD", title)
    title = "".join(c for c in title if not unicodedata.combining(c))
    return " ".join(title.casefold().split())


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


def sources(vault: Path) -> list[Path]:
    result = []
    for folder in sorted(WATCH_DIRS):
        root = vault / folder
        if root.is_dir():
            result.extend(
                path for path in root.rglob("*.md")
                if path.is_file() and get_note_status(path) == "done"
            )
    stems = {}
    for path in result:
        previous = stems.get(comparable(path.stem))
        if previous is not None:
            raise ValueError(f"Ambiguous French filename for {previous} and {path}")
        stems[comparable(path.stem)] = path
    return sorted(result, key=lambda path: path.relative_to(vault).as_posix())


class NotionAPI:
    def __init__(self, token: str, page_id: str):
        if not token:
            raise ValueError("NOTION_API_KEY is not configured")
        self.page_id = page_id
        self.client = httpx.Client(
            base_url="https://api.notion.com",
            timeout=25,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Notion-Version": NOTION_VERSION,
            },
        )

    def request(self, method: str, path: str, **kwargs) -> dict:
        # Creating a page is not retried automatically: a timeout may mean the
        # write succeeded. On a subsequent run we rediscover it by title.
        attempts = 1 if method == "POST" else 4
        for attempt in range(attempts):
            try:
                response = self.client.request(method, path, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt == attempts - 1:
                    raise
                time.sleep(2 ** attempt)
                continue
            if response.status_code in {429, 500, 502, 503, 504} and attempt < attempts - 1:
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
            raise ValueError(f"Notion page has unrendered blocks: {page_id}")
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

    def replace(self, page_id: str, body: str) -> None:
        result = self.request("PATCH", f"/v1/pages/{page_id}/markdown", json={
            "markdown_version": "v2",
            "type": "replace_content",
            "replace_content": {"new_str": body},
        })
        if result.get("truncated") or result.get("unknown_block_ids") or result.get("warnings"):
            raise ValueError(f"Notion reported an incomplete or lossy update: {page_id}")


class NotionSync:
    def __init__(self, vault: Path, api: NotionAPI, state_path: Path = STATE_FILE):
        self.vault = vault.resolve()
        self.api = api
        self.state_path = state_path
        self.state = self._load()
        self.remote = self.api.child_pages()
        self._check_remote()

    def _load(self) -> dict:
        if not self.state_path.exists():
            return {"version": 1, "pages": {}}
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("pages"), dict):
            raise ValueError("Unrecognized Notion sync ledger format")
        return data

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
                raise ValueError(f"Duplicate Notion sequence #{number}")
            if comparable(title) in names:
                raise ValueError(f"Duplicate Notion title {title}")
            numbers[number] = page["id"]
            names[comparable(title)] = page

    def key(self, path: Path) -> str:
        return path.resolve().relative_to(self.vault).as_posix()

    def match(self, path: Path) -> dict | None:
        matches = []
        for page in self.remote:
            parts = title_parts(page["title"])
            if parts and comparable(parts[1]) == comparable(path.stem):
                matches.append(page)
        if len(matches) > 1:
            raise ValueError(f"Ambiguous Notion title: {path.stem}")
        return matches[0] if matches else None

    def pending(self) -> list[Path]:
        return [
            path for path in sources(self.vault)
            if french_path(self.vault, path).is_file() and not self.match(path)
        ]

    def audit(self) -> list[dict]:
        rows = []
        for path in sources(self.vault):
            key = self.key(path)
            remote = self.match(path)
            rows.append({
                "source": key,
                "translation": french_path(self.vault, path).is_file(),
                "notion": remote["title"] if remote else None,
                "tracked": key in self.state["pages"],
                "status": "missing_translation" if not french_path(self.vault, path).is_file()
                          else "published" if remote else "pending",
            })
        return rows

    def sync_one(self, path: Path, *, ordered_backfill: bool = False) -> str:
        path = path.resolve()
        key = self.key(path)
        if get_note_status(path) != "done":
            raise ValueError(f"Not Done: {key}")
        body = french_body(self.vault, path)
        source_hash = digest(body)
        found = self.match(path)
        record = self.state["pages"].get(key)

        if record:
            if not found or found["id"] != record["page_id"]:
                raise ValueError(f"Tracked Notion page was moved or renamed: {key}")
            remote_md = self.api.markdown(found["id"])
            if digest(remote_md) != record["remote_hash"]:
                raise ValueError(f"Notion has human edits: {found['title']}; preserved")
            if source_hash == record["source_hash"]:
                return f"unchanged: {found['title']}"
            self.api.replace(found["id"], body)
            updated_md = self.api.markdown(found["id"])
            record.update(source_hash=source_hash, remote_hash=digest(updated_md))
            self._save()
            return f"updated: {found['title']}"

        if found:
            # Existing handwritten or crash-recovered pages are not overwritten.
            remote_md = self.api.markdown(found["id"])
            if remote_md.strip() == body.strip():
                self.state["pages"][key] = {
                    "page_id": found["id"],
                    "source_hash": source_hash,
                    "remote_hash": digest(remote_md),
                }
                self._save()
                return f"adopted matching page: {found['title']}"
            raise ValueError(
                f"Existing Notion page differs from local translation: {found['title']}; "
                "manual reconciliation required (no overwrite)"
            )

        other_pending = [p for p in self.pending() if p != path]
        if other_pending and not ordered_backfill:
            names = ", ".join(self.key(p) for p in other_pending)
            raise ValueError(
                "Unnumbered completed notes require an ordered backfill first: " + names
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
            raise ValueError(
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
                raise ValueError("Order file must list each pending source note exactly once")
            paths = [self.vault / key for key in ordered_keys]
        else:
            paths = missing
        return [self.sync_one(path, ordered_backfill=True) for path in paths]

    def reconcile(self) -> list[str]:
        results = []
        for key in sorted(self.state["pages"]):
            path = self.vault / key
            if path.is_file() and get_note_status(path) == "done" and french_path(self.vault, path).is_file():
                results.append(self.sync_one(path))
        # Recover a single completed note missed during a restart. Never
        # guess the order when several unpublished French notes coexist.
        awaiting = self.pending()
        if len(awaiting) == 1:
            results.append(self.sync_one(awaiting[0]))
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
            os.getenv("NOTION_NOTES_PAGE_ID", DEFAULT_NOTES_PAGE),
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
    except (ValueError, OSError, httpx.HTTPError, KeyError) as error:
        print(f"[NOTION] Sync blocked: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
