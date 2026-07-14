#!/usr/bin/env python3
"""Build a project-level literary map for the Notre Paris vault."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from config import FAST_READER, LITERARY_EDITOR

SOURCE_DIRS = {
    "01 Places",
    "02 People",
    "03 Scenes",
    "04 Themes",
    "05 Fragments",
}

IGNORED_DIRS = {
    ".obsidian",
    "08 Archive",
    "10 Literary Map",
    "10 AI Suggestions",
    "11 Proofread",
    "12 French",
    "13 Editorial Review",
}

OUTPUT_DIR = "10 Literary Map"
OUTPUT_FILE = "Literary Map.md"

EXTRACTION_SYSTEM_PROMPT = """\
Eres un lector-cartógrafo de Notre Paris.

Tu tarea es leer un conjunto de notas del proyecto y extraer señales literarias útiles para comprender cómo está evolucionando el libro.

No produzcas YAML.
No produzcas metadatos técnicos.
No clasifiques como una base de datos.
No inventes contenido que no esté sugerido por las notas.

Extrae, de forma sintética:
- constelaciones principales
- motivos recurrentes
- lugares fuertes
- zonas todavía débiles o poco desarrolladas
- hilos narrativos visibles
- relaciones simbólicas
- posibles secciones del libro
- preguntas abiertas

Escribe en español claro, preciso y literario. No incluyas bloques <think>.
"""

EXTRACTION_USER_PROMPT_TEMPLATE = """\
/no_think

Lee estas notas de Notre Paris y extrae señales literarias del conjunto:

{corpus}
"""

MAP_SYSTEM_PROMPT = """\
Eres el cartógrafo literario de Notre Paris.

Debes construir un mapa sintético del estado actual del libro para ayudar a Ernesto a comprender cómo está evolucionando el proyecto.

No produzcas metadatos.
No produzcas YAML.
No produzcas taxonomías técnicas.
No escribas como informe académico.
No inventes escenas ni datos.

Usa exactamente esta estructura de encabezados:

# Notre Paris — Literary Map

## Project State

## Main Constellations

## Recurring Motifs

## Strongest Places

## Underdeveloped Areas

## Narrative Threads

## Symbolic Network

## Possible Book Sections

## Open Questions

El cuerpo debe estar en español.
El tono debe ser claro, útil y sintético.
Usa párrafos breves y viñetas sólo cuando mejoren la lectura.
No incluyas bloques <think>. No envuelvas la respuesta en bloques de código.
"""

MAP_USER_PROMPT_TEMPLATE = """\
/no_think

Construye el mapa literario del proyecto a partir de estas señales extraídas de las notas:

