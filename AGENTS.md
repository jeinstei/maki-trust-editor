# AGENTS.md — maki-trust-editor internals

Deep notes for anyone (human or agent) changing this app. The user-facing
overview lives in `README.md`; this file is the design, the invariants, and the
traps. Read it before editing.

## What this is

A QtPy (not raw PyQt/PySide) desktop app that reads and rewrites the files maki
keeps for permissions and folder trust. QtPy is mandatory — never import a
concrete binding. Python is managed with `uv`; formatting is `black`, linting is
`ruff`, both in the `dev` extra inside `.venv`.

## Code guidelines

**Write code in the style of the repo root `maki/AGENTS.md`.** That file is the
source of truth for how we write code; this section is its Python translation.
When the two disagree, the root file wins. Read it before editing.

From the root, applied here:

- No trivial comments. Every comment explains *why*, not *what*.
- Minimal bloat (KISS, DRY, SRP). No unnecessary state; every line must justify
  its existence.
- No wildcard imports; import names at the top and use the short name everywhere
  (`from pathlib import Path`, then `Path`, never `pathlib.Path(...)` inline).
- Constants at the top of the file, right after imports.
- Descriptive names for variables and functions.
- No inline magic numbers or strings; name them (module constant or an
  `Enum`/`Literal` alias).
- Explicit error handling over silent failure; raise a typed exception for
  domain errors instead of a bare `Exception`, and give a helpful message.
- Prefer structured, wide logging with useful fields over a single string.
- Tests: snake_case names, one behaviour per test, no tautologies or flaky
  timing; in tests assert against the shared constant rather than a copied
  literal. See `## Testing` below.

