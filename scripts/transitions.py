"""Status transition semantics for Notre Paris."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Action(str, Enum):
    EDITORIAL_REVIEW = "editorial_review"
    TRANSLATE_FR = "translate_fr"
    LITERARY_MAP = "literary_map"


@dataclass(frozen=True)
class Transition:
    previous: str | None
    current: str
    actions: tuple[Action, ...]
    transition_id: str

    @property
    def label(self) -> str:
        return f"{self.previous or '∅'}→{self.current}"


# Explicit workflow. Do not invent extra transitions.
TRANSITION_ACTIONS: dict[tuple[str | None, str], tuple[Action, ...]] = {
    ("draft", "ready-for-review"): (Action.EDITORIAL_REVIEW,),
    ("ready-for-review", "done"): (Action.TRANSLATE_FR, Action.LITERARY_MAP),
}


def detect_transition(previous: str | None, current: str | None) -> Transition | None:
    """Return a Transition if previous→current maps to workflow actions."""
    if current is None:
        return None
    if previous == current:
        return None
    actions = TRANSITION_ACTIONS.get((previous, current))
    if not actions:
        return None
    transition_id = f"{previous or 'none'}__{current}"
    return Transition(
        previous=previous,
        current=current,
        actions=actions,
        transition_id=transition_id,
    )
