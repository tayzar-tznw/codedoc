package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// The integration tests build and run instrumented programs with the
// toolchain that runs the tests (GOROOT/bin/go). They are skipped with -short.

func goTool(t *testing.T) string {
	t.Helper()
	if testing.Short() {
		t.Skip("builds and runs instrumented programs")
	}
	if runtime.GOARCH != "amd64" || runtime.GOOS != "linux" {
		t.Skip("goracle is pinned to linux/amd64")
	}
	gobin := filepath.Join(runtime.GOROOT(), "bin", "go")
	if _, err := os.Stat(gobin); err != nil {
		t.Skip("no go binary:", err)
	}
	return gobin
}

func goEnv(extra ...string) []string {
	var env []string
	for _, kv := range os.Environ() {
		if k, _, _ := strings.Cut(kv, "="); k == "GOFLAGS" || k == "GOWORK" || k == "GOPROXY" || k == "GORACLE_OUT" ||
			k == "GORACLE_SLOW" || k == "GOOS" || k == "GOARCH" || k == "CGO_ENABLED" {
			continue
		}
		env = append(env, kv)
	}
	env = append(env, "GOFLAGS=-modcacherw", "GOWORK=off", "GOPROXY=off", "GOTOOLCHAIN=local", "GOOS=linux",
		"GOARCH=amd64", "CGO_ENABLED=0")
	return append(env, extra...)
}

func runGo(t *testing.T, gobin, dir string, env []string, args ...string) string {
	t.Helper()
	cmd := exec.Command(gobin, args...)
	cmd.Dir, cmd.Env = dir, env
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("go %s in %s: %v\n%s", strings.Join(args, " "), dir, err, out)
	}
	return string(out)
}

var oracleFlags = []string{"-gcflags=all=-l", "-ldflags=-checklinkname=0", "-buildvcs=false"}

// instrumentAndRun instruments testdata/<name>, runs the commands in the copy
// and reports; it returns the copy and the observations file.
func instrumentAndRun(t *testing.T, gobin, name string, slow bool, commands ...[]string) (string, string) {
	t.Helper()
	work := t.TempDir()
	copyDir := filepath.Join(work, "copy")
	if _, _, err := instrumentTree(filepath.Join("testdata", name), copyDir, instrumentOptions{mode: "all", tests: true, counters: true}); err != nil {
		t.Fatal(err)
	}
	hits := filepath.Join(work, "hits")
	if err := os.MkdirAll(hits, 0o755); err != nil {
		t.Fatal(err)
	}
	extra := []string{"GORACLE_OUT=" + hits}
	if slow {
		extra = append(extra, "GORACLE_SLOW=1")
	}
	for _, c := range commands {
		args := append([]string{c[0]}, oracleFlags...)
		if c[0] == "test" {
			args = append(args, "-count=1", "-vet=off")
		}
		runGo(t, gobin, copyDir, goEnv(extra...), append(args, c[1:]...)...)
	}
	tb := loadTable(sitesPathOf(copyDir))
	recs, st, _ := attribute(tb, hitFiles(hits))
	if st.nameBad != 0 {
		t.Errorf("runtime names differ from the prediction: %v", st.nameBadEx)
	}
	obs := filepath.Join(work, "obs.jsonl")
	writeJSONL(obs, append([]any{&ObsMeta{Kind: "meta", Sites: sitesPathOf(copyDir), Root: copyDir}}, recs...))
	return copyDir, obs
}

// pairs lists site -> callee pairs of an observations file.
func pairs(t *testing.T, obs string) []string {
	t.Helper()
	set := loadObs(obs)
	bySite, byGroup := set.bySite, set.byGroup
	var out []string
	for _, o := range bySite {
		for _, c := range o.Callees {
			out = append(out, o.File+":"+itoa(o.Line)+":"+itoa(o.Col)+" "+c.QName)
		}
	}
	for _, o := range byGroup {
		for _, c := range o.Callees {
			out = append(out, "group "+o.QName+" "+c.QName)
		}
	}
	sort.Strings(out)
	return out
}

