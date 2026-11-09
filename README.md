# maki trust editor

NOTE as of 9.11.2026: This app was created with directed AI coding and actual
user testing. The code has not yet been reviewed by a human overall.

A small desktop app for **reviewing and changing the permissions maki keeps** —
the rules for individual chat sessions, per-project rules, your global rules, and
which folders maki is allowed to work in — all in one window, with simple
checklists instead of hand-editing config files.

maki stores these rules in a few different files with no built-in way to edit
them. This app reads all of them, shows you what is set, and writes your changes
back safely.

## What you can do

- **See and edit the rules for a session.** Open maki's session list, pick one,
  and get its rules grouped by tool (e.g. `bash`, `read`) with an **Allow** /
  **Deny** choice per entry.
- **Edit a project's rules.** Rules that apply to one repo (its
  `.maki/permissions.toml`).
- **Edit your global rules.** The rules that apply everywhere.
- **Manage folder trust.** Which folders maki trusts, which it rejects, and the
  one-time "pre-trust" snapshot.
- **Filter long lists.** A section picker and a `*`/`?` wildcard filter make a
  big rule set easy to scan.

## Getting started

You need Python and one Qt binding (PySide6 or PyQt6). The easiest way:

```sh
./run_app.sh                 # uses an existing .venv, else an ephemeral uv env
```

Or, if you already have the dependencies in your environment:

```sh
./run_app.py
```

You can also install it:

```sh
uv sync --extra pyside6      # or --extra pyqt6
uv run maki-trust-editor
```

`psutil` is an optional extra (`--extra psutil`). With it, the app detects a
running maki through one cross-platform API; without it, it falls back to the
native `pgrep` (Unix) or `tasklist` (Windows).