---
{extracted_information}
---
"""


@dataclass(frozen=True)
class VaultNote:
    relative_path: Path
    title: str
    content: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the Notre Paris Literary Map using the configured FAST_READER role.",
    )
    parser.add_argument(
        "vault_path",
        type=Path,
        help="Path to the Obsidian vault.",
    )
    parser.add_argument(
        "--extraction-max-tokens",
        type=int,
        default=6000,
        help="Maximum tokens for the extraction pass (default: 6000).",
    )
    parser.add_argument(
        "--map-max-tokens",
        type=int,
        default=5000,
        help="Maximum tokens for the map pass (default: 5000).",
    )
    return parser.parse_args()


def log(message: str) -> None:
    print(message, flush=True)


def validate_fast_reader() -> None:
    if FAST_READER.provider != "local":
        raise ValueError(
            f"FAST_READER.provider must be 'local' for literary_map.py; got {FAST_READER.provider!r}."
        )

    if FAST_READER.model == LITERARY_EDITOR.model or "gpt-5.5" in FAST_READER.model.lower():
        raise ValueError("literary_map.py must use FAST_READER and cannot use GPT-5.5.")


def should_read_note(path: Path, vault_path: Path) -> bool:
    if path.suffix.lower() != ".md":
        return False

    try:
        relative = path.relative_to(vault_path)
    except ValueError:
        return False

    if not relative.parts:
        return False

    if any(part in IGNORED_DIRS for part in relative.parts):
        return False

    return relative.parts[0] in SOURCE_DIRS


def read_vault_notes(vault_path: Path) -> list[VaultNote]:
    if not vault_path.exists():
        raise FileNotFoundError(f"Vault not found: {vault_path}")
    if not vault_path.is_dir():
        raise ValueError(f"Not a directory: {vault_path}")

    notes: list[VaultNote] = []
    for source_dir in sorted(SOURCE_DIRS):
        root = vault_path / source_dir
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            if not should_read_note(path, vault_path):
                continue
            content = path.read_text(encoding="utf-8").strip()
            if not content:
                continue
            relative = path.relative_to(vault_path)
            notes.append(
                VaultNote(
                    relative_path=relative,
                    title=path.stem,
                    content=content,
                )
            )

    return notes


def build_corpus(notes: list[VaultNote]) -> str:
    sections: list[str] = []
    for note in notes:
        sections.append(
            "\n".join(
                [
                    f"## Nota: {note.title}",
                    f"Ruta: {note.relative_path.as_posix()}",
                    "",
                    note.content,
                ]
            )
        )
    return "\n\n---\n\n".join(sections)


def build_chat_prompt(tokenizer, system_prompt: str, user_prompt: str) -> str:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    return tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
    )


def run_local_model(model, tokenizer, prompt: str, *, max_tokens: int) -> str:
    from mlx_lm import generate

    return generate(
        model,
        tokenizer,
        prompt=prompt,
        verbose=False,
        max_tokens=max_tokens,
    )


def extract_markdown_response(text: str) -> str:
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


def extract_literary_information(model, tokenizer, notes: list[VaultNote], max_tokens: int) -> str:
    corpus = build_corpus(notes)
    prompt = build_chat_prompt(
        tokenizer,
        EXTRACTION_SYSTEM_PROMPT,
        EXTRACTION_USER_PROMPT_TEMPLATE.format(corpus=corpus),
    )
    raw_response = run_local_model(model, tokenizer, prompt, max_tokens=max_tokens)
    extracted = extract_markdown_response(raw_response)
    if not extracted:
        raise ValueError("Model returned an empty literary extraction.")
    return extracted


def build_literary_map(model, tokenizer, extracted_information: str, max_tokens: int) -> str:
    prompt = build_chat_prompt(
        tokenizer,
        MAP_SYSTEM_PROMPT,
        MAP_USER_PROMPT_TEMPLATE.format(extracted_information=extracted_information),
    )
    raw_response = run_local_model(model, tokenizer, prompt, max_tokens=max_tokens)
    literary_map = extract_markdown_response(raw_response)
    if not literary_map:
        raise ValueError("Model returned an empty literary map.")
    return literary_map


def literary_map_output_path(vault_path: Path) -> Path:
    return vault_path / OUTPUT_DIR / OUTPUT_FILE


def write_literary_map(vault_path: Path, content: str) -> Path:
    out_path = literary_map_output_path(vault_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content.rstrip() + "\n", encoding="utf-8")
    return out_path


def generate_literary_map(
    vault_path: Path,
    *,
    extraction_max_tokens: int = 6000,
    map_max_tokens: int = 5000,
) -> Path:
    from mlx_lm import load

    validate_fast_reader()

    log("[LITERARY_MAP] Reading notes...")
    notes = read_vault_notes(vault_path)
    if not notes:
        raise ValueError("No source markdown notes found for the literary map.")

    model, tokenizer = load(FAST_READER.model)

    log("[LITERARY_MAP] Extracting motifs...")
    extracted_information = extract_literary_information(
        model,
        tokenizer,
        notes,
        extraction_max_tokens,
    )

    log("[LITERARY_MAP] Building map...")
    literary_map = build_literary_map(
        model,
        tokenizer,
        extracted_information,
        map_max_tokens,
    )

    log("[LITERARY_MAP] Writing Literary Map.md")
    out_path = write_literary_map(vault_path, literary_map)
    log("[LITERARY_MAP] Success")
    return out_path


def main() -> int:
    args = parse_args()
    vault_path = args.vault_path.expanduser().resolve()

    try:
        out_path = generate_literary_map(
            vault_path,
            extraction_max_tokens=args.extraction_max_tokens,
            map_max_tokens=args.map_max_tokens,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Literary map written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
