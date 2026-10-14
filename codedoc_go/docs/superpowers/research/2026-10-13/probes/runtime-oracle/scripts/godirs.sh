#!/bin/bash
# godirs.sh ROOT EXCLUDE_REGEX: comma-separated package dirs (relative) with .go files
cd "$1" && find . -name '*.go' -not -path '*/testdata/*' -not -path '*/vendor/*' -not -path '*/.*' -not -path '*/_*' -printf '%h\n' \
 | sed 's#^\./##; s#^\.$#.#' | sort -u | grep -Ev "${2:-^$}" | paste -sd, -
