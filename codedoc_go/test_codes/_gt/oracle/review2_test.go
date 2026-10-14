package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// srcPos returns the line of testdata/review2/rel found by matching markers
// on successive lines (each after the previous one) and the 1-based byte
// column of token on it.
func srcPos(t *testing.T, rel, token string, markers ...string) (int, int) {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("testdata", "review2", filepath.FromSlash(rel)))
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(string(data), "\n")
	at := 0
	for _, m := range markers {
		for at < len(lines) && !strings.Contains(lines[at], m) {
			at++
		}
		if at == len(lines) {
			t.Fatalf("%s: no line with %q", rel, m)
		}
		if m != markers[len(markers)-1] {
			at++
		}
	}
	c := strings.Index(lines[at], token)
	if c < 0 {
		t.Fatalf("%s:%d: no %q", rel, at+1, token)
	}
	return at + 1, c + 1
}

// The second goracle review's findings (testdata/review2/README.md): the
// edges of q to m are correct and each once got a WRONG verdict; w holds
// wrong edges to calls that ran under runtime.gopanic.
func TestReview2Probes(t *testing.T) {
	gobin := goTool(t)
	_, obs := instrumentAndRun(t, gobin, "review2", false, []string{"run", "./cmd/run"},
		[]string{"run", "./cmd/stop"}, []string{"run", "./cmd/serve"}, []string{"test", "./pm"})
	site := func(rel, token string, markers ...string) string {
		l, c := srcPos(t, rel, token, markers...)
		return `"file":"` + rel + `","line":` + itoa(l) + `,"col":` + itoa(c) + `,"name":"` + token + `"`
	}
	decl := func(rel, qual, token string, markers ...string) string {
		l, _ := srcPos(t, rel, token, markers...)
		return `{"file":"` + rel + `","line":` + itoa(l) + `,"qualname":"` + qual + `"}`
	}
	closer := decl("v/v.go", "Closer.Close", "Close", "type Closer")
	closers := `[` + decl("v/v.go", "A.Close", "Close", "func (A) Close") + `,` +
		decl("v/v.go", "Inner.Close", "Close", "func (Inner) Close") + `]`
	compute := decl("v/v.go", "compute", "compute", "func compute")
	edges := []struct{ rec, want string }{
		// a one-line interface: the target names its second method by line
		// and qualname only (evaluate's shape)
		{`{` + site("q/q.go", "Write", "rw.Write()") + `,"target":` + decl("q/q.go", "RW.Write", "Write", "type RW") +
			`,"candidates":[` + decl("q/q.go", "F.Write", "Write", "func (F) Write") + `]}`, "IFACE-CONSISTENT"},
		{`{` + site("q/q.go", "Write", "rw.Write()") + `,"target":` + decl("q/q.go", "RW.Write", "Write", "type RW") + `}`,
			"IFACE-CONSISTENT"},
		// deferred calls run by runtime.deferreturn after a recovered panic,
		// through an itab wrapper, a -fm method value and a promoted method:
		// they belong to the defer group, not to the compute site
		{`{` + site("v/v.go", "compute", "func ViaItab", "panic(") + `,"target":` + compute + `}`, "CONFIRMED-NOEXEC"},
		{`{` + site("v/v.go", "Close", "func ViaItab", "defer c.Close()") + `,"target":` + closer + `,"candidates":` + closers + `}`,
			"DEFER-GROUP-CONSISTENT"},
		{`{` + site("v/v.go", "compute", "func MethodValue", "panic(") + `,"target":` + compute + `}`, "CONFIRMED-NOEXEC"},
		{`{` + site("v/v.go", "f", "func MethodValue", "defer f()") + `,"target":` +
			decl("v/v.go", "A.Close", "Close", "func (A) Close") + `}`, "DEFER-GROUP-CONSISTENT"},
		{`{` + site("v/v.go", "compute", "func Promoted", "panic(") + `,"target":` + compute + `}`, "CONFIRMED-NOEXEC"},
		{`{` + site("v/v.go", "Close", "func Promoted", "defer c.Close()") + `,"target":` + closer + `,"candidates":` + closers + `}`,
			"DEFER-GROUP-CONSISTENT"},
		// a deferred call that never ran: os.Exit, a goroutine blocked at exit
		{`{` + site("x/x.go", "b", "defer b()") + `,"target":` + decl("x/x.go", "b", "b", "func b()") + `}`, "UNEXERCISED"},
		{`{` + site("y/y.go", "b", "defer b()") + `,"target":` + decl("y/y.go", "b", "b", "func b()") + `}`, "UNEXERCISED"},
		// b ran under runtime.gopanic inside a func literal of a range-over-func
		// body, which the runtime names Loop-range1.1
		{`{` + site("k/k.go", "b", "defer b()") + `,"target":` + decl("k/k.go", "b", "b", "func b()") + `}`, "UNEXERCISED"},
		// package main in its test binary, where the runtime uses the import path
		{`{` + site("pm/main.go", "b", "defer b()") + `,"target":` + decl("pm/main.go", "b", "b", "func b()") + `}`,
			"DEFER-GROUP-CONSISTENT"},
		// WRONG edges to calls that ran under runtime.gopanic as G's and H's
		// defers: the deferwrap of defer c(1) names its owner G, so F's group
		// misses c; defer d() has no deferwrap and stays credited to every
		// function with defers on the stack (a known leniency, README)
		{`{` + site("w/w.go", "a", "func F", "defer a()") + `,"target":` + decl("w/w.go", "c", "c", "func c(") + `}`,
			"DEFER-GROUP-OTHER"},
		{`{` + site("w/w.go", "a", "func F", "defer a()") + `,"target":` + decl("w/w.go", "a", "a", "func a()") + `}`,
			"DEFER-GROUP-CONSISTENT"},
		{`{` + site("w/w.go", "a", "func E", "defer a()") + `,"target":` + decl("w/w.go", "d", "d", "func d()") + `}`,
			"UNEXERCISED"},
		// compute() panics while the defer's arguments are evaluated: report
		// is never registered, and neither is the run counter after it
		{`{` + site("r/r.go", "report", "defer report(") + `,"target":` + decl("r/r.go", "report", "report", "func report(") + `}`,
			"UNEXERCISED"},
	}
	var recLines []string
	for _, e := range edges {
		recLines = append(recLines, e.rec)
	}
	work := t.TempDir()
	out := filepath.Join(work, "verdicts.jsonl")
	var buf bytes.Buffer
	cmdMatch([]string{"--obs", obs, "--records", writeEdges(t, work, recLines), "--out", out}, &buf)
	var got []Verdict
	readJSONL(out, func(b []byte) {
		var v Verdict
		if err := json.Unmarshal(b, &v); err != nil {
			t.Fatal(err)
		}
		got = append(got, v)
	})
	if len(got) != len(edges) {
		t.Fatalf("%d verdicts for %d edges:\n%s", len(got), len(edges), buf.String())
	}
	for i, e := range edges {
		if got[i].Verdict != e.want {
			t.Errorf("edge %d: %s (%s, observed %v), want %s\n%s", i, got[i].Verdict, got[i].Detail, got[i].Observed, e.want, e.rec)
		}
	}

	// the judge on the same observations
	tgt := func(qual, file string) string {
		return `{"qualname":"` + qual + `","module":"example.com/review2","kind":"method","file":"` + file + `"}`
	}
	pos := func(rel, token string, markers ...string) string {
		l, c := srcPos(t, rel, token, markers...)
		return `"file":"` + rel + `","line":` + itoa(l) + `,"col":` + itoa(c) + `,"token":"` + token + `"`
	}
	cases := []struct{ id, json, want string }{
		{"STOP-B", `{"id":"STOP-B",` + pos("x/x.go", "b", "defer b()") + `,"site_kind":"defer","entry":"run",` +
			`"expect":{"status":"resolved","targets":[` + tgt("b", "x/x.go") + `]}}`, "UNVERIFIABLE"},
		{"NOSPLIT-IMPL", `{"id":"NOSPLIT-IMPL",` + pos("n/n.go", "M", "return i.M()") + `,"site_kind":"call","entry":"run",` +
			`"expect":{"status":"resolved","targets":[` + tgt("I.M", "n/n.go") + `],"runtime_targets":[` + tgt("A.M", "n/n.go") +
			`],"open_world":false}}`, "UNVERIFIABLE"},
		{"TWO-B", `{"id":"TWO-B",` + pos("pm/main.go", "b", "defer b()") + `,"site_kind":"defer","entry":"run",` +
			`"expect":{"status":"resolved","targets":[` + tgt("b", "pm/main.go") + `]}}`, "DEFER-GROUP-CONSISTENT"},
		{"ITAB-CLOSE", `{"id":"ITAB-CLOSE",` + pos("v/v.go", "Close", "func ViaItab", "defer c.Close()") +
			`,"site_kind":"defer","entry":"run","expect":{"status":"resolved","targets":[` + tgt("Closer.Close", "v/v.go") +
			`],"runtime_targets":[` + tgt("A.Close", "v/v.go") + `]}}`, "DEFER-GROUP-CONSISTENT"},
		// regexp.MustCompile panics before its statement completes: the reach
		// counter before the statement proves the site ran
		{"MUSTFAIL", `{"id":"MUSTFAIL",` + pos("m/m.go", "MustCompile", "regexp.MustCompile(") +
			`,"site_kind":"call","entry":"run","expect":{"status":"external","targets":[],"external":{"module":"std"}}}`,
			"CONSISTENT-NOEXEC"},
		{"ARGPANIC", `{"id":"ARGPANIC",` + pos("r/r.go", "report", "defer report(") + `,"site_kind":"defer","entry":"run",` +
			`"expect":{"status":"resolved","targets":[` + tgt("report", "r/r.go") + `]}}`, "UNVERIFIABLE"},
		// boom panics first: the reach counter before the statement vouches
		// for boom only, so ToUpper and keep never ran (NOT-RUN is the truth)
		{"BOOM", `{"id":"BOOM",` + pos("s/s.go", "boom", "keep(boom()") + `,"site_kind":"call","entry":"run",` +
			`"expect":{"status":"resolved","targets":[` + tgt("boom", "s/s.go") + `]}}`, "CONFIRMED-NOEXEC"},
		{"TOUPPER", `{"id":"TOUPPER",` + pos("s/s.go", "ToUpper", "keep(boom()") + `,"site_kind":"call","entry":"run",` +
			`"expect":{"status":"external","targets":[],"external":{"module":"std"}}}`, "NOT-RUN"},
		{"KEEP", `{"id":"KEEP",` + pos("s/s.go", "keep", "keep(boom()") + `,"site_kind":"call","entry":"run",` +
			`"expect":{"status":"resolved","targets":[` + tgt("keep", "s/s.go") + `]}}`, "NOT-RUN"},
	}
	var parts []string
	for _, c := range cases {
		parts = append(parts, c.json)
	}
	man := filepath.Join(work, "manifest.json")
	if err := os.WriteFile(man, []byte(`{"fixture":"review2","cases":[`+strings.Join(parts, ",")+`]}`), 0o644); err != nil {
		t.Fatal(err)
	}
	jout := filepath.Join(work, "judge.json")
	buf.Reset()
	cmdJudge([]string{"--obs", obs, "--manifest", man, "--ran", "run", "--out", jout}, &buf)
	data, err := os.ReadFile(jout)
	if err != nil {
		t.Fatal(err)
	}
	var res struct{ Cases []Verdict }
	if err := json.Unmarshal(data, &res); err != nil {
		t.Fatal(err)
	}
	if len(res.Cases) != len(cases) {
		t.Fatalf("%d verdicts for %d cases:\n%s", len(res.Cases), len(cases), buf.String())
	}
	for i, c := range cases {
		if v := res.Cases[i]; v.ID != c.id || v.Verdict != c.want {
			t.Errorf("%s: %s %s (%s), want %s", c.id, v.ID, v.Verdict, v.Detail, c.want)
		}
	}
}
