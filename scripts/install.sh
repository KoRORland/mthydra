#!/bin/sh
# scripts/install.sh — naked-24.04 bootstrap for the mthydra EU host installer.
# Does ONLY what must precede Python (apt prereqs, git clone, venv build), then
# execs the tested Python orchestrator. Holds no domain logic and no secrets.
set -eu

SUBCMD="install"
GIT_URL="${MTHYDRA_GIT_URL:-}"
GIT_REF="${MTHYDRA_GIT_REF:-}"
SRC_DIR="${MTHYDRA_SRC_DIR:-/opt/mthydra/src}"
VENV_DIR="${MTHYDRA_VENV_DIR:-/opt/mthydra/venv}"
CFG_FILE=""
FWD=""   # forwarded args

while [ $# -gt 0 ]; do
  case "$1" in
    --standby) SUBCMD="install-standby" ;;
    --git-url) GIT_URL="$2"; shift ;;
    --git-ref) GIT_REF="$2"; shift ;;
    --src-dir) SRC_DIR="$2"; shift ;;
    --venv-dir) VENV_DIR="$2"; shift ;;
    --config) CFG_FILE="$2"; FWD="$FWD --config $2"; shift ;;
    *) FWD="$FWD $1" ;;
  esac
  shift
done

# If a --config was given, seed any unset shell-layer params from its [install]
# section so the operator only types git_url / git_ref in ONE place (the ini).
# Tiny awk-based parser, no extra deps.
ini_get() {
  # ini_get FILE SECTION KEY → echoes value or empty
  [ -r "$1" ] || return 0
  awk -v section="[$2]" -v key="$3" '
    $0 == section { in_s = 1; next }
    /^\[/ { in_s = 0; next }
    in_s && $0 ~ "^[ \t]*" key "[ \t]*=" {
      sub(/^[^=]*=[ \t]*/, "")
      sub(/[ \t]*[;#].*$/, "")
      sub(/[ \t]*$/, "")
      print
      exit
    }
  ' "$1"
}
if [ -n "$CFG_FILE" ]; then
  [ -z "$GIT_URL" ] && GIT_URL="$(ini_get "$CFG_FILE" install git_url)"
  [ -z "$GIT_REF" ] && GIT_REF="$(ini_get "$CFG_FILE" install git_ref)"
fi
[ -z "$GIT_REF" ] && GIT_REF="main"   # last-resort default

say() { printf '[install.sh] %s\n' "$1"; }

if [ "$(id -u)" -ne 0 ] && [ "${MTHYDRA_SKIP_APT:-0}" != "1" ]; then
  echo "install.sh must run as root" >&2; exit 1
fi

if [ -r /etc/os-release ]; then
  . /etc/os-release
  case "${VERSION_ID:-}" in
    24.04) : ;;
    *) say "WARNING: tested only on Ubuntu 24.04 (found ${PRETTY_NAME:-unknown})" ;;
  esac
fi

if [ "${MTHYDRA_SKIP_APT:-0}" != "1" ]; then
  say "installing build prerequisites via apt"
  apt-get update
  apt-get install -y python3.12 python3.12-venv git age build-essential
fi

if [ "${MTHYDRA_SKIP_BUILD:-0}" != "1" ]; then
  if [ -d "$SRC_DIR/.git" ]; then
    say "updating existing checkout at $SRC_DIR"
    git -C "$SRC_DIR" fetch --tags origin
    git -C "$SRC_DIR" checkout "$GIT_REF"
  else
    [ -n "$GIT_URL" ] || {
      echo "git_url required: pass --git-url, set MTHYDRA_GIT_URL, or put it in the [install] section of --config" >&2
      exit 2
    }
    say "cloning $GIT_URL@$GIT_REF → $SRC_DIR"
    git clone --branch "$GIT_REF" "$GIT_URL" "$SRC_DIR"
  fi
  say "building venv at $VENV_DIR"
  python3.12 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --upgrade pip
  "$VENV_DIR/bin/pip" install -e "$SRC_DIR"
fi

OPS="$VENV_DIR/bin/mthydra-ops"
"$OPS" --help >/dev/null 2>&1 || { echo "mthydra-ops smoke failed" >&2; exit 3; }

say "handing off to: mthydra-ops $SUBCMD$FWD"
# shellcheck disable=SC2086  # intentional word-splitting of forwarded args
exec "$OPS" "$SUBCMD" $FWD
