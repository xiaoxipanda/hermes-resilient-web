#!/bin/sh
set -eu

profile="${1:-default}"
root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
hermes_bin="${HERMES_BIN:-hermes}"

if ! command -v "$hermes_bin" >/dev/null 2>&1; then
  printf 'Hermes CLI not found: %s\n' "$hermes_bin" >&2
  exit 1
fi

exec "$hermes_bin" -p "$profile" plugins install "file://$root" --enable
