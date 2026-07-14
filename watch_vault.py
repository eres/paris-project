#!/usr/bin/env python3

import argparse
import hashlib
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv(Path(__file__).parent / ".env")

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers.polling import PollingObserver
except ModuleNotFoundError:
    Observer = None
    PollingObserver = None
    FileSystemEventHandler = object

from proofread_note import proofread_output_path

IGNORED_DIRS = {
    ".obsidian",
    "08 Archive",
    "10 Literary Map",
    "10 AI Suggestions",
    "11 Proofread",
    "12 French",
    "13 Editorial Review",
}

WATCH_DIRS = {
    "01 Places",
    "02 People",
    "03 Scenes",
    "04 Themes",
    "05 Fragments",
}

PROCESSABLE_STATUSES = {"ready_for_review", "done"}
PIPELINES = {
    "ready_for_review": ("proofread", "editorial_review"),
    "done": ("translate_fr", "literary_map"),
}

DEFAULT_DEBOUNCE_SECONDS = 60
DEBUG_DEBOUNCE_SECONDS = 5
OBSERVER_START_TIMEOUT_SECONDS = 15
VAULT_ACCESS_TIMEOUT_SECONDS = 10


def log(message: str, *, stream=sys.stdout) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{timestamp} {message}", file=stream, flush=True)


