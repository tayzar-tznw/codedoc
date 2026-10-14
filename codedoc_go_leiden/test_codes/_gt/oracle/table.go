package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// Records of the site table, one JSON object per line: one meta line, then
// file, func, imethod, site, hot, stmt and reach records.

// MetaRec describes the instrumented tree.
type MetaRec struct {
	Kind    string   `json:"kind"` // "meta"
	Version int      `json:"version"`
	Src     string   `json:"src,omitempty"`
	Root    string   `json:"root"` // the tree the positions refer to (the copy)
	Mode    string   `json:"mode"`
	RT      string   `json:"rt"`
	Go      string   `json:"go"`
	GOOS    string   `json:"goos"`
	GOARCH  string   `json:"goarch"`
	CGO     bool     `json:"cgo"`
	Tags    []string `json:"tags"`
	Funcs   int      `json:"funcs"`
	Sites   int      `json:"sites"`
	Skipped int      `json:"skipped_files"`
}

// FileRec lists each scanned file and whether it was instrumented.
type FileRec struct {
	Kind   string `json:"kind"` // "file"
	File   string `json:"file"` // relative to the root, slash-separated
	Abs    string `json:"abs"`
	Real   string `json:"real,omitempty"` // a copied symlink: relative path of its target
	Pkg    string `json:"pkg"`
	Status string `json:"status"` // instrumented, skipped:<reason>
	Build  bool   `json:"build"`  // compiled by the build configuration (GOOS, GOARCH, cgo, tags)
	Hooks  int    `json:"hooks"`
	Sites  int    `json:"sites"`
	Tags   int    `json:"tags"`
}

// FuncRec describes one function body (FuncDecl or FuncLit).
type FuncRec struct {
	Kind      string `json:"kind"`     // "func"
	Fid       int    `json:"fid"`      // id passed to E
	Pkg       string `json:"pkg"`      // import path ("_test" suffix for external test packages)
	QName     string `json:"qname"`    // pkg.F, pkg.T.M, pkg.(*T).M, pkg.Outer.func1 (type parameters dropped)
	RTName    string `json:"rtname"`   // the name the runtime reports (generics as [...])
	Qualname  string `json:"qualname"` // codedoc C3 grammar: F, T.M, F.func1, F.func1.func1, <init>.func1, init#1, _#1
	Name      string `json:"name"`     // declared name; "func" for literals
	Recv      string `json:"recv,omitempty"`
	Ptr       bool   `json:"ptr,omitempty"`
	File      string `json:"file"` // relative to the root
	Line      int    `json:"line"` // name identifier (FuncDecl) or func keyword (FuncLit)
	Col       int    `json:"col"`
	EndLine   int    `json:"end_line"`
	Lit       bool   `json:"lit"`
	Parent    int    `json:"parent"` // enclosing fid, -1 for declarations and package-level literals
	Hooked    bool   `json:"hooked"`
	HasDefer  bool   `json:"has_defer"`
	ExitLines []int  `json:"exit_lines,omitempty"`
	Defers    []int  `json:"defers,omitempty"` // site ids of the deferred calls
}

// IMethodRec is a method of a declared interface type. It has no body, so it
// is never entered; an edge to it is judged IFACE-*.
type IMethodRec struct {
	Kind     string `json:"kind"` // "imethod"
	Pkg      string `json:"pkg"`
	Iface    string `json:"iface"`
	Name     string `json:"name"`
	Qualname string `json:"qualname"` // I.M
	QName    string `json:"qname"`    // pkg.I.M
	File     string `json:"file"`
	Line     int    `json:"line"`
	Col      int    `json:"col"`
}

