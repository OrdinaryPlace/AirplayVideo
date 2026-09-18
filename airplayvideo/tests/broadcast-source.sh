#!/bin/sh
set -eu
fixture=$(mktemp --suffix=.ts)
trap 'rm -f "$fixture"' EXIT
gzip -dc "$(dirname "$0")/fixtures/broadcast-mid-gop.ts.gz" > "$fixture"
"$1" "$fixture"
