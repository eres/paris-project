#!/usr/bin/env python3
"""Proofread a Spanish markdown note with a local MLX Qwen model."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from config import FAST_READER

DEFAULT_MODEL = FAST_READER.model

SYSTEM_PROMPT = """\
Eres un corrector literario para Notre Paris, un proyecto de prosa, poesía, diálogo, memoria y observación filosófica en español.

Tu tarea es corregir una nota markdown en español sin reescribirla creativamente.

Debes:
- Corregir ortografía, tildes, puntuación y errores gramaticales evidentes.
- Preservar el estilo, el ritmo y el tono literario del autor.
- Mantener la estructura markdown original: encabezados, listas, enlaces wiki de Obsidian, citas y bloques de código.
- Conservar la longitud aproximada del texto; no acortes ni amplíes de forma significativa.

No debes:
- Reescribir pasajes con tu propia voz.
- Cambiar el significado, la intención o la atmósfera.
- Añadir comentarios, explicaciones, metadatos ni notas editoriales.
- Envolver la respuesta en bloques de código ni añadir prefacios como "Aquí está la versión corregida".

Responde únicamente con el markdown corregido de la nota. No incluyas bloques <think>. No expliques tu razonamiento.
"""

USER_PROMPT_TEMPLATE = """\
/no_think

Corrige la siguiente nota markdown en español y devuelve sólo el markdown corregido:

---
{content}
---
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Proofread a Spanish markdown note using a local MLX Qwen model.",
    )
    parser.add_argument(
        "note_path",
        type=Path,
        help="Path to the markdown note to proofread.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"MLX model id or local path (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=4096,
        help="Maximum tokens to generate (default: 4096).",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.1,
        help="Sampling temperature (default: 0.1).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path (default: 11 Proofread/<same-name>.proofread.md when inside an Obsidian vault).",
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


def proofread_output_path(note_path: Path, output: Path | None) -> Path:
    if output is not None:
        return output

    vault_root = find_vault_root(note_path)
    if vault_root is None:
        return note_path.with_name(f"{note_path.stem}.proofread.md")

    proofread_dir = vault_root / "11 Proofread"
    proofread_dir.mkdir(parents=True, exist_ok=True)
    return proofread_dir / f"{note_path.stem}.proofread.md"


def build_prompt(tokenizer, content: str) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_PROMPT_TEMPLATE.format(content=content)},
    ]
    return tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
    )


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


def run_model(
    model,
    tokenizer,
    prompt: str,
    *,
    max_tokens: int,
    temperature: float,
) -> str:
    from mlx_lm import generate

    # mlx-lm 0.31.x does not accept temperature/temp through this Python API path.
    # Keep sampling defaults here; expose temperature later through the CLI/server path if needed.
    return generate(
        model,
        tokenizer,
        prompt=prompt,
        verbose=False,
        max_tokens=max_tokens,
    )


def write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def proofread_note(
    note_path: Path,
    *,
    model_name: str = DEFAULT_MODEL,
    max_tokens: int = 4096,
    temperature: float = 0.1,
    output: Path | None = None,
) -> Path:
    from mlx_lm import load

    content = read_note(note_path)
    out_path = proofread_output_path(note_path, output)

    model, tokenizer = load(model_name)
    prompt = build_prompt(tokenizer, content)
    raw_response = run_model(
        model,
        tokenizer,
        prompt,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    corrected = extract_markdown_block(raw_response)
    if not corrected:
        raise ValueError("Model returned an empty proofread note.")

    write_markdown(out_path, corrected)
    return out_path


def main() -> int:
    args = parse_args()
    note_path = args.note_path.expanduser().resolve()

    try:
        out_path = proofread_note(
            note_path,
            model_name=args.model,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            output=args.output.expanduser().resolve() if args.output else None,
        )
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Proofread note written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
