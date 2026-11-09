#!/usr/bin/env bash
# Run the maki trust editor from source, without installing the package.
#
# Two ways to launch, in order of preference:
#
#   1. If a uv venv already exists (from `uv sync`), use it.
#   2. Otherwise run with an ephemeral env built from pyproject dependencies.
#
# Pass --extra=pyqt6 (or pyside6) to force a Qt binding when no venv exists.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

BINDING="${MAKI_TRUST_EDITOR_QT:-}"

usage() {
    cat <<'EOF'
Usage: ./run_app.sh [--extra pyside6|pyqt6]

Runs the maki trust editor from source. Uses an existing uv venv when present,
otherwise an ephemeral uv environment. Nothing is installed into your shell.

Environment:
  MAKI_TRUST_EDITOR_QT   Force a Qt binding when no venv exists (pyside6|pyqt6).
EOF
}

EXTRA_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --extra) BINDING="${2:-}"; shift 2 ;;
        --extra=*) BINDING="${1#--extra=}"; shift ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found on PATH. Install it from https://docs.astral.sh/uv/" >&2
    exit 1
fi

# 1. An existing environment wins: it is the fastest and respects whatever the
#    user already synced.
if [[ -x .venv/bin/python ]]; then
    exec uv run python -m maki_trust_editor
fi

# 2. No venv yet: build a throwaway one from the declared dependencies. Qt
#    bindings are optional extras, so pick one explicitly.
if [[ -z "$BINDING" ]]; then
    for candidate in pyside6 pyqt6; do
        if uv run --quiet --with "$candidate" python -c "import qtpy" >/dev/null 2>&1; then
            BINDING="$candidate"
            break
        fi
    done
fi
BINDING="${BINDING:-pyqt6}"

echo "No .venv found; using an ephemeral uv environment with $BINDING." >&2
exec env PYTHONPATH=src uv run --quiet \
    --with "$BINDING" --with qtpy --with tomlkit --with psutil \
    python -m maki_trust_editor
