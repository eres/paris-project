#!/usr/bin/env python3
"""Environment + access diagnostics for the Notre Paris vault watcher.

Run this on the Mac that hosts Obsidian Sync / launchd.
It does NOT assume permissions are broken — it probes and reports evidence.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def run(cmd: list[str]) -> dict:
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return {
            "cmd": cmd,
            "exit_code": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }
    except OSError as exc:
        return {"cmd": cmd, "exit_code": 127, "stdout": "", "stderr": str(exc)}


def check_path(path: Path) -> dict:
    info: dict = {"path": str(path), "exists": path.exists()}
    if not path.exists():
        return info
    try:
        info["realpath"] = str(path.resolve())
    except OSError as exc:
        info["realpath_error"] = str(exc)
    try:
        st = path.stat()
        info["mode"] = oct(st.st_mode)
        info["uid"] = st.st_uid
        info["gid"] = st.st_gid
        info["is_dir"] = path.is_dir()
        info["is_symlink"] = path.is_symlink()
    except OSError as exc:
        info["stat_error"] = str(exc)
        return info
    try:
        if path.is_dir():
            list(path.iterdir())
            info["readable"] = True
        else:
            path.read_bytes()[:1]
            info["readable"] = True
    except OSError as exc:
        info["readable"] = False
        info["read_error"] = str(exc)
    probe = path / ".notre_paris_write_probe" if path.is_dir() else path.with_suffix(path.suffix + ".probe")
    try:
        if path.is_dir():
            probe.write_text("ok\n", encoding="utf-8")
            probe.unlink()
            info["writable"] = True
        else:
            info["writable"] = os.access(path, os.W_OK)
    except OSError as exc:
        info["writable"] = False
        info["write_error"] = str(exc)
    return info


def main() -> int:
    vault = Path(
        os.environ.get(
            "NOTRE_PARIS_VAULT",
            str(Path.home() / "Documents" / "Obsidian Vaults" / "Notre-Paris"),
        )
    ).expanduser()
    agent = Path(
        os.environ.get(
            "NOTRE_PARIS_AGENT",
            str(Path.home() / "dev" / "notre-paris-agent"),
        )
    ).expanduser()
    label = "com.notreparis.watchvault"

    report = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "system": platform.system(),
        "python": sys.executable,
        "python_version": sys.version,
        "uid": os.getuid() if hasattr(os, "getuid") else None,
        "user": os.environ.get("USER"),
        "cwd": os.getcwd(),
        "env": {
            "NOTRE_PARIS_VAULT": os.environ.get("NOTRE_PARIS_VAULT"),
            "NOTRE_PARIS_STATE_DIR": os.environ.get("NOTRE_PARIS_STATE_DIR"),
            "NOTRE_PARIS_LOG": os.environ.get("NOTRE_PARIS_LOG"),
            "PATH": os.environ.get("PATH"),
        },
        "which_python3": shutil.which("python3"),
        "vault": check_path(vault),
        "agent": check_path(agent),
        "watch_dirs": [],
        "process": run(["pgrep", "-af", "watch_vault.py"]),
        "launchctl": run(["launchctl", "print", f"gui/{os.getuid()}/{label}"]),
        "commands": {
            "pwd": run(["pwd"]),
            "realpath_vault": run(["realpath", str(vault)]),
            "ls_ld_vault": run(["ls", "-ld", str(vault)]),
            "stat_vault": run(["stat", str(vault)]),
        },
    }

    for name in (
        "01 Places",
        "02 People",
        "03 Scenes",
        "04 Themes",
        "05 Fragments",
    ):
        report["watch_dirs"].append(check_path(vault / name))

    print(json.dumps(report, indent=2, ensure_ascii=False))

    # Exit non-zero only for hard blockers on this machine.
    if platform.system() != "Darwin":
        print(
            "\nNOTE: This diagnostic ran outside macOS. "
            "launchd/Full Disk Access checks are inconclusive here.",
            file=sys.stderr,
        )
        return 0
    if not report["vault"].get("exists"):
        return 2
    if report["vault"].get("readable") is False:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
