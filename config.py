"""Central model configuration for Notre Paris agents."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    model: str
    base_url: str | None = None


FAST_READER = ModelConfig(
    provider=os.getenv("FAST_READER_PROVIDER", os.getenv("LOCAL_MODEL_PROVIDER", "local")),
    base_url=os.getenv("FAST_READER_BASE_URL", os.getenv("LOCAL_MODEL_BASE_URL", "")) or None,
    model=os.getenv("FAST_READER_MODEL", os.getenv("LOCAL_MODEL_NAME", "mlx-community/Qwen3-14B-4bit")),
)

LITERARY_EDITOR = ModelConfig(
    provider=os.getenv("LITERARY_EDITOR_PROVIDER", os.getenv("EDITORIAL_MODEL_PROVIDER", "openai")),
    base_url=os.getenv("LITERARY_EDITOR_BASE_URL", os.getenv("EDITORIAL_MODEL_BASE_URL", "")) or None,
    model=os.getenv("LITERARY_EDITOR_MODEL", os.getenv("EDITORIAL_MODEL_NAME", "gpt-5.5")),
)

# Backward-compatible aliases for older scripts or shell workflows.
LOCAL_MODEL = FAST_READER
EDITORIAL_MODEL = LITERARY_EDITOR