func TestProbe3ReproducesResearch(t *testing.T) {
	gobin := goTool(t)
	commands := [][]string{{"run", "./cmd/demo"}, {"test", "./..."}}
	_, obs := instrumentAndRun(t, gobin, "probe3", false, commands...)
	var buf bytes.Buffer
	cmdMatch([]string{"--obs", obs, "--records", filepath.Join("testdata", "probe3-edges.jsonl")}, &buf)
	var got []string
	for _, line := range strings.Split(strings.TrimSpace(buf.String()), "\n") {
		if f := strings.Fields(line); len(f) >= 2 && f[0] != "summary:" {
			got = append(got, f[0]+" "+f[1])
		}
	}
	want, err := os.ReadFile(filepath.Join("testdata", "probe3-verdicts.txt"))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(got, "\n") != strings.TrimSpace(string(want)) {
		t.Errorf("verdicts:\n%s\nwant (research, single defer as CONFIRMED-NOEXEC):\n%s", strings.Join(got, "\n"), want)
	}
	// the research's frame-pointer fast path and the runtime.Callers slow
	// path must observe the same pairs
	_, slowObs := instrumentAndRun(t, gobin, "probe3", true, commands...)
	fast, slow := pairs(t, obs), pairs(t, slowObs)
	if strings.Join(fast, "\n") != strings.Join(slow, "\n") {
		t.Errorf("fast and slow paths differ:\nfast %v\nslow %v", fast, slow)
	}
	if len(fast) < 70 {
		t.Errorf("only %d pairs observed", len(fast))
	}
}

func TestConstructsJudge(t *testing.T) {
	gobin := goTool(t)
	_, obs := instrumentAndRun(t, gobin, "constructs", false, []string{"run", "./cmd/run"})
	var buf bytes.Buffer
	out := filepath.Join(t.TempDir(), "verdicts.json")
	cmdJudge([]string{"--obs", obs, "--manifest", filepath.Join("testdata", "constructs", "ground_truth", "manifest.json"),
		"--ran", "run", "--out", out}, &buf)
	data, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	var res struct{ Cases []Verdict }
	if err := json.Unmarshal(data, &res); err != nil {
		t.Fatal(err)
	}
	wantData, err := os.ReadFile(filepath.Join("testdata", "constructs", "ground_truth", "want.json"))
	if err != nil {
		t.Fatal(err)
	}
	var want map[string]string
	if err := json.Unmarshal(wantData, &want); err != nil {
		t.Fatal(err)
	}
	if len(res.Cases) != len(want) {
		t.Fatalf("%d verdicts for %d cases", len(res.Cases), len(want))
	}
	for _, v := range res.Cases {
		if want[v.ID] != v.Verdict {
			t.Errorf("%s: %s (%s), want %s", v.ID, v.Verdict, v.Detail, want[v.ID])
		}
	}
}

// A vendored module and a workspace build after plumbing, without -mod flags.
func TestPlumbedModulesBuild(t *testing.T) {
	gobin := goTool(t)
	src := t.TempDir()
	writeTree(t, src, map[string]string{
		"vend/go.mod":                        "module example.com/vend\n\ngo 1.26\n\nrequire example.com/dep v0.0.0\n\nreplace example.com/dep => ./dep\n",
		"vend/dep/go.mod":                    "module example.com/dep\n\ngo 1.26\n",
		"vend/dep/dep.go":                    "package dep\n\nfunc D() int { return 1 }\n",
		"vend/vendor/modules.txt":            "# example.com/dep v0.0.0 => ./dep\n## explicit; go 1.26\nexample.com/dep\n# example.com/dep => ./dep\n",
		"vend/vendor/example.com/dep/dep.go": "package dep\n\nfunc D() int { return 1 }\n",
		"vend/main.go":                       "package main\n\nimport \"example.com/dep\"\n\nfunc main() { println(dep.D()) }\n",
		"ws/go.work":                         "go 1.26\n\nuse (\n\t./a\n\t./b\n)\n",
		"ws/a/go.mod":                        "module example.com/a\n\ngo 1.26\n",
		"ws/a/a.go":                          "package a\n\nfunc A() int { return 2 }\n",
		"ws/b/go.mod":                        "module example.com/b\n\ngo 1.26\n",
		"ws/b/main.go":                       "package main\n\nimport \"example.com/a\"\n\nfunc main() { println(a.A()) }\n",
	})
	copyDir := filepath.Join(t.TempDir(), "copy")
	if _, _, err := instrumentTree(src, copyDir, instrumentOptions{mode: "all", tests: true, counters: true}); err != nil {
		t.Fatal(err)
	}
	hits := t.TempDir()
	env := goEnv("GORACLE_OUT=" + hits)
	runGo(t, gobin, filepath.Join(copyDir, "vend"), env, append(append([]string{"run"}, oracleFlags...), ".")...)
	wsEnv := append([]string{}, env...)
	for i, kv := range wsEnv {
		if kv == "GOWORK=off" {
			wsEnv[i] = "GOWORK=" + filepath.Join(copyDir, "ws", "go.work")
		}
	}
	runGo(t, gobin, filepath.Join(copyDir, "ws", "b"), wsEnv, append(append([]string{"run"}, oracleFlags...), ".")...)
	files := hitFiles(hits)
	if len(files) != 2 {
		t.Fatalf("hit files %v", files)
	}
	calls := 0
	for _, f := range files {
		fh, err := os.Open(f)
		if err != nil {
			t.Fatal(err)
		}
		sc := bufio.NewScanner(fh)
		for sc.Scan() {
			if strings.Contains(sc.Text(), `"kind":"call"`) {
				calls++
			}
		}
		fh.Close()
	}
	// vend: main only (the vendored copy of dep is what vendor mode compiles,
	// and vendor directories are never instrumented); ws: main and A
	if calls != 3 {
		t.Errorf("%d call records, want 3", calls)
	}
	if _, err := os.Stat(filepath.Join(copyDir, "ws", "go.work.sum")); err == nil {
		t.Errorf("go.work.sum written: the plumbing needed a download")
	}
}

