package resolve

import (
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

var (
	clsOnce sync.Once
	clsRes  *result
	clsDir  string
)

// cls loads the stdlib-only fixture module once (single-module group,
// GOWORK=off, empty module cache, GOPROXY=off).
func cls(t *testing.T) *result {
	t.Helper()
	clsOnce.Do(func() {
		clsDir = fixture(t, "cls")
		clsRes = resolveRun(t, &Options{RepoRoot: clsDir, Dir: clsDir, Pattern: "./...", GoWork: "off",
			MainModules: []string{"example.com/cls"}}, goEnv(t, nil))
	})
	if clsRes == nil {
		t.Fatal("cls fixture failed to load")
	}
	return clsRes
}

func TestClsLoadsWithoutErrors(t *testing.T) {
	r := cls(t)
	for _, e := range r.errors {
		t.Errorf("unexpected error record: %+v", e)
	}
	if r.summary.Packages != 10 {
		t.Errorf("summary.packages = %d, want 10", r.summary.Packages)
	}
}

func TestStaticCalls(t *testing.T) {
	r := cls(t)
	const f = "static/static.go"
	cases := []struct {
		snippet, tok, class, shape string
		declSnippet, recv          string
		depth                      int
	}{
		{"_ = Plain()", "Plain", "func", "ident", "func Plain()", "", 0},
		{"_ = (Plain)()", "Plain", "func", "ident", "func Plain()", "", 0},
		{"_ = c.Value()", "Value", "method", "selector", "func (c Counter) Value()", "Counter", 0},
		{"c.Inc()", "Inc", "method", "selector", "func (c *Counter) Inc()", "*Counter", 0},
		{"_ = p.Value()", "Value", "method", "selector", "func (c Counter) Value()", "Counter", 0},
		{"_ = t.Hello()", "Hello", "method", "selector", "func (Base) Hello()", "Base", 2},
		{"_ = t.Shadowed()", "Shadowed", "method", "selector", "func (Mid) Shadowed()", "Mid", 1},
		{"_ = m.Hello()", "Hello", "method", "selector", "func (Base) Hello()", "Base", 1},
		{"_ = Counter.Value(c)", "Value", "method", "selector", "func (c Counter) Value()", "Counter", 0},
		{"(*Counter).Inc(p)", "Inc", "method", "selector", "func (c *Counter) Inc()", "*Counter", 0},
		{"_ = Identity(3)", "Identity", "func", "ident", "func Identity[T any]", "", 0},
		{`_ = Identity[string]("x")`, "Identity", "func", "index", "func Identity[T any]", "", 0},
		{"l.Push(1)", "Push", "method", "selector", "func (l *List[T]) Push", "*List", 0},
		{"a.Push(2)", "Push", "method", "selector", "func (l *List[T]) Push", "*List", 0},
	}
	for _, c := range cases {
		s := r.site(t, f, c.snippet, c.tok)
		if s.Class != c.class || s.Shape != c.shape || s.PromotedDepth != c.depth || s.SiteKind != "call" {
			t.Errorf("%s: class/shape/depth/kind = %s/%s/%d/%s, want %s/%s/%d/call", c.snippet, s.Class, s.Shape, s.PromotedDepth, s.SiteKind, c.class, c.shape, c.depth)
		}
		r.wantTarget(t, c.snippet, s.Target, f, c.declSnippet, c.tok)
		if s.Target.Recv != c.recv {
			t.Errorf("%s: recv %q, want %q", c.snippet, s.Target.Recv, c.recv)
		}
		wantQual := c.tok
		if c.recv != "" {
			wantQual = strings.TrimPrefix(c.recv, "*") + "." + c.tok
		}
		if s.Target.Qualname != wantQual {
			t.Errorf("%s: qualname %q, want %q", c.snippet, s.Target.Qualname, wantQual)
		}
		if s.Target.Module != "example.com/cls" || s.Target.Pkg != "example.com/cls/static" || s.Target.Version != "" {
			t.Errorf("%s: module/pkg/version = %q/%q/%q", c.snippet, s.Target.Module, s.Target.Pkg, s.Target.Version)
		}
		if s.Stub || s.EmbedInvalid || s.Asm || s.Alias != nil || s.Iface != nil || len(s.Variants) != 0 || s.Build != "linux-amd64" {
			t.Errorf("%s: unexpected flags %+v", c.snippet, s)
		}
		wantObj := "method"
		if c.class == "func" {
			wantObj = "func"
		}
		if s.Target.Obj != wantObj {
			t.Errorf("%s: obj %q, want %q", c.snippet, s.Target.Obj, wantObj)
		}
	}
	s := r.site(t, f, `_ = strings.ToUpper("x")`, "ToUpper")
	if s.Class != "func" || s.Target == nil || !strings.HasSuffix(s.Target.Path, "/src/strings/strings.go") || s.Target.Module != "" || s.Target.Pkg != "strings" {
		t.Errorf("strings.ToUpper: %+v %+v", s, s.Target)
	}
}

func TestSiteRecordPositions(t *testing.T) {
	r := cls(t)
	const f = "static/static.go"
	off, line, col := at(t, filepath.Join(r.root, f), "_ = t.Shadowed()", "Shadowed")
	s := r.sites[f][off]
	if s == nil || s.Line != line || s.Col != col || s.EndCol != col+len("Shadowed") || s.Name != "Shadowed" || s.Offset != off {
		t.Fatalf("position fields: %+v (want line %d col %d off %d)", s, line, col, off)
	}
	if s.Path != filepath.Join(r.root, f) {
		t.Errorf("path %q is not the real path", s.Path)
	}
}

func TestInterfaceCalls(t *testing.T) {
	r := cls(t)
	const f = "iface/iface.go"
	const ft = "iface/iface_test.go"
	type cand struct {
		file, declSnippet, tok, typ string
		pointer, testOnly, via      bool
	}
	cases := []struct {
		snippet, tok, declSnippet, recvType string
		depth                               int
		cands                               []cand
		openWorld                           bool
	}{
		{"z.Nothing()", "Nothing", "type Zero interface{ Nothing() }", "example.com/cls/iface.Zero", 0, nil, false},
		{"s.Only()", "Only", "type Single interface{ Only() }", "example.com/cls/iface.Single", 0,
			[]cand{{f, "func (OnlyImpl) Only()", "Only", "OnlyImpl", false, false, false}}, false},
		{"_ = m.Do()", "Do", "type Multi interface{ Do() string }", "example.com/cls/iface.Multi", 0,
			[]cand{{f, "func (A) Do()", "Do", "A", false, false, false}, {f, "func (*B) Do()", "Do", "B", true, false, false}}, false},
		{`l.Printf("x")`, "Printf", "\tPrintf(format string, v ...any)", "example.com/cls/iface.Logger", 0,
			[]cand{{f, "func (MyLog) Printf", "Printf", "MyLog", false, false, false}}, true},
		{"_ = w.Greet()", "Greet", "type Greeter interface{ Greet() string }", "example.com/cls/iface.Greeter", 1,
			[]cand{{f, "type Greeter interface{ Greet() string }", "Greet", "Wrapper", false, false, true},
				{f, "func (English) Greet()", "Greet", "English", false, false, false}}, false},
		{"_ = c.Now()", "Now", "type Clock interface{ Now() int64 }", "example.com/cls/iface.Clock", 0,
			[]cand{{f, "func (realClock) Now()", "Now", "realClock", false, false, false},
				{ft, "func (fakeClock) Now()", "Now", "fakeClock", false, true, false}}, false},
		{"_ = Multi.Do(m)", "Do", "type Multi interface{ Do() string }", "example.com/cls/iface.Multi", 0,
			[]cand{{f, "func (A) Do()", "Do", "A", false, false, false}, {f, "func (*B) Do()", "Do", "B", true, false, false}}, false},
	}
	for _, c := range cases {
		s := r.site(t, f, c.snippet, c.tok)
		if s.Class != "iface" || s.PromotedDepth != c.depth {
			t.Errorf("%s: class %s depth %d, want iface %d", c.snippet, s.Class, s.PromotedDepth, c.depth)
			continue
		}
		r.wantTarget(t, c.snippet, s.Target, f, c.declSnippet, c.tok)
		if s.Target.Obj != "iface_method" {
			t.Errorf("%s: obj %q", c.snippet, s.Target.Obj)
		}
		if iname := strings.TrimPrefix(c.recvType, "example.com/cls/iface."); s.Target.Qualname != iname+"."+c.tok {
			t.Errorf("%s: qualname %q", c.snippet, s.Target.Qualname)
		}
		if s.Iface == nil {
			t.Errorf("%s: no iface field", c.snippet)
			continue
		}
		if s.Iface.RecvType != c.recvType || s.Iface.OpenWorld != c.openWorld || s.Iface.Suppressed {
			t.Errorf("%s: recv_type %q open_world %v suppressed %v, want %q %v false", c.snippet, s.Iface.RecvType, s.Iface.OpenWorld, s.Iface.Suppressed, c.recvType, c.openWorld)
		}
		if len(s.Iface.Candidates) != len(c.cands) {
			t.Errorf("%s: %d candidates %+v, want %d", c.snippet, len(s.Iface.Candidates), s.Iface.Candidates, len(c.cands))
			continue
		}
		for i, want := range c.cands {
			got := s.Iface.Candidates[i]
			path, line, col := r.decl(t, want.file, want.declSnippet, want.tok)
			wantQual := want.typ + "." + want.tok
			if want.via {
				wantQual = strings.TrimPrefix(c.recvType, "example.com/cls/iface.") + "." + want.tok
			}
			if got.Qualname != wantQual {
				t.Errorf("%s: candidate %d qualname %q, want %q", c.snippet, i, got.Qualname, wantQual)
			}
			if got.Path != path || got.Line != line || got.Col != col || got.Type != want.typ || got.Pointer != want.pointer ||
				got.TestOnly != want.testOnly || got.ViaEmbeddedIface != want.via || got.Name != want.tok {
				t.Errorf("%s: candidate %d = %+v, want %s:%d:%d type %s pointer %v test %v via %v", c.snippet, i, got, path, line, col, want.typ, want.pointer, want.testOnly, want.via)
			}
		}
	}
}

// Test-only methods on production types: the interface is declared in a test
// file (only the test variant knows it), or in production code with the
// implementing method only in a test file.
func TestInterfaceTestOnlyMethods(t *testing.T) {
	r := cls(t)
	s := r.site(t, "iface/iface_test.go", "return s.synced()", "synced")
	if s.Iface == nil || s.Iface.Suppressed || len(s.Iface.Candidates) != 1 {
		t.Fatalf("s.synced: %+v", s.Iface)
	}
	c := s.Iface.Candidates[0]
	path, line, col := r.decl(t, "iface/iface_test.go", "func (OnlyImpl) synced()", "synced")
	if c.Path != path || c.Line != line || c.Col != col || c.Type != "OnlyImpl" || !c.TestOnly {
		t.Errorf("s.synced candidate: %+v", c)
	}
	s = r.site(t, "iface/iface.go", "{ f.Flush() }", "Flush")
	if s.Iface == nil || s.Iface.Suppressed || len(s.Iface.Candidates) != 1 {
		t.Fatalf("f.Flush: %+v", s.Iface)
	}
	c = s.Iface.Candidates[0]
	path, line, col = r.decl(t, "iface/iface_test.go", "func (A) Flush()", "Flush")
	if c.Path != path || c.Line != line || c.Col != col || c.Type != "A" || !c.TestOnly {
		t.Errorf("f.Flush candidate: %+v", c)
	}
}

func TestInterfaceSpecialReceivers(t *testing.T) {
	r := cls(t)
	const f = "iface/iface.go"
	// Embedded interface method: the target is the declaring interface's method,
	// the candidates come from the static receiver type.
	s := r.site(t, f, "_, _ = rc.Read(nil)", "Read")
	if s.Class != "iface" || s.Target == nil || !strings.HasSuffix(s.Target.Path, "/src/io/io.go") || s.Target.Recv != "Reader" {
		t.Errorf("rc.Read: %+v %+v", s, s.Target)
	}
	if s.Iface == nil || s.Iface.RecvType != "example.com/cls/iface.ReadCloser" || !s.Iface.OpenWorld {
		t.Errorf("rc.Read iface: %+v", s.Iface)
	}
	// Error on the predeclared error interface: class builtin with a target in
	// builtin.go and no candidates (ruling 2026-10-13).
	s = r.site(t, f, "_ = err.Error()", "Error")
	if s.Class != "builtin" || s.Target == nil || !strings.HasSuffix(s.Target.Path, "/src/builtin/builtin.go") || s.Target.Line == 0 ||
		s.Target.Pkg != "" || s.Target.Recv != "error" || s.Target.Qualname != "error.Error" || s.Iface != nil {
		t.Errorf("err.Error: %+v %+v", s, s.Target)
	}
	// A type parameter constrained by exactly error: class builtin, no
	// candidates (ruling 2026-10-14).
	s = r.site(t, f, "func Errs[T error](v T) string { return v.Error() }", "Error")
	if s.Class != "builtin" || s.Iface != nil || s.Target == nil || s.Target.Qualname != "error.Error" {
		t.Errorf("Errs v.Error: %+v %+v", s, s.Target)
	}
	// Type-parameter method through its constraint.
	s = r.site(t, f, "return v.String()", "String")
	if s.Class != "typeparam" || s.Target == nil || !strings.HasSuffix(s.Target.Path, "/src/fmt/print.go") || s.Target.Name != "String" {
		t.Fatalf("v.String: %+v %+v", s, s.Target)
	}
	if s.Iface == nil || s.Iface.RecvType != "fmt.Stringer" || !s.Iface.OpenWorld || len(s.Iface.Candidates) != 1 || s.Iface.Candidates[0].Type != "Named" {
		t.Errorf("v.String iface: %+v", s.Iface)
	}
}

func TestValueCalls(t *testing.T) {
	r := cls(t)
	const f = "values/values.go"
	cases := []struct {
		snippet, tok, kind string
		alias              string // decl snippet of the alias target ("" = no alias)
		aliasTok           string
	}{
		{"_ = local()", "local", "local", "func target()", "target"},
		{"_ = mv()", "mv", "local", "func (T) M()", "M"},
		{"_ = im()", "im", "local", "type I interface{ M() string }", "M"},
		{"_ = re()", "re", "local", "", ""},
		{"_ = addr()", "addr", "local", "", ""},
		{"_ = cl()", "cl", "local", "", ""},
		{"_ = pkgFn()", "pkgFn", "pkgvar", "", ""},
		{"_ = param()", "param", "param", "", ""},
		{"_ = h.fn()", "fn", "field", "", ""},
		{`_ = m["a"]()`, "m", "index", "", ""},
		{"_ = fs[0]()", "fs", "index", "", ""},
		{"_ = noinit()", "noinit", "local", "", ""},
		{"_ = gi(1)", "gi", "local", "func Identity[X any]", "Identity"},
		{"_ = me(T{})", "me", "local", "func (T) M()", "M"},
		{"_ = rv()", "rv", "local", "func target()", "target"},
		{"_ = (*fp)()", "(", "other", "", ""},
		{"_ = x.(func() string)()", ")()", "other", "", ""},
		{"_ = multi()", "multi", "local", "func target()", "target"},
		{"_ = other2()", "other2", "local", "func other()", "other"},
		{"_ = res()", "res", "result", "", ""},
	}
	for _, c := range cases {
		var s *siteRecord
		if c.kind == "other" {
			s = r.siteAtLparen(t, f, c.snippet) // rule 4: the site is at the call's Lparen
		} else {
			s = r.site(t, f, c.snippet, c.tok)
		}
		checkValue(t, r, f, c.snippet, s, c.kind, c.alias, c.aliasTok)
	}
	// The func literal alias points at the literal's func keyword.
	s := r.site(t, f, "_ = lit()", "lit")
	path, line, col := r.decl(t, f, `lit := func() string { return "lit" }`, "func")
	if s.Alias == nil || s.Alias.Obj != "closure" || s.Alias.Path != path || s.Alias.Line != line || s.Alias.Col != col {
		t.Errorf("lit alias: %+v", s.Alias)
	}
	// The interface method value alias behaves like an iface site.
	s = r.site(t, f, "_ = im()", "im")
	if s.Iface == nil || s.Iface.RecvType != "example.com/cls/values.I" || len(s.Iface.Candidates) != 1 || s.Iface.Candidates[0].Type != "T" {
		t.Errorf("im iface: %+v", s.Iface)
	}
}

// siteAtLparen returns the site whose offset is the last "(" of snippet that
// opens the call's argument list ("()" at the end of the snippet).
func (r *result) siteAtLparen(t *testing.T, relpath, snippet string) *siteRecord {
	t.Helper()
	off, _, _ := at(t, filepath.Join(r.root, relpath), snippet, snippet)
	off += strings.LastIndex(snippet, "(")
	s := r.sites[relpath][off]
	if s == nil {
		t.Fatalf("no site at the Lparen of %q (offset %d)", snippet, off)
	}
	return s
}

func checkValue(t *testing.T, r *result, f, snippet string, s *siteRecord, kind, alias, aliasTok string) {
	t.Helper()
	if s.Class != "value" || s.ValueKind == nil || *s.ValueKind != kind || s.Target != nil {
		vk := "<nil>"
		if s.ValueKind != nil {
			vk = *s.ValueKind
		}
		t.Errorf("%s: class %s value_kind %s target %+v, want value %s null", snippet, s.Class, vk, s.Target, kind)
		return
	}
	if alias == "" {
		if s.Alias != nil {
			t.Errorf("%s: alias %+v, want null", snippet, s.Alias)
		}
		return
	}
	r.wantTarget(t, snippet+" alias", s.Alias, f, alias, aliasTok)
}

func TestFuncLitsRangeConversionsBuiltins(t *testing.T) {
	r := cls(t)
	const f = "lits/lits.go"
	for _, c := range []struct{ snippet, kind string }{
		{"\tfunc() {}()", "call"},
		{"\tgo func() {}()", "go"},
		{"\tdefer func() {}()", "defer"},
	} {
		s := r.site(t, f, c.snippet, "func")
		path, line, col := r.decl(t, f, c.snippet, "func")
		if s.Class != "funclit" || s.Shape != "funclit" || s.SiteKind != c.kind || s.Target == nil || s.Target.Obj != "closure" ||
			s.Target.Path != path || s.Target.Line != line || s.Target.Col != col || s.Name != "func" || s.EndCol != s.Col+4 {
			t.Errorf("%q: %+v %+v", c.snippet, s, s.Target)
		}
	}
	s := r.site(t, f, "for v := range b.All {", "All")
	if s.SiteKind != "range" || s.Class != "method" || s.Shape != "selector" {
		t.Errorf("range b.All: %+v", s)
	}
	r.wantTarget(t, "range b.All", s.Target, f, "func (b *Bag) All(", "All")
	s = r.site(t, f, "for v := range Seq {", "Seq")
	if s.SiteKind != "range" || s.Class != "func" {
		t.Errorf("range Seq: %+v", s)
	}
	r.wantTarget(t, "range Seq", s.Target, f, "func Seq(", "Seq")
	// range over a call keeps the inner call only; range over a slice is no call
	s = r.site(t, f, "for v := range Count(3) {", "Count")
	if s.SiteKind != "call" || s.Class != "func" {
		t.Errorf("range Count(3): %+v", s)
	}
	off, _, _ := at(t, filepath.Join(r.root, f), "for _, x := range xs {", "xs")
	if !r.notcalls[f][off] || r.sites[f][off] != nil {
		t.Errorf("range over a slice: want a notcall record at %d", off)
	}
	// conversions are records, never sites
	for _, c := range []struct{ snippet, tok string }{
		{"_ = int32(3)", "int32"},
		{"_ = MyFunc(hello)", "MyFunc"},
		{"_ = Celsius(1.5)", "Celsius"},
	} {
		off, _, _ := at(t, filepath.Join(r.root, f), c.snippet, c.tok)
		if !r.convs[f][off] || r.sites[f][off] != nil {
			t.Errorf("%s: want a conversion record", c.snippet)
		}
	}
	for _, snippet := range []string{`_ = []byte("x")`, "_ = (*[]int)(p)", "_ = (func() string)(hello)"} {
		off, _, _ := at(t, filepath.Join(r.root, f), snippet, snippet)
		off += strings.LastIndex(snippet, "(")
		if !r.convs[f][off] {
			t.Errorf("%s: want a conversion record at its Lparen %d", snippet, off)
		}
	}
	off, _, _ = at(t, filepath.Join(r.root, f), "_ = Vec[int](xs)", "Vec")
	if !r.convs[f][off] {
		t.Error("Vec[int](xs): want a conversion record at the base ident")
	}
	// calls in array lengths are constant expressions, not sites
	for _, snippet := range []string{`var table [len("abcd")]int`, `var buf [len("xyz")]byte`, `func More(xs []int) [len("ab")]byte`} {
		off, _, _ := at(t, filepath.Join(r.root, f), snippet, "len")
		if r.sites[f][off] != nil {
			t.Errorf("%s: a call in an array length must not be a site", snippet)
		}
	}
	for _, c := range []struct{ snippet, tok string }{
		{"_ = len(xs)", "len"}, {"_ = append(xs, 1)", "append"}, {"_ = min(1, 2)", "min"},
	} {
		s := r.site(t, f, c.snippet, c.tok)
		if s.Class != "builtin" || s.Target != nil {
			t.Errorf("%s: %+v", c.snippet, s)
		}
	}
	for _, c := range []struct{ snippet, kind string }{{"\tgo hello()", "go"}, {"\tdefer hello()", "defer"}} {
		s := r.site(t, f, c.snippet, "hello")
		if s.SiteKind != c.kind || s.Class != "func" {
			t.Errorf("%s: %+v", c.snippet, s)
		}
	}
	// paren shape: (*[]int)(p) is a conversion with shape paren; check the
	// shape of a value call with a parenthesized callee instead
	s = r.siteAtLparen(t, "values/values.go", "_ = (*fp)()")
	if s.Shape != "paren" {
		t.Errorf("(*fp)(): shape %q, want paren", s.Shape)
	}
	s = r.siteAtLparen(t, "values/values.go", "_ = x.(func() string)()")
	if s.Shape != "other" {
		t.Errorf("x.(func() string)(): shape %q, want other", s.Shape)
	}
}

func TestLinknameAndAsm(t *testing.T) {
	r := cls(t)
	const f = "low/low.go"
	s := r.site(t, f, "_ = runtimeNanotime()", "runtimeNanotime")
	if s.Class != "linkname" || s.Linkname == nil || *s.Linkname != "runtime.nanotime" || s.Target == nil ||
		s.Target.Name != "nanotime" || s.Target.Pkg != "runtime" || !strings.Contains(s.Target.Path, "/src/runtime/") {
		t.Errorf("runtime linkname: %+v %+v", s, s.Target)
	}
	s = r.site(t, f, "_ = hiddenSecret()", "hiddenSecret")
	if s.Class != "linkname" || s.Linkname == nil || *s.Linkname != "example.com/cls/hidden.secret" {
		t.Errorf("in-repo linkname: %+v", s)
	}
	r.wantTarget(t, "hiddenSecret", s.Target, "hidden/hidden.go", "func secret()", "secret")
	s = r.site(t, f, "_ = Add(1, 2)", "Add")
	if s.Class != "func" || !s.Asm {
		t.Errorf("asm: %+v", s)
	}
	r.wantTarget(t, "asm Add", s.Target, f, "func Add(a, b int) int", "Add")
}

func TestEmbedImplementsImports(t *testing.T) {
	r := cls(t)
	const f = "facts/facts.go"
	type emb struct {
		owner, embedded string
		pointer         bool
	}
	got := map[emb]bool{}
	for _, e := range r.embeds {
		if r.rel(e.Type.Path) != f {
			continue
		}
		name := e.Embedded.Pkg + "." + e.Embedded.Name
		got[emb{e.Type.Name, name, e.Pointer}] = true
		if e.Embedded.Obj != "type" {
			t.Errorf("embedded obj %q", e.Embedded.Obj)
		}
	}
	for _, w := range []emb{
		{"Server", "example.com/cls/facts.Base", false},
		{"Server", "example.com/cls/static.Counter", true},
		{"Server", "io.Writer", false},
		{"ReadCloser", "io.Reader", false},
		{"ErrBox", ".error", false},
		{"IntBox", ".int", false},
		{"AnyBox", ".any", false},
		{"Cmp", ".comparable", false},
	} {
		if !got[w] {
			t.Errorf("missing embed %+v (have %v)", w, got)
		}
	}
	if len(got) != 8 {
		t.Errorf("embeds in facts.go: %v", got)
	}
	// Only embedded interfaces of interfaces are embeddings: no record for
	// the interface literal in Lit or the union in Number (ruling 2026-10-14).
	for _, e := range r.embeds {
		if r.rel(e.Type.Path) == f && (e.Type.Name == "Lit" || e.Type.Name == "Number") {
			t.Errorf("unexpected embed record for %s: %+v", e.Type.Name, e.Embedded)
		}
	}
	for _, e := range r.embeds {
		switch e.Type.Name {
		case "ErrBox", "IntBox", "AnyBox", "Cmp":
		default:
			continue
		}
		want := map[string]string{"ErrBox": "error", "IntBox": "int", "AnyBox": "any", "Cmp": "comparable"}[e.Type.Name]
		if e.Embedded == nil || e.Embedded.Name != want || e.Embedded.Qualname != want || e.Embedded.Pkg != "" ||
			!strings.HasSuffix(e.Embedded.Path, "/src/builtin/builtin.go") || e.Embedded.Line == 0 {
			t.Errorf("embedded predeclared type in %s: %+v", e.Type.Name, e.Embedded)
		}
	}
	type impl struct {
		typ, iface string
		ptr        bool
	}
	impls := map[impl]bool{}
	for _, e := range r.impls {
		impls[impl{e.Type.Pkg + "." + e.Type.Name, e.Iface.Pkg + "." + e.Iface.Name, e.PointerReceiver}] = true
		if e.Iface.Name == "Empty" {
			t.Errorf("implements record for an empty interface: %+v", e)
		}
	}
	for _, w := range []impl{
		{"example.com/cls/facts.Square", "example.com/cls/facts.Shape", false},
		{"example.com/cls/facts.Circle", "example.com/cls/facts.Shape", true},
		{"example.com/cls/iface.B", "example.com/cls/iface.Multi", true},
		{"example.com/cls/iface.Wrapper", "example.com/cls/iface.Greeter", false},
		{"example.com/cls/iface.fakeClock", "example.com/cls/iface.Clock", false},
	} {
		if !impls[w] {
			t.Errorf("missing implements %+v", w)
		}
	}
	imps := r.imports[f]
	off, line, col := at(t, filepath.Join(r.root, f), `"io"`, `"io"`)
	im := imps[off]
	if im == nil || im.ImportPath != "io" || !im.Std || im.Module != "" || im.Error != nil || im.Line != line || im.Col != col ||
		!strings.HasSuffix(im.TargetDir, "/src/io") {
		t.Errorf("import io: %+v", im)
	}
	off, _, _ = at(t, filepath.Join(r.root, f), `"example.com/cls/static"`, `"example.com/cls/static"`)
	im = imps[off]
	if im == nil || im.Std || im.Module != "example.com/cls" || im.TargetDir != filepath.Join(r.root, "static") || im.Error != nil {
		t.Errorf("import static: %+v", im)
	}
}

func TestSummaryAndFileRecords(t *testing.T) {
	r := cls(t)
	n, conv, nc := 0, 0, 0
	for _, m := range r.sites {
		n += len(m)
	}
	for _, m := range r.convs {
		conv += len(m)
	}
	for _, m := range r.notcalls {
		nc += len(m)
	}
	if r.summary.Sites != n || r.summary.Conversions != conv || r.summary.Notcalls != nc {
		t.Errorf("summary %+v, counted sites %d conversions %d notcalls %d", r.summary, n, conv, nc)
	}
	// lits.go has 7 conversions (one generic); values.go and static.go none
	if got := len(r.convs["lits/lits.go"]); got != 7 {
		t.Errorf("conversions in lits.go = %d, want 7", got)
	}
	if r.summary.PeakRSSBytes <= 0 || r.summary.LoadSeconds <= 0 || r.summary.Seconds < r.summary.LoadSeconds {
		t.Errorf("summary timing/memory: %+v", r.summary)
	}
	f := r.files["iface/iface_test.go"]
	if f == nil || len(f.SHA256) != 64 || len(f.Packages) != 1 || f.Packages[0] != "example.com/cls/iface [example.com/cls/iface.test]" {
		t.Errorf("file record for iface_test.go: %+v", f)
	}
	f = r.files["iface/iface.go"]
	if f == nil || strings.Join(f.Packages, ",") != "example.com/cls/iface,example.com/cls/iface [example.com/cls/iface.test]" {
		t.Errorf("file record for iface.go: %+v", f)
	}
	if len(r.files) != 12 {
		t.Errorf("file records: %d, want 12 (the .s file is not Go)", len(r.files))
	}
}

// Local named interfaces, interface literals and inline constraints have
// recv_type "" (R2) and their own candidate sets (a local I must not share
// the package-level I's implementers).
func TestNonPackageLevelInterfaces(t *testing.T) {
	r := cls(t)
	const f = "localiface/l.go"
	types := func(s *siteRecord) string {
		var out []string
		for _, c := range s.Iface.Candidates {
			out = append(out, c.Type)
		}
		return strings.Join(out, ",")
	}
	s := r.site(t, f, "func F(i I) { i.M() }", "M")
	if s.Iface == nil || s.Iface.RecvType != "example.com/cls/localiface.I" || types(s) != "A,B" {
		t.Errorf("package-level I: %+v", s.Iface)
	}
	s = r.site(t, f, "\tvar i I = b\n\ti.M()", "M")
	if s.Class != "iface" || s.Iface == nil || s.Iface.RecvType != "" || types(s) != "B" {
		t.Errorf("function-local I: class %s iface %+v", s.Class, s.Iface)
	}
	s = r.site(t, f, "}).N()", "N")
	if s.Class != "iface" || s.Iface == nil || s.Iface.RecvType != "" || types(s) != "B" || s.Target == nil {
		t.Errorf("interface literal: %+v %+v", s, s.Iface)
	} else if s.Target.Qualname != "" {
		t.Errorf("a method of an interface literal has no qualname: %q", s.Target.Qualname)
	}
	s = r.site(t, "facts/facts.go", "return l.Close()", "Close")
	if s.Class != "iface" || s.Target == nil || s.Target.Qualname != "" || s.Iface == nil || s.Iface.RecvType != "example.com/cls/facts.Lit" {
		t.Errorf("method of an embedded interface literal: %+v %+v %+v", s, s.Target, s.Iface)
	}
	s = r.site(t, f, "(v T) { v.N() }", "N")
	if s.Class != "typeparam" || s.Iface == nil || s.Iface.RecvType != "" || types(s) != "B" {
		t.Errorf("inline constraint: %+v %+v", s, s.Iface)
	}
}

// A real package whose import path ends in ".test" is not a test main.
func TestDotTestPackage(t *testing.T) {
	r := cls(t)
	s := r.site(t, "dot.test/d.go", "return helper()", "helper")
	if s.Class != "func" || s.Target == nil {
		t.Errorf("dot.test package site: %+v", s)
	}
}

// Positions are unadjusted (R3): //line directives never move a site or a
// target (spec-review probe p3).
func TestUnadjustedPositions(t *testing.T) {
	r := cls(t)
	const f = "lined/gen.go"
	for _, c := range []struct{ snippet, tok, declFile, declSnippet, declTok string }{
		{"func Lined2() string { return Lined() }", "return Lined", f, "func Lined() string", "Lined"},
		{"func CallLined() string { return Lined2() }", "return Lined2", f, "func Lined2() string", "Lined2"},
		{"return T{}.Name()", "Name", "lined/m.go", "func (T) Name()", "Name"},
	} {
		off, line, col := at(t, filepath.Join(r.root, f), c.snippet, c.tok)
		if strings.HasPrefix(c.tok, "return ") {
			off += len("return ")
			col += len("return ")
		}
		s := r.sites[f][off]
		if s == nil || s.Line != line || s.Col != col || s.Path != filepath.Join(r.root, f) {
			t.Errorf("%s: site %+v, want %s:%d:%d", c.snippet, s, f, line, col)
			continue
		}
		r.wantTarget(t, c.snippet, s.Target, c.declFile, c.declSnippet, c.declTok)
	}
}

// linkname targets in the method forms pkg.T.m and pkg.(*T).m (R36).
func TestLinknameMethodForms(t *testing.T) {
	r := cls(t)
	const f = "low/low.go"
	for _, c := range []struct{ snippet, tok, link, decl, declTok, qual string }{
		{"_ = valueMethod(hidden.T{})", "valueMethod", "example.com/cls/hidden.T.Value", "func (T) Value()", "Value", "T.Value"},
		{"_ = ptrMethod(&hidden.T{})", "ptrMethod", "example.com/cls/hidden.(*T).Ptr", "func (*T) Ptr()", "Ptr", "T.Ptr"},
	} {
		s := r.site(t, f, c.snippet, c.tok)
		if s.Class != "linkname" || s.Linkname == nil || *s.Linkname != c.link {
			t.Errorf("%s: %+v", c.snippet, s)
			continue
		}
		r.wantTarget(t, c.snippet, s.Target, "hidden/hidden.go", c.decl, c.declTok)
		if s.Target.Qualname != c.qual || s.Target.Obj != "method" {
			t.Errorf("%s: qualname %q obj %q", c.snippet, s.Target.Qualname, s.Target.Obj)
		}
	}
}

// func-alias decides by the selected method object (R36): a method value or
// method expression of an interface method, here promoted from an embedded
// io.Reader, is an iface-like alias.
func TestAliasOfEmbeddedInterfaceMethod(t *testing.T) {
	r := cls(t)
	const f = "values/values.go"
	for _, c := range []struct{ snippet, tok string }{{"_, _ = rd(nil)", "rd"}, {"_, _ = me2(rs, nil)", "me2"}} {
		s := r.site(t, f, c.snippet, c.tok)
		if s.Class != "value" || s.Alias == nil || s.Alias.Name != "Read" || s.Alias.Qualname != "Reader.Read" ||
			s.Alias.Obj != "iface_method" || !strings.HasSuffix(s.Alias.Path, "/src/io/io.go") {
			t.Errorf("%s: alias %+v", c.snippet, s.Alias)
		}
		if s.Iface == nil || s.Iface.RecvType != "io.Reader" || !s.Iface.OpenWorld {
			t.Errorf("%s: iface %+v", c.snippet, s.Iface)
		}
	}
}
