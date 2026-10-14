#!/bin/sh
# usage: listfiles.sh REPO CGO_ENABLED OUT  -- lists every .go file go list loads (all workspace packages, tests incl.)
cd "$1" && CGO_ENABLED=$2 go list -e -test=false -f '{{$d := .Dir}}{{range .GoFiles}}{{$d}}/{{.}}
{{end}}{{range .CgoFiles}}{{$d}}/{{.}}
{{end}}{{range .TestGoFiles}}{{$d}}/{{.}}
{{end}}{{range .XTestGoFiles}}{{$d}}/{{.}}
{{end}}' work | grep . | sort -u > "$3.loaded"
cd "$1" && CGO_ENABLED=$2 go list -e -test=false -f '{{$d := .Dir}}{{range .IgnoredGoFiles}}{{$d}}/{{.}}
{{end}}' work | grep . | sort -u > "$3.ignored"
cd "$1" && CGO_ENABLED=$2 go list -e -test=false -f '{{$d := .Dir}}{{range .CgoFiles}}{{$d}}/{{.}}
{{end}}' work | grep . | sort -u > "$3.cgo"