def normalize_note_status(value) -> str | None:
    if value is None:
        return None

    normalized = str(value).strip().strip("\"'“”‘’").lower()
    normalized = re.sub(r"[\s-]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized or None


def get_note_status(note_path: Path) -> str | None:
    try:
        with note_path.open("r", encoding="utf-8") as file:
            first_line = file.readline()
            if first_line.strip() != "---":
                return None

            frontmatter_lines: list[str] = []
            for line in file:
                if line.strip() == "---":
                    break
                frontmatter_lines.append(line)
            else:
                return None
    except OSError as exc:
        log(f"[STATUS] Warning: could not read frontmatter from {note_path}: {exc}", stream=sys.stderr)
        return None

    frontmatter = "".join(frontmatter_lines)
    if yaml is not None:
        try:
            parsed = yaml.safe_load(frontmatter)
        except yaml.YAMLError as exc:
            log(f"[STATUS] Warning: invalid YAML frontmatter in {note_path}: {exc}", stream=sys.stderr)
            return None

        if not isinstance(parsed, dict):
            return None

        status_value = None
        for key, value in parsed.items():
            if str(key).lower() == "status":
                status_value = value
                break

        if status_value is None:
            return None

        return normalize_note_status(status_value)

    return parse_status_without_yaml(frontmatter, note_path)


def parse_status_without_yaml(frontmatter: str, note_path: Path) -> str | None:
    for line in frontmatter.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ("[" in stripped and "]" not in stripped) or ("{" in stripped and "}" not in stripped):
            log(f"[STATUS] Warning: invalid YAML frontmatter in {note_path}", stream=sys.stderr)
            return None

    for line in frontmatter.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().lower() == "status":
            return normalize_note_status(value)

    return None


def get_editorial_output_path(note_path: Path) -> Path:
    current = note_path.parent
    vault_root = None
    for candidate in [current, *current.parents]:
        if (candidate / ".obsidian").exists():
            vault_root = candidate
            break

    filename = f"{note_path.stem}.editorial.md"
    if vault_root is None:
        return note_path.with_name(filename)

    editorial_dir = vault_root / "13 Editorial Review"
    editorial_dir.mkdir(parents=True, exist_ok=True)
    return editorial_dir / filename


def validate_watch_access(watched_paths: list[Path]) -> bool:
    """Check protected-folder access in a killable child before starting FSEvents."""
    check_code = (
        "import os, sys; "
        "[(next(iter(os.scandir(path)), None)) for path in sys.argv[1:]]"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", check_code, *(str(path) for path in watched_paths)],
            check=False,
            capture_output=True,
            text=True,
            timeout=VAULT_ACCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        log(
            "[WATCH] Vault access check timed out. On macOS, grant Full Disk Access "
            "to the Python.app used by .venv before restarting the service.",
            stream=sys.stderr,
        )
        return False

    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        log(f"[WATCH] Vault access check failed: {detail}", stream=sys.stderr)
        return False

    return True


class VaultHandler(FileSystemEventHandler):
    def __init__(
        self,
        vault_path: Path,
        proofread_path: Path,
        translate_path: Path,
        editorial_path: Path,
        literary_map_path: Path,
        editorial_review_enabled: bool = True,
        literary_map_enabled: bool = True,
        force_translate: bool = False,
        force_editorial_review: bool = False,
        debounce_seconds: int = DEFAULT_DEBOUNCE_SECONDS,
    ):
        self.vault_path = vault_path
        self.proofread_path = proofread_path
        self.translate_path = translate_path
        self.editorial_path = editorial_path
        self.literary_map_path = literary_map_path
        self.editorial_review_enabled = editorial_review_enabled
        self.literary_map_enabled = literary_map_enabled
        self.force_translate = force_translate
        self.force_editorial_review = force_editorial_review
        self.debounce_seconds = debounce_seconds
        self.pending = {}
        self.last_observed_status: dict[Path, str | None] = {}
        self.status_generation: dict[Path, int] = {}
        self.last_successful_signature: dict[tuple[Path, str], str] = {}
        self.last_successful_generation: dict[tuple[Path, str], int] = {}

    def observe_status(self, path: Path, status: str | None) -> int:
        if path not in self.last_observed_status:
            self.last_observed_status[path] = status
            self.status_generation[path] = 0
        elif self.last_observed_status[path] != status:
            self.last_observed_status[path] = status
            self.status_generation[path] += 1
        return self.status_generation[path]

    def should_process(self, path: Path) -> tuple[bool, str]:
        if path.suffix.lower() != ".md":
            return False, "not a .md file"

        if any(part in IGNORED_DIRS for part in path.parts):
            return False, "inside generated or ignored directory"

        try:
            relative = path.relative_to(self.vault_path)
        except ValueError:
            return False, f"outside vault path {self.vault_path}"

        if not relative.parts:
            return False, "path is the vault root"

        if relative.parts[0] not in WATCH_DIRS:
            allowed = ", ".join(sorted(WATCH_DIRS))
            return False, f"outside watched note directories ({allowed})"

        return True, "eligible markdown note"

    def log_ignored(self, path: Path, reason: str) -> None:
        log(f"[WATCH] Ignored: {path} ({reason})")

    def queue(self, path: Path, event_type: str):
        resolved = path.expanduser().resolve()
        log(f"[WATCH] Event detected: {event_type} {resolved}")

        should_process, reason = self.should_process(resolved)
        if not should_process:
            self.log_ignored(resolved, reason)
            return

        self.observe_status(resolved, get_note_status(resolved))
        self.pending[resolved] = time.time()
        log(f"[WATCH] Queued: {resolved}")

    def on_created(self, event):
        if event.is_directory:
            self.log_ignored(Path(event.src_path), "directory event")
            return
        self.queue(Path(event.src_path), "created")

    def on_modified(self, event):
        if event.is_directory:
            self.log_ignored(Path(event.src_path), "directory event")
            return
        self.queue(Path(event.src_path), "modified")

    def on_moved(self, event):
        if event.is_directory:
            self.log_ignored(Path(event.dest_path), "directory event")
            return
        self.queue(Path(event.dest_path), "moved")

    def run_step(self, tag: str, script: Path, note_path: Path) -> bool:
        command = [sys.executable, str(script), str(note_path)]
        log(f"[{tag}] Running: {' '.join(command)}")

        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
        except Exception:
            log(f"[{tag}] Subprocess could not start for {note_path}", stream=sys.stderr)
            log(traceback.format_exc(), stream=sys.stderr)
            return False

        if result.stdout:
            log(f"[{tag}] stdout:\n{result.stdout.rstrip()}")
        if result.stderr:
            log(f"[{tag}] stderr:\n{result.stderr.rstrip()}", stream=sys.stderr)

        if result.returncode == 0:
            log(f"[{tag}] Success: {note_path}")
            return True

        log(f"[{tag}] Failure: {note_path} (exit code {result.returncode})", stream=sys.stderr)
        return False

    def run_review_pipeline(self, path: Path) -> bool:
        if not self.run_step("PROOFREAD", self.proofread_path, path):
            return False

        proofread_file = proofread_output_path(path, None)
        if not proofread_file.exists():
            log(
                f"[PROOFREAD] Failure: expected output not found at {proofread_file}",
                stream=sys.stderr,
            )
            return False

        log(f"[PROOFREAD] Output found: {proofread_file}")

        if not self.editorial_review_enabled:
            log(f"[EDITORIAL] Editorial review disabled: {path}")
            return True

        log(f"[EDITORIAL] Editorial review started: {path}")
        if not self.run_step("EDITORIAL", self.editorial_path, path):
            return False

        editorial_file = get_editorial_output_path(path)
        if not editorial_file.exists():
            log(
                f"[EDITORIAL] Failure: expected output not found at {editorial_file}",
                stream=sys.stderr,
            )
            return False

        log(f"[EDITORIAL] Output found: {editorial_file}")
        return True

    def run_done_pipeline(self, path: Path) -> bool:
        log(f"[TRANSLATE] Final translation started: {path}")
        if not self.run_step("TRANSLATE", self.translate_path, path):
            log(
                "[LITERARY_MAP] Skipped because final translation failed.",
                stream=sys.stderr,
            )
            return False

        if not self.literary_map_enabled:
            log("[LITERARY_MAP] Literary map disabled")
            return True

        log(f"[LITERARY_MAP] Literary map started: {self.vault_path}")
        if not self.run_step("LITERARY_MAP", self.literary_map_path, self.vault_path):
            return False

        return True

    def run_pipeline(self, path: Path) -> bool:
        should_process, reason = self.should_process(path)
        if not should_process:
            self.log_ignored(path, reason)
            return False

        if not path.exists():
            self.log_ignored(path, "file no longer exists")
            return False

        status = get_note_status(path)
        generation = self.observe_status(path, status)
        if status is None:
            log("[STATUS] Missing")
        else:
            log(f"[STATUS] Found: {status}")

        requested_pipelines = []
        if status in PIPELINES:
            requested_pipelines.append(status)
        if self.force_editorial_review and "ready_for_review" not in requested_pipelines:
            log("[EDITORIAL] Review pipeline forced by –force-editorial-review")
            requested_pipelines.append("ready_for_review")
        if self.force_translate and "done" not in requested_pipelines:
            log("[TRANSLATE] Final pipeline forced by –force-translate")
            requested_pipelines.append("done")

        if not requested_pipelines:
            allowed = ", ".join(sorted(PROCESSABLE_STATUSES))
            log(f"[WATCH] Skipped: status {status!r} is not one of {allowed}")
            return True

        try:
            signature = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            log(f"[WATCH] Failure: could not fingerprint {path}: {exc}", stream=sys.stderr)
            return False

        for pipeline_status in requested_pipelines:
            key = (path, pipeline_status)
            last_signature = self.last_successful_signature.get(key)
            if last_signature == signature:
                log(
                    f"[WATCH] Skipped duplicate unchanged {pipeline_status} note: {path}"
                )
                continue

            if self.last_successful_generation.get(key) == generation:
                log(
                    f"[WATCH] Skipped changed note still in {pipeline_status}; "
                    "move it to another status before setting this status again."
                )
                continue

            actions = " -> ".join(PIPELINES[pipeline_status])
            log(f"[WATCH] Processing {pipeline_status}: {actions} ({path})")
            if pipeline_status == "ready_for_review":
                succeeded = self.run_review_pipeline(path)
            else:
                succeeded = self.run_done_pipeline(path)

            if not succeeded:
                return False

            self.last_successful_signature[key] = signature
            self.last_successful_generation[key] = generation

        return True

    def process_pending(self):
        now = time.time()
        ready = [
            path for path, timestamp in self.pending.items()
            if now - timestamp >= self.debounce_seconds
        ]

        for path in ready:
            # Remove the event before processing so a new edit that arrives while
            # the pipeline is running remains queued for a later pass.
            self.pending.pop(path, None)
            self.run_pipeline(path)


def main():
    parser = argparse.ArgumentParser(
        description="Watch or manually process Notre Paris markdown notes.",
    )
    parser.add_argument(
        "vault_path",
        type=Path,
        nargs="?",
        default=Path("."),
        help="Path to the Obsidian vault (default: current directory).",
    )
    parser.add_argument(
        "--once",
        type=Path,
        default=None,
        help="Process a single note path once and exit.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=f"Use a shorter debounce ({DEBUG_DEBOUNCE_SECONDS}s) and verbose logs.",
    )
    parser.add_argument(
        "--observer",
        choices=("auto", "native", "polling"),
        default="auto",
        help="Filesystem observer backend (auto uses polling on macOS).",
    )
    parser.add_argument(
        "--proofread",
        type=Path,
        default=Path(__file__).parent / "proofread_note.py",
    )
    parser.add_argument(
        "--translate",
        type=Path,
        default=Path(__file__).parent / "translate_note_fr.py",
    )
    parser.add_argument(
        "--editorial",
        type=Path,
        default=Path(__file__).parent / "editorial_review.py",
    )
    parser.add_argument(
        "--literary-map",
        type=Path,
        default=Path(__file__).parent / "literary_map.py",
    )
    parser.add_argument(
        "--editorial-review",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run the Editorial Review stage (default: enabled).",
    )
    parser.add_argument(
        "--skip-literary-map",
        action="store_true",
        help="Skip the Literary Map stage.",
    )
    parser.add_argument(
        "--force-translate",
        action="store_true",
        help="Run French translation even when the note frontmatter status is not done.",
    )
    parser.add_argument(
        "--force-editorial-review",
        action="store_true",
        help="Run Editorial Review even when the note frontmatter status is not ready_for_review.",
    )
    args = parser.parse_args()

    vault_path = args.vault_path.expanduser().resolve()
    proofread_path = args.proofread.expanduser().resolve()
    translate_path = args.translate.expanduser().resolve()
    editorial_path = args.editorial.expanduser().resolve()
    literary_map_path = args.literary_map.expanduser().resolve()
    debounce_seconds = DEBUG_DEBOUNCE_SECONDS if args.debug else DEFAULT_DEBOUNCE_SECONDS

    handler = VaultHandler(
        vault_path,
        proofread_path,
        translate_path,
        editorial_path,
        literary_map_path,
        editorial_review_enabled=args.editorial_review,
        literary_map_enabled=not args.skip_literary_map,
        force_translate=args.force_translate,
        force_editorial_review=args.force_editorial_review,
        debounce_seconds=debounce_seconds,
    )

    if args.debug:
        log("[WATCH] Debug mode enabled")

    if args.once is not None:
        note_path = args.once.expanduser().resolve()
        log(f"[WATCH] Manual once mode: {note_path}")
        return 0 if handler.run_pipeline(note_path) else 1

    if Observer is None:
        log(
            "[WATCH] watchdog is not installed; install requirements.txt to watch the vault.",
            stream=sys.stderr,
        )
        return 1

    use_polling = args.observer == "polling" or (
        args.observer == "auto" and sys.platform == "darwin"
    )
    observer_class = PollingObserver if use_polling else Observer
    if observer_class is None:
        log("[WATCH] Requested observer backend is unavailable.", stream=sys.stderr)
        return 1

    if not vault_path.is_dir():
        log(f"[WATCH] Vault directory not found: {vault_path}", stream=sys.stderr)
        return 1

    watched_paths = []
    for directory_name in sorted(WATCH_DIRS):
        watched_path = vault_path / directory_name
        if watched_path.is_dir():
            watched_paths.append(watched_path)
        else:
            log(f"[WATCH] Watched directory is absent: {watched_path}")

    if not watched_paths:
        log("[WATCH] No eligible note directories exist in the vault.", stream=sys.stderr)
        return 1

    if not validate_watch_access(watched_paths):
        return 1

    observer = observer_class()
    for watched_path in watched_paths:
        observer.schedule(handler, str(watched_path), recursive=True)

    start_errors = []
    start_finished = threading.Event()

    def start_observer():
        try:
            observer.start()
        except BaseException as exc:
            start_errors.append(exc)
        finally:
            start_finished.set()

    log(f"[WATCH] Starting {'polling' if use_polling else 'native'} observer for {vault_path}")
    start_thread = threading.Thread(target=start_observer, daemon=True)
    start_thread.start()

    if not start_finished.wait(OBSERVER_START_TIMEOUT_SECONDS):
        log(
            "[WATCH] Filesystem observer startup timed out. On macOS, grant Full Disk "
            "Access to the Python.app used by .venv before restarting the service.",
            stream=sys.stderr,
        )
        return 1

    if start_errors:
        log("[WATCH] Filesystem observer could not start.", stream=sys.stderr)
        log(
            "".join(traceback.format_exception(start_errors[0])),
            stream=sys.stderr,
        )
        return 1

    log(f"[WATCH] Watching vault: {vault_path}")
    for watched_path in watched_paths:
        log(f"[WATCH] Watching directory: {watched_path}")
    log(f"[WATCH] Observer: {'polling' if use_polling else 'native'}")
    log(f"[WATCH] Debounce: {debounce_seconds}s")
    log(f"[WATCH] Editorial review: {'enabled' if args.editorial_review else 'disabled'}")
    log(f"[WATCH] Literary map: {'enabled' if not args.skip_literary_map else 'disabled'}")
    log(f"[WATCH] Force translate: {'enabled' if args.force_translate else 'disabled'}")
    log(f"[WATCH] Force editorial review: {'enabled' if args.force_editorial_review else 'disabled'}")
    log("[WATCH] Press Ctrl+C to stop.")

    stop_requested = threading.Event()

    def request_stop(signum, _frame):
        log(f"[WATCH] Stop requested by signal {signum}")
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    try:
        while not stop_requested.is_set():
            handler.process_pending()
            stop_requested.wait(5)
    finally:
        observer.stop()
        observer.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
