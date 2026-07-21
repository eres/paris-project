#!/usr/bin/env python3
"""Print raw filesystem events for a path (Obsidian write-pattern probe)."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class Printer(FileSystemEventHandler):
    def on_any_event(self, event) -> None:  # noqa: ANN001
        if event.is_directory:
            return
        dest = getattr(event, "dest_path", None)
        print(
            f"{time.time():.3f}\ttype={event.event_type}\tsrc={event.src_path}\tdest={dest}",
            flush=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--seconds", type=float, default=60.0)
    args = parser.parse_args()
    root = args.path.expanduser().resolve()
    if not root.exists():
        print(f"path does not exist: {root}", file=sys.stderr)
        return 2
    observer = Observer()
    observer.schedule(Printer(), str(root), recursive=True)
    observer.start()
    print(f"listening on {root} for {args.seconds}s", flush=True)
    try:
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        pass
    observer.stop()
    observer.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
