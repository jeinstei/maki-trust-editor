# maki trust editor

A small QtPy desktop app to **review and edit the permissions maki keeps** —
session rules, per-project rules, global rules, folder trust, and the frozen
pre-trust snapshot — in one clear window, with checkbox lists instead of
hand-edited TOML.

maki has no built-in or plugin UI for this today: session rules live inside the
session `.jsonl` files, folder trust lives in `trusted-folders.json`, and
permissions live in `permissions.toml` files. This app reads all of them and
writes them back safely.

## What it edits

| Store | File | Written as |
| --- | --- | --- |
| Session rules | `<state>/sessions/**/*.jsonl` (the last `t:"meta"` line) | JSON, one line |
| Project rules | `<repo>/.maki/permissions.toml` | TOML |
| Global rules | `~/.config/maki/permissions.toml` (or `~/.maki/…`) | TOML |
| Folder trust | `<state>/trusted-folders.json` | JSON |
| Pre-trust roots | `<state>/sessions/pre-trust-roots.json` | JSON array |

`<state>` is resolved like maki resolves it, cross-platform: `~/.maki` if that
directory exists, otherwise the platform state dir (XDG on Linux/BSD,
`Application Support` on macOS, `%LOCALAPPDATA%` on Windows).

The pre-trust snapshot is the one-time record of project roots that predate
folder trust; maki reads it at startup to grandfather those roots and freezes it
itself if it is missing. The app shows it, and lets you add or remove roots, so a
snapshot frozen wrongly can be corrected. Only root paths that exist on disk can
be added.

## Safety model

Two things can go wrong when editing maki's files behind its back: maki
Overwrites your edit, or maki's lock file races your write. This app handles
both:

- **Backups.** Every save copies the previous file to `<name>.bak`
  next to the original before writing. Only one backup is kept per file, with
  each save overwriting the previous backup.
- **Only changed stores.** Save compares each store against what was loaded and
  writes only the ones you actually changed, so an untouched file is never
  rewritten or re-backed-up, and a file that did not exist is not created by an
  unrelated edit.
- **Atomic replace.** Writes go to a temp file in the same directory and are
  renamed into place, so a crash never leaves a half-written file.
- **A maki-closed guard.** The app detects a running `maki` process
  (cross-platform: psutil, else `tasklist` on Windows / `pgrep` on POSIX) and
  will not silently overwrite:
  - **Shared** files (project/global `permissions.toml` and
    `trusted-folders.json`) can be written by *any* maki process, so a running
    maki blocks them. On Linux the guard checks `/proc/<pid>/fd` to see if maki
    actually holds the specific file open; on other platforms, all shared files
    are blocked whenever any maki process is running.
  - **Session** files are only appended by the process that has them open, so a
    running maki triggers a warning, not a hard block; you can choose to write
    just the session files and skip the shared ones.

The clean path is still: close every maki session, then edit.

## Interface

Four tabs, one **Save** button at the bottom:

- **Sessions** — pick a session from the list; its rules appear grouped by tool
  as a section default effect plus one row per scope, each row an **Allow** /
  **Deny** choice (mutually exclusive) with a **Remove** button. A scope-less
  rule ("every scope for this tool") is preserved as its own all-scopes control
  rather than being dropped. Add a group with **+ Add tool group**. Switching
  sessions with unsaved changes prompts for confirmation.
- **Projects** — pick a discovered project (derived from session working
  directories); edit its `.maki/permissions.toml` the same way, with
  **+ Tool section** and **+ MCP server** to add sections, and a per-section
  default effect and "allow every scope" switch. Switching projects with unsaved
  changes prompts for confirmation to prevent accidental data loss.
- **Folders** — the folder-trust document: every trusted folder (with its gated
  files) and every rejected folder. Use **+ Add folder** to add new entries,
  double-click to edit, or **Remove selected** to delete. Below it, a
  **Pre-trust roots** section lists the frozen snapshot with **+ Add root** and
  **Remove selected**.
- **Global** — the user-wide `permissions.toml`. Like Projects, it has the
  section picker and filter.

**Keyboard shortcuts:** Ctrl+S to Save, Ctrl+R to Reload.

**Filtering rules:** each rules view (Sessions, Projects, Global) has a
**Section** picker to the right of its heading and a collapsible **Filter** box
below it. The picker narrows to a single section (e.g. `bash`, `edit`,
`mcp.github`). The filter matches scope text and section names with the familiar
`*` (any run) and `?` (one character) wildcards — or a plain substring when you
type no wildcard; it is applied on Enter, not on every keystroke. The two
combine with AND. Filtering is purely visual — hidden sections and rows are
still saved, and never count as edits. The filter text persists when you switch
sessions or projects (so you can compare the same rule across sessions), and the
shown rules are always recomputed against it; the section picker resets to
**All sections** on switch.

**Open sessions:** a running maki holds an advisory lock on
`sessions/locks/<id>`, so the app can tell which sessions are live and marks them
in the list with a padlock (falling back to a filled dot when the font lacks the
glyph). Opening a session is unaffected — you can still view and edit it — but
saving rules to a session that is open warns first, since maki may re-append on
exit and clobber the change. Lock files are never deleted, so only a lock that is
actually held counts as open.

