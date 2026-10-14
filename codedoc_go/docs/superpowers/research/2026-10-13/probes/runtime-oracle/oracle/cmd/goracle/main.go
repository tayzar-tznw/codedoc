// Command goracle is a runtime call-edge oracle for Go (standard library only).
//
//	goracle instrument -root COPY -dirs a,b -sites sites.jsonl [-tag needed|all|none]
//	    rewrites the copy in place: every function body and func literal starts
//	    with __goracle.E(fid); call sites on ambiguous lines get a /*line*/
//	    directive whose file name carries the site id.
//	(build or test the copy with -gcflags=all=-l -ldflags=-checklinkname=0 and
//	 GORACLE_OUT=DIR; package codedoc.dev/goracle/rt must be in the workspace)
//	goracle report -sites sites.jsonl -hits DIR -out obs.jsonl
//	    maps the recorded caller frames back to sites.
//	goracle match -obs obs.jsonl -sites sites.jsonl -edges resolver.jsonl
//	    judges resolver edges against the observations.
package main

import (
	"fmt"
	"os"
)

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	switch os.Args[1] {
	case "instrument":
		cmdInstrument(os.Args[2:])
	case "report":
		cmdReport(os.Args[2:])
	case "match":
		cmdMatch(os.Args[2:])
	default:
		usage()
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: goracle instrument|report|match [flags]")
	os.Exit(2)
}
