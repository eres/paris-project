#!/usr/bin/env python3
"""Translate a proofread Spanish markdown note into literary French with a local MLX Qwen model."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from config import FAST_READER

DEFAULT_MODEL = FAST_READER.model

SYSTEM_PROMPT = """\
Tu es un traducteur littéraire pour Notre Paris, un projet hybride : prose, poésie, dialogue, mémoire et observation philosophique.

Ta tâche est de traduire une note markdown en espagnol vers un français littéraire naturel.

Tu dois :
- Préserver le ton, le rythme, l'atmosphère et l'intention littéraire de l'original.
- Traduire de façon naturelle en français, sans calquer l'espagnol mot à mot.
- Conserver la structure markdown : titres, listes, citations, blocs de code.
- Préserver les liens wiki Obsidian ([[...]]) lorsque c'est possible ; ne les traduis pas.
- Conserver la longueur et la densité du texte ; ne résume pas, n'abrège pas, n'allonge pas.

Tu ne dois pas :
- Expliquer ta traduction ni commenter le texte.
- Résumer ou paraphraser de façon réductrice.
- Ajouter du contenu, des notes de traduction ni des métadonnées.
- Envelopper la réponse dans des blocs de code ni ajouter de préface comme « Voici la traduction ».

Réponds uniquement avec le markdown traduit en français. N'inclus pas de blocs <think>. N'explique pas ton raisonnement.
"""

USER_PROMPT_TEMPLATE = """\
/no_think

Traduis la note markdown suivante de l'espagnol vers le français littéraire et renvoie uniquement le markdown traduit.
Les lignes NOTRE_PARIS_SOURCE_BEGIN et NOTRE_PARIS_SOURCE_END délimitent la note source ; elles ne font pas partie de la note et ne doivent jamais apparaître dans ta réponse.

NOTRE_PARIS_SOURCE_BEGIN
{content}
NOTRE_PARIS_SOURCE_END
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Translate a proofread Spanish markdown note into literary French using a local MLX Qwen model.",
    )
    parser.add_argument(
        "note_path",
        type=Path,
        help="Path to the markdown note to translate (preferably from 11 Proofread).",
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
        default=0.2,
        help="Sampling temperature (default: 0.2).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path (default: 12 French/<same-name>.fr.md when inside an Obsidian vault).",
    )
    parser.add_argument(
        "--debug-raw",
        action="store_true",
        help="Print RAW MODEL OUTPUT before any cleanup or transformation.",
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


def french_output_stem(note_path: Path) -> str:
    """Derive the output stem, stripping a .proofread suffix when present."""
    stem = note_path.stem
    if stem.endswith(".proofread"):
        return stem.removesuffix(".proofread")
    return stem


def french_output_path(note_path: Path, output: Path | None) -> Path:
    if output is not None:
        return output

    vault_root = find_vault_root(note_path)
    base_name = f"{french_output_stem(note_path)}.fr.md"

    if vault_root is None:
        return note_path.with_name(base_name)

    french_dir = vault_root / "12 French"
    french_dir.mkdir(parents=True, exist_ok=True)
    return french_dir / base_name


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
    cleaned = re.sub(
        r"<think>.*?</think>",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )

    if "<think>" in cleaned.lower():
        raise ValueError("Model response still contains a <think> block.")

    fenced = re.search(
        r"```(?:markdown|md)?\s*\n(.*?)```",
        cleaned,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        return fenced.group(1)

    return cleaned


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
    if not content.endswith("\n"):
        content += "\n"
    path.write_text(content, encoding="utf-8")


def translate_note_fr(
    note_path: Path,
    *,
    model_name: str = DEFAULT_MODEL,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    output: Path | None = None,
    debug_raw: bool = False,
) -> Path:
    from mlx_lm import load

    content = read_note(note_path)
    out_path = french_output_path(note_path, output)

    model, tokenizer = load(model_name)
    prompt = build_prompt(tokenizer, content)
    raw_response = run_model(
        model,
        tokenizer,
        prompt,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    if debug_raw:
        print("RAW MODEL OUTPUT")
        print(raw_response)
        print("END RAW MODEL OUTPUT")

    translated = extract_markdown_block(raw_response)
    if not translated.strip():
        raise ValueError("Model returned an empty translated note.")

    write_markdown(out_path, translated)
    return out_path


def main() -> int:
    args = parse_args()
    note_path = args.note_path.expanduser().resolve()

    try:
        out_path = translate_note_fr(
            note_path,
            model_name=args.model,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            output=args.output.expanduser().resolve() if args.output else None,
            debug_raw=args.debug_raw,
        )
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"French translation written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
