# Notre Paris watchdog on macOS

The watchdog runs as the per-user LaunchAgent `com.eres.notreparis.watchdog`. It starts at login, is restarted after an error, uses the project's `.venv`, and does not require Terminal to remain open.

## Install

Run the installer once, passing the verified Obsidian vault path:

```bash
./scripts/watchdog-install.sh "/absolute/path/to/Notre-Paris"
```

The installer validates the vault and virtual environment, stores the vault path in the private local file `.watchdog-service.conf`, creates `~/Library/LaunchAgents/com.eres.notreparis.watchdog.plist`, then uses `launchctl bootstrap` and `kickstart`. It never writes `.env` values to the plist. At runtime, `watch_vault.py` loads the project `.env` with `python-dotenv` before importing model configuration.

## Operate

```bash
./scripts/watchdog-status.sh
./scripts/watchdog-restart.sh
./scripts/watchdog-stop.sh
./scripts/watchdog-start.sh
./scripts/watchdog-logs.sh
```

`watchdog-status.sh` prints the LaunchAgent state and PID. Logs persist in `~/Library/Logs/NotreParis/watchdog.out.log` and `watchdog.err.log`.

To uninstall the LaunchAgent while preserving logs:

```bash
./scripts/watchdog-uninstall.sh
```

## When paths change

If the project moves, run `scripts/watchdog-install.sh` from its new location. If the vault moves, rerun the installer with the new absolute vault path. If `.venv` is recreated, install `requirements.txt` into `.venv` and restart the service:

```bash
.venv/bin/pip install -r requirements.txt
./scripts/watchdog-restart.sh
```

The LaunchAgent uses the native macOS FSEvents observer and schedules only the five eligible source folders. A sandboxed manual run can use `--observer polling`, but polling may block when macOS protects folders under `Documents`; the installed user LaunchAgent therefore uses native FSEvents, which is verified during installation testing.

## Processing rules

Only notes under `01 Places`, `02 People`, `03 Scenes`, `04 Themes`, or `05 Fragments` are eligible. Status spelling is normalized, but the two editorial stages remain distinct:

| Note status | Actions, in order |
| --- | --- |
| `Ready-for-review` / `ready_for_review` | `proofread_note.py` → `editorial_review.py` |
| `Done` / `done` | `translate_note_fr.py` → `literary_map.py` |

`Ready-for-review` never creates a French translation or updates the literary map. `Done` translates the final source note directly; it does not repeat proofreading or editorial review. The literary map runs only after a successful final translation.

Generated folders (`11 Proofread`, `12 French`, `13 Editorial Review`, and `10 Literary Map`) are ignored, so their outputs cannot reactivate the source pipeline. During one service run, the watchdog remembers the last successful content signature and status transition for each note. The same version and status are processed once.

If Spanish content changes after translation, first move the note out of `Done` while editing, then set it to `Done` again. A content edit that leaves the note continuously marked `Done` does not automatically regenerate French; explicitly re-establishing `Done` triggers translation of the new final version followed by the literary map update.

## macOS access to a vault under Documents

If the error log says that observer startup timed out, macOS is blocking the background Python process from opening the vault under `~/Documents`. This permission cannot be accepted by a background LaunchAgent.

Open **System Settings → Privacy & Security → Full Disk Access**, click **+**, press **Command-Shift-G**, and add this stable Homebrew app path:

```text
/opt/homebrew/opt/python@3.14/Frameworks/Python.framework/Versions/3.14/Resources/Python.app
```

Enable its toggle, then run:

```bash
./scripts/watchdog-restart.sh
./scripts/watchdog-status.sh
```

The status must say `state = running`, and `watchdog.out.log` must contain `Observer: native`. If Python is upgraded to a different major/minor Homebrew formula, add the corresponding `Python.app` and reinstall or restart the service.