// SiteRec describes one call expression, or the implicit iterator call of a
// range statement.
type SiteRec struct {
	Kind    string `json:"kind"` // "site"
	Sid     int    `json:"sid"`
	File    string `json:"file"`
	Line    int    `json:"line"` // callee token, as extract.Sites has it ("(" for paren, other and conversion)
	Col     int    `json:"col"`  // callee token, 1-based byte column
	Name    string `json:"name"`
	Form    string `json:"form"`        // ident, selector, index, funclit, paren, other, conversion, callresult, range
	LLine   int    `json:"lparen_line"` // position the call instruction takes: "(" (range: the for keyword)
	LCol    int    `json:"lparen_col"`
	Fid     int    `json:"fid"`  // enclosing function, -1 for package-level initializers
	Stmt    string `json:"stmt"` // call, go, defer, range
	GoLine  int    `json:"go_line,omitempty"`
	Tagged  bool   `json:"tagged"`
	Hot     string `json:"hot,omitempty"` // why the line is ambiguous: multi, exit, range, deferfn
	InRange bool   `json:"in_range_header,omitempty"`
}

// HotRec marks a line where implicit calls happen: deferred calls at an exit
// (return statement or closing brace of a function with defers) or the
// iterator call of a range-over-func loop.
type HotRec struct {
	Kind string `json:"kind"` // "hot"
	File string `json:"file"`
	Line int    `json:"line"`
	Why  string `json:"why"` // exit, range
}

// StmtRec is a counter. Kind "stmt": a completion counter after a simple
// statement; every listed site runs exactly once each time the statement
// completes. Kind "reach": a counter before a return, if, for, switch, go or
// defer statement (or panic); every listed site is evaluated when the
// statement starts (the go/defer call itself runs later). Calls under the
// right operand of && or || and inside func literals are never listed.
type StmtRec struct {
	Kind  string `json:"kind"` // "stmt" or "reach"
	ID    int    `json:"id"`
	File  string `json:"file"`
	Line  int    `json:"line"`
	Sites []int  `json:"sites"`
}

type table struct {
	meta      *MetaRec
	files     map[string]*FileRec // abs -> file
	byRel     map[string]*FileRec // rel -> file
	funcs     map[int]*FuncRec
	imethods  []*IMethodRec
	sites     map[int]*SiteRec
	untagged  map[string][]*SiteRec // abs:line -> untagged sites (by lparen line)
	goSites   map[string][]*SiteRec // abs:goline -> go statement sites
	exitFuncs map[string][]*FuncRec // abs:line -> functions with defers that exit there
	hot       map[string]string     // abs:line -> hot reason
	stmtOf    map[int]int           // sid -> completion counter id
	reachOf   map[int]int           // sid -> reach counter id
	counter   map[int]string        // counter id -> "stmt" or "reach"
	deferFns  map[string][]*FuncRec // runtime name (unescaped) -> functions with defers
	realFiles map[string]*FileRec   // real path -> file, built on first need
}

func lk(abs string, line int) string { return abs + ":" + strconv.Itoa(line) }

// sitesPathOf returns the site table of an instrumented copy.
func sitesPathOf(copyDir string) string {
	return filepath.Join(copyDir, "_goracle", "sites.jsonl")
}

