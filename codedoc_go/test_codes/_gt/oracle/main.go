// Command goracle is a runtime call-edge oracle for Go (standard library only).
//
//	goracle instrument --src DIR --out COPY [--mode all|needed|none] [--dirs a,b] [--force]
//	    copies DIR to COPY and rewrites the copy: every function body and func
//	    literal starts with __goracle.E(fid); every call site gets a /*line*/
//	    directive whose file name carries the site id (mode all); completion
//	    and reach counters mark executed statements. The runtime package joins
//	    every instrumented module (require + replace, vendor/modules.txt when
//	    vendored). The site table goes to COPY/_goracle/sites.jsonl.
//	(build or test the copy with -gcflags=all=-l -ldflags=-checklinkname=0 and
//	 GORACLE_OUT=DIR)
//	goracle report --copy COPY --hits DIR[,DIR...] [--out obs.jsonl]
//	    maps the recorded caller frames back to sites (default output
//	    COPY/_goracle/obs.jsonl).
//	goracle match --obs obs.jsonl --records records.jsonl [--out verdicts.jsonl]
//	    judges resolver records (one edge each) against the observations.
//	goracle judge --obs obs.jsonl --manifest manifest.json [--ran a,b] [--failed c] [--out verdicts.json]
//	    judges ground-truth cases: observed ⊆ targets ∪ runtime_targets and
//	    every runtime target observed.
//	goracle scan --root DIR [--dirs a,b] [--out sites.jsonl]
//	    writes the site table of DIR without copying or rewriting anything.
//
// Pinned to Go 1.27.2 (see package rt). Exit codes: 0 ok, 1 failure (for match
// and judge: at least one wrong verdict), 2 usage.
package main

import (
	_ "embed"
	"fmt"
	"os"
)

//go:embed rt/rt.go
var rtSource []byte

// rtExitSource becomes exit.go of the runtime package in an instrumented copy.
// It is not a .go file here, so the oracle module builds and tests without
// -ldflags=-checklinkname=0.
//
//go:embed rt/exit.go.in
var rtExitSource []byte

// rtModule is the module path of the runtime package in an instrumented copy.
const rtModule = "codedoc.local/goracle/rt"

// rtGoVersion is the go line of the runtime module: older than any workspace
// that may use it.
const rtGoVersion = "1.21"

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	switch os.Args[1] {
	case "instrument":
		cmdInstrument(os.Args[2:])
	case "scan":
		cmdScan(os.Args[2:])
	case "report":
		cmdReport(os.Args[2:])
	case "match":
		os.Exit(cmdMatch(os.Args[2:], os.Stdout))
	case "judge":
		os.Exit(cmdJudge(os.Args[2:], os.Stdout))
	default:
		usage()
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: goracle instrument|scan|report|match|judge [flags]")
	os.Exit(2)
}

func check(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "goracle:", err)
		os.Exit(1)
	}
}
