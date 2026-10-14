#!/bin/bash
# prepare.sh SRC COPY DIRS TAGMODE: copy SRC to COPY, add the rt module to the
# workspace and instrument DIRS (comma-separated, relative to COPY).
. /home/admin/scratch/codedoc-go-research/runtime-oracle/scripts/env.sh
SRC=$1 COPY=$2 DIRS=$3 TAG=${4:-needed}
[ -n "$SRC" ] && [ -n "$COPY" ] && [ -n "$DIRS" ] || { echo "usage: prepare.sh SRC COPY DIRS [TAG]"; exit 2; }
case "$COPY" in $W/*) ;; *) echo "COPY must be under $W"; exit 2;; esac
rm -rf "$COPY" && mkdir -p "$(dirname "$COPY")" && cp -a "$SRC" "$COPY" || { echo FAIL copy; exit 1; }
rm -rf "$COPY/.git"
cp -a $W/oracle/rt "$COPY/_goracle_rt" || { echo FAIL rt; exit 1; }
if [ -f "$COPY/go.work" ]; then
  printf '\nuse ./_goracle_rt\n' >> "$COPY/go.work"
else
  printf 'go 1.27\n\nuse (\n\t.\n\t./_goracle_rt\n)\n' > "$COPY/go.work"
fi
$W/bin/goracle instrument -root "$COPY" -dirs "$DIRS" -tag "$TAG" -sites "$COPY.sites.jsonl" || { echo FAIL instrument; exit 1; }
