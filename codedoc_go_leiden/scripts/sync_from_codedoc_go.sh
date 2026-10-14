#!/usr/bin/env bash
#
# Bring codedoc_go_leiden up to date with codedoc_go, its parent (the family's
# fork convention, leiden spec §0).
#
# 1. rsync copies every file of ../codedoc_go here, except the paths listed in
#    scripts/leiden_owned.txt: the leiden-only modules, tests and docs, and local
#    state (.venv, .env, output dirs, webapp/frontend/node_modules and dist).
#    Listed paths are never overwritten or deleted; every other file that
#    codedoc_go does not have is deleted.
# 2. scripts/leiden_hooks.py re-applies the LEIDEN-HOOK edits to the shared
#    files and verifies them. A hook whose anchor changed in codedoc_go stops
#    the sync with its name; re-anchor it in leiden_hooks.py and run again.
#
# Usage:  codedoc_go_leiden/scripts/sync_from_codedoc_go.sh [--dry-run]
#   --dry-run  list what rsync would change, and change nothing

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
leiden="$(dirname "$here")"
parent="$(dirname "$leiden")/codedoc_go"

# Exact directories only: this tree must be codedoc_go_leiden and the source its sibling
# codedoc_go (never a copy of either, such as a codedoc_go_v2_* directory).
if [ "$(basename "$leiden")" != codedoc_go_leiden ]; then
  echo "ERROR: $leiden is not codedoc_go_leiden; this script only syncs codedoc_go into codedoc_go_leiden" >&2
  exit 1
fi
if [ ! -f "$parent/graph_generator/pipeline.py" ]; then
  echo "ERROR: $parent is not codedoc_go (graph_generator/pipeline.py is missing)" >&2
  exit 1
fi

if [ "${1:-}" = "--dry-run" ]; then
  rsync -a --delete --dry-run --itemize-changes --exclude-from="$here/leiden_owned.txt" \
    "$parent/" "$leiden/"
  exit 0
fi

rsync -a --delete --exclude-from="$here/leiden_owned.txt" "$parent/" "$leiden/"

python="$leiden/.venv/bin/python"
[ -x "$python" ] || python="python3"
"$python" "$here/leiden_hooks.py" apply --root "$leiden"
"$python" "$here/leiden_hooks.py" verify --root "$leiden"
echo "codedoc_go_leiden is in sync with codedoc_go; every LEIDEN-HOOK is applied."