**Section defaults:** a `default` maki would reject (anything but
`allow`/`deny`/`prompt`, e.g. an empty string) is shown as an `(invalid)` entry so
the row stays visible; it is preserved verbatim on save unless you pick a real
effect, rather than being silently dropped.

**Unsaved-changes indicator:** a tab whose store has pending edits shows a
trailing ` *` in its title (e.g. `Global *`); it clears as soon as Save writes
that store. Switching sessions or projects with unsaved changes prompts for
confirmation. Merely opening a session never marks it changed: the baseline is
the canonical rendering (rules grouped by tool and sorted), so the order the
rules happen to be stored in is not mistaken for an edit. A store with no
baseline (a session file that is missing or has no meta line) is likewise never
changed.

**Validation:** Tool and MCP server names are validated to ensure they form valid
TOML section names; duplicate section names are prevented. Pre-trust roots must
be existing directories.

**Malformed stores:** if a store fails to parse on load (bad TOML or JSON), the
app falls back to an empty store for that file and warns you with the path and
the parse error, so one broken file never blocks the rest of the workspace.

**Ctrl-C and closing:** Ctrl-C asks the window to close, honoring the same
unsaved-changes prompt. Cancelling the prompt returns the app to exactly the
state it was in before, with Ctrl-C still armed for another try. Pressing Ctrl-C
a second time while the prompt is open skips the guard and closes immediately.

## Running it

The app is built on [QtPy](https://github.com/spyder-ide/qtpy), so it runs
against **whichever Qt binding you have** — PySide6, PyQt6, or their older
siblings. Nothing in the source imports a specific binding; QtPy selects one at
runtime (set `QT_API` to force a choice).

### Without installing anything

Both launchers run straight from source. Neither installs the package into your
environment.

```sh
./run_app.sh                 # uses an existing .venv, else an ephemeral uv env
./run_app.py                 # any Python that already has the deps
```

`run_app.sh` prefers a venv created by `uv sync`; if none exists it builds a
throwaway environment with `uv run --with …` and discards it after the run. It
takes `--extra pyside6|pyqt6` (or `MAKI_TRUST_EDITOR_QT`) to pick a binding.

`run_app.py` adds `src/` to the import path itself, so no install is needed, and
prints the exact fix command if a dependency is missing.

You can also skip the scripts entirely:

```sh
uv run --with pyside6 --with qtpy --with tomlkit --with psutil run_app.py
```

### Installing it (optional)

```sh
uv sync --extra pyside6      # or --extra pyqt6
uv run maki-trust-editor
```

Or without the console script:

```sh
uv run python -m maki_trust_editor
```

## Layout

```
run_app.sh         launcher: existing venv, else ephemeral uv env
run_app.py         launcher: any Python, adds src/ to the path itself
src/maki_trust_editor/
  paths.py         locate maki's stores (cross-platform)
  permissions.py   parse/serialize permissions.toml
  sessions.py      read/rewrite session_rules in a session .jsonl
  folders.py       read/rewrite trusted-folders.json and pre-trust-roots.json
  workspace.py     gather everything into view models
  safe_write.py    backup + atomic write + maki-closed guard
  widgets.py       checkbox scope editor widgets
  window.py        the main window, tabs, and folder trust dialog
  app.py           entry point, logging, Ctrl-C handling
```

## Logging

The app logs to a rotating file in its own state directory — not maki's, so
nothing there is disturbed:

- Linux: `~/.local/state/maki-trust-editor/maki-trust-editor.log`
- macOS: `~/Library/Application Support/maki-trust-editor/maki-trust-editor.log`
- Windows: `%LOCALAPPDATA%\maki-trust-editor\maki-trust-editor.log`

It records the state of the session locks at startup (how many sessions, which
ids are open), each save (targets and result), the warning when saving to an
open session, and any file that failed to load. Uncaught exceptions are written
here too, so a crash leaves a trace even though a GUI has no console. The log
keeps three rotated backups of 1 MB each.

## Caveats

- Editing a session's last meta line changes the rules maki will load on resume;
  if that session is open, close it first or your change may be overwritten.
- Opening a session reads only its first line (the header) and its last line (the
  current state); the megabytes in between are never loaded, so an 80 MB
  transcript opens as fast as a small one. Saving rewrites only that last line
  and streams the rest through untouched, so every other byte — including any
  non-UTF-8 encoding maki wrote — is preserved exactly.
- The app never runs maki. It reads maki's lock files to tell which sessions are
  open, but never takes or writes a lock; it only reads and writes the data
  files, guarded as described above.
- `permissions.toml` keys the app does not model are preserved verbatim on save,
  both unknown top-level keys and unknown keys inside a tool/MCP section.
- Tool and MCP server section names must contain only word characters (letters,
  digits, underscore) and hyphens, and must start with a word character.
- Folder paths and gated file names are free-form strings compatible with
  maki's internal representation.
- Session rules only allow `allow` and `deny` (maki's session format has no
  per-scope `prompt`); `prompt` is offered only as a section default in
  `permissions.toml`.
