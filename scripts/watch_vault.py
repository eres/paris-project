#!/usr/bin/env python3
"""Hybrid Obsidian vault watcher for Notre Paris.

Strategy:
  1. React immediately to filesystem events (created/modified/moved/deleted).
  2. Debounce + wait until the file is size/mtime stable (Obsidian atomic saves).
  3. Compare status against a persistent cache.
  4. Periodically reconcile by scanning watched folders (catch missed FSEvents).
  5. Idempotent transitions via cached last_transition fingerprint.

Workflow semantics (do not change lightly):
  draft → ready-for-review  → editorial_review only
  ready-for-review → done   → translate_fr then literary_map
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# Allow running as `python scripts/watch_vault.py`
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from actions import run_action
from frontmatter import get_status_from_text, read_note_text
from status_cache import StatusCache
from transitions import detect_transition

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency 'watchdog'. Install with: pip install -r requirements.txt"
    ) from exc

DEFAULT_WATCH_DIRS = (
    "01 Places",
    "02 People",
    "03 Scenes",
    "04 Themes",
    "05 Fragments",
)

logger = logging.getLogger("notre_paris.watcher")


def configure_logging(log_file: Path | None, verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=handlers,
        force=True,
    )


def fingerprint_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_note_path(path: Path) -> bool:
    name = path.name
    if not name.endswith(".md"):
        return False
    if name.startswith("."):
        return False
    if name.endswith(".tmp") or name.endswith("~"):
        return False
    # Obsidian / editor temp patterns
    if name.startswith("~$") or ".obsidian" in path.parts:
        return False
    return True


@dataclass
class PendingEvent:
    path: Path
    scheduled_at: float
    source: str


class NoteProcessor:
    """Debounced, idempotent note transition processor."""

    def __init__(
        self,
        vault: Path,
        cache: StatusCache,
        *,
        debounce_s: float = 0.35,
        stable_checks: int = 3,
        stable_interval_s: float = 0.12,
        dry_run: bool = False,
        python: str | None = None,
    ) -> None:
        self.vault = vault.resolve()
        self.cache = cache
        self.debounce_s = debounce_s
        self.stable_checks = stable_checks
        self.stable_interval_s = stable_interval_s
        self.dry_run = dry_run
        self.python = python
        self._lock = threading.RLock()
        self._pending: dict[str, PendingEvent] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._processing: set[str] = set()

    def note_key(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.vault))
        except ValueError:
            return str(path.resolve())

    def schedule(self, path: Path, *, source: str) -> None:
        if not is_note_path(path):
            logger.debug("ignored_non_note path=%s source=%s", path, source)
            return
        key = self.note_key(path)
        with self._lock:
            self._pending[key] = PendingEvent(
                path=path.resolve(),
                scheduled_at=time.time(),
                source=source,
            )
            old = self._timers.pop(key, None)
            if old:
                old.cancel()
            timer = threading.Timer(self.debounce_s, self._run_pending, args=(key,))
            timer.daemon = True
            self._timers[key] = timer
            timer.start()
            logger.info(
                "event_scheduled key=%s path=%s source=%s debounce_s=%.3f",
                key,
                path,
                source,
                self.debounce_s,
            )

    def _wait_until_stable(self, path: Path) -> bool:
        last: tuple[int, float] | None = None
        stable = 0
        for _ in range(self.stable_checks * 4):
            try:
                st = path.stat()
            except OSError as exc:
                logger.warning("stable_stat_failed path=%s error=%s", path, exc)
                time.sleep(self.stable_interval_s)
                continue
            sig = (st.st_size, st.st_mtime)
            if sig == last:
                stable += 1
                if stable >= self.stable_checks:
                    logger.debug(
                        "file_stable path=%s size=%s mtime=%s",
                        path,
                        st.st_size,
                        st.st_mtime,
                    )
                    return True
            else:
                stable = 0
                last = sig
            time.sleep(self.stable_interval_s)
        logger.warning("file_unstable_giving_up path=%s", path)
        return path.is_file()

    def _run_pending(self, key: str) -> None:
        with self._lock:
            pending = self._pending.pop(key, None)
            self._timers.pop(key, None)
            if not pending:
                return
            if key in self._processing:
                # Reschedule after current run.
                self.schedule(pending.path, source=f"{pending.source}+busy")
                return
            self._processing.add(key)
        try:
            self.process_path(pending.path, source=pending.source)
        finally:
            with self._lock:
                self._processing.discard(key)

    def process_path(self, path: Path, *, source: str) -> bool:
        """Process one note. Returns True if a transition action ran."""
        key = self.note_key(path)
        logger.info("process_begin key=%s path=%s source=%s", key, path, source)

        if not path.is_file():
            logger.info("process_skip_missing key=%s path=%s", key, path)
            return False

        if not self._wait_until_stable(path):
            logger.warning("process_skip_unstable key=%s path=%s", key, path)
            return False

        try:
            text = read_note_text(path)
        except Exception:
            logger.exception("process_read_failed key=%s path=%s", key, path)
            return False

        if text is None:
            logger.warning("process_skip_unreadable key=%s path=%s", key, path)
            return False

        current = get_status_from_text(text)
        fp = fingerprint_text(text)
        previous = self.cache.get_status(key)
        last_transition = self.cache.get_last_transition(key)

        logger.info(
            "status_compare key=%s previous=%r current=%r fingerprint=%s source=%s",
            key,
            previous,
            current,
            fp[:12],
            source,
        )

        if current is None:
            logger.info("decision=no_status key=%s", key)
            self.cache.update(key, status=None, fingerprint=fp)
            return False

        transition = detect_transition(previous, current)
        if transition is None:
            logger.info(
                "decision=no_transition key=%s previous=%r current=%r",
                key,
                previous,
                current,
            )
            self.cache.update(key, status=current, fingerprint=fp)
            return False

        # Idempotency: same transition already applied for this fingerprint.
        transition_token = f"{transition.transition_id}:{fp}"
        if last_transition == transition_token:
            logger.info(
                "decision=already_processed key=%s token=%s",
                key,
                transition_token,
            )
            self.cache.update(key, status=current, fingerprint=fp)
            return False

        logger.info(
            "decision=run_transition key=%s transition=%s actions=%s",
            key,
            transition.label,
            [a.value for a in transition.actions],
        )

        all_ok = True
        for action in transition.actions:
            result = run_action(
                action,
                path,
                python=self.python,
                dry_run=self.dry_run,
            )
            if not result.ok:
                all_ok = False
                logger.error(
                    "transition_action_failed key=%s action=%s exit_code=%s",
                    key,
                    action.value,
                    result.exit_code,
                )
                break

        if all_ok:
            self.cache.update(
                key,
                status=current,
                fingerprint=fp,
                last_transition=transition_token,
            )
            logger.info("transition_complete key=%s transition=%s", key, transition.label)
            return True

        # Keep previous status so a later reconcile can retry.
        logger.error("transition_aborted_keeping_previous key=%s previous=%r", key, previous)
        return False

    def reconcile(self, roots: list[Path]) -> int:
        """Scan watched roots and process notes whose status differs from cache."""
        logger.info("reconcile_begin roots=%s", [str(r) for r in roots])
        ran = 0
        for root in roots:
            if not root.is_dir():
                logger.warning("reconcile_missing_root path=%s", root)
                continue
            for path in sorted(root.rglob("*.md")):
                if not is_note_path(path):
                    continue
                key = self.note_key(path)
                try:
                    text = read_note_text(path)
                except Exception:
                    logger.exception("reconcile_read_failed path=%s", path)
                    continue
                if text is None:
                    continue
                current = get_status_from_text(text)
                previous = self.cache.get_status(key)
                if current == previous:
                    continue
                logger.info(
                    "reconcile_diff key=%s previous=%r current=%r",
                    key,
                    previous,
                    current,
                )
                if self.process_path(path, source="reconcile"):
                    ran += 1
        logger.info("reconcile_end transitions_run=%d", ran)
        return ran

    def bootstrap(self, roots: list[Path]) -> None:
        """Seed cache with current statuses without running transitions."""
        logger.info("bootstrap_begin")
        count = 0
        for root in roots:
            if not root.is_dir():
                continue
            for path in root.rglob("*.md"):
                if not is_note_path(path):
                    continue
                key = self.note_key(path)
                try:
                    text = read_note_text(path)
                except Exception:
                    logger.exception("bootstrap_read_failed path=%s", path)
                    continue
                if text is None:
                    continue
                status = get_status_from_text(text)
                fp = fingerprint_text(text)
                self.cache.update(key, status=status, fingerprint=fp)
                count += 1
        logger.info("bootstrap_end notes=%d", count)

    def shutdown(self) -> None:
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
            self._pending.clear()


class VaultEventHandler(FileSystemEventHandler):
    def __init__(self, processor: NoteProcessor) -> None:
        super().__init__()
        self.processor = processor

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        event_type = event.event_type
        src = getattr(event, "src_path", None)
        dest = getattr(event, "dest_path", None)
        logger.info(
            "fs_event type=%s src_path=%s dest_path=%s",
            event_type,
            src,
            dest,
        )

        # Obsidian atomic save often looks like: write temp → rename over target.
        if event_type in {"moved", "renamed"} and dest:
            self.processor.schedule(Path(str(dest)), source=f"fs:{event_type}:dest")
            if src:
                # Also poke src in case of weird swap patterns.
                src_path = Path(str(src))
                if is_note_path(src_path):
                    self.processor.schedule(src_path, source=f"fs:{event_type}:src")
            return

        if event_type in {"created", "modified", "closed"} and src:
            self.processor.schedule(Path(str(src)), source=f"fs:{event_type}")
            return

        if event_type == "deleted" and src:
            logger.info("fs_deleted_ignored_for_transition src_path=%s", src)


class WatchService:
    def __init__(
        self,
        vault: Path,
        watch_dirs: list[str],
        cache_path: Path,
        *,
        reconcile_every_s: float = 5.0,
        debounce_s: float = 0.35,
        dry_run: bool = False,
        bootstrap: bool = False,
        python: str | None = None,
    ) -> None:
        self.vault = vault.resolve()
        self.watch_dir_names = watch_dirs
        self.roots = [(self.vault / name).resolve() for name in watch_dirs]
        self.cache = StatusCache(cache_path)
        self.processor = NoteProcessor(
            self.vault,
            self.cache,
            debounce_s=debounce_s,
            dry_run=dry_run,
            python=python,
        )
        self.reconcile_every_s = reconcile_every_s
        self.bootstrap = bootstrap
        self._stop = threading.Event()
        self._observer: Observer | None = None

    def start(self) -> None:
        logger.info("watcher_start vault=%s cwd=%s pid=%s python=%s", self.vault, os.getcwd(), os.getpid(), sys.executable)
        logger.info("watcher_paths=%s", [str(r) for r in self.roots])
        for root in self.roots:
            if not root.is_dir():
                logger.error(
                    "watched_path_missing path=%s — create the folder or fix VAULT path",
                    root,
                )
            else:
                # Prove read access without assuming write.
                try:
                    list(root.iterdir())
                    logger.info("watched_path_ok path=%s readable=yes", root)
                except OSError as exc:
                    logger.exception(
                        "watched_path_permission_error path=%s error=%s",
                        root,
                        exc,
                    )

        if self.bootstrap or not self.cache.path.exists():
            self.processor.bootstrap(self.roots)

        handler = VaultEventHandler(self.processor)
        observer = Observer()
        self._observer = observer
        scheduled = 0
        for root in self.roots:
            if root.is_dir():
                observer.schedule(handler, str(root), recursive=True)
                scheduled += 1
                logger.info("observer_scheduled path=%s recursive=true", root)
        if scheduled == 0:
            raise SystemExit("No watched directories exist; refusing to start.")

        observer.start()
        logger.info("observer_started")

        # Initial reconcile for transitions that happened while offline.
        self.processor.reconcile(self.roots)

        while not self._stop.wait(self.reconcile_every_s):
            try:
                self.processor.reconcile(self.roots)
            except Exception:
                logger.exception("reconcile_loop_error")

        logger.info("watcher_stopping")
        observer.stop()
        observer.join(timeout=5)
        self.processor.shutdown()
        logger.info("watcher_stopped")

    def stop(self) -> None:
        self._stop.set()


def default_vault() -> Path:
    env = os.environ.get("NOTRE_PARIS_VAULT")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Documents" / "Obsidian Vaults" / "Notre-Paris"


def default_cache_path() -> Path:
    env = os.environ.get("NOTRE_PARIS_STATE_DIR")
    if env:
        return Path(env).expanduser() / "status_cache.json"
    return Path.home() / ".local" / "state" / "notre-paris" / "status_cache.json"


def default_log_path() -> Path:
    env = os.environ.get("NOTRE_PARIS_LOG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "state" / "notre-paris" / "watch_vault.log"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Notre Paris Obsidian vault watcher")
    parser.add_argument("--vault", type=Path, default=default_vault())
    parser.add_argument(
        "--watch-dir",
        action="append",
        dest="watch_dirs",
        help="Relative folder under vault to watch (repeatable)",
    )
    parser.add_argument("--cache", type=Path, default=default_cache_path())
    parser.add_argument("--log-file", type=Path, default=default_log_path())
    parser.add_argument("--reconcile-every", type=float, default=5.0)
    parser.add_argument("--debounce", type=float, default=0.35)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Seed cache from current statuses without firing transitions first",
    )
    parser.add_argument("--once-reconcile", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--python", default=sys.executable)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_file, args.verbose)
    watch_dirs = args.watch_dirs or list(DEFAULT_WATCH_DIRS)
    service = WatchService(
        vault=args.vault,
        watch_dirs=watch_dirs,
        cache_path=args.cache,
        reconcile_every_s=args.reconcile_every,
        debounce_s=args.debounce,
        dry_run=args.dry_run,
        bootstrap=args.bootstrap,
        python=args.python,
    )

    if args.once_reconcile:
        if args.bootstrap or not service.cache.path.exists():
            service.processor.bootstrap(service.roots)
        ran = service.processor.reconcile(service.roots)
        logger.info("once_reconcile_done transitions_run=%d", ran)
        return 0

    def _handle_signal(signum: int, _frame: object) -> None:
        logger.info("signal_received signum=%s", signum)
        service.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    service.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
