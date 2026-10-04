#!/bin/sh
# Sieve installer — POSIX sh, uv-managed environment, no sudo.
# Usage: sh scripts/install.sh [--venv DIR] [--minimal] [--extras LIST] [--repo URL] [--no-check]
# For remote installs, download a versioned release artifact, inspect it, then run this script.
set -eu

REPO_DEFAULT="https://github.com/shy-tangerine/Sieve.git"
REPO=""
VENV_DIR=".venv"
EXTRAS="all"
DO_CHECK=1
REVISION=""
SRC_TMP=""
CHECK_JSON=""
CHECK_ERR=""

# One POSIX cleanup path covers checkout and clone modes, including early exits.
_sievecleanup() {
  [ -z "$SRC_TMP" ] || rm -rf -- "$SRC_TMP"
  [ -z "$CHECK_JSON" ] || rm -f -- "$CHECK_JSON"
  [ -z "$CHECK_ERR" ] || rm -f -- "$CHECK_ERR"
  return 0
}
trap _sievecleanup 0
trap 'exit 130' INT
trap 'exit 143' TERM

usage() {
  echo "Usage: $0 [--venv DIR] [--minimal] [--extras LIST] [--repo URL] [--no-check]" >&2
  echo "  --venv DIR     venv path (default: .venv)" >&2
  echo "  --minimal      install core dependencies only" >&2
  echo "  --extras LIST  install a comma-separated optional-extra set (default: all)" >&2
  echo "  --repo URL     clone URL if not running inside a checkout (default: $REPO_DEFAULT)" >&2
  echo "  --no-check     skip post-install self-check" >&2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --venv) VENV_DIR="$2"; shift 2 ;;
    --all) EXTRAS="all"; shift ;;
    --minimal) EXTRAS=""; shift ;;
    --extras) EXTRAS="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --revision) REVISION="$2"; shift 2 ;;
    --no-check) DO_CHECK=0; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    -*) echo "unknown flag: $1" >&2; usage; exit 2 ;;
    *) REPO="$1"; shift ;;
  esac
done

# Resolve source dir: use current checkout if it looks like sieve, else clone
SRC_DIR=""
if [ -f "./pyproject.toml" ] && [ -d "./sieve" ]; then
  SRC_DIR="$(pwd)"
else
  if [ -z "$REPO" ]; then
    # allow bare URL as first positional arg fallback
    REPO="$REPO_DEFAULT"
  fi
  # if REPO looks like URL, clone to temp; else treat as path
  case "$REPO" in
    http*|git@*|ssh://*)
      SRC_TMP="$(mktemp -d 2>/dev/null || mktemp -d -t sieve-src)"
      SRC_DIR="$SRC_TMP"
      echo "Cloning $REPO -> $SRC_DIR ..." >&2
      git clone --depth 1 "$REPO" "$SRC_DIR" >&2
      if [ -n "$REVISION" ]; then (cd "$SRC_DIR" && git fetch --depth 1 origin "$REVISION" && git checkout --detach FETCH_HEAD) >&2; fi
      ;;
    *)
      if [ -d "$REPO/sieve" ]; then SRC_DIR="$REPO"; else
        echo "error: not a sieve checkout: $REPO" >&2; exit 1
      fi
      ;;
  esac
fi

# Normalize the environment path before running uv from a cloned/source directory.
case "$VENV_DIR" in
  /*) ;;
  *) VENV_DIR="$(pwd)/$VENV_DIR" ;;
esac

# 1) uv and Python >=3.11
if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

PY=""
for c in python3.11 python3.12 python3.13 python3 python; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info>=(3,11) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  echo "error: python >=3.11 not found. Install python3.11+ and retry." >&2
  exit 1
fi
echo "Using $PY ($($PY --version 2>&1))" >&2

# 2) sync the locked project environment
export UV_PROJECT_ENVIRONMENT="$VENV_DIR"
set --
if [ -n "$EXTRAS" ]; then
  old_ifs="$IFS"
  IFS=,
  for extra in $EXTRAS; do set -- "$@" --extra "$extra"; done
  IFS="$old_ifs"
fi
if [ "$EXTRAS" = "all" ]; then
  set -- --all-extras
fi
echo "Syncing Sieve with uv${EXTRAS:+ [$EXTRAS]} ..." >&2
(cd "$SRC_DIR" && uv sync --locked "$@") >&2

# 3) Report reusable local capabilities. No configuration is changed here.
echo "--- detected local capabilities (advisory) ---" >&2
uv run --directory "$SRC_DIR" python -c 'from sieve.capabilities import detect; import json; print(json.dumps(detect(), indent=2))' >&2 || true

# 4) yt-dlp check (warn only, never sudo silently)
if ! command -v yt-dlp >/dev/null 2>&1 && ! "$VENV_DIR/bin/python" -c "import yt_dlp" 2>/dev/null; then
  echo "WARN: yt-dlp not found (needed for 'sieve youtube'). Install:" >&2
  echo "  uv tool install yt-dlp" >&2
else
  echo "yt-dlp: $(yt-dlp --version 2>/dev/null || python -c 'import yt_dlp; print(yt_dlp.version.__version__)' 2>/dev/null)" >&2
fi

# 5) self-check (warnings only)
if [ "$DO_CHECK" = 1 ]; then
  echo "--- self-check ---" >&2
  if ! uv run --directory "$SRC_DIR" sieve --version 2>&1; then
    echo "WARN: 'sieve --version' failed" >&2
  fi
  # bounded live fetch with timeout (keyless, no auth)
  CHECK_JSON="$(mktemp 2>/dev/null || mktemp -t sieve-check)"
  CHECK_ERR="$(mktemp 2>/dev/null || mktemp -t sieve-check-err)"
  URL="https://example.com"
  TMOUT=20
  if command -v timeout >/dev/null 2>&1; then
    if ! timeout "$TMOUT" uv run --directory "$SRC_DIR" sieve fetch "$URL" >"$CHECK_JSON" 2>"$CHECK_ERR"; then
      echo "WARN: 'sieve fetch $URL' failed or timed out after ${TMOUT}s (network may be blocked) — install still OK" >&2
      cat "$CHECK_ERR" >&2 2>/dev/null || true
    else
      echo "self-check: sieve fetch $URL OK ($(wc -c <"$CHECK_JSON") bytes)" >&2
    fi
  else
    # no timeout(1): try without bound but warn
    if ! uv run --directory "$SRC_DIR" sieve fetch "$URL" >"$CHECK_JSON" 2>"$CHECK_ERR"; then
      echo "WARN: 'sieve fetch $URL' failed (no timeout available)" >&2
    else
      echo "self-check: sieve fetch $URL OK" >&2
    fi
  fi
  echo "Run commands with: uv run --directory $SRC_DIR sieve ..." >&2
fi
echo "Done." >&2