// TestR50InterfaceCandidates runs review-b5b6's probes p6 (an edge to the
// wrong interface method), p9b (a generic implementer, which spec §5.4 never
// offers as a candidate) and p9c (a second dispatch through an embedded
// interface field) on a real program.
func TestR50InterfaceCandidates(t *testing.T) {
	gobin := goTool(t)
	work := t.TempDir()
	src := filepath.Join(work, "src")
	lib := `package tiny

import "io"

type Shape interface{ Area() int }

type Plot interface {
	Area() int
	Name() string
}

type Sq struct{}

func (Sq) Area() int { return 4 }

func Use(s Shape) int { return s.Area() }

type Sizer interface{ Volume() int8 }

type Plain struct{}

func (Plain) Volume() int8 { return 1 }

type Box[T any] struct{ v T }

func (Box[T]) Volume() int8 { return 2 }

func Measure(s Sizer) int8 { return s.Volume() }

type I interface {
	M() int
	N() int
}

type J interface{ M() int }

type W struct{ J }

func (W) N() int { return 0 }

type X struct{}

func (X) M() int { return 7 }

func Call(i I) int { return i.M() }

type Sink interface {
	Write(p []byte) (int, error)
	Flush() int8
}

type WW struct{ io.Writer }

func (WW) Flush() int8 { return 0 }

type XW struct{}

func (XW) Write(p []byte) (int, error) { return len(p), nil }

type DW struct{}

func (DW) Write(p []byte) (int, error) { return len(p), nil }

func (DW) Flush() int8 { return 0 }

func Emit(s Sink) int {
	n, _ := s.Write([]byte("x"))
	return n
}

func EmitDirect(s Sink) int {
	n, _ := s.Write(nil)
	return n
}

type Lener interface{ Len() int }

type Two struct{}

func (Two) Len() int { return 2 }

func Size(l Lener) int { return l.Len() }

func SizeExt(l Lener) int { return l.Len() }
`
	writeTree(t, src, map[string]string{
		"go.mod":      "module example.com/tiny\n\ngo 1.26\n",
		"lib.go":      lib,
		"lib_test.go": "package tiny\n\nimport (\n\t\"strings\"\n\t\"testing\"\n)\n\nfunc TestUse(t *testing.T) {\n\tif Use(Sq{}) != 4 || Use(&Sq{}) != 4 || Measure(Box[int]{}) != 2 || Call(W{J: X{}}) != 7 ||\n\t\tEmit(WW{Writer: XW{}}) != 1 || EmitDirect(DW{}) != 0 ||\n\t\tSize(&strings.Builder{}) != 0 || Size(Two{}) != 2 || SizeExt(&strings.Builder{}) != 0 {\n\t\tt.Fatal(\"bad\")\n\t}\n}\n",
	})
	copyDir := filepath.Join(work, "copy")
	if _, _, err := instrumentTree(src, copyDir, instrumentOptions{mode: "all", tests: true, counters: true}); err != nil {
		t.Fatal(err)
	}
	hits := filepath.Join(work, "hits")
	if err := os.MkdirAll(hits, 0o755); err != nil {
		t.Fatal(err)
	}
	runGo(t, gobin, copyDir, goEnv("GORACLE_OUT="+hits), append(append([]string{"test"}, oracleFlags...), "-count=1", "-vet=off", ".")...)
	tb := loadTable(sitesPathOf(copyDir))
	recs, _, _ := attribute(tb, hitFiles(hits))
	obs := filepath.Join(work, "obs.jsonl")
	writeJSONL(obs, append([]any{&ObsMeta{Kind: "meta", Sites: sitesPathOf(copyDir), Root: copyDir}}, recs...))
	lines := strings.Split(lib, "\n")
	pos := func(prefix, token string) (int, int) {
		for i, l := range lines {
			if strings.HasPrefix(l, prefix) {
				return i + 1, strings.Index(l, token) + 1
			}
		}
		t.Fatalf("no line %q", prefix)
		return 0, 0
	}
	decl := func(prefix, name string) string {
		l, c := pos(prefix, name)
		return `{"file":"lib.go","line":` + itoa(l) + `,"col":` + itoa(c) + `}`
	}
	site := func(prefix, token string) string {
		l, c := pos(prefix, token)
		return `"file":"lib.go","line":` + itoa(l) + `,"col":` + itoa(c) + `,"name":"` + strings.TrimSuffix(token, "()") + `"`
	}
	useSite, measureSite, callSite := site("func Use", "Area()"), site("func Measure", "Volume()"), site("func Call", "M()")
	emitSite, directSite := site("\tn, _ := s.Write([]byte", "Write"), site("\tn, _ := s.Write(nil)", "Write")
	sinkWrite := decl("\tWrite(p []byte) (int, error)", "Write")
	sizeSite, sizeExtSite := site("func Size(", "Len()"), site("func SizeExt(", "Len()")
	lenerLen, twoLen := decl("type Lener", "Len()"), decl("func (Two) Len", "Len")
	shapeArea, plotArea := decl("type Shape", "Area"), decl("\tArea() int", "Area")
	sqArea, plainVolume := decl("func (Sq) Area", "Area"), decl("func (Plain) Volume", "Volume")
	sizerVolume := decl("type Sizer", "Volume")
	iM, jM := decl("\tM() int", "M"), decl("type J", "M")
	absSq := strings.Replace(sqArea, `"lib.go"`, strconv.Quote(filepath.Join(copyDir, "lib.go")), 1)
	edges := []struct{ rec, want string }{
		{`{` + useSite + `,"target":` + shapeArea + `,"candidates":[` + sqArea + `]}`, "IFACE-CONSISTENT"},
		// p6: the planted edge to Plot.Area, with the helper's (empty) implementer set of Plot
		{`{` + useSite + `,"target":` + plotArea + `,"candidates":[]}`, "IFACE-OTHER"},
		// without a candidates key only the method name is checked
		{`{` + useSite + `,"target":` + plotArea + `}`, "IFACE-CONSISTENT"},
		// p9b: Box[T] is generic, never a candidate; the name decides
		{`{` + measureSite + `,"target":` + sizerVolume + `,"candidates":[` + plainVolume + `]}`, "IFACE-CONSISTENT"},
		// p9c: W reaches X.M through its embedded J (the candidate is J.M, flagged)
		{`{` + callSite + `,"target":` + iM + `,"candidates":[` + strings.Replace(jM, "}", `,"via_embedded_iface":true}`, 1) + `]}`, "IFACE-CONSISTENT"},
		{`{` + callSite + `,"target":` + iM + `,"candidates":[` + jM + `]}`, "IFACE-CONSISTENT"},
		{`{` + callSite + `,"target":` + iM + `,"candidates":[` + sqArea + `]}`, "IFACE-OTHER"},
		{`{` + useSite + `,"target":` + shapeArea + `,"candidates":[` + absSq + `]}`, "IFACE-CONSISTENT"},
		// p9d: WW gets Write from its embedded io.Writer (declared outside the
		// repo), so XW.Write, which does not implement Sink, runs. The
		// compiler's wrapper (*WW).Write jumps to it (a tail call), so no
		// frame shows the second dispatch: the resolver's via_embedded_iface
		// candidate must say it, also without a position ...
		{`{` + emitSite + `,"target":` + sinkWrite + `,"candidates":[{"via_embedded_iface":true}]}`, "IFACE-CONSISTENT"},
		// ... because an empty list means no implementers at all
		{`{` + emitSite + `,"target":` + sinkWrite + `,"candidates":[]}`, "IFACE-OTHER"},
		// a direct dispatch to a type outside the candidates is wrong
		{`{` + directSite + `,"target":` + sinkWrite + `,"candidates":[]}`, "IFACE-OTHER"},
		// open world: *strings.Builder (outside the repo, uninstrumented) also
		// runs at Size; it is always allowed, while an in-repo callee must be
		// a candidate, an empty set included
		{`{` + sizeSite + `,"target":` + lenerLen + `,"candidates":[` + twoLen + `]}`, "IFACE-CONSISTENT"},
		{`{` + sizeSite + `,"target":` + lenerLen + `,"candidates":[]}`, "IFACE-OTHER"},
		{`{` + sizeExtSite + `,"target":` + lenerLen + `,"candidates":[]}`, "UNEXERCISED"},
	}
	var recLines []string
	for _, e := range edges {
		recLines = append(recLines, e.rec)
	}
	recPath := writeEdges(t, work, recLines)
	out := filepath.Join(work, "verdicts.jsonl")
	var buf bytes.Buffer
	cmdMatch([]string{"--obs", obs, "--records", recPath, "--out", out}, &buf)
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
	if !strings.Contains(got[3].Detail, "generic implementer") {
		t.Errorf("the generic exemption is not reported: %s", got[3].Detail)
	}
}
