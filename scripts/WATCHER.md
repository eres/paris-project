# Notre Paris vault watcher

## Semántica de estados

| Transición | Acciones |
|---|---|
| `draft` → `ready-for-review` | solo `editorial_review.py` |
| `ready-for-review` → `done` | `translate_note_fr.py` y luego `literary_map.py` |

Editar texto sin cambiar `status` no dispara acciones.
`ready-for-review` **no** traduce ni ejecuta literary map.

## Arquitectura (híbrida)

1. **Eventos** (`created` / `modified` / `moved` / `closed`) vía watchdog.
2. **Debounce + estabilidad** (espera a que size/mtime dejen de cambiar).
3. **Caché persistente** de estados (`status_cache.json`).
4. **Reconciliación periódica** (default 5s) para transiciones perdidas por FSEvents/Obsidian Sync.
5. **Idempotencia** por token `transition:fingerprint`.

Esto mitiga el patrón típico de Obsidian (escritura atómica vía temporal + rename),
donde un watcher que solo escucha `modified` puede no ver el cambio real.

## Uso manual

```bash
python3 -m pip install -r requirements.txt

export NOTRE_PARIS_VAULT="$HOME/Documents/Obsidian Vaults/Notre-Paris"
export NOTRE_PARIS_STATE_DIR="$HOME/.local/state/notre-paris"

# Primera ejecución: sembrar caché sin disparar transiciones históricas
python3 scripts/watch_vault.py --bootstrap --verbose

# Servicio en primer plano
python3 scripts/watch_vault.py --verbose
```

## Diagnóstico en el Mac

```bash
python3 scripts/diagnose_watcher.py | tee /tmp/notre-paris-diag.json
python3 scripts/probe_fs_events.py "$NOTRE_PARIS_VAULT/01 Places" --seconds 30
```

Mientras `probe_fs_events.py` corre, cambia `status` en Obsidian y compara
los eventos con una edición desde terminal.

## launchd

```bash
mkdir -p ~/.local/state/notre-paris
cp scripts/launchd/com.notreparis.watchvault.plist ~/Library/LaunchAgents/
# Ajusta rutas en el plist si tu home/repo difieren.
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.notreparis.watchvault.plist
launchctl print gui/$(id -u)/com.notreparis.watchvault
tail -f ~/.local/state/notre-paris/watch_vault.log
```

## Permisos macOS (solo si la evidencia lo exige)

Si `diagnose_watcher.py` reporta `readable=false` o el proceso muere al tocar el vault:

1. **System Settings → Privacy & Security → Full Disk Access**
2. Añade el binario real de Python que usa launchd (`python3 -c 'import sys; print(sys.executable)'`)
3. También Terminal/iTerm si pruebas a mano
4. Reinicia el LaunchAgent

No asumas FDA solo porque macOS lo sugiere: confirma con el JSON de diagnóstico.

## Pruebas

```bash
python3 -m unittest discover -s tests -v
```
