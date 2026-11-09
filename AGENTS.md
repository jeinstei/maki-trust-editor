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

- `QT_QPA_PLATFORM=minimal .venv/bin/python -m pytest -q` — always with a
  **timeout**. A `QMessageBox` modal under the `minimal` platform hangs forever;
  any test that can trigger a dialog must stub it or guarantee it does not fire.
- `ruff check src tests` and `black --check src tests` must be clean.
- 89 tests as of last change. Keep them fast (whole suite well under a couple of
  seconds).

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
| `widgets.py` | Scope editor widgets, filter, section picker |
| `window.py` | Main window, tabs, session/project selection, save logic |
| `app.py` | Entry point, logging, Ctrl-C handling |

### Data model

- `sessions.Session` — path, header fields, and `rules: list[SessionRule]`. It
  reads only the **first line (header)** and **last line (the `t:"meta"`
  record)** of the file; the megabytes in between are never loaded. The last-line
  offset is kept so `write()` can stream the prefix and rewrite only the last
  line (`shutil.copyfileobj` + `truncate` + `write` + `os.replace`). Prefix bytes
  are never decoded, so any encoding maki wrote survives.
- `SessionRule(tool, scope, effect)`. A rule with `scope=None` means "every scope
  for this tool".
- `ToolGroup.group(rules)` buckets rules by tool and **sorts tools**; within a
  tool, scope rules come first and the scope-less rule last. `to_rules()`
  reconstructs. `sessions.canonical_rules(rules)` is the flattened canonical
  form — the exact order a save writes.

## Invariants (do not break)

1. **`_changed_stores()` is a pure read.** It must not mutate any model. It used
   to call `_commit_current()`, which is what caused the "dirty on open"
   regression (see below). Commit happens elsewhere.
2. **`SessionView.reload()` must be skipped for a dirty row.** `_populate_row`
   calls `view.reload()` only when `not self._is_dirty(row)`. Reloading a row
   that holds a committed-but-unsaved edit would re-read the file and drop the
   edit. The reload exists because a live session grows while maki runs, so the
   recorded last-line offset goes stale.
3. **Widget change signals fire during construction.** Building an editor
   (`set_section`, `add_row`) emits `currentIndexChanged`, which would otherwise
   be mistaken for a user edit and commit a half-built editor. Two guards:
   - `ScopeRulesEditor._notify()` is a no-op while `self._loading` is set;
     `set_section` wraps its widget mutations in `_loading`.
   - `SessionsTab.loading` is True for the duration of `_populate_row`, and
     `MainWindow._mark_dirty` returns early while `self._loading or
     self.sessions_tab.loading`.
4. **Dirty = model vs. baseline, per file.** `_capture_baselines()` records each
   store's canonical serialization at load. A store with no baseline (missing or
   empty file) is never dirty. Sessions are canonicalized before hashing the
   baseline, so the file's raw rule order is not mistaken for an edit.
5. **Baselines advance per saved file only.** After a save, `_serialized(path)`
   recomputes the baseline for the saved file(s). Never call a full `_reload()`
   after a save — it would discard other files' unsaved edits.
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
  them and there is no discard prompt. `_project_selected` and `_tab_changed`
  commit the outgoing selection first.
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
3. If a maki process is running: shared targets are blocked (Linux checks
   `/proc/<pid>/fd`; other platforms block all shared files), and the user is
   asked whether to write the session files only.
4. `safe_write.save_all()` — backup, then atomic replace or the session writer.
5. Advance only the saved files' baselines; `_refresh_marks()`.

## Session liveness (locks)

- Liveness is an **advisory lock**, not file existence. maki holds an exclusive
  lock on `<state>/sessions/locks/<session-id>` (an empty file). Those files are
  **never deleted** — there are ~18 stale ones on a normal machine — so only a
  *held* lock counts as open.
- `sessions.is_locked(sessions_dir, id)` takes a **non-blocking SHARED** lock
  (POSIX `fcntl.flock`, Windows `msvcrt.locking(LK_NBLCK)`); `Held` means open.
- **POSIX-only test** (`test_locks.py`, `test_per_file_save.py` lock cases skip
  on Windows).
- The list shows a padlock (`LOCK_ICON`, U+1F512) or a filled dot
  (`LOCK_ICON_FALLBACK`, U+25CF). Pick it with `QFontMetrics.inFontUcs4(ord(c))`
  — **not `inFont`**, which takes a single UTF-16 `QChar` and raises on astral
  characters such as the padlock.

## gotchas

- **The `READ ONLY` reasoning is subtle:** a locked session shows all its rules
  but with every control disabled (`set_read_only`), including **+ Add tool
  group** (`self._add_button`). It is excluded from `_session_targets()` (so it
  is not dirty and not a save target), and `_drop_locked_sessions` is the
  save-time backstop for a session that became locked after load. The two layers
  are deliberate: `_session_targets()` uses the cached `view.locked`, while
  `_drop_locked_sessions` re-probes live.
- **`SessionView.reload()` canonicalizes rules** via `sessions.canonical_rules()`.
  Do not remove that: a freshly reloaded non-canonical-order session would look
  dirty.
- **Widget identity:** `SessionsTab._editors` is `[(tool_name, ScopeRulesEditor)]`;
  `collect()` rebuilds `SessionRule`s from it. Renaming a group rebuilds all
  rules, so keep `ToolGroup` as the single place that defines rule order.
- **`_remove` / `_add` are instance attributes** on the widgets so they can be
  disabled for read-only mode. The `ScopeRow` remove button is `self._remove`.
- **Pre-trust snapshot** is frozen by maki itself; the app shows it and can add
  or remove roots, but only paths that exist on disk.
- **Unknown keys** in `permissions.toml` (top-level and per-section) are preserved
  on save; `PermissionsFile.load(previous)` keeps them.
- **B3 invalid default:** a section `default` maki would reject (e.g. `""`) is
  shown as `(invalid)` (`INVALID_LABEL`) and preserved verbatim unless the user
  picks a real effect. maki only accepts `allow`/`deny`/`prompt`
  (maki-config/src/lib.rs:1071, `DefaultEffect`:1152) and treats anything else as
  absent.

## Logging

`app.setup_logging()` installs a `RotatingFileHandler` (1 MB × 3) at
`paths.log_path()`, in **our** state dir (`<state-root>/maki-trust-editor`), never
maki's. `sys.excepthook` writes uncaught exceptions. `window.LOGGER` records the
startup lock state, every save (targets + result), the open-session skip, and any
load problem. Logging is best-effort: an `OSError` means the app runs without a
log.

## Environment notes

- The app writes its own state under the platform state dir (see `paths.py`);
  some environments need an explicit write grant there before the app, or the
  test suite, can create it.
- `/tmp` (or the system temp dir) may or may not be writable depending on the
  distribution and sandbox: it is sometimes blocked, not always. Keep scratch
  files inside the project directory and delete them when done.
- A restricted sandbox may also break `git` (a version control operation fails
  outright). If so, recover by reading files rather than `git checkout`.
- Prefer `main`/feature branches, but never assume a network-dependent or VCS
  command will behave the same everywhere; verify before relying on it.
