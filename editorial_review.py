#!/usr/bin/env python3
"""Create a structured literary editorial review for one Notre Paris note."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

from config import LITERARY_EDITOR

load_dotenv()

DEFAULT_MODEL = LITERARY_EDITOR.model

SYSTEM_PROMPT = """\
You are an editorial reader for Notre Paris.

Act as:
- a literary editor
- a narrative critic
- an attentive reader

Do not act as:
- a style corrector
- a writing professor
- a commercial editor
- a motivational coach

Assume Notre Paris is "un libro de poesía disfrazado de narrativa".

Your task is to evaluate the provided markdown note from that perspective.

Do not correct grammar.
Do not rewrite the text.
Do not suggest line-by-line changes.
Do not produce motivational praise.
Do not treat the note as tourism writing or commercial fiction.

Produce a structured editorial reflection in markdown.
Use exactly these headings, in this order:

# Editorial Review

## Summary

## Strengths

## Phenomenology

## Literary Qualities

## Risks

## Connections

## Opportunities

## Editorial Assessment

For Risks, consider possible literary risks such as:
- exceso de explicación
- nostalgia
- descripción turística
- abstracción excesiva
- repetición
- pérdida de tensión

For Connections, relate the note to central Notre Paris themes:
- amistad
- juventud
- ciudad
- lenguaje
- identidad
- coincidencia
- presente
- memoria

Write in Spanish, but keep the required section headings exactly as listed above.
Keep the tone lucid, precise, literary, and unsentimental.
Respond only with the markdown review. Do not wrap the response in code fences. Do not include <think> blocks.
"""

USER_PROMPT_TEMPLATE = """\
Review the following Notre Paris markdown note and return only the structured editorial review:

---
{content}
---
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a structured literary editorial review for one Notre Paris markdown note.",
    )
    parser.add_argument(
        "note_path",
        type=Path,
        help="Path to the markdown note to review.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"OpenAI model id (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=2400,
        help="Maximum output tokens to generate (default: 2400).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path (default: 13 Editorial Review/<same-name>.editorial.md when inside an Obsidian vault).",
    )
    return parser.parse_args()


def read_note(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Note not found: {path}")
    if not path.is_file():
        raise ValueError(f"Not a file: {path}")
    return path.read_text(encoding="utf-8")


def find_vault_root(path: Path) -> Path | None:
    """Find the nearest Obsidian vault root by looking for a .obsidian folder."""
    current = path.parent
    for candidate in [current, *current.parents]:
        if (candidate / ".obsidian").exists():
            return candidate
    return None


def editorial_output_path(note_path: Path, output: Path | None) -> Path:
    if output is not None:
        return output

    vault_root = find_vault_root(note_path)
    filename = f"{note_path.stem}.editorial.md"

    if vault_root is None:
        return note_path.with_name(filename)

    editorial_dir = vault_root / "13 Editorial Review"
    editorial_dir.mkdir(parents=True, exist_ok=True)
    return editorial_dir / filename


def build_user_prompt(content: str) -> str:
    return USER_PROMPT_TEMPLATE.format(content=content)


def extract_markdown_block(text: str) -> str:
    text = re.sub(
        r"<think>.*?</think>",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    ).strip()

    if "<think>" in text.lower():
        raise ValueError("Model response still contains a <think> block.")

    fenced = re.search(
        r"```(?:markdown|md)?\s*\n(.*?)```",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        return fenced.group(1).strip()

    return text.strip()


def write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def get_openai_client():
    try:
        api_key = os.environ["OPENAI_API_KEY"]
    except KeyError as exc:
        raise EnvironmentError(
            "OPENAI_API_KEY was not found in the environment or in .env."
        ) from exc

    from openai import OpenAI

    return OpenAI(api_key=api_key)


def run_openai_review(
    client,
    *,
    instructions: str,
    user_input: str,
    model: str,
    max_output_tokens: int,
) -> str:
    response = client.responses.create(
        model=model,
        instructions=instructions,
        input=user_input,
        max_output_tokens=max_output_tokens,
    )
    output_text = getattr(response, "output_text", None)
    if output_text:
        return output_text

    chunks: list[str] = []
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                chunks.append(text)

    if not chunks:
        raise ValueError("OpenAI response did not contain any text output.")

    return "\n".join(chunks)


def editorial_review(
    note_path: Path,
    *,
    model: str = DEFAULT_MODEL,
    max_output_tokens: int = 2400,
    output: Path | None = None,
) -> Path:
    resolved_path = note_path.expanduser().resolve()
    content = read_note(resolved_path)
    out_path = editorial_output_path(resolved_path, output)

    client = get_openai_client()
    raw_response = run_openai_review(
        client,
        instructions=SYSTEM_PROMPT,
        user_input=build_user_prompt(content),
        model=model,
        max_output_tokens=max_output_tokens,
    )

    review = extract_markdown_block(raw_response)
    if not review:
        raise ValueError("Model returned an empty editorial review.")

    write_markdown(out_path, review)
    return out_path


def main() -> int:
    args = parse_args()
    note_path = args.note_path.expanduser().resolve()

    try:
        out_path = editorial_review(
            note_path,
            model=args.model,
            max_output_tokens=args.max_output_tokens,
            output=args.output.expanduser().resolve() if args.output else None,
        )
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    except EnvironmentError as exc:
        print(exc, file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Editorial review written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