Python specifics (the analogue of the root's Rust idioms):

- Format with `black`, lint with `ruff`; both must be clean.
- Type-annotate public functions; `from __future__ import annotations` at the top.
- Prefer `dataclasses` for plain records; prefer `pathlib.Path` over raw strings.
- Cross-platform: never assume POSIX or Windows-only behaviour.
- Prefer editing existing files; keep `README.md` consistent with behaviour.
- **Ask the user before any functional change** (standing rule).

## Testing

- `.venv/bin/python -m pytest -q` — always with a **timeout**. A `QMessageBox`
  modal under the `minimal` platform hangs forever; any test that can trigger a
  dialog must stub it or guarantee it does not fire.
- `ruff check src tests` and `black --check src tests` must be clean.
- 158 tests as of last change, in ~1.2 s. Keep the whole suite well under a
  couple of seconds.
- `tmp_path` is redirected to `.pytest-tmp/` by the root `conftest.py` (see
  *Environment notes*). Do not add a `basetemp` ini key: pytest does not read
  one (`--basetemp` is a CLI option only), and it only warns `Unknown config
  option`.

### Verification pipeline

A green suite is the *floor*, not the proof. Before believing a change is done,
run all of these:

1. **Green suite + lint.** `pytest`, `ruff check src tests`,
   `black --check src tests`. All clean.
2. **Mutation check every new or changed assertion.** Temporarily break the
   exact branch the test claims to cover (flip the condition, drop the guard,
   return the wrong value) and confirm the test **fails**. Then restore. A test
   that still passes under the mutation is vacuous — rewrite or delete it. Two
   tests in this project passed while their target was fully broken until this
   step caught them.
3. **Exercise the real path, not a mock-only path.** When the bug involves a
   file changing under us, drive the real store: a **background thread/process
   that keeps appending** to a live session while the window reacts, asserting
   the invariant on each iteration. Prefer the actual running session on this
   machine over a synthetic copy when one exists.
4. **Check the negative case too.** If a test proves "X is blocked", also prove
   the near-miss is *not* blocked, so a stub that blocks everything cannot pass
   (e.g. block only an in-use file, not all shared files).
5. **Timeouts everywhere.** Any run that can reach a Qt dialog or wait on I/O
   goes through `timeout`. Never `sleep`-and-poll in a test; make the assertion
   deterministic instead.

Keep scratch repros inside the project dir and delete them when done — `/tmp` is
sometimes unavailable (see Environment notes).

## Architecture

Rust-style layering, one concern per module under `src/maki_trust_editor/`:

| Module | Responsibility |
| --- | --- |
| `paths.py` | Locate maki's stores, cross-platform; our own state/log dirs |
| `permissions.py` | Parse/serialize `permissions.toml` (tomlkit, preserves unknowns) |
| `sessions.py` | Read/rewrite `session_rules` in a session `.jsonl` (byte-oriented) |
| `folders.py` | Read/rewrite `trusted-folders.json` and `pre-trust-roots.json` |
| `workspace.py` | Gather every store into view models; only module that reads files for the UI |
| `safe_write.py` | Backup + atomic write + maki-running guard |
| `changes.py` | What a save will change, diffed against the file on disk |
| `widgets.py` | Scope editor widgets, filter, section picker, conflict notes |
| `window.py` | Main window, tabs, selection, save/reload, dialogs |
| `i18n.py` | Locale detection + Qt translator install; loads `.qm` catalogs |
| `app.py` | Entry point, logging, Ctrl-C handling |

Writes follow a symlink to its target (`_real_target`): `os.replace` on the link
itself would replace it with a regular file, so backup and write both land on
`path.resolve()`. An existing target is backed up beside the real file
(`<name>.bak`). `psutil` is an **optional** extra; `detect_maki` falls back to
native `pgrep`/`tasklist` when it is absent.

### Data model

- `sessions.Session` — path, header fields, and `rules: list[SessionRule]`. It
  reads only the **first line (header)** and **last line (the `t:"meta"`
  record)** of the file; the megabytes in between are never loaded. The last-line
  offset is kept so `write()` can stream the prefix and rewrite only the last
  line (`shutil.copyfileobj` + `truncate` + `write` + `os.replace`). Prefix bytes
  are never decoded, so any encoding maki wrote survives.
- `SessionRule(tool, scope, effect)`. A rule with `scope=None` means "every scope
  for this tool".
- `sessions.decode_session_id(session_id)` takes an id apart for display. maki
  names a session with a base58-encoded UUID (maki-storage's `MakiId`): a
  **UUIDv7** today, whose first 48 bits are a Unix-millisecond timestamp, or a
  legacy **v4** (random, no time). The parse mirrors `MakiId` — hyphenated or
  bare-hex UUID first, then base58 — and returns `None` for anything else. The
  Sessions header (`SessionsTab._session_header`) shows `<raw> - <uuid> (vN)`,
  collapsing to just `<uuid> (vN)` when the raw form already is the UUID (a
  legacy hex id), then a `created:` row: the id's own time, `(none)` for a v4,
  flagged when it disagrees with the header's `created_at` by more than a couple
  of seconds (the id is millisecond-precise, the header whole seconds).
- `ToolGroup.group(rules)` buckets rules by tool and **sorts tools**; within a
  tool, scope rules come first and the scope-less rule last. `to_rules()`
  reconstructs, and it is the order a save writes.
- `sessions.rule_set(rules)` is a `Counter` of `(tool, scope, effect)`: the
  comparison used for "did the rules change". It is a multiset on purpose —
  order and JSON spacing must not count as an edit, but a duplicated rule must.
  `rules_line()` is for *writing* only; comparing serialized lines made every
  reload look like an edit.
- `sessions.scope_conflicts(rules)` returns the `(tool, scope)` keys held more
  than once. It is derived on demand, never stored, and drives the per-row
  warning in the editor.

### Empty rules area

`ScopedHolder(title, empty_hint)` shows `empty_hint` centered in the rules area
while it has **no** sections, and hides it as soon as one is added
(`add_editor`); `clear_editors()` brings it back and `set_empty_hint("")`
suppresses it. `PermissionsTab` (Projects/Global) passes a hint pointing at the
**+** buttons and blanks it when `load(None)` (nothing selected) so an unset
picker never claims you can add rules; `SessionsTab` does the same for "no
session selected". A blank/absent store therefore looks actionable instead of a
dead grey box — the add buttons work without saving an empty file first. The
hint keys off **zero sections**, not off the filter, so a filter that hides every
section does not bring it back.

## Invariants (do not break)

1. **`_changed_stores()` is a pure read.** It must not mutate any model. It used
   to call `_commit_current()`, which is what caused the "dirty on open"
   regression (see below). Commit happens elsewhere.
2. **`reload()` must be skipped for a dirty store.** A session row reloads a
   view only when `not self._is_dirty(row)` (`_populate_row`); the project
   selector reloads only when the incoming row is not in `_project_targets()`
   (`_project_selected`). Reloading a store that holds a committed-but-unsaved
   edit would re-read the file and drop it. The reload exists because a live
   session (or a project file maki rewrites) changes while maki runs, so the
   cached copy goes stale.
3. **Widget change signals fire during construction.** Building an editor
   (`set_section`, `add_row`) emits `currentIndexChanged`, which would otherwise
   be mistaken for a user edit and commit a half-built store, and would re-dirty
   a file that was just re-read. The guard belongs **in the tab that loads the
   widgets**, not at its callers — a caller that forgets it silently corrupts
   the store, and two did:
   - `ScopeRulesEditor._notify()` is a no-op while `self._loading` is set;
     `set_section` wraps its widget mutations in `_loading`.
   - `PermissionsTab.load()` sets `self._loading` around `_load()`, and its
     `_notify()` (the `on_change` callback) honours it. Every project/global
     load path — `_project_selected`, `_reload_project_row`, `_reload_global`,
     `_tab_changed`, `_populate` — therefore gets the guard for free and none of
     them wrap `load()` themselves.
   - `SessionsTab._populate_row()` does the same around `_fill_row()`; its
     `_notify()` honours it, so `MainWindow._mark_dirty` no longer inspects
     `sessions_tab.loading`. `_mark_dirty` keeps only the window's own
     `self._loading` gate.
4. **Dirty = model vs. baseline, per file.** `_capture_baselines()` records each
   store's canonical serialization at load. A store with no baseline (missing or
   empty file) is never dirty. Sessions are canonicalized before hashing the
   baseline, so the file's raw rule order is not mistaken for an edit.
5. **Baselines advance per file, never wholesale.** After a save,
   `_serialized(path)` recomputes the baseline for the saved file(s);
   `_tab_changed` re-reads Global and advances only Global's baseline. Never call
   a full `_capture_baselines()` outside `_populate()`: it resets *every* store's
   baseline to its current model, which clears the dirty mark of an untouched-but-
   edited store so its edit is never saved (the F1 loss bug). Likewise
   `_commit_tab(PROJECTS_TAB)` must park the visible editor in
   `_shown_project_row`, not `projects_selector.currentRow()` — the signal fires
   after the row already changed, so `currentRow()` is the incoming row and would
   write the editor into the wrong project. Every reload prompt goes through
   `_confirm_discard(action, names)`, which **names the files at risk**; a bare
   "you have unsaved changes" cannot tell a reload of the file being edited from
   an unrelated edit elsewhere. `_reload` (Reload all) re-reads every store;
   `_reload_current` (`Ctrl+R`) re-reads only the active tab's file(s). Only the
   latter has a shortcut: the riskier action stays a deliberate click.
6. **Append-only session writes.** A session write rewrites only the final meta
   line; everything before it is streamed unchanged.
7. **Locked sessions are read-only, never saved, and never dirty.**
   `sessions.is_locked()` is the single source of truth. `_session_targets()`
   skips any `view.locked` row, which covers saving, row/tab marks, and
   `_dirty()` in one place. Why it matters: `_populate_row()` reloads a session
   from disk (a live session grows), and maki rewriting a locked file while it
   runs would otherwise make the reloaded rules differ from the baseline — a
   spurious "unsaved changes" with no edit made. A locked row cannot be saved,
   so it must never be a target.

## Saving model (per-file)

- **Design chosen by the user:** save is per file, not global. `_current_targets()`
  returns the active tab's current selection only. Sessions → the shown session;
  Projects → the shown project; Global → `permissions.toml`; Folders → both
  folder files.
- **Dirty persists across switches.** Edits commit into the model on every change
  (`_mark_dirty` → `_commit_current()`), so leaving a session/project never loses
  them and switching needs no discard prompt. `_project_selected` and
  `_tab_changed` commit the outgoing selection first. Only the destructive
  actions prompt: `closeEvent` and every reload (`_confirm_discard`).
- **Dirty markers.** `_refresh_marks()` marks tab titles and list rows with
  `DIRTY_MARK` (` *`). Row labels are stored (`SessionsTab._labels`,
  `MainWindow._project_labels`) so the mark can be rebuilt without re-deriving
  them.
- **Locked sessions are dropped at save time** by `_drop_locked_sessions()`, which
  probes live (a session may have opened since load) and tells the user which
  were skipped. This is a hard block, not a prompt.

### Save flow

1. `_save()` → `_current_targets()` (commits the current editor, picks the
   active tab's target).
2. `_drop_locked_sessions()` — remove any session a running maki holds open.
3. `_review_changes()` builds the change list from the file **on disk** (not the
   baseline, which a running maki may have outdated) and reports it. A session
   save skips the dialog: it is not shared and the lock check already covers it.
   Any other target opens `SaveDialog` (Write / Cancel); Cancel writes nothing
   and leaves the edit pending. An empty list means the view already matches
   disk, so nothing is written and no dialog appears.
4. `safe_write.detect_maki()` then `blocked_targets()`: a shared file a running
   maki holds open is **refused** (`_refuse_shared_write`), not offered as a
   choice. Session targets are never blocked here.
5. `safe_write.write_targets()` — backup, then atomic replace or the session writer.
6. Advance only the saved files' baselines; `_refresh_marks()`.

## Reload

Two buttons, one rule: **nothing is re-read over an unsaved edit without asking.**

- **Reload all** (`_reload`) re-reads every store via
  `workspace.load_workspace()` + `_populate()`. No shortcut: discarding every
  store's edits is the riskier action, so it stays a deliberate click.
- **Reload file** (`_reload_current`, `Ctrl+R`) re-reads only what the active tab
  shows:
  `_reload_session_row`, `_reload_project_row`, `_reload_global`,
  `_reload_folders` (both folder files, matching how Save writes them). It
  follows the **selection**, not whatever was open at load.

Both call `_confirm_discard(action, names)`; `_dirty_names()` answers "is this
specific file pending?" so a clean file reloads silently. Cancelling leaves every
edit pending.

**A reload must not re-dirty itself.** `projects_tab.load()` / `global_tab.load()`
fire the change callback, which commits the tab into the model — if that runs
after `view.reload()`, the stale widget state overwrites the file just read and
the reload is undone. The guard is `PermissionsTab.load()`'s own `_loading`
(invariant 3), so no reload path needs to wrap the call; each one only advances
the baseline from the freshly read file. This bit twice before the guard moved
into the tab: reloading a project left it dirty, and merely *selecting* projects
marked them all dirty.

## Session liveness (locks)

- Liveness is an **advisory lock**, not file existence. maki holds an exclusive
  lock on `<state>/sessions/locks/<session-id>` (an empty file). Those files are
  **never deleted** — there are ~18 stale ones on a normal machine — so only a
  *held* lock counts as open.
- `sessions.is_locked(sessions_dir, id)` takes a **non-blocking SHARED** lock
  (POSIX `fcntl.flock`, Windows `msvcrt.locking(LK_NBLCK)`); `Held` means open.
- **POSIX-only test** (`test_locks.py`, `test_per_file_save.py`, `test_liveness.py`
  lock cases skip on Windows).
- **The lock is polled, not just read at load.** `MainWindow._refresh_liveness()`
  runs on a 2 s `QTimer` (`LIVENESS_POLL_MS`) and on window focus (`changeEvent`).
  It re-probes (a) the maki process via `_maki_status()` and announces a start or
  exit, and (b) only the **visible** session rows — the active row plus the
  viewport — via `_probe_visible_locks()`. No scrollbar hook: an off-screen lock
  is caught when its row is selected, which re-probes it (`_populate_row`).
  `_apply_lock_changes` flips a changed row: reload unless it is dirty, set
  `view.locked`, re-populate the active row. A dirty row that just became locked
  is **parked, not discarded** — the edit stays in the model, the row leaves the
  save targets, and the status line warns (`_park_on_lock`).
- The list shows a padlock (`LOCK_ICON`, U+1F512) or a filled dot
  (`LOCK_ICON_FALLBACK`, U+25CF). Pick it with `QFontMetrics.inFontUcs4(ord(c))`
  — **not `inFont`**, which takes a single UTF-16 `QChar` and raises on astral
  characters such as the padlock.

## Save review and rule conflicts

**`changes.py` diffs the file on disk, not the baseline.** The baseline is what
the app *loaded*, which a running maki can have moved since; the dialog has to
describe the write that is about to happen. Every entry point re-reads the target
(`Session.load`, `PermissionsFile.load`, `TrustDocument.load`) and compares.
Markers: `+` added, `-` removed, `~` effect flipped (`deny  (was allow)`).

**The comparison is a multiset** (`sessions.rule_set`), matching the dirty check,
so the dialog can never say "nothing to save" while the window says dirty.
Keying on `(tool, scope)` alone would collapse duplicates and do exactly that.
One known imprecision: a key that both flips effect *and* gains or loses a copy
reports only the flip.

**Rule identity is `(tool, scope)`.** Two rows resolving to the same pair are
flagged in the editor, never blocked — a file may already hold them, and
rewriting the user's intent is worse than showing it. `scope_conflicts()` returns
the repeated keys; `ScopeRulesEditor._mark_conflicts()` renders a note per row
(`duplicate rule for this scope`, `conflicting effects: allow, deny`). It runs on
every mutation **and** in `add_row`, so a file loaded with a conflict shows it
before the user touches anything. `ScopeRulesEditor` therefore takes the section
name (`ScopeRulesEditor(on_change, tool)`); the holder's label cannot serve,
since it is decorated for display (`[bash]`, `[mcp.github]`) in the TOML tabs but
raw in sessions.

## gotchas

- **The `READ ONLY` reasoning is subtle:** a locked session shows all its rules
  but with every control disabled (`set_read_only`), including **+ Add tool
  group** (`self._add_button`). It is excluded from `_session_targets()` (so it
  is not dirty and not a save target), and `_drop_locked_sessions` is the
  save-time backstop for a session that became locked after load. The two layers
  are deliberate: `_session_targets()` uses the cached `view.locked`, while
  `_drop_locked_sessions` re-probes live. The poll (`_refresh_liveness`) keeps
  the cached `view.locked` current, so a lock that appears or vanishes mid-view
  is reflected within a tick instead of only at Save.
- **A parked row must not be re-populated.** `_populate_row` reloads a row whose
  `_is_dirty` is False — but a locked row is *excluded* from `_session_targets()`,
  so a parked (locked + edited) row reads as clean and `_populate_row` would
  reload it, dropping the edit. `_apply_lock_changes` therefore calls
  `SessionsTab.set_read_only(True)` in place for a parked shown row instead of
  re-populating it. The dirty test it uses is `_session_pending_edit`, a
  **lock-agnostic** check (model's `rule_set()` vs `_baseline`) — not
  `_session_row_dirty`, which excludes locked rows and would read a parked row as
  clean, so the parked edit would be silently re-baselined and lose its mark.
- **Unlock re-baselines, or it invents a spurious dirty.** When a lock vanishes
  (`_apply_lock_changes`), the row is reloaded because maki may have rewritten
  the file while it held the lock. The baseline was captured at load, so that
  reload differs from it and marks a session the user never touched as dirty.
  `_rebaseline_session` therefore advances `_baseline[path]` to the freshly
  reloaded rule set whenever the row is clean (and `has_rules_line`).
  Tests: `test_unlock_after_maki_rewrites_is_not_dirty` (the bug) and
  `test_parked_edit_survives_unlock_and_stays_dirty` (the parked edit must *not*
  be re-baselined).
- **The dirty check compares `sessions.rule_set()`, not serialized lines.** maki
  writes its own rule order and compact JSON separators; re-serializing from the
  parsed model reproduces neither, so a line comparison marked every reloaded
  session dirty. Comparing a multiset of `(tool, scope, effect)` is immune to
  both, and still notices a duplicated rule.
- **Widget identity:** `SessionsTab._editors` is `[(tool_name, ScopeRulesEditor)]`;
  `collect()` rebuilds `SessionRule`s from it. Renaming a group rebuilds all
  rules, so keep `ToolGroup` as the single place that defines rule order.
- **`_remove` / `_add` are instance attributes** on the widgets so they can be
  disabled for read-only mode. The `ScopeRow` remove button is `self._remove`.
- **Pre-trust snapshot** is frozen by maki itself; the app shows it and can add
  or remove roots, but only paths that exist on disk.
- **Unknown keys** are preserved on save: top-level and per-section in
  `permissions.toml` (`PermissionsFile._unknown` / `ToolRules.unknown`), and
  top-level plus **per folder entry** in `trusted-folders.json`
  (`TrustDocument._unknown` / `FolderEntry.unknown`). Unknown keys are kept
  verbatim, but a `permissions.toml` save rewrites the file in canonical order
  and drops comments (see the module docstring).
- **B3 invalid default:** a section `default` maki would reject (e.g. `""`) is
  shown as `(invalid)` (`INVALID_LABEL`) and preserved verbatim unless the user
  picks a real effect. maki only accepts `allow`/`deny`/`prompt`
  (maki-config/src/lib.rs:1071, `DefaultEffect`:1152) and treats anything else as
  absent.

## Internationalization (i18n)

- **Source language is English.** Every user-facing string is wrapped with
  `self.tr("...")` (or `QCoreApplication.translate("ctx", "...")` where there is
  no `QObject`); internal constants, dict keys, log messages and regexes are not
  translated. `TAB_TITLES` stays English on purpose — it is indexed with
  `.index(...)`; the visible tab text comes from `MainWindow._tab_title()`, whose
  if/elif chain puts each literal in a `tr()` call so `pylupdate6` can see it.
- **`i18n.py`** resolves the locale (`MAKI_TRUST_EDITOR_LANG` env override, else
  `QLocale.system()`; an unparseable value falls back rather than raising),
  installs our catalog plus Qt's own `qtbase_<locale>.qm`, and returns the
  translators so the caller keeps them alive (a `QTranslator` is C++-owned; if the
  Python reference is dropped it is collected and translations silently vanish).
- `app.main()` calls `i18n.install(app)` **before** constructing `MainWindow`: a
  widget captures its text at construction, so a later install would miss it.
- **Nothing is required to run**: with no catalog the app is English, which is
  always valid because the source strings are English.
- **Catalogs.** `translations/app.ts` is the English **template**. A translation
  is `translations/app_<locale>.ts` (tracked) compiled to `.qm` (build output,
  gitignored, packaged into the wheel by `pyproject.toml`'s `include`).
  `scripts/build_translations.py` does both: `--extract` refreshes the template
  from the source with `pylupdate6`, and the default compiles every locale `.ts`
  with `lrelease` (it skips the template).
- **Tool discovery is defensive.** A `lrelease`/`pylupdate6` on `PATH` may work,
  may be a shim, or may be a different major version; the script therefore
  validates every candidate by running it (`-version` or `-V`, because
  `pylupdate6` rejects `-version`) and falls back to versioned Qt bin dirs.
  English-only is a valid state, so a missing compiler is reported, not fatal.
- **Licensing.** `lrelease`/`lupdate`/`linguist` are Qt tools under
  **GPL-3.0 WITH Qt-GPL-exception-1.0** (or commercial). The exception means the
  **output (`.qm`) is not GPL**, and the tools run at build time rather than being
  distributed — so no Qt commercial license is needed for the i18n toolchain.
  (The app's own runtime binding is a separate matter: PySide6 is LGPL-3,
  PyQt6 is GPL-3.)
- **Testing.** The i18n tests use a **synthetic** catalog built on the fly to
  prove the mechanism. Do **not** add tests that assert a specific real
  translation (e.g. that a shipped `de` catalog says a particular word): a
  machine-assisted or in-progress translation is not a testable invariant, and
  such a test would break on every legitimate edit to the wording.

## Logging

`app.setup_logging()` installs a `RotatingFileHandler` (1 MB × 3) at
`paths.log_path()`, in **our** state dir (`<state-root>/maki-trust-editor`), never
maki's. `sys.excepthook` writes uncaught exceptions. At `INFO` the log carries
what a user would report: startup lock state, each save and its result, a skipped
open session, a refused shared write, and any load problem. Logging is
best-effort: an `OSError` means the app runs without a log.

Per-change tracing (which rows are dirty after a lock flip, each reload, the
save target count) is `DEBUG`, which `INFO` suppresses. Enable it for a run with
`MAKI_TRUST_EDITOR_DEBUG=1`, or at any time by right-clicking the window and
toggling **Debug logging** (`app.set_debug_logging`).

## Environment notes

- The app writes its own state under the platform state dir (see `paths.py`);
  some environments need an explicit write grant there before the app, or the
  test suite, can create it.
- **`/tmp` can be unusable in a way that looks like it works.** A sandbox may
  allow an ordinary write but refuse `tempfile`'s random `O_CREAT|O_EXCL` probe
  (`touch /tmp/x` succeeds, `tempfile` still falls through to the cwd). The
  symptom is a `tmp********/` directory appearing in the project root after every
  pytest run. Root `conftest.py` sets `basetemp` to `.pytest-tmp/` so the litter
  is contained either way. Note `basetemp` is **not** a pytest ini option —
  `pyproject.toml` rejects it with `Unknown config option` and pytest ignores it.
- Keep scratch files inside the project directory and delete them when done.
- A restricted sandbox may break `git`; recover by reading files rather than
  `git checkout`.
- **A Qt app can die at interpreter shutdown** with SIGSEGV/SIGABRT
  (`malloc_consolidate`, no Python frame) once PySide6 starts tearing down. No
  `try`/`except` or decorator catches it; that is why the suite's exit code is
  not a reliable signal on its own — check the `N passed` line. Do not add a
  `pytest_sessionfinish` hook that pumps events to dodge it: it hangs.
