#!/usr/bin/env python3
"""Analyze a markdown note with a local MLX Qwen model and extract literary metadata."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

from config import FAST_READER

DEFAULT_MODEL = FAST_READER.model

SYSTEM_PROMPT = """\
Eres el Archivista Literario de Notre Paris, un proyecto híbrido: parte prosa, parte poesía, parte diálogo, parte memoria y parte observación filosófica.

El proyecto parte de la experiencia de París en 2005, especialmente el quinto arrondissement, pero no debe convertirse en memoria nostálgica ni literatura de viaje. París debe tratarse como una presencia psicológica y metafísica activa.

Lee la nota markdown proporcionada por el usuario y extrae metadatos literarios estructurados para organizar el proyecto a largo plazo en Obsidian.

Responde sólo con YAML. No incluyas bloques markdown, comentario, análisis o texto fuera del documento YAML. No incluyas bloques <think>. No expliques tu razonamiento.

Todos los nombres de campos y todos los valores deben estar en español. Usa exactamente los nombres de campo del esquema siguiente.

Distinciones importantes:
- `personajes_o_personas` es sólo para personajes nombrados, figuras recurrentes o personas reales que importan narrativamente.
- No incluyas grupos genéricos como "estudiantes", "gente", "chicos", "transeuntes" o "multitudes" en `personajes_o_personas`.
- Los grupos humanos genéricos como estudiantes, parejas, transeuntes o multitudes deben ir en `presencia_humana`.
- `fecha_escrita` significa la fecha en que la nota fue escrita, no el año recordado de la experiencia.
- Si la nota describe recuerdos de París en 2005, coloca eso en `periodo_temporal`, no en `fecha_escrita`.
- `temas` debe nombrar preocupaciones literarias profundas, no actividades simples. Prefiere valores como juventud, amistad, lenguaje, extranamiento, pertenencia, atencion, transformacion, ciudad como interlocutora, presente, deseo, coincidencia, silencio.
- `observaciones_editoriales` debe describir cómo funciona la nota, no juzgarla genéricamente.
- `sugerencias_editoriales` debe evitar consejos vagos como "agrega mas detalle". Prefiere preguntas precisas, por ejemplo: "¿La ausencia de sonido es intencional aquí?"
- `posibles_enlaces_obsidian`:
  - "[[Jardin du Luxembourg]]"
Esquema obligatorio:

titulo: string or null
tipo: lugar | persona | escena | tema | fragmento | investigacion | otro
idioma: ISO 639-1 code or null
idioma_del_analisis: es
fecha_escrita: string or null
ambientacion:
  ciudad: string or null
  distrito: string or null
  lugares_especificos: [list of strings]
periodo_temporal: string or null
personajes_o_personas: [list of strings]
presencia_humana: [list of generic human presences, such as estudiantes, transeuntes, parejas, multitudes]
temas: [list of strings]
motivos: [list of strings]
objetos_o_texturas: [list of strings]
clima_o_luz: [list of strings]
sonidos: [list of strings]
registro_emocional: [list of strings]
enfoque_experiencial: [list of remembered experiential qualities, such as caminar, esperar, mirar hacia arriba, entrar, descender]
papel_de_la_ciudad: [list chosen from interlocutora, maestra, testigo, laberinto, espejo, catalizador, umbral, refugio, presion]
relevancia_de_la_amistad: ninguna | periferica | central
transformacion_de_identidad: ninguna | emergente | explicita
sentidos_faltantes: [list of senses that are absent or underdeveloped, such as olor, sonido, tacto, gusto]
preguntas_filosoficas: [list of strings]
hilos_narrativos: [list of strings]
posibles_enlaces_obsidian: [list of quoted strings using Obsidian wiki-link format, for example "[[Jardin du Luxembourg]]"]
notas_existentes_relacionadas: [list of note titles that likely already exist or are directly implied by this note]
posibles_notas_futuras: [list of new note titles suggested by the material]
posibles_contradicciones_a_verificar: [list of strings]
observaciones_editoriales: [list of concise observations about how the note works]
sugerencias_editoriales: [list of concise suggestions for revision, phrased as questions when possible]
posibles_adiciones: [list of concise suggestions for what the author could add]
resumen: one or two sentences
confianza: baja | media | alta
"""

USER_PROMPT_TEMPLATE = """\
/no_think

Analyze the following markdown note and return literary metadata as YAML only:

