# Diagnóstico: watchdog `draft → ready-for-review`

Fecha: 2026-07-21  
Entorno de investigación: Cursor Cloud Agent (Linux), repo `eres/paris-project`  
Agente: https://cursor.com/agents/bc-019f86ad-1e7f-71aa-8411-2a95855ee07c

## Bloqueo de evidencia sobre el sistema local existente

Comandos ejecutados:

```text
pwd
→ /workspace

realpath "/Users/eres2/Documents/Obsidian Vaults/Notre-Paris"
→ No such file or directory

ls -ld "/Users/eres2/Documents/Obsidian Vaults/Notre-Paris"
→ No such file or directory

stat "/Users/eres2/Documents/Obsidian Vaults/Notre-Paris"
→ No such file or directory

ls "/Users/eres2/dev/notre-paris-agent"
→ No such file or directory

find / -name watch_vault.py
→ (sin resultados)

uname -a
→ Linux … x86_64

git remote
→ github.com/eres/paris-project
```

**Conclusión de entorno:** este agente no tiene acceso al Mac, al vault de Obsidian Sync, ni al código local `notre-paris-agent`. Por tanto **no se puede confirmar la causa raíz del watcher que ya tenías instalado**, ni demostrar una transición real desde Obsidian (Caso E Obsidian / Caso F launchd real).

Lo que sí se hizo: reconstruir e instrumentar un watcher robusto en este repo (`scripts/`), con tests de aceptación reproducibles que cubren las fallas más frecuentes del síntoma reportado.

## Hipótesis priorizadas (por sintomatología típica, no por inspección del binario local)

| # | Hipótesis | Veredicto en este entorno |
|---|---|---|
| 1 | Eventos FS no emitidos / perdidos | Plausible; mitigado con reconcile periódico |
| 2–4 | Escritura atómica Obsidian (temp+rename) | Muy plausible; test `test_obsidian_atomic_rename_pattern` pasa con manejo de `moved` |
| 5 | Debounce incorrecto | Mitigado (debounce + estabilidad size/mtime) |
| 6 | Caché de estados | Implementada persistente + idempotencia |
| 7 | Lectura prematura | Mitigado con retries + wait-stable |
| 8–11 | Permisos / FDA / launchd | **No demostrable aquí**; usar `scripts/diagnose_watcher.py` en el Mac |
| 12–13 | Ruta / patrón de archivos | Configurable; ignora temps/dotfiles |
| 14–15 | Parser / comparación de status | Cubierto por tests (quotes, CRLF, YAML roto) |
| 16 | Obsidian Sync | No asumido; distinguir edición local vs sync con `probe_fs_events.py` |
| 17 | watchdog/FSEvents | No confiar solo en eventos → híbrido |
| 18–20 | Proceso muerto / logs / except pass | Logging explícito; errores no se silencian |

## Causa raíz confirmada (alcance cloud)

**No confirmada para el binario local ausente.**  
**Confirmado para el diseño anterior típico:** depender solo de `on_modified` falla ante renames atómicos; la corrección mínima robusta es híbrida (eventos `created|modified|moved` + debounce/estabilidad + caché + reconcile).

## Cambios en este PR

| Archivo | Rol |
|---|---|
| `scripts/watch_vault.py` | Watcher híbrido instrumentado |
| `scripts/frontmatter.py` | Parser robusto de `status` |
| `scripts/status_cache.py` | Caché persistente + tokens de transición |
| `scripts/transitions.py` | Semántica draft→ready / ready→done |
| `scripts/actions.py` | Ejecución con exit code/stdout/stderr |
| `scripts/editorial_review.py` | Acción ready-for-review |
| `scripts/translate_note_fr.py` | Acción done (FR) |
| `scripts/literary_map.py` | Acción final done |
| `scripts/proofread_note.py` | Alias → editorial |
| `scripts/probe_fs_events.py` | Sonda de eventos FS |
| `scripts/diagnose_watcher.py` | Diagnóstico de entorno Mac |
| `scripts/launchd/com.notreparis.watchvault.plist` | LaunchAgent plantilla |
| `scripts/WATCHER.md` | Runbook |
| `tests/*` | Parser, transiciones, aceptación A–F |

## Validación en cloud

Ver salida de `python3 -m unittest discover -s tests -v` en el PR.

| Caso | Resultado cloud |
|---|---|
| A draft→ready-for-review | PASS (solo editorial) |
| B ready→done | PASS (translate + literary map) |
| C edit sin status | PASS (sin acciones) |
| D saves rápidos | PASS (sin duplicar) |
| E terminal + eventos | PASS (WatchService) |
| E Obsidian GUI | **NO DEMOSTRABLE aquí** |
| F restart/reconcile offline | PASS (reconcile) |
| F launchd/Mac reboot | **NO DEMOSTRABLE aquí** |

## Riesgos pendientes

- Full Disk Access / TCC en macOS deben validarse en máquina real.
- Obsidian Sync remoto puede generar patrones de eventos distintos a la edición local.
- Los scripts de acción actuales son stubs instrumentados (marcan `.ran`); sustituir por la lógica editorial/traducción real del agente local.
- Copiar este árbol a `/Users/eres2/dev/notre-paris-agent` o apuntar el plist a este checkout.

## Recomendación final

**Estrategia híbrida** (no watchdog puro ni polling puro):

1. reaccionar a eventos de inmediato;
2. reconciliar cada pocos segundos;
3. detectar transiciones perdidas vía caché;
4. evitar duplicados con fingerprint + last_transition.

## Checklist obligatorio en el Mac

```bash
cd /Users/eres2/dev/notre-paris-agent   # o este checkout
python3 scripts/diagnose_watcher.py | tee /tmp/notre-diag.json
python3 scripts/probe_fs_events.py "$HOME/Documents/Obsidian Vaults/Notre-Paris/01 Places" --seconds 45
# En otra terminal / en Obsidian: draft → ready-for-review
python3 scripts/watch_vault.py --bootstrap --verbose
# Repetir transición y verificar en el log:
# decision=run_transition ... actions=['editorial_review']
```

Solo entonces se puede declarar el problema **resuelto** de extremo a extremo.
