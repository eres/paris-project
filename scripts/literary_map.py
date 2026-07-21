#!/usr/bin/env python3
"""Literary map final-step action for done notes."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("note", type=Path)
    args = parser.parse_args()
    note = args.note.resolve()
    payload = {
        "action": "literary_map",
        "note": str(note),
        "exists": note.is_file(),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(payload, ensure_ascii=False))
    if not note.is_file():
        print(f"ERROR: note not found: {note}", file=sys.stderr)
        return 2
    marker = note.with_suffix(note.suffix + ".literary_map.ran")
    marker.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
