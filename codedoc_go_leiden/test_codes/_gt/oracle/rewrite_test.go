package main

import (
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"
)

func writeTree(t *testing.T, root string, files map[string]string) {
	t.Helper()
	for rel, text := range files {
		path := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

// instrumentFiles writes a module and rewrites it in place.
func instrumentFiles(t *testing.T, files map[string]string) (*instrumenter, string) {
	t.Helper()
	root := t.TempDir()
	writeTree(t, root, files)
	in := newInstrumenter(root, "all", nil)
	in.run(nil)
	return in, root
}

func read(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func funcBy(in *instrumenter, file, qual string) *FuncRec {
	for _, f := range in.funcs {
		if f.File == file && f.Qualname == qual {
			return f
		}
	}
	return nil
}

const qualSrc = `package p

var Hook = func() int { return 1 }

var pair = []func() int{func() int { return 2 }, nil}

type T struct{}

func (T) Val() int { return 1 }

func (t *T) Ptr() int {
	f := func() int {
		g := func() int { return 3 }
		return g()
	}
	return f()
}

type G[K comparable, V any] struct{ m map[K]V }

func (g *G[K, V]) Get(k K) V { return g.m[k] }

func Gen[X any](x X) X { return x }

func init() { _ = Hook() }

func init() { _ = pair[0]() }

func _() {}

type I interface {
	M() int
	fmt.Stringer
	N(x int) (int, error)
}
`

func TestQualifiedAndRuntimeNames(t *testing.T) {
	in, _ := instrumentFiles(t, map[string]string{"go.mod": "module example.com/m\n\ngo 1.26\n", "p/p.go": qualSrc})
	want := map[string]string{ // C3 qualname -> runtime name
		"<init>.func1":      "example.com/m/p.init.func1",
		"<init>.func2":      "example.com/m/p.init.func2",
		"T.Val":             "example.com/m/p.T.Val",
		"T.Ptr":             "example.com/m/p.(*T).Ptr",
		"T.Ptr.func1":       "example.com/m/p.(*T).Ptr.func1",
		"T.Ptr.func1.func1": "example.com/m/p.(*T).Ptr.func1.1",
		"G.Get":             "example.com/m/p.(*G[...]).Get",
		"Gen":               "example.com/m/p.Gen[...]",
		"init#1":            "example.com/m/p.init.0",
		"init#2":            "example.com/m/p.init.1",
		"_#1":               "example.com/m/p._",
	}
	for qual, rt := range want {
		f := funcBy(in, "p/p.go", qual)
		if f == nil {
			t.Errorf("no function %s", qual)
			continue
		}
		if f.RTName != rt {
			t.Errorf("%s: runtime name %s, want %s", qual, f.RTName, rt)
		}
	}
	if f := funcBy(in, "p/p.go", "T.Ptr"); f.Recv != "T" || !f.Ptr || f.Name != "Ptr" || f.Line != 11 || f.Col != 13 {
		t.Errorf("T.Ptr record %+v", f)
	}
	if f := funcBy(in, "p/p.go", "T.Ptr.func1"); !f.Lit || f.Line != 12 || f.Col != 7 || f.Recv != "" {
		t.Errorf("closure record %+v", f)
	}
	var ims []string
	for _, m := range in.imethods {
		ims = append(ims, m.Qualname)
	}
	if strings.Join(ims, ",") != "I.M,I.N" {
		t.Errorf("interface methods %v", ims)
	}
}

func TestMainAndExternalTestPackageNames(t *testing.T) {
	in, _ := instrumentFiles(t, map[string]string{
		"go.mod":         "module example.com/m\n\ngo 1.26\n",
		"cmd/x/main.go":  "package main\n\nfunc main() { helper() }\n\nfunc helper() {}\n",
		"lib/a.go":       "package lib\n\nfunc A() {}\n",
		"lib/a_test.go":  "package lib_test\n\nimport \"testing\"\n\nfunc TestA(t *testing.T) { h() }\n\nfunc h() {}\n",
		"lib/in_test.go": "package lib\n\nfunc init() {}\n",
	})
	if f := funcBy(in, "cmd/x/main.go", "helper"); f.RTName != "main.helper" || f.Pkg != "example.com/m/cmd/x" {
		t.Errorf("main package names %+v", f)
	}
	if f := funcBy(in, "lib/a_test.go", "h"); f.RTName != "example.com/m/lib_test.h" || f.Pkg != "example.com/m/lib_test" {
		t.Errorf("external test package names %+v", f)
	}
	if f := funcBy(in, "lib/in_test.go", "init#1"); f.RTName != "example.com/m/lib.init.0" {
		t.Errorf("in-package test init %+v", f)
	}
}

const siteSrc = `package p

import "fmt"

func a() int { return 1 }
func b(x int) int { return x }

func Line() int { return b(a()) + b(2) }

func Stmts(xs []int, f func(func(int) bool)) {
	defer fmt.Println("done")
	go b(a())
	for x := range f {
		_ = b(x)
	}
	_ = []byte("x")
	_ = (*int)(nil)
	if a() > 0 && b(1) > 0 {
		panic(b(2))
	} else if b(3) > 0 {
		return
	}
	switch b(4) {
	}
	for i := a(); i < b(5); i++ {
	}
	return
}
`

func TestSitesTagsAndCounters(t *testing.T) {
	in, root := instrumentFiles(t, map[string]string{"go.mod": "module example.com/m\n\ngo 1.26\n", "p/p.go": siteSrc})
	out := read(t, filepath.Join(root, "p", "p.go"))
	if got, want := strings.Count(out, "\n"), strings.Count(siteSrc, "\n"); got != want {
		t.Fatalf("line count %d, want %d", got, want)
	}
	fset := token.NewFileSet()
	if _, err := parser.ParseFile(fset, "p.go", out, 0); err != nil {
		t.Fatalf("rewritten file does not parse: %v\n%s", err, out)
	}
	byKey := map[string]*SiteRec{}
	for _, s := range in.sites {
		byKey[s.Name+"@"+itoa(s.Line)+":"+itoa(s.Col)] = s
	}
	// three calls on one line: each its own tagged site
	for _, k := range []string{"b@8:26", "a@8:28", "b@8:35"} {
		if s := byKey[k]; s == nil || !s.Tagged || s.Stmt != "call" {
			t.Errorf("site %s: %+v", k, s)
		}
	}
	if s := byKey["Println@11:12"]; s == nil || s.Stmt != "defer" {
		t.Errorf("defer site %+v", s)
	}
	if s := byKey["b@12:5"]; s == nil || s.Stmt != "go" || s.GoLine != 12 {
		t.Errorf("go site %+v", s)
	}
	r := byKey["f@13:17"]
	if r == nil || r.Stmt != "range" || r.LLine != 13 || r.LCol != 2 || !r.Tagged {
		t.Fatalf("range site %+v", r)
	}
	if !strings.Contains(out, "/*line "+filepath.Join(root, "p", "p.go")+"#"+itoa(r.Sid)+":13:2*/for/*line ") {
		t.Errorf("range tag not around the for keyword:\n%s", out)
	}
	// every call expression is a site, as in extract.Sites: []byte(...) is
	// certainly a conversion, (*int)(nil) could be a call through a func
	// pointer; neither has a name, and the token is the call's "("
	if s := byKey["@16:12"]; s == nil || s.Form != "conversion" || s.LCol != 12 {
		t.Errorf(`[]byte("x") site %+v`, s)
	}
	if s := byKey["@17:12"]; s == nil || s.Form != "paren" || s.LCol != 12 {
		t.Errorf("(*int)(nil) site %+v", s)
	}
	for k, s := range byKey {
		if strings.HasPrefix(k, "@") && k != "@16:12" && k != "@17:12" {
			t.Errorf("unexpected site without a name: %s %+v", k, s)
		}
	}
	// counters: completion after the simple statement, reach before return,
	// if, switch, for, go, defer and panic; none for the && right operand
	stmtOf, reachOf := map[int]string{}, map[int]string{}
	for _, c := range in.stmts {
		for _, sid := range c.Sites {
			if c.Kind == "stmt" {
				stmtOf[sid] = "x"
			} else {
				reachOf[sid] = "x"
			}
		}
	}
	check := func(k string, completion, reach bool) {
		s := byKey[k]
		if s == nil {
			t.Errorf("no site %s", k)
			return
		}
		if _, ok := stmtOf[s.Sid]; ok != completion {
			t.Errorf("%s completion counter %v, want %v", k, ok, completion)
		}
		if _, ok := reachOf[s.Sid]; ok != reach {
			t.Errorf("%s reach counter %v, want %v", k, ok, reach)
		}
	}
	check("b@8:26", false, true)        // return
	check("b@14:7", true, false)        // assignment in the loop body
	check("Println@11:12", false, true) // defer
	check("b@12:5", false, true)        // go
	check("a@12:7", false, true)        // go argument
	check("f@13:17", false, true)       // range site
	check("a@18:5", false, true)        // if condition
	check("b@18:16", false, false)      // right operand of &&
	check("panic@19:3", false, true)    // panic gets a reach counter, never a completion counter
	check("b@20:12", false, false)      // else-if condition: no statement list to put a counter in
	check("b@23:9", false, true)        // switch tag
	check("a@25:11", false, true)       // for init
	check("b@25:20", false, true)       // for condition
	if strings.Contains(out, "panic(b(2)); __goracle.D") {
		t.Errorf("counter after panic:\n%s", out)
	}
	if f := funcBy(in, "p/p.go", "Stmts"); !f.HasDefer || len(f.Defers) != 1 || len(f.ExitLines) != 3 {
		t.Errorf("defer bookkeeping %+v", f)
	}
}

func itoa(n int) string { return strconv.Itoa(n) }

// Every callee shape takes the token extract.Sites gives it (master plan C2,
// helper-extract ruling): the identifier through parentheses, the index
// base, the func keyword, else the call's "(" with no name.
func TestSiteTokensFollowExtract(t *testing.T) {
	src := `package p

type T struct{ f func() }

func g() func() { return nil }

func Gen[A any](yield func(A) bool) {}

func Shapes(m map[string]func(), fs [][]func(), x any, fp *func(), ch chan func(), t T) {
	m["k"]()
	fs[0][1]()
	x.(func())()
	(*fp)()
	(<-ch)()
	g()()
	(g())()
	(t.f)()
	func() {}()
	(func() {})()
	_ = []byte("s")
	_ = (*[4]byte)(nil)
	_ = map[string]int(nil)
	Gen[int](func(int) bool { return true })
	for range Gen[int] {
	}
	for range (Gen[string]) {
	}
	for range func(yield func(int) bool) {} {
	}
}
`
	in, root := instrumentFiles(t, map[string]string{"go.mod": "module example.com/m\n\ngo 1.26\n", "p/p.go": src})
	if _, err := parser.ParseFile(token.NewFileSet(), "p.go", read(t, filepath.Join(root, "p", "p.go")), 0); err != nil {
		t.Fatalf("rewritten file does not parse: %v", err)
	}
	sites := append([]*SiteRec(nil), in.sites...)
	sort.Slice(sites, func(i, j int) bool {
		return sites[i].Line < sites[j].Line || sites[i].Line == sites[j].Line && sites[i].Col < sites[j].Col
	})
	var got []string
	for _, s := range sites {
		name := s.Name
		if name == "" {
			name = "-"
		}
		got = append(got, itoa(s.Line)+":"+itoa(s.Col)+" "+name+" "+s.Form+" "+s.Stmt)
		if s.Name == "" && s.Form != "range" && (s.Line != s.LLine || s.Col != s.LCol) {
			t.Errorf("site without a name not at its \"(\": %+v", s)
		}
	}
	want := []string{
		"10:2 m index call", "11:10 - other call", "12:12 - other call", "13:7 - paren call", "14:8 - paren call",
		"15:2 g ident call", "15:5 - callresult call", "16:3 g ident call", "16:7 - callresult call",
		"17:5 f selector call", "18:2 func funclit call", "19:3 func funclit call",
		"20:12 - conversion call", "21:16 - conversion call", "22:20 - conversion call", "23:2 Gen index call",
		"24:12 Gen range range", "26:13 Gen range range", "28:12 func range range",
	}
	if strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Errorf("sites:\n%s\nwant:\n%s", strings.Join(got, "\n"), strings.Join(want, "\n"))
	}
}

func TestHooksBodylessAndNosplit(t *testing.T) {
	src := "package p\n\nfunc asm() int\n\n//go:nosplit\nfunc fast() int { return 1 }\n\nfunc normal() { _ = fast() }\n"
	in, root := instrumentFiles(t, map[string]string{"go.mod": "module example.com/m\n\ngo 1.26\n", "p/p.go": src})
	if f := funcBy(in, "p/p.go", "asm"); f == nil || f.Hooked {
		t.Errorf("body-less declaration %+v", f)
	}
	if f := funcBy(in, "p/p.go", "fast"); f == nil || f.Hooked {
		t.Errorf("nosplit function %+v", f)
	}
	n := funcBy(in, "p/p.go", "normal")
	out := read(t, filepath.Join(root, "p", "p.go"))
	if !n.Hooked || !strings.Contains(out, "func normal() {__goracle.E("+itoa(n.Fid)+");") {
		t.Errorf("hook missing:\n%s", out)
	}
	if !strings.Contains(out, "package p; import __goracle \"codedoc.local/goracle/rt\"") {
		t.Errorf("import missing:\n%s", out)
	}
}

func TestSkippedFiles(t *testing.T) {
	in, root := instrumentFiles(t, map[string]string{
		"go.mod":          "module example.com/m\n\ngo 1.26\n",
		"p/cgo.go":        "package p\n\nimport \"C\"\n\nfunc c() {}\n",
		"p/line.go":       "package p\n\n//line other.go:10\nfunc l() {}\n",
		"p/bad.go":        "package p\n\nfunc (\n",
		"p/a_win.go":      "//go:build windows\n\npackage p\n\nfunc init() {}\n",
		"p/ok.go":         "package p\n\nfunc init() {}\n",
		"p/testdata/t.go": "package t\n\nfunc t() {}\n",
	})
	_ = root
	status := map[string]string{}
	build := map[string]bool{}
	for _, f := range in.files {
		status[f.File] = f.Status
		build[f.File] = f.Build
	}
	want := map[string]string{"p/cgo.go": "skipped:cgo", "p/line.go": "skipped:line-directives", "p/bad.go": "skipped:parse",
		"p/a_win.go": "instrumented", "p/ok.go": "instrumented"}
	for f, s := range want {
		if status[f] != s {
			t.Errorf("%s: status %q, want %q", f, status[f], s)
		}
	}
	if _, ok := status["p/testdata/t.go"]; ok {
		t.Errorf("testdata scanned")
	}
	if build["p/a_win.go"] || !build["p/ok.go"] {
		t.Errorf("build flags %v", build)
	}
	// a_win.go sorts first but is not compiled: it must not shift init.N of ok.go
	if f := funcBy(in, "p/ok.go", "init#1"); f.RTName != "example.com/m/p.init.0" {
		t.Errorf("init numbering %+v", f)
	}
	in2 := newInstrumenter(t.TempDir(), "all", nil)
	writeTree(t, in2.root, map[string]string{"x/x.go": "package x\n\nfunc X() {}\n"})
	in2.run(nil)
	if len(in2.files) != 1 || in2.files[0].Status != "skipped:no-module" {
		t.Errorf("module-less file %+v", in2.files)
	}
}

func TestCopyTreeSymlinks(t *testing.T) {
	src := t.TempDir()
	writeTree(t, src, map[string]string{"a/real.go": "package a\n", "b/keep.txt": "x"})
	if err := os.Symlink("../a/real.go", filepath.Join(src, "b", "link.go")); err != nil {
		t.Skip("no symlinks:", err)
	}
	if err := os.Symlink("a", filepath.Join(src, "dirlink")); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(src, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}
	dst := filepath.Join(t.TempDir(), "copy")
	real := map[string]string{}
	if err := copyTree(src, dst, real); err != nil {
		t.Fatal(err)
	}
	if fi, err := os.Lstat(filepath.Join(dst, "b", "link.go")); err != nil || !fi.Mode().IsRegular() {
		t.Errorf("file symlink not materialized: %v %v", fi, err)
	}
	if real["b/link.go"] != "a/real.go" {
		t.Errorf("real map %v", real)
	}
	if fi, err := os.Lstat(filepath.Join(dst, "dirlink")); err != nil || fi.Mode()&os.ModeSymlink == 0 {
		t.Errorf("dir symlink not kept: %v %v", fi, err)
	}
	if _, err := os.Stat(filepath.Join(dst, ".git")); err == nil {
		t.Errorf(".git copied")
	}
}

func TestPlumbRequireAndVendor(t *testing.T) {
	root := t.TempDir()
	writeTree(t, root, map[string]string{
		"m/go.mod":             "module example.com/m\n\ngo 1.26",
		"m/vendor/modules.txt": "# example.com/dep v0.0.0 => ./dep\n## explicit; go 1.26\nexample.com/dep\n",
		"m/p.go":               "package m\n\nfunc F() {}\n",
	})
	in := newInstrumenter(root, "all", nil)
	in.run(nil)
	if err := in.plumb(); err != nil {
		t.Fatal(err)
	}
	if err := in.plumb(); err != nil { // idempotent
		t.Fatal(err)
	}
	rtDir := filepath.Join(root, rtDirName)
	gomod := read(t, filepath.Join(root, "m", "go.mod"))
	if strings.Count(gomod, "require "+rtModule+" v0.0.0") != 1 || !strings.Contains(gomod, "replace "+rtModule+" => "+rtDir) {
		t.Errorf("go.mod:\n%s", gomod)
	}
	mt := read(t, filepath.Join(root, "m", "vendor", "modules.txt"))
	wantMT := "# " + rtModule + " v0.0.0 => " + rtDir + "\n## explicit; go " + rtGoVersion + "\n" + rtModule + "\n# " + rtModule + " => " + rtDir + "\n"
	if !strings.HasSuffix(mt, wantMT) || strings.Count(mt, rtModule+"\n") != 1 {
		t.Errorf("modules.txt:\n%s", mt)
	}
	vrt := filepath.Join(root, "m", "vendor", "codedoc.local", "goracle", "rt")
	if read(t, filepath.Join(vrt, "rt.go")) != string(rtSource) || read(t, filepath.Join(vrt, "exit.go")) != string(rtExitSource) {
		t.Errorf("vendored rt differs")
	}
	if read(t, filepath.Join(rtDir, "exit.go")) != string(rtExitSource) {
		t.Errorf("rt exit.go missing")
	}
	if !regexp.MustCompile(`^module codedoc.local/goracle/rt\n\ngo 1.21\n$`).MatchString(read(t, filepath.Join(rtDir, "go.mod"))) {
		t.Errorf("rt go.mod")
	}
}

func TestReachCounterAndLabels(t *testing.T) {
	src := "package p\n\nfunc f() int { return 1 }\n\nfunc G(n int) int {\n\tif n > 0 {\n\t\tgoto L\n\t}\nM:\n\tfor i := 0; i < f(); i++ {\n\t\tcontinue M\n\t}\nL:\n\treturn f()\n}\n"
	_, root := instrumentFiles(t, map[string]string{"go.mod": "module example.com/m\n\ngo 1.26\n", "p/p.go": src})
	out := read(t, filepath.Join(root, "p", "p.go"))
	if !regexp.MustCompile(`(?m)^__goracle\.D\(\d+\); M:\n\tfor `).MatchString(out) {
		t.Errorf("a loop label must stay on the loop:\n%s", out)
	}
	if !regexp.MustCompile(`(?m)^L:\n\t__goracle\.D\(\d+\); return f`).MatchString(out) {
		t.Errorf("a goto label must run the counter:\n%s", out)
	}
	if _, err := parser.ParseFile(token.NewFileSet(), "p.go", out, 0); err != nil {
		t.Fatal(err)
	}
}

func TestFileLookupThroughSymlinkedPath(t *testing.T) {
	dir := t.TempDir()
	real := filepath.Join(dir, "real")
	if err := os.MkdirAll(real, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(real, "a.go"), []byte("package a\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(real, filepath.Join(dir, "link")); err != nil {
		t.Skip("no symlinks:", err)
	}
	tb := &table{files: map[string]*FileRec{filepath.Join(real, "a.go"): {File: "a.go", Abs: filepath.Join(real, "a.go")}}}
	if f := tb.file(filepath.Join(dir, "link", "a.go")); f == nil || f.File != "a.go" {
		t.Errorf("lookup through the symlink: %+v", f)
	}
	if tb.file(filepath.Join(dir, "nope.go")) != nil {
		t.Errorf("missing file found")
	}
}

func TestStarCallsUnsafeArgsAndHooklessFiles(t *testing.T) {
	in, root := instrumentFiles(t, map[string]string{
		"go.mod":    "module example.com/m\n\ngo 1.26\n",
		"p/p.go":    "package p\n\nimport \"unsafe\"\n\nvar fp = new(func() int)\n\nfunc mk() int { return 1 }\n\nfunc F() uintptr {\n\t_ = (*fp)()\n\treturn unsafe.Sizeof(mk())\n}\n",
		"p/vars.go": "package p\n\nvar Made = mk()\n",
	})
	var star, sizeof, mkInSizeof, made *SiteRec
	for _, s := range in.sites {
		switch {
		case s.File == "p/p.go" && s.Line == 10:
			star = s
		case s.Name == "Sizeof":
			sizeof = s
		case s.Name == "mk" && s.Line == 11:
			mkInSizeof = s
		case s.File == "p/vars.go":
			made = s
		}
	}
	if star == nil || star.Form != "paren" || !star.Tagged || star.Col != star.LCol {
		t.Errorf("(*fp)() must be a tagged site at its \"(\": %+v", star)
	}
	if sizeof == nil || mkInSizeof == nil {
		t.Errorf("unsafe.Sizeof %+v, call in its argument %+v", sizeof, mkInSizeof)
	}
	out := read(t, filepath.Join(root, "p", "vars.go"))
	if made == nil || !made.Tagged || !strings.Contains(out, "mk/*line ") || strings.Contains(out, "import __goracle") {
		t.Errorf("a file without function bodies keeps its tags and needs no import:\n%s", out)
	}
	for _, c := range in.stmts {
		for _, sid := range c.Sites {
			if mkInSizeof != nil && sid == mkInSizeof.Sid {
				t.Errorf("counter covers an unevaluated argument")
			}
		}
	}
}

func TestIgnoreDirectories(t *testing.T) {
	if got := ignoreLines("module m\n\nignore ./gen // generated\nignore(\n\tstatic\n\t\"content/html\"\n)\nignore\tvendor2\n"); strings.Join(got, ",") != "./gen,static,content/html,vendor2" {
		t.Errorf("ignore lines %v", got)
	}
	in, _ := instrumentFiles(t, map[string]string{
		"go.mod":                "module example.com/m\n\ngo 1.26\n\nignore (\n\t./generated\n\tstatic\n)\n",
		"main.go":               "package main\n\nfunc main() {}\n",
		"generated/g.go":        "package generated\n\nfunc G() {}\n",
		"a/generated/keep.go":   "package generated\n\nfunc K() {}\n",
		"a/b/static/s.go":       "package static\n\nfunc S() {}\n",
		"static/t.go":           "package static\n\nfunc T() {}\n",
		"sub/go.mod":            "module example.com/sub\n\ngo 1.26\n",
		"sub/static/own.go":     "package static\n\nfunc O() {}\n",
		"staticx/notignored.go": "package staticx\n\nfunc N() {}\n",
	})
	files := map[string]bool{}
	for _, f := range in.files {
		files[f.File] = true
	}
	for _, f := range []string{"main.go", "a/generated/keep.go", "sub/static/own.go", "staticx/notignored.go"} {
		if !files[f] {
			t.Errorf("%s not instrumented", f)
		}
	}
	for _, f := range []string{"generated/g.go", "a/b/static/s.go", "static/t.go"} {
		if files[f] {
			t.Errorf("%s lies in an ignore directory but was instrumented", f)
		}
	}
	if len(in.ignored) != 3 {
		t.Errorf("ignored dirs %v", in.ignored)
	}
}