func loadTable(path string) *table {
	t := &table{files: map[string]*FileRec{}, byRel: map[string]*FileRec{}, funcs: map[int]*FuncRec{},
		sites: map[int]*SiteRec{}, untagged: map[string][]*SiteRec{}, goSites: map[string][]*SiteRec{},
		exitFuncs: map[string][]*FuncRec{}, hot: map[string]string{}, stmtOf: map[int]int{},
		reachOf: map[int]int{}, counter: map[int]string{}, deferFns: map[string][]*FuncRec{}}
	relAbs := map[string]string{}
	var hots []*HotRec
	readJSONL(path, func(b []byte) {
		var probe struct{ Kind string }
		check(json.Unmarshal(b, &probe))
		switch probe.Kind {
		case "meta":
			t.meta = &MetaRec{}
			check(json.Unmarshal(b, t.meta))
		case "file":
			r := &FileRec{}
			check(json.Unmarshal(b, r))
			t.files[r.Abs] = r
			t.byRel[r.File] = r
			relAbs[r.File] = r.Abs
		case "func":
			r := &FuncRec{}
			check(json.Unmarshal(b, r))
			t.funcs[r.Fid] = r
		case "imethod":
			r := &IMethodRec{}
			check(json.Unmarshal(b, r))
			t.imethods = append(t.imethods, r)
		case "site":
			r := &SiteRec{}
			check(json.Unmarshal(b, r))
			t.sites[r.Sid] = r
		case "hot":
			r := &HotRec{}
			check(json.Unmarshal(b, r))
			hots = append(hots, r)
		case "stmt", "reach":
			r := &StmtRec{}
			check(json.Unmarshal(b, r))
			t.counter[r.ID] = r.Kind
			for _, sid := range r.Sites {
				if r.Kind == "stmt" {
					t.stmtOf[sid] = r.ID
				} else {
					t.reachOf[sid] = r.ID
				}
			}
		}
	})
	if t.meta == nil {
		check(fmt.Errorf("%s: no meta record (not a goracle site table)", path))
	}
	for _, h := range hots {
		t.hot[lk(relAbs[h.File], h.Line)] = h.Why
	}
	for _, s := range t.sites {
		abs := relAbs[s.File]
		if s.Hot != "" && t.hot[lk(abs, s.LLine)] == "" {
			t.hot[lk(abs, s.LLine)] = s.Hot
		}
		if !s.Tagged && s.Form != "conversion" {
			t.untagged[lk(abs, s.LLine)] = append(t.untagged[lk(abs, s.LLine)], s)
		}
		if s.Stmt == "go" {
			t.goSites[lk(abs, s.GoLine)] = append(t.goSites[lk(abs, s.GoLine)], s)
		}
	}
	for _, fn := range t.funcs {
		abs := relAbs[fn.File]
		for _, l := range fn.ExitLines {
			t.exitFuncs[lk(abs, l)] = append(t.exitFuncs[lk(abs, l)], fn)
		}
		if fn.HasDefer {
			n := unescapeName(fn.RTName)
			t.deferFns[n] = append(t.deferFns[n], fn)
		}
	}
	return t
}

// file finds the file record of a frame's file name; when the go command saw
// the copy through a symlinked path, the real path still finds it.
func (t *table) file(abs string) *FileRec {
	if f := t.files[abs]; f != nil {
		return f
	}
	if t.realFiles == nil {
		t.realFiles = map[string]*FileRec{}
		for a, f := range t.files {
			if r, err := filepath.EvalSymlinks(a); err == nil {
				t.realFiles[r] = f
			}
		}
	}
	if r, err := filepath.EvalSymlinks(abs); err == nil {
		return t.realFiles[r]
	}
	return nil
}

// unescapeName undoes the runtime's escaping of a dot in the last path
// element of an import path (example.com/a/v1%2ex.F).
func unescapeName(s string) string {
	return strings.ReplaceAll(s, "%2e", ".")
}

// funcByQual finds a function by file and C3 qualified name; a file copied
// from a symlink also answers for its target.
func (t *table) funcByQual(file, qual string) []*FuncRec {
	var out []*FuncRec
	for _, fn := range t.funcs {
		if fn.Qualname != qual {
			continue
		}
		if fn.File == file || (t.byRel[fn.File] != nil && t.byRel[fn.File].Real == file) {
			out = append(out, fn)
		}
	}
	return out
}

func (t *table) imethodByQual(file, qual string) *IMethodRec {
	for _, m := range t.imethods {
		if m.Qualname == qual && m.File == file {
			return m
		}
	}
	return nil
}

func readJSONL(path string, fn func([]byte)) {
	f, err := os.Open(path)
	check(err)
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<26)
	for sc.Scan() {
		if len(sc.Bytes()) > 0 {
			fn(sc.Bytes())
		}
	}
	check(sc.Err())
}

func writeJSONL(path string, recs []any) {
	check(os.MkdirAll(filepath.Dir(path), 0o755))
	tmp := path + ".tmp"
	f, err := os.Create(tmp)
	check(err)
	w := bufio.NewWriter(f)
	enc := json.NewEncoder(w)
	for _, r := range recs {
		check(enc.Encode(r))
	}
	check(w.Flush())
	check(f.Close())
	check(os.Rename(tmp, path))
}
