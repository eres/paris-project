#!/usr/bin/env python3
"""Perform a deep literary and editorial review of one or more Notre Paris notes using OpenAI."""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from config import LITERARY_EDITOR

DEFAULT_MODEL = LITERARY_EDITOR.model

SYSTEM_PROMPT = """\
Eres un lector editorial profundo para Notre Paris, un proyecto híbrido entre prosa, poesía, diálogo, memoria y observación filosófica.

Actúas simultáneamente como:
- crítico literario
- editor de desarrollo
- arquitecto narrativo
- lector fenomenológico

Notre Paris no es una novela convencional. Parte de la experiencia de París en 2005, especialmente el quinto arrondissement, pero no debe leerse como memoria nostálgica ni literatura de viaje. París funciona como presencia psicológica, interlocutora y metafísica.

Temas centrales del proyecto:
- juventud
- amistad
- presente
- lenguaje
- transformación de identidad
- ciudad como interlocutora
- coincidencia
- pertenencia
- extranamiento

Tu tarea es analizar una o más notas markdown proporcionadas por el autor y producir una revisión editorial profunda orientada al desarrollo del proyecto.

No debes:
- reescribir el texto del autor
- generar ficción nueva
- corregir ortografía o gramática de forma sistemática
- resumir la nota como si fuera un informe académico distante

Debes:
- leer con atención literaria, fenomenológica y arquitectónica
- identificar atmósfera, lenguaje, percepción y ritmo
- señalar originalidad, tensiones, ecos y material no resuelto
- proponer conexiones con el proyecto mayor
- formular preguntas editoriales precisas
- sugerir oportunidades narrativas futuras sin inventar escenas completas

Responde únicamente con markdown bien estructurado. No incluyas bloques de código alrededor de la respuesta. No expliques tu razonamiento fuera del documento.

Usa exactamente estas secciones, en este orden, con estos encabezados de nivel 2:

## Resumen

## Valoración breve

## Lo que funciona

## Lo que se siente único

## Conexiones con otros temas

## Oportunidades narrativas

## Preguntas editoriales

## Contradicciones o tensiones

## Enlaces Obsidian sugeridos

Dentro de cada sección:
- escribe en español
- usa párrafos breves y listas con viñetas cuando ayuden a la claridad
- en «Enlaces Obsidian sugeridos», usa formato wiki-link de Obsidian, por ejemplo [[Jardin du Luxembourg]]
- mantén un tono exigente, lúcido y respetuoso con la voz del autor
"""

USER_PROMPT_TEMPLATE = """\
Analiza en profundidad la siguiente nota o conjunto de notas de Notre Paris y devuelve sólo la revisión editorial en markdown:

---
{content}
---
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Perform a deep literary and editorial review of one or more markdown notes using OpenAI.",
    )
    parser.add_argument(
        "note_paths",
        nargs="+",
        type=Path,
        help="Path(s) to the markdown note(s) to review.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"OpenAI model id (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=4096,
        help="Maximum output tokens to generate (default: 4096).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path (default: 20 Editorial Reviews/<stem>.editorial.md when inside an Obsidian vault).",
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


def editorial_output_stem(note_paths: list[Path]) -> str:
    if len(note_paths) == 1:
        return note_paths[0].stem
    return " + ".join(path.stem for path in note_paths)


def editorial_output_path(note_paths: list[Path], output: Path | None) -> Path:
    if output is not None:
        return output

    anchor = note_paths[0]
    vault_root = find_vault_root(anchor)
    filename = f"{editorial_output_stem(note_paths)}.editorial.md"

    if vault_root is None:
        return anchor.with_name(filename)

    reviews_dir = vault_root / "20 Editorial Reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    return reviews_dir / filename


def combine_notes(note_paths: list[Path]) -> str:
    sections: list[str] = []
    for path in note_paths:
        content = read_note(path)
        sections.append(f"## Archivo: {path.name}\n\n{content.strip()}")
    return "\n\n---\n\n".join(sections)


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


def render_review_header(note_paths: list[Path], review_body: str) -> str:
    title = editorial_output_stem(note_paths)
    sources = ", ".join(f"`{path.name}`" for path in note_paths)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    header = (
        f"# Revisión editorial — {title}\n\n"
        f"- Notas analizadas: {sources}\n"
        f"- Generado: {generated_at}\n\n"
        "---\n\n"
    )
    return header + review_body.strip() + "\n"


def get_openai_client():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError("OPENAI_API_KEY is not set.")

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


def deep_editorial_review(
    note_paths: list[Path],
    *,
    model: str = DEFAULT_MODEL,
    max_output_tokens: int = 4096,
    output: Path | None = None,
) -> Path:
    resolved_paths = [path.expanduser().resolve() for path in note_paths]
    combined_content = combine_notes(resolved_paths)
    out_path = editorial_output_path(resolved_paths, output)

    client = get_openai_client()
    raw_response = run_openai_review(
        client,
        instructions=SYSTEM_PROMPT,
        user_input=build_user_prompt(combined_content),
        model=model,
        max_output_tokens=max_output_tokens,
    )

    review_body = extract_markdown_block(raw_response)
    if not review_body:
        raise ValueError("Model returned an empty editorial review.")

    write_markdown(out_path, render_review_header(resolved_paths, review_body))
    return out_path


def main() -> int:
    args = parse_args()
    note_paths = [path.expanduser().resolve() for path in args.note_paths]

    try:
        out_path = deep_editorial_review(
            note_paths,
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
