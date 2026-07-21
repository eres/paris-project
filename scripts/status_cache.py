"""Persistent status cache for vault notes."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger("notre_paris.cache")


class StatusCache:
    """Disk-backed cache of last-known note statuses and content fingerprints."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, Any] = {"notes": {}}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            logger.info("cache_missing path=%s (starting empty)", self.path)
            return
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
            if "notes" not in self._data or not isinstance(self._data["notes"], dict):
                self._data = {"notes": {}}
            logger.info(
                "cache_loaded path=%s notes=%d",
                self.path,
                len(self._data["notes"]),
            )
        except (OSError, json.JSONDecodeError) as exc:
            logger.exception("cache_load_failed path=%s error=%s", self.path, exc)
            self._data = {"notes": {}}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._data, indent=2, sort_keys=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent),
            prefix=".status_cache.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def get_status(self, note_key: str) -> str | None:
        entry = self._data["notes"].get(note_key)
        if not entry:
            return None
        return entry.get("status")

    def get_fingerprint(self, note_key: str) -> str | None:
        entry = self._data["notes"].get(note_key)
        if not entry:
            return None
        return entry.get("fingerprint")

    def get_last_transition(self, note_key: str) -> str | None:
        entry = self._data["notes"].get(note_key)
        if not entry:
            return None
        return entry.get("last_transition")

    def update(
        self,
        note_key: str,
        *,
        status: str | None,
        fingerprint: str | None,
        last_transition: str | None = None,
    ) -> None:
        entry = self._data["notes"].setdefault(note_key, {})
        entry["status"] = status
        if fingerprint is not None:
            entry["fingerprint"] = fingerprint
        if last_transition is not None:
            entry["last_transition"] = last_transition
        self.save()

    def mark_transition(self, note_key: str, transition_id: str) -> None:
        entry = self._data["notes"].setdefault(note_key, {})
        entry["last_transition"] = transition_id
        self.save()

    def all_keys(self) -> set[str]:
        return set(self._data["notes"].keys())