---
{content}
---
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract literary metadata from a markdown note using a local MLX Qwen model.",
    )
    parser.add_argument(
        "note_path",
        type=Path,
        help="Path to the markdown note to analyze.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"MLX model id or local path (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=2400,
        help="Maximum tokens to generate (default: 2400).",
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
        help="Output path (default: <note_stem>.analysis.md in 10 AI Suggestions when inside an Obsidian vault).",
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


def analysis_output_path(note_path: Path, output: Path | None) -> Path:
    if output is not None:
        return output

    vault_root = find_vault_root(note_path)
    if vault_root is None:
        return note_path.with_name(f"{note_path.stem}.analysis.md")

    suggestions_dir = vault_root / "10 AI Suggestions"
    suggestions_dir.mkdir(parents=True, exist_ok=True)
    return suggestions_dir / f"{note_path.stem}.analysis.md"


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


def extract_yaml_block(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    fenced = re.search(
        r"```(?:ya?ml)?\s*\n(.*?)```",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        return fenced.group(1).strip()

    for marker in ("titulo:", "title:"):
        start = text.find(marker)
        if start != -1:
            return text[start:].strip()

    start = text.find("---")
    if start != -1:
        return text[start:].strip()

    return text.strip()


def validate_yaml(text: str) -> tuple[dict | list | str | int | float | bool | None, str]:
    if "<think>" in text.lower():
        raise ValueError("Model response still contains a <think> block.")
    parsed = yaml.safe_load(text)
    if parsed is None:
        raise ValueError("YAML document is empty.")
    if not isinstance(parsed, dict):
        raise ValueError("Expected a YAML mapping at the top level.")
    normalized = yaml.safe_dump(parsed, sort_keys=False, allow_unicode=True)
    return parsed, normalized


def as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    if isinstance(value, str):
        return [value]
    return [str(value)]


def render_bullets(items: list[str]) -> str:
    if not items:
        return "- —\n"
    return "".join(f"- {item}\n" for item in items)


def render_analysis_markdown(parsed: dict, normalized_yaml: str) -> str:
    titulo = parsed.get("titulo") or "Sin título"
    tipo = parsed.get("tipo") or "—"
    resumen = parsed.get("resumen") or "—"
    ambientacion = parsed.get("ambientacion") or {}
    ciudad = ambientacion.get("ciudad") or "—"
    distrito = ambientacion.get("distrito") or "—"
    periodo = parsed.get("periodo_temporal") or "—"
    confianza = parsed.get("confianza") or "—"

    sections = [
        f"# Análisis IA — {titulo}\n\n",
        "## Resumen\n\n",
        f"{resumen}\n\n",
        "## Ficha\n\n",
        f"- Tipo: {tipo}\n",
        f"- Ciudad: {ciudad}\n",
        f"- Distrito: {distrito}\n",
        f"- Periodo temporal: {periodo}\n",
        f"- Confianza: {confianza}\n\n",
        "## Lugares\n\n",
        render_bullets(as_list(ambientacion.get("lugares_especificos"))),
        "\n## Personas y presencia humana\n\n",
        "### Personajes o personas\n\n",
        render_bullets(as_list(parsed.get("personajes_o_personas"))),
        "\n### Presencia humana\n\n",
        render_bullets(as_list(parsed.get("presencia_humana"))),
        "\n## Temas, motivos y experiencia\n\n",
        "### Temas\n\n",
        render_bullets(as_list(parsed.get("temas"))),
        "\n### Motivos\n\n",
        render_bullets(as_list(parsed.get("motivos"))),
        "\n### Enfoque experiencial\n\n",
        render_bullets(as_list(parsed.get("enfoque_experiencial"))),
        "\n## Sensaciones\n\n",
        "### Objetos o texturas\n\n",
        render_bullets(as_list(parsed.get("objetos_o_texturas"))),
        "\n### Clima o luz\n\n",
        render_bullets(as_list(parsed.get("clima_o_luz"))),
        "\n### Sonidos\n\n",
        render_bullets(as_list(parsed.get("sonidos"))),
        "\n### Sentidos faltantes\n\n",
        render_bullets(as_list(parsed.get("sentidos_faltantes"))),
        "\n## Lectura narrativa\n\n",
        f"- Papel de la ciudad: {', '.join(as_list(parsed.get('papel_de_la_ciudad'))) or '—'}\n",
        f"- Relevancia de la amistad: {parsed.get('relevancia_de_la_amistad') or '—'}\n",
        f"- Transformación de identidad: {parsed.get('transformacion_de_identidad') or '—'}\n\n",
        "### Hilos narrativos\n\n",
        render_bullets(as_list(parsed.get("hilos_narrativos"))),
        "\n### Preguntas filosóficas\n\n",
        render_bullets(as_list(parsed.get("preguntas_filosoficas"))),
        "\n## Conexiones Obsidian\n\n",
        render_bullets(as_list(parsed.get("posibles_enlaces_obsidian"))),
        "\n### Notas relacionadas\n\n",
        render_bullets(as_list(parsed.get("notas_existentes_relacionadas"))),
        "\n### Posibles notas futuras\n\n",
        render_bullets(as_list(parsed.get("posibles_notas_futuras"))),
        "\n## Observaciones editoriales\n\n",
        render_bullets(as_list(parsed.get("observaciones_editoriales"))),
        "\n## Sugerencias editoriales\n\n",
        render_bullets(as_list(parsed.get("sugerencias_editoriales"))),
        "\n## Posibles adiciones\n\n",
        render_bullets(as_list(parsed.get("posibles_adiciones"))),
        "\n## Contradicciones a revisar\n\n",
        render_bullets(as_list(parsed.get("posibles_contradicciones_a_verificar"))),
        "\n---\n\n",
        "## YAML original\n\n",
        "```yaml\n",
        normalized_yaml,
        "```\n",
    ]
    return "".join(sections)


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


def analyze_note(
    note_path: Path,
    *,
    model_name: str = DEFAULT_MODEL,
    max_tokens: int = 2400,
    temperature: float = 0.2,
    output: Path | None = None,
) -> Path:
    from mlx_lm import load

    content = read_note(note_path)
    out_path = analysis_output_path(note_path, output)

    model, tokenizer = load(model_name)
    prompt = build_prompt(tokenizer, content)
    raw_response = run_model(
        model,
        tokenizer,
        prompt,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    yaml_text = extract_yaml_block(raw_response)
    try:
        parsed, normalized = validate_yaml(yaml_text)
        markdown_output = render_analysis_markdown(parsed, normalized)
        out_path.write_text(markdown_output, encoding="utf-8")
    except (yaml.YAMLError, ValueError) as exc:
        print(
            f"Warning: model output is not valid YAML ({exc}); writing raw response.",
            file=sys.stderr,
        )
        out_path.write_text("# Análisis IA\n\n" + yaml_text + "\n", encoding="utf-8")

    return out_path


def main() -> int:
    args = parse_args()
    note_path = args.note_path.expanduser().resolve()

    try:
        out_path = analyze_note(
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

    print(f"Analysis written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
