"""Action runners invoked by the vault watcher."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from transitions import Action

logger = logging.getLogger("notre_paris.actions")

SCRIPTS_DIR = Path(__file__).resolve().parent

ACTION_SCRIPTS: dict[Action, str] = {
    Action.EDITORIAL_REVIEW: "editorial_review.py",
    Action.TRANSLATE_FR: "translate_note_fr.py",
    Action.LITERARY_MAP: "literary_map.py",
}


@dataclass
class ActionResult:
    action: Action
    command: list[str]
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def build_command(action: Action, note_path: Path, python: str | None = None) -> list[str]:
    script = SCRIPTS_DIR / ACTION_SCRIPTS[action]
    exe = python or sys.executable
    return [exe, str(script), str(note_path)]


def run_action(
    action: Action,
    note_path: Path,
    *,
    python: str | None = None,
    dry_run: bool = False,
    env: dict[str, str] | None = None,
) -> ActionResult:
    command = build_command(action, note_path, python=python)
    logger.info(
        "action_start action=%s note=%s command=%s dry_run=%s",
        action.value,
        note_path,
        command,
        dry_run,
    )
    if dry_run:
        return ActionResult(
            action=action,
            command=command,
            exit_code=0,
            stdout="[dry-run]",
            stderr="",
        )

    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=merged_env,
            check=False,
        )
    except OSError as exc:
        logger.exception(
            "action_exec_failed action=%s note=%s error=%s",
            action.value,
            note_path,
            exc,
        )
        return ActionResult(
            action=action,
            command=command,
            exit_code=127,
            stdout="",
            stderr=str(exc),
        )

    result = ActionResult(
        action=action,
        command=command,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    logger.info(
        "action_finished action=%s note=%s exit_code=%s stdout=%r stderr=%r",
        action.value,
        note_path,
        result.exit_code,
        result.stdout[-2000:],
        result.stderr[-2000:],
    )
    return result
