#!/usr/bin/env python3
"""Publish a finalized French Notre Paris note to the existing Notion Notes page.

Obsidian is authoritative. This script creates one numbered Notion page per source
note and replaces the French body on later Done transitions, preserving its number.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from markdown_it import MarkdownIt
from translate_note_fr import find_vault_root, french_output_path, split_frontmatter

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
MAX_CHILDREN = 100
NUMBERED_TITLE = re.compile(r"^\s*(\d+)\s*[.)]\s+(.+)$")


class PublishError(RuntimeError):
    pass


def rich_text(text: str, *, annotations=None, href=None) -> list[dict]:
    """Notion limits each rich-text item to 2,000 characters."""
    result = []
    for start in range(0, len(text), 1800):
        item = {"type": "text", "text": {"content": text[start:start + 1800]}}
        if href and href.startswith(("https://", "http://")):
            item["text"]["link"] = {"url": href}
        if annotations:
            item["annotations"] = annotations.copy()
        result.append(item)
    return result


def inline_parts(token) -> list[dict]:
    parts = []
    flags = {"bold": False, "italic": False, "strikethrough": False, "code": False}
    link = None
    for child in token.children or []:
        kind = child.type
        if kind == "strong_open":
            flags["bold"] = True
        elif kind == "strong_close":
            flags["bold"] = False
        elif kind == "em_open":
            flags["italic"] = True
        elif kind == "em_close":
            flags["italic"] = False
        elif kind == "s_open":
            flags["strikethrough"] = True
        elif kind == "s_close":
            flags["strikethrough"] = False
        elif kind == "link_open":
            link = child.attrGet("href")
        elif kind == "link_close":
            link = None
        elif kind == "code_inline":
            parts.extend(rich_text(child.content, annotations={**flags, "code": True}))
        elif kind in ("softbreak", "hardbreak"):
            parts.extend(rich_text("\n", annotations=flags))
        elif kind in ("text", "html_inline", "image"):
            parts.extend(rich_text(child.content, annotations=flags, href=link))
    return parts or rich_text(token.content)


def markdown_blocks(body: str) -> list[dict]:
    blocks = []
    list_types = []
    blockquote_depth = 0
    kind = "paragraph"
    for token in MarkdownIt("commonmark").parse(body):
        name = token.type
        if name == "bullet_list_open":
            list_types.append("bulleted_list_item")
        elif name == "ordered_list_open":
            list_types.append("numbered_list_item")
        elif name in ("bullet_list_close", "ordered_list_close"):
            list_types.pop()
        elif name == "blockquote_open":
            blockquote_depth += 1
        elif name == "blockquote_close":
            blockquote_depth -= 1
        elif name == "heading_open":
            kind = "heading_" + token.tag[-1]
        elif name == "paragraph_open":
            kind = list_types[-1] if list_types else ("quote" if blockquote_depth else "paragraph")
        elif name == "inline":
            actual_kind = kind if kind in {
                "paragraph", "quote", "heading_1", "heading_2", "heading_3",
                "bulleted_list_item", "numbered_list_item"
            } else "paragraph"
            blocks.append({
                "object": "block", "type": actual_kind,
                actual_kind: {"rich_text": inline_parts(token)},
            })
        elif name in ("fence", "code_block"):
            blocks.append({
                "object": "block", "type": "code",
                "code": {"rich_text": rich_text(token.content), "language": "plain text"},
            })
        elif name == "hr":
            blocks.append({"object": "block", "type": "divider", "divider": {}})
    if not blocks:
        raise PublishError("French note has no publishable body.")
    return blocks


class NotionAPI:
    def __init__(self, token: str):
        self.token = token

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = NOTION_API_BASE + path
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        for attempt in range(4):
            request = Request(url, method=method, data=data, headers={
                "Authorization": "Bearer " + self.token,
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            })
            try:
                with urlopen(request, timeout=30) as response:
                    return json.load(response)
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
                if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                    delay = exc.headers.get("Retry-After", "1")
                    try:
                        seconds = min(30, max(1, float(delay)))
                    except ValueError:
                        seconds = 2 ** attempt
                    time.sleep(seconds)
                    continue
                raise PublishError(f"Notion HTTP {exc.code} on {method} {path}: {detail}") from exc
            except (URLError, TimeoutError) as exc:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise PublishError(f"Notion unreachable on {method} {path}: {exc}") from exc
        raise PublishError("Notion request retries exhausted.")

    def children(self, block_id: str) -> list[dict]:
        output = []
        cursor = None
        while True:
            query = {"page_size": 100}
            if cursor:
                query["start_cursor"] = cursor
            response = self.request("GET", f"/blocks/{block_id}/children?{urlencode(query)}")
            output.extend(response.get("results", []))
            if not response.get("has_more"):
                return output
            cursor = response.get("next_cursor")
            if not cursor:
                raise PublishError("Notion pagination returned no cursor.")

    def append(self, page_id: str, blocks: list[dict]) -> list[str]:
        created_ids = []
        for start in range(0, len(blocks), MAX_CHILDREN):
            response = self.request("PATCH", f"/blocks/{page_id}/children", {
                "children": blocks[start:start + MAX_CHILDREN]
            })
            created_ids.extend(item["id"] for item in response.get("results", []))
        return created_ids

    def replace_body(self, page_id: str, blocks: list[dict]) -> None:
        """Append fully first; only remove old content after successful upload."""
        old = self.children(page_id)
        new_ids = []
        try:
            # Keep track of every successful batch for best-effort rollback.
            for start in range(0, len(blocks), MAX_CHILDREN):
                response = self.request("PATCH", f"/blocks/{page_id}/children", {
                    "children": blocks[start:start + MAX_CHILDREN]
                })
                new_ids.extend(item["id"] for item in response.get("results", []))
        except Exception:
            for block_id in new_ids:
                try:
                    self.request("DELETE", f"/blocks/{block_id}")
                except PublishError:
                    pass
            raise
        for item in old:
            self.request("DELETE", f"/blocks/{item['id']}")

    def create_page(self, parent_id: str, title: str, blocks: list[dict]) -> str:
        response = self.request("POST", "/pages", {
            "parent": {"type": "page_id", "page_id": parent_id},
            "properties": {"title": [{"type": "text", "text": {"content": title}}]},
            "children": blocks[:MAX_CHILDREN],
        })
        page_id = response["id"]
        if len(blocks) > MAX_CHILDREN:
            self.append(page_id, blocks[MAX_CHILDREN:])
        return page_id


def identity_title(title: str) -> str:
    match = NUMBERED_TITLE.match(title)
    without_number = match.group(2) if match else title
    return unicodedata.normalize("NFC", without_number).strip().casefold()


def select_destination(pages: list[dict], raw_title: str, stored_id: str | None):
    items = [
        (item["id"], item.get("child_page", {}).get("title", ""))
        for item in pages if item.get("type") == "child_page" and not item.get("archived")
    ]
    if stored_id:
        for page_id, title in items:
            if page_id == stored_id:
                return page_id, title
        raise PublishError("Previously published page is missing from Notes. Check access or restore it; refusing to create a duplicate.")
    matches = [(page_id, title) for page_id, title in items
               if identity_title(title) == identity_title(raw_title)]
    if len(matches) > 1:
        raise PublishError(f"Multiple Notion pages match {raw_title!r}; resolve manually.")
    if matches:
        return matches[0]
    numbers = [
        int(match.group(1))
        for _, title in items
        if (match := NUMBERED_TITLE.match(title))
    ]
    return None, f"{max(numbers, default=0) + 1}. {raw_title}"


def read_state(path: Path) -> dict:
    if not path.exists():
        return {"pages": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or not isinstance(state.get("pages"), dict):
        raise PublishError(f"Invalid publication state: {path}")
    return state


def save_state(path: Path, state: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".notion-state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(state, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def publish(note_path: Path, *, dry_run: bool = False, api: NotionAPI | None = None) -> tuple[str, str, str | None]:
    note_path = note_path.expanduser().resolve()
    vault = find_vault_root(note_path)
    if vault is None:
        raise PublishError("Source note must be inside an Obsidian vault.")
    source = note_path.relative_to(vault)
    if not source.parts or source.parts[0] not in {
        "01 Places", "02 People", "03 Scenes", "04 Themes", "05 Fragments"
    }:
        raise PublishError("Source must be in a Notre Paris note directory.")
    french_path = french_output_path(note_path, None)
    if not french_path.is_file():
        raise PublishError(f"French translation not found: {french_path}")
    _, body = split_frontmatter(french_path.read_text(encoding="utf-8"))
    blocks = markdown_blocks(body)
    token = os.environ.get("NOTION_API_KEY", "").strip()
    parent_id = os.environ.get("NOTION_NOTES_PAGE_ID", "").strip()
    if api is None and not token:
        raise PublishError("NOTION_API_KEY is missing.")
    if not parent_id:
        raise PublishError("NOTION_NOTES_PAGE_ID is missing.")
    api = api or NotionAPI(token)
    state_path = Path(os.environ.get("NOTION_PUBLISH_STATE_FILE", vault / ".notre-paris-notion-state.json"))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # macOS fcntl serializes manual runs and the watchdog, protecting numbering.
    lock_path = state_path.with_name(state_path.name + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = read_state(state_path)
        entry = state["pages"].get(str(source), {})
        previous_id = entry.get("page_id")
        children = api.children(parent_id)
        page_id, title = select_destination(children, note_path.stem, previous_id)
        operation = "update" if page_id else "create"
        if dry_run:
            return operation, title, page_id
        if page_id:
            api.replace_body(page_id, blocks)
        else:
            page_id = api.create_page(parent_id, title, blocks)
        state["pages"][str(source)] = {"page_id": page_id, "title": title}
        save_state(state_path, state)
        return operation, title, page_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish a finalized French Notre Paris note to Notion.")
    parser.add_argument("note_path", type=Path, help="Spanish source note, not the .fr.md output")
    parser.add_argument("--dry-run", action="store_true", help="Look up destination without publishing")
    args = parser.parse_args()
    try:
        operation, title, page_id = publish(args.note_path, dry_run=args.dry_run)
    except (PublishError, OSError, ValueError) as exc:
        print(f"[NOTION] Failed: {exc}", file=sys.stderr)
        return 1
    preview = " (dry-run)" if args.dry_run else ""
    print(f"[NOTION] {operation}{preview}: {title} ({page_id or 'new page'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