The app is built on [QtPy](https://github.com/spyder-ide/qtpy), so it runs
against whichever Qt binding you have; nothing in the source picks one for you.
Set `QT_API` to force a specific binding.

## How the app is laid out

There are four tabs along the top and a **Save** button at the bottom:

- **Sessions** — every maki session, newest first. Pick one to see and edit its
  rules. The panel above the rules shows the session's id with the UUID it
  encodes and the creation time read out of that id, so you can tell when a
  session was made even before maki records anything.
- **Projects** — every project maki has seen (found from session working
  directories). Pick one to edit its `.maki/permissions.toml`.
- **Folders** — the folder-trust list and the pre-trust snapshot.
- **Global** — your user-wide rules.

**Save** writes the file for the tab you are on (see *Saving* below).

**Keyboard:** Ctrl+S saves, Ctrl+R reloads everything from disk.

## Editing rules

Each rules view groups rules into a section per tool (for example `bash` or
`edit`, or `mcp.github` for an MCP server). Each section has:

- a **section default** (what happens when no entry matches), and
- one row per scope, each an **Allow** / **Deny** choice.

A rule that applies to every scope of a tool is kept as its own "all scopes"
control, so it is never lost.

In **Sessions**, add a tool group with **+ Add tool group**. In **Projects** and
**Global**, add sections with **+ Tool section** and **+ MCP server**; tool and
server names may contain letters, digits, underscores and hyphens, and must start
with a letter, digit or underscore — duplicates are rejected.

When a project or global file is empty (or does not exist yet) the rules area
shows a hint pointing at those add buttons, so you can start straight away — you
do not need to save an empty file first.

**Finding things:** to the right of a section heading is a **Section** picker
that narrows the view to one tool, and a collapsible **Filter** box (the
**Filter** button opens it) that matches scope text and section names. Filter
accepts `*` (any run of characters) and `?` (one character) wildcards, or plain
text for a substring match, and is applied when you press Enter. Filtering only
hides things — hidden rules are still saved and never count as an edit. The
filter text stays when you switch sessions or projects; the section picker resets
to **All sections**.

## Saving

**Save is per file.** It writes only the file for the current selection — the
session you have open on the **Sessions** tab, the project on the **Projects**
tab, the global file on **Global**, and both folder files at once on **Folders**.

Your edits are **not thrown away when you switch away**. If you edit session A,
switch to session B, and come back, A still has your change. Any file with
unsaved changes is marked with a trailing ` *` — both in the tab title (e.g.
`Sessions *`) and next to the item in the list — until you save that file.

Nothing but a changed file is written: on Save, maki-trust-editor compares each
file against what it loaded and writes only the ones you actually changed, so
untouched files are never rewritten or backed up.

## Staying safe

Editing maki's files while maki is running can go wrong in two ways: maki may
overwrite your edit, or the two may write at the same time. The app protects
against both:

- **Backups.** Before writing, the previous file is copied to `<name>.bak` beside
  it (one backup per file, replaced on each save).
- **Atomic writes.** Changes go to a temporary file and are renamed into place,
  so a crash never leaves a half-written file. A symlinked target is followed to
  the real file, so a dotfile-managed link is never replaced by a regular file.
- **A "maki is running" guard.** The app looks for a running `maki` process and
  refuses to silently overwrite:
  - **Shared** files (project/global `permissions.toml` and the folder-trust
    file) can be written by any maki process, so a running maki blocks them.
    On Linux the app also checks whether maki actually holds that file open.
  - **Session** files are only appended by the process that has them open, so a
    running maki only warns; you can still choose to write.

The cleanest approach is still to close all maki sessions before editing.

**Open sessions are read-only here.** When maki has a session open, the app marks
it in the list with a padlock (or a filled dot if your font has no padlock) and
shows its rules **read-only** — you can look, but not change them, and they are
never saved, because maki would overwrite your change anyway. maki leaves these
lock files behind, so only a lock that is *currently held* counts as open.

An open session is also **never marked as having unsaved changes**, even though
maki keeps appending to its file while you look at it. Since nothing on that
screen can be edited or saved, the ` *` marker would be meaningless there.

The lock state is **refreshed while the app runs**: a background check (every two
seconds, and whenever the window regains focus) notices a session maki opens or
closes without a reload, flipping the row read-only or editable on the spot. If
maki opens a session you have already edited, your edit is kept but *parked* —
the row stops being writable and the status line tells you so; only reloading
discards it. When maki *closes* a session it may have rewritten the file as it
ran, and the app picks that up on the next refresh without marking the row as
unsaved on its own.

## Good to know

- **Session defaults.** If a session or section has a `default` maki would reject
  (anything other than `allow`, `deny` or `prompt` — for example an empty
  string), the app shows it as an `(invalid)` entry so the row stays visible and
  the value is preserved exactly unless you pick a real effect.
- **Broken files.** If a file fails to load (bad TOML or JSON), the app opens it
  as empty and warns you with the path and the error, so one bad file never
  blocks the rest.
- **Unknown settings are kept.** Keys the app does not model are preserved
  verbatim when you save.
- **Big sessions open instantly.** Opening a session reads only its first and
  last lines, not the whole transcript, so an 80 MB session opens as fast as a
  small one. Saving rewrites only the rules line and streams the rest of the file
  through untouched, byte for byte.
- **Folder paths** and gated file names are free-form text.
- **Session rules** only offer `allow` and `deny` per scope (maki's session
  format has no `prompt` there); `prompt` is available only as a section default
  in `permissions.toml`.
- **Ctrl-C** asks the window to close and honours the unsaved-changes prompt.
  Pressing Ctrl-C a second time while the prompt is open closes immediately.

## Languages

The app's own text is written in English and is set up to be translated. If a
translation is installed for your language, the app uses it automatically based
on your system locale; otherwise it simply stays English. (Qt's own built-in
buttons, like **OK** and **Cancel**, are translated by Qt itself.)

To force a language, set the `MAKI_TRUST_EDITOR_LANG` environment variable, for
example `MAKI_TRUST_EDITOR_LANG=de_DE`. An unknown value is ignored and the
system locale is used.

## Closing with unsaved changes

If you close the window with unsaved changes, the app asks first. Cancelling
returns you to exactly where you were.

## Where things live

| What | File |
| --- | --- |
| Session rules | `<state>/sessions/**/*.jsonl` (the last `t:"meta"` line) |
| Project rules | `<repo>/.maki/permissions.toml` |
| Global rules | `~/.config/maki/permissions.toml` (or `~/.maki/…`) |
| Folder trust | `<state>/trusted-folders.json` |
| Pre-trust roots | `<state>/sessions/pre-trust-roots.json` |

`<state>` is resolved the same way maki resolves it: `~/.maki` if that directory
exists, otherwise the platform state directory (XDG on Linux/BSD, `Application
Support` on macOS, `%LOCALAPPDATA%` on Windows). The "pre-trust roots" snapshot
is a one-time record of project roots that existed before folder trust; maki uses
it to keep trusting those roots. The app shows it and lets you add or remove
roots (only directories that exist can be added).

## Log and troubleshooting

The app writes a rotating log (three 1 MB files) in its **own** state directory,
separate from maki's:

- Linux: `~/.local/state/maki-trust-editor/maki-trust-editor.log`
- macOS: `~/Library/Application Support/maki-trust-editor/maki-trust-editor.log`
- Windows: `%LOCALAPPDATA%\maki-trust-editor\maki-trust-editor.log`

It records the session locks at startup, every save and its result, and any file
that failed to load, including uncaught crashes — useful because a windowed app
has no console.

## Project layout (for developers)

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

See `AGENTS.md` for the internal design, invariants and gotchas.
