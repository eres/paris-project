"""Robust YAML frontmatter extraction for Obsidian notes."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

FRONTMATTER_RE = re.compile(
    r"\A---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)",
    re.DOTALL,
)

STATUS_LINE_RE = re.compile(
    r"(?mi)^status\s*:\s*(.+?)\s*$",
)


def normalize_status(value: Any) -> str | None:
    """Normalize a status value to a comparable lowercase string."""
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value).lower()
    text = str(value).strip()
    if not text:
        return None
    if (text.startswith('"') and text.endswith('"')) or (
        text.startswith("'") and text.endswith("'")
    ):
        text = text[1:-1].strip()
    return text.lower()


def parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse YAML frontmatter from markdown text. Returns {} if absent/invalid."""
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    raw = match.group(1)
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        # Fallback: extract status line even if the rest of YAML is broken.
        status_match = STATUS_LINE_RE.search(raw)
        if status_match:
            return {"status": normalize_status(status_match.group(1))}
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def get_status_from_text(text: str) -> str | None:
    """Return normalized status from note text, or None."""
    fm = parse_frontmatter(text)
    if "status" in fm:
        return normalize_status(fm.get("status"))
    # Last-resort line scan (incomplete frontmatter / mid-write).
    status_match = STATUS_LINE_RE.search(text[:4000])
    if status_match:
        return normalize_status(status_match.group(1))
    return None


def read_note_text(path: Path, *, retries: int = 5, delay_s: float = 0.05) -> str | None:
    """Read note text with retries for mid-write / atomic replace races."""
    import time

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            if not path.is_file():
                time.sleep(delay_s)
                continue
            text = path.read_text(encoding="utf-8")
            # Treat truncated frontmatter as unstable.
            if text.startswith("---") and text.count("---") < 2:
                time.sleep(delay_s)
                continue
            return text
        except (OSError, UnicodeDecodeError) as exc:
            last_error = exc
            time.sleep(delay_s * (attempt + 1))
    if last_error:
        raise last_error
    return None


def get_status(path: Path) -> str | None:
    """Read and return normalized status for a note path."""
    text = read_note_text(path)
    if text is None:
        return None
    return get_status_from_text(text)
