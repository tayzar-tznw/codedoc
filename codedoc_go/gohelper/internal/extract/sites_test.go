package extract

import (
	"go/ast"
	"go/parser"
	"go/token"
	"strings"
	"testing"
)

func parseSrc(t *testing.T, src string) (*token.File, *ast.File) {
	t.Helper()
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, "x.go", src, parseMode)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	return fset.File(f.FileStart), f
}

// lineCol computes the 1-based line and byte column of a byte offset
// independently of go/token.
func lineCol(src string, off int) (int, int) {
	line := 1 + strings.Count(src[:off], "\n")
	return line, off - (strings.LastIndex(src[:off], "\n") + 1) + 1
}

type siteView struct {
	kind, shape, name string
	line              int
	tok               string // source text at the token
	recv              string
}

func viewSites(t *testing.T, tf *token.File, src string, sites []Site) []siteView {
	t.Helper()
	var out []siteView
	for _, s := range sites {
		off, end := tf.Offset(s.Pos), tf.Offset(s.End)
		line, col := lineCol(src, off)
		p := tf.PositionFor(s.Pos, false)
		if p.Line != line || p.Column != col || p.Offset != off {
			t.Errorf("position of %q: token %d:%d@%d, independent %d:%d@%d", s.Name, p.Line, p.Column, p.Offset, line, col, off)
		}
		v := siteView{kind: s.Kind, shape: s.Shape, name: s.Name, line: line, tok: src[off:end]}
		if s.Recv != nil {
			v.recv = src[tf.Offset(s.Recv.Pos()):tf.Offset(s.Recv.End())]
		}
		out = append(out, v)
	}
	return out
}

func TestSitesShapesTokensAndKinds(t *testing.T) {
	src := `package p

import "pkg"

var sink = initValue()

func F(x X, m map[string]func(int), s string, p *T) {
	g(1)
	x.m(2)
	pkg.F(3)
	G[int](4)
	x.G[int, string](5)
	m["k"](6)
	func() {}()
	(h)(7)
	(*T)(p)
	[]byte(s)
	f()(8)
	go run()
	defer x.Close()
	go func() {}()
	for range seq {
	}
	for v := range x.All {
		_ = v
	}
	for range items() {
	}
	x.(func())()
	fs()[0](9)
	a.b().c()
	T(s)
	(func() {})()
	for range Gen[int] {
	}
	for range m["k"] {
	}
	for range 10 {
	}
	for range []int{1} {
	}
}
`
	tf, f := parseSrc(t, src)
	got := viewSites(t, tf, src, Sites(f))
	want := []siteView{
		{"call", "ident", "initValue", 5, "initValue", ""},
		{"call", "ident", "g", 8, "g", ""},
		{"call", "selector", "m", 9, "m", "x"},
		{"call", "selector", "F", 10, "F", "pkg"},
		{"call", "index", "G", 11, "G", ""},
		{"call", "index", "G", 12, "G", "x"},
		{"call", "index", "m", 13, "m", ""},
		{"call", "funclit", "func", 14, "func", ""},
		{"call", "ident", "h", 15, "h", ""},
		{"call", "paren", "", 16, "(", ""},
		{"call", "other", "", 17, "(", ""},
		{"call", "ident", "f", 18, "f", ""},
		{"go", "ident", "run", 19, "run", ""},
		{"defer", "selector", "Close", 20, "Close", "x"},
		{"go", "funclit", "func", 21, "func", ""},
		{"range", "ident", "seq", 22, "seq", ""},
		{"range", "selector", "All", 24, "All", "x"},
		{"call", "ident", "items", 27, "items", ""},
		{"call", "other", "", 29, "(", ""},
		{"call", "ident", "fs", 30, "fs", ""},
		{"call", "other", "", 30, "(", ""},
		{"call", "selector", "b", 31, "b", "a"},
		{"call", "selector", "c", 31, "c", "a.b()"},
		{"call", "ident", "T", 32, "T", ""},
		{"call", "funclit", "func", 33, "func", ""},
		{"range", "index", "Gen", 34, "Gen", ""},
		{"range", "index", "m", 36, "m", ""},
	}
	if len(got) != len(want) {
		t.Fatalf("got %d sites, want %d:\n%v", len(got), len(want), got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("site %d: got %+v, want %+v", i, got[i], want[i])
		}
	}
}

func TestSitesParenAndOtherTokenIsTheCallsLparen(t *testing.T) {
	src := "package p\n\nfunc F() {\n\t(*T)(p)\n\t[]byte(s)\n}\n"
	tf, f := parseSrc(t, src)
	sites := Sites(f)
	if len(sites) != 2 {
		t.Fatalf("got %d sites", len(sites))
	}
	for i, call := range []*ast.CallExpr{sites[0].Call, sites[1].Call} {
		if sites[i].Pos != call.Lparen {
			t.Errorf("site %d: token %d, want the call's Lparen %d", i, tf.Offset(sites[i].Pos), tf.Offset(call.Lparen))
		}
		if sites[i].End != call.Lparen+1 {
			t.Errorf("site %d: token end must be Lparen+1", i)
		}
	}
}

func TestSitesSkipCompileTimeOnlyCalls(t *testing.T) {
	src := `package p

import "unsafe"

const size = unsafe.Sizeof(x)

const (
	a = len("abc")
	b = T(1)
)

var buf [unsafe.Sizeof(x)]byte

var arr = [len(s)]int{}

type A [len(s)]int

func F(v [len(s)]int) [cap(s)]int {
	const local = len("x")
	var tmp [len(s)]int
	_ = new([len(s)]int)
	return [cap(s)]int{}
}
`
	tf, f := parseSrc(t, src)
	got := viewSites(t, tf, src, Sites(f))
	want := []siteView{{"call", "ident", "new", 21, "new", ""}}
	if len(got) != len(want) || got[0] != want[0] {
		t.Fatalf("got %v, want %v", got, want)
	}
}

func TestSitesRecordTheStatementAndRangeNodes(t *testing.T) {
	src := "package p\n\nfunc F() {\n\tgo f(g())\n\tfor range seq {\n\t}\n}\n"
	_, f := parseSrc(t, src)
	sites := Sites(f)
	if len(sites) != 3 {
		t.Fatalf("got %d sites", len(sites))
	}
	if sites[0].Kind != "go" || sites[0].Name != "f" || sites[0].Call == nil || sites[0].Range != nil {
		t.Errorf("go site: %+v", sites[0])
	}
	if sites[1].Kind != "call" || sites[1].Name != "g" {
		t.Errorf("argument of a go statement is a plain call: %+v", sites[1])
	}
	if sites[2].Kind != "range" || sites[2].Range == nil || sites[2].Call != nil {
		t.Errorf("range site: %+v", sites[2])
	}
}
