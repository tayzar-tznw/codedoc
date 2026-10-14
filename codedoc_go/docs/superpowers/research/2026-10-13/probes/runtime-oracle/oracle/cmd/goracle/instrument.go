package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

// FuncRec describes one instrumented function body (FuncDecl or FuncLit).
type FuncRec struct {
	Kind      string `json:"kind"` // "func"
	Fid       int    `json:"fid"`
	Pkg       string `json:"pkg"`    // import path ("_test" suffix for external test packages)
	QName     string `json:"qname"`  // pkg.F, pkg.T.M, pkg.(*T).M, pkg.Outer.func1 (type parameters dropped)
	RTName    string `json:"rtname"` // the name the runtime is expected to report (generics as [...])
	File      string `json:"file"`   // relative to the root
	Line      int    `json:"line"`   // name identifier (FuncDecl) or func keyword (FuncLit)
	Col       int    `json:"col"`
	EndLine   int    `json:"end_line"`
	Lit       bool   `json:"lit"`
	Parent    int    `json:"parent"` // enclosing fid, -1 for declarations
	Hooked    bool   `json:"hooked"`
	HasDefer  bool   `json:"has_defer"`
	ExitLines []int  `json:"exit_lines,omitempty"`
	Defers    []int  `json:"defers,omitempty"` // site ids of the deferred calls
}

// SiteRec describes one call expression.
type SiteRec struct {
	Kind    string `json:"kind"` // "site"
	Sid     int    `json:"sid"`
	File    string `json:"file"`
	Line    int    `json:"line"` // callee token
	Col     int    `json:"col"`  // callee token, 1-based byte column
	Name    string `json:"name"`
	Form    string `json:"form"` // ident, selector, index, funclit, callresult, other
	LLine   int    `json:"lparen_line"`
	LCol    int    `json:"lparen_col"`
	Fid     int    `json:"fid"`  // enclosing function, -1 for package-level initializers
	Stmt    string `json:"stmt"` // call, go, defer
	GoLine  int    `json:"go_line,omitempty"`
	Tagged  bool   `json:"tagged"`
	Hot     string `json:"hot,omitempty"` // why the line is ambiguous: multi, exit, range
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

// StmtRec is a simple statement followed by a completion counter
// __goracle.D(id). Every listed site runs exactly once each time the statement
// completes (calls under the right operand of && or || and inside func
// literals are not listed).
type StmtRec struct {
	Kind  string `json:"kind"` // "stmt"
	ID    int    `json:"id"`
	File  string `json:"file"`
	Line  int    `json:"line"`
	Sites []int  `json:"sites"`
}

// FileRec lists each scanned file and whether it was instrumented.
type FileRec struct {
	Kind   string `json:"kind"` // "file"
	File   string `json:"file"`
	Abs    string `json:"abs"`
	Pkg    string `json:"pkg"`
	Status string `json:"status"` // instrumented, skipped:<reason>
	Hooks  int    `json:"hooks"`
	Sites  int    `json:"sites"`
	Tags   int    `json:"tags"`
}

type edit struct {
	off  int
	seq  int
	text string
}

type instrumenter struct {
	root     string
	tagMode  string
	rtImport string
	tests    bool
	nextFid  int
	nextSid  int
	funcs    []*FuncRec
	sites    []*SiteRec
	files    []*FileRec
	hots     []*HotRec
	stmts    []*StmtRec
	nextStmt int
	done     bool
}

func cmdInstrument(args []string) {
	fs := flag.NewFlagSet("instrument", flag.ExitOnError)
	root := fs.String("root", "", "copied module or workspace root (rewritten in place)")
	dirs := fs.String("dirs", "", "comma-separated package directories relative to -root")
	tag := fs.String("tag", "needed", "column tags: needed, all or none")
	out := fs.String("sites", "", "output site table (JSON lines)")
	rtImport := fs.String("rt", "codedoc.dev/goracle/rt", "import path of the runtime package")
	tests := fs.Bool("tests", true, "instrument _test.go files too")
	done := fs.Bool("done", true, "add completion counters after simple statements")
	fs.Parse(args)
	if *root == "" || *dirs == "" || *out == "" {
		fs.Usage()
		os.Exit(2)
	}
	abs, err := filepath.Abs(*root)
	check(err)
	in := &instrumenter{root: abs, tagMode: *tag, rtImport: *rtImport, tests: *tests, done: *done}
	for _, d := range strings.Split(*dirs, ",") {
		if d = strings.TrimSpace(d); d != "" {
			in.pkgDir(filepath.Join(abs, d))
		}
	}
	f, err := os.Create(*out)
	check(err)
	w := bufio.NewWriter(f)
	enc := json.NewEncoder(w)
	for _, r := range in.files {
		check(enc.Encode(r))
	}
	for _, r := range in.funcs {
		check(enc.Encode(r))
	}
	for _, r := range in.sites {
		check(enc.Encode(r))
	}
	for _, r := range in.hots {
		check(enc.Encode(r))
	}
	for _, r := range in.stmts {
		check(enc.Encode(r))
	}
	check(w.Flush())
	check(f.Close())
	nf, ns, nt := 0, 0, 0
	for _, r := range in.files {
		if r.Status == "instrumented" {
			nf++
		}
	}
	for _, s := range in.sites {
		ns++
		if s.Tagged {
			nt++
		}
	}
	covered := 0
	for _, r := range in.stmts {
		covered += len(r.Sites)
	}
	fmt.Printf("instrumented %d of %d files: %d functions, %d call sites, %d tagged (mode %s), %d sites under %d completion counters\n",
		nf, len(in.files), len(in.funcs), ns, nt, *tag, covered, len(in.stmts))
}

func check(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "goracle:", err)
		os.Exit(1)
	}
}

// modulePath finds the go.mod above dir and returns the import path of dir.
func modulePath(dir string) (string, error) {
	for d := dir; ; d = filepath.Dir(d) {
		data, err := os.ReadFile(filepath.Join(d, "go.mod"))
		if err == nil {
			for _, line := range strings.Split(string(data), "\n") {
				line = strings.TrimSpace(line)
				if strings.HasPrefix(line, "module ") {
					mod := strings.Trim(strings.TrimSpace(strings.TrimPrefix(line, "module")), `"`)
					rel, _ := filepath.Rel(d, dir)
					if rel == "." {
						return mod, nil
					}
					return mod + "/" + filepath.ToSlash(rel), nil
				}
			}
			return "", fmt.Errorf("%s/go.mod has no module line", d)
		}
		if filepath.Dir(d) == d {
			return "", fmt.Errorf("no go.mod above %s", dir)
		}
	}
}

func (in *instrumenter) pkgDir(dir string) {
	ents, err := os.ReadDir(dir)
	check(err)
	ipath, err := modulePath(dir)
	check(err)
	var plain, tests []string
	for _, e := range ents {
		n := e.Name()
		if e.IsDir() || !strings.HasSuffix(n, ".go") || strings.HasPrefix(n, ".") || strings.HasPrefix(n, "_") {
			continue
		}
		if strings.HasSuffix(n, "_test.go") {
			if in.tests {
				tests = append(tests, n)
			}
		} else {
			plain = append(plain, n)
		}
	}
	sort.Strings(plain)
	sort.Strings(tests)
	// init.N numbering follows the compiler's file order: package files, then
	// in-package test files; the external test package counts on its own.
	initN := map[string]*int{}
	for _, n := range append(plain, tests...) {
		in.file(filepath.Join(dir, n), ipath, initN)
	}
}

func hasLineDirective(f *ast.File) bool {
	for _, cg := range f.Comments {
		for _, c := range cg.List {
			if strings.HasPrefix(c.Text, "//line ") || strings.HasPrefix(c.Text, "/*line ") {
				return true
			}
		}
	}
	return false
}

func hasPragma(doc *ast.CommentGroup, names ...string) bool {
	if doc == nil {
		return false
	}
	for _, c := range doc.List {
		for _, n := range names {
			if strings.HasPrefix(c.Text, "//go:"+n) {
				return true
			}
		}
	}
	return false
}

type fileState struct {
	in      *instrumenter
	fset    *token.FileSet
	tf      *token.File
	src     []byte
	abs     string
	rel     string
	pkg     string // import path of the package the file belongs to
	rtPkg   string // symbol prefix the runtime uses ("main" for package main)
	edits   []edit
	seq     int
	hooks   int
	initN   map[string]*int
	fsites  []*SiteRec
	lineCnt map[int]int
	hot     map[int]string
	stack   []*funcCtx
	inRange map[*ast.CallExpr]bool
	siteOf  map[*ast.CallExpr]int
}

type funcCtx struct {
	rec     *FuncRec
	litGen  int // closures directly inside (for the .funcN / .N suffix)
	isLit   bool
	retLine map[int]bool
}

func (in *instrumenter) file(path, ipath string, initN map[string]*int) {
	rel, _ := filepath.Rel(in.root, path)
	rel = filepath.ToSlash(rel)
	rec := &FileRec{Kind: "file", File: rel, Abs: path, Pkg: ipath}
	in.files = append(in.files, rec)
	src, err := os.ReadFile(path)
	check(err)
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, path, src, parser.ParseComments|parser.SkipObjectResolution)
	if err != nil {
		rec.Status = "skipped:parse"
		return
	}
	for _, im := range f.Imports {
		if im.Path.Value == `"C"` {
			rec.Status = "skipped:cgo"
			return
		}
	}
	if hasLineDirective(f) {
		rec.Status = "skipped:line-directives"
		return
	}
	pkg := ipath
	rtPkg := ipath
	if strings.HasSuffix(f.Name.Name, "_test") && strings.HasSuffix(path, "_test.go") {
		pkg = ipath + "_test"
		rtPkg = pkg
	}
	if f.Name.Name == "main" {
		rtPkg = "main"
	}
	rec.Pkg = pkg
	st := &fileState{in: in, fset: fset, tf: fset.File(f.Pos()), src: src, abs: path, rel: rel, pkg: pkg, rtPkg: rtPkg,
		initN: initN, lineCnt: map[int]int{}, hot: map[int]string{}, inRange: map[*ast.CallExpr]bool{},
		siteOf: map[*ast.CallExpr]int{}}
	if initN[pkg] == nil {
		initN[pkg] = new(int)
	}

	// pass 1: lines that need column tags
	st.findHotLines(f)
	// pass 2: functions, hooks and sites
	for _, d := range f.Decls {
		switch d := d.(type) {
		case *ast.FuncDecl:
			st.funcDecl(d)
		case *ast.GenDecl:
			st.walk(d, nil)
		}
	}
	if st.in.done {
		for _, d := range f.Decls {
			if fd, ok := d.(*ast.FuncDecl); ok && fd.Body != nil && !hasPragma(fd.Doc, "nosplit", "systemstack", "nowritebarrierrec") {
				st.doneMarkers(fd.Body)
			} else if gd, ok := d.(*ast.GenDecl); ok {
				ast.Inspect(gd, func(n ast.Node) bool {
					if fl, ok := n.(*ast.FuncLit); ok {
						st.doneMarkers(fl.Body)
						return false
					}
					return true
				})
			}
		}
	}
	var hl []int
	for l, why := range st.hot {
		if why != "multi" {
			hl = append(hl, l)
		}
	}
	sort.Ints(hl)
	for _, l := range hl {
		in.hots = append(in.hots, &HotRec{Kind: "hot", File: rel, Line: l, Why: st.hot[l]})
	}
	rec.Hooks = st.hooks
	rec.Sites = len(st.fsites)
	for _, s := range st.fsites {
		if s.Tagged {
			rec.Tags++
		}
	}
	if st.hooks > 0 {
		st.add(st.off(f.Name.End()), "; import __goracle "+strconv.Quote(in.rtImport))
	}
	if len(st.edits) == 0 {
		rec.Status = "instrumented"
		return
	}
	sort.SliceStable(st.edits, func(i, j int) bool {
		if st.edits[i].off != st.edits[j].off {
			return st.edits[i].off > st.edits[j].off
		}
		return st.edits[i].seq > st.edits[j].seq
	})
	out := append([]byte(nil), src...)
	for _, e := range st.edits {
		out = append(out[:e.off], append([]byte(e.text), out[e.off:]...)...)
	}
	check(os.WriteFile(path, out, 0o644))
	rec.Status = "instrumented"
}

func (st *fileState) off(p token.Pos) int { return st.tf.Offset(p) }
func (st *fileState) pos(p token.Pos) token.Position {
	return st.fset.PositionFor(p, false)
}
func (st *fileState) add(off int, text string) {
	st.seq++
	st.edits = append(st.edits, edit{off, st.seq, text})
}

// isConversionForm reports callee expressions that are certainly types.
func isConversionForm(e ast.Expr) bool {
	switch e := e.(type) {
	case *ast.ParenExpr:
		return isConversionForm(e.X)
	case *ast.ArrayType, *ast.ChanType, *ast.FuncType, *ast.MapType, *ast.InterfaceType, *ast.StructType, *ast.StarExpr:
		return true
	}
	return false
}

func (st *fileState) findHotLines(f *ast.File) {
	// functions with defers: their return lines and closing brace host the
	// deferred calls at run time
	var visitFunc func(body *ast.BlockStmt)
	visitFunc = func(body *ast.BlockStmt) {
		if body == nil {
			return
		}
		hasDefer := false
		var rets []int
		ast.Inspect(body, func(n ast.Node) bool {
			switch n := n.(type) {
			case *ast.FuncLit:
				visitFunc(n.Body)
				return false
			case *ast.DeferStmt:
				hasDefer = true
			case *ast.ReturnStmt:
				rets = append(rets, st.pos(n.Pos()).Line)
			}
			return true
		})
		if hasDefer {
			for _, l := range rets {
				st.hot[l] = "exit"
			}
			st.hot[st.pos(body.Rbrace).Line] = "exit"
		}
	}
	ast.Inspect(f, func(n ast.Node) bool {
		switch n := n.(type) {
		case *ast.FuncDecl:
			visitFunc(n.Body)
		case *ast.CallExpr:
			if !isConversionForm(n.Fun) {
				st.lineCnt[st.pos(n.Lparen).Line]++
			}
		case *ast.RangeStmt:
			// range-over-func calls the iterator from the for line
			from, to := st.pos(n.For).Line, st.pos(n.Body.Lbrace).Line
			for l := from; l <= to; l++ {
				if _, ok := st.hot[l]; !ok {
					st.hot[l] = "range"
				}
			}
			if n.X != nil {
				ast.Inspect(n.X, func(m ast.Node) bool {
					if c, ok := m.(*ast.CallExpr); ok {
						st.inRange[c] = true
					}
					return true
				})
			}
		}
		return true
	})
	// FuncLits at package level (var initializers) are visited here
	ast.Inspect(f, func(n ast.Node) bool {
		if g, ok := n.(*ast.GenDecl); ok {
			ast.Inspect(g, func(m ast.Node) bool {
				if fl, ok := m.(*ast.FuncLit); ok {
					visitFunc(fl.Body)
					return false
				}
				return true
			})
			return false
		}
		return true
	})
	for l, c := range st.lineCnt {
		if c >= 2 {
			if _, ok := st.hot[l]; !ok {
				st.hot[l] = "multi"
			}
		}
	}
}

func recvName(fl *ast.FieldList) (base string, ptr, generic bool) {
	if fl == nil || len(fl.List) == 0 {
		return "", false, false
	}
	t := fl.List[0].Type
	if p, ok := t.(*ast.ParenExpr); ok {
		t = p.X
	}
	if s, ok := t.(*ast.StarExpr); ok {
		ptr = true
		t = s.X
	}
	switch x := t.(type) {
	case *ast.IndexExpr:
		generic = true
		t = x.X
	case *ast.IndexListExpr:
		generic = true
		t = x.X
	}
	if id, ok := t.(*ast.Ident); ok {
		base = id.Name
	}
	return
}

func (st *fileState) funcDecl(d *ast.FuncDecl) {
	if d.Body == nil {
		return
	}
	var qname, rtname string
	if d.Recv != nil {
		base, ptr, generic := recvName(d.Recv)
		g := ""
		if generic {
			g = "[...]"
		}
		if ptr {
			qname = st.pkg + ".(*" + base + ")." + d.Name.Name
			rtname = st.rtPkg + ".(*" + base + g + ")." + d.Name.Name
		} else {
			qname = st.pkg + "." + base + "." + d.Name.Name
			rtname = st.rtPkg + "." + base + g + "." + d.Name.Name
		}
	} else {
		qname = st.pkg + "." + d.Name.Name
		rtname = st.rtPkg + "." + d.Name.Name
		if d.Type.TypeParams != nil && len(d.Type.TypeParams.List) > 0 {
			rtname += "[...]"
		}
		if d.Name.Name == "init" {
			n := st.initN[st.pkg]
			qname = st.pkg + ".init." + strconv.Itoa(*n)
			rtname = st.rtPkg + ".init." + strconv.Itoa(*n)
			*n++
		}
	}
	p := st.pos(d.Name.Pos())
	rec := &FuncRec{Kind: "func", Fid: st.in.nextFid, Pkg: st.pkg, QName: qname, RTName: rtname, File: st.rel,
		Line: p.Line, Col: p.Column, EndLine: st.pos(d.Body.Rbrace).Line, Parent: -1}
	st.in.nextFid++
	st.in.funcs = append(st.in.funcs, rec)
	nosplit := hasPragma(d.Doc, "nosplit", "systemstack", "nowritebarrierrec")
	st.body(rec, d.Body, false, !nosplit)
	if d.Recv != nil {
		st.walk(d.Recv, nil)
	}
	st.walk(d.Type, nil)
}

func (st *fileState) body(rec *FuncRec, b *ast.BlockStmt, lit, hook bool) {
	if hook {
		rec.Hooked = true
		st.hooks++
		st.add(st.off(b.Lbrace)+1, "__goracle.E("+strconv.Itoa(rec.Fid)+");")
	}
	ctx := &funcCtx{rec: rec, isLit: lit, retLine: map[int]bool{}}
	st.stack = append(st.stack, ctx)
	for _, s := range b.List {
		st.walk(s, ctx)
	}
	st.stack = st.stack[:len(st.stack)-1]
	if rec.HasDefer {
		lines := map[int]bool{st.pos(b.Rbrace).Line: true}
		for l := range ctx.retLine {
			lines[l] = true
		}
		for l := range lines {
			rec.ExitLines = append(rec.ExitLines, l)
		}
		sort.Ints(rec.ExitLines)
	}
}

// walk visits n inside function ctx (nil at package level), recording call
// sites and descending into func literals as new functions.
func (st *fileState) walk(n ast.Node, ctx *funcCtx) {
	if n == nil {
		return
	}
	stmtOf := map[*ast.CallExpr]string{}
	goLine := map[*ast.CallExpr]int{}
	ast.Inspect(n, func(m ast.Node) bool {
		switch m := m.(type) {
		case *ast.FuncLit:
			st.funcLit(m, ctx)
			return false
		case *ast.ReturnStmt:
			if ctx != nil {
				ctx.retLine[st.pos(m.Pos()).Line] = true
			}
		case *ast.DeferStmt:
			if ctx != nil {
				ctx.rec.HasDefer = true
			}
			stmtOf[m.Call] = "defer"
		case *ast.GoStmt:
			stmtOf[m.Call] = "go"
			goLine[m.Call] = st.pos(m.Go).Line
		case *ast.CallExpr:
			st.site(m, ctx, stmtOf[m], goLine[m])
		}
		return true
	})
}

func (st *fileState) funcLit(fl *ast.FuncLit, ctx *funcCtx) {
	var qname, rtname string
	if ctx == nil {
		// package-level initializer
		n := st.initN[st.pkg+"#glob"]
		if n == nil {
			n = new(int)
			st.initN[st.pkg+"#glob"] = n
		}
		*n++
		qname = st.pkg + ".init.func" + strconv.Itoa(*n)
		rtname = st.rtPkg + ".init.func" + strconv.Itoa(*n)
	} else {
		ctx.litGen++
		sep := ".func"
		if ctx.isLit {
			sep = "."
		}
		qname = ctx.rec.QName + sep + strconv.Itoa(ctx.litGen)
		rtname = ctx.rec.RTName + sep + strconv.Itoa(ctx.litGen)
	}
	p := st.pos(fl.Type.Func)
	parent := -1
	if ctx != nil {
		parent = ctx.rec.Fid
	}
	rec := &FuncRec{Kind: "func", Fid: st.in.nextFid, Pkg: st.pkg, QName: qname, RTName: rtname, File: st.rel,
		Line: p.Line, Col: p.Column, EndLine: st.pos(fl.Body.Rbrace).Line, Lit: true, Parent: parent}
	st.in.nextFid++
	st.in.funcs = append(st.in.funcs, rec)
	st.walk(fl.Type, ctx)
	st.body(rec, fl.Body, true, true)
}

func calleeName(e ast.Expr) (name, form string, p token.Pos) {
	switch x := e.(type) {
	case *ast.Ident:
		return x.Name, "ident", x.Pos()
	case *ast.SelectorExpr:
		return x.Sel.Name, "selector", x.Sel.Pos()
	case *ast.IndexExpr:
		n, _, pp := calleeName(x.X)
		return n, "index", pp
	case *ast.IndexListExpr:
		n, _, pp := calleeName(x.X)
		return n, "index", pp
	case *ast.ParenExpr:
		return calleeName(x.X)
	case *ast.FuncLit:
		return "func", "funclit", x.Pos()
	case *ast.CallExpr:
		return "", "callresult", x.Pos()
	}
	return "", "other", e.Pos()
}

func (st *fileState) site(c *ast.CallExpr, ctx *funcCtx, stmt string, goLine int) {
	if isConversionForm(c.Fun) {
		return
	}
	if stmt == "" {
		stmt = "call"
	}
	name, form, np := calleeName(c.Fun)
	p := st.pos(np)
	lp := st.pos(c.Lparen)
	fid := -1
	if ctx != nil {
		fid = ctx.rec.Fid
	}
	s := &SiteRec{Kind: "site", Sid: st.in.nextSid, File: st.rel, Line: p.Line, Col: p.Column, Name: name, Form: form,
		LLine: lp.Line, LCol: lp.Column, Fid: fid, Stmt: stmt, GoLine: goLine, Hot: st.hot[lp.Line], InRange: st.inRange[c]}
	st.in.nextSid++
	st.siteOf[c] = s.Sid
	st.in.sites = append(st.in.sites, s)
	st.fsites = append(st.fsites, s)
	if stmt == "defer" && ctx != nil {
		ctx.rec.Defers = append(ctx.rec.Defers, s.Sid)
	}
	tag := st.in.tagMode == "all" || (st.in.tagMode == "needed" && s.Hot != "")
	if tag {
		s.Tagged = true
		o := st.off(c.Lparen)
		st.add(o, fmt.Sprintf("/*line %s#%d:%d:%d*/", st.abs, s.Sid, lp.Line, lp.Column))
		st.add(o+1, fmt.Sprintf("/*line %s:%d:%d*/", st.abs, lp.Line, lp.Column+1))
	}
}

// doneMarkers puts a completion counter after every simple statement of a
// statement list that holds unconditional call sites.
func (st *fileState) doneMarkers(body ast.Node) {
	ast.Inspect(body, func(n ast.Node) bool {
		var list []ast.Stmt
		switch n := n.(type) {
		case *ast.BlockStmt:
			list = n.List
		case *ast.CaseClause:
			list = n.Body
		case *ast.CommClause:
			list = n.Body
		}
		for _, s := range list {
			if l, ok := s.(*ast.LabeledStmt); ok {
				s = l.Stmt
			}
			switch s.(type) {
			case *ast.ExprStmt, *ast.AssignStmt, *ast.IncDecStmt, *ast.SendStmt, *ast.DeclStmt:
			default:
				continue
			}
			if d, ok := s.(*ast.DeclStmt); ok {
				if g, ok := d.Decl.(*ast.GenDecl); !ok || g.Tok != token.VAR {
					continue
				}
			}
			var sids []int
			var visit func(m ast.Node) bool
			visit = func(m ast.Node) bool {
				switch m := m.(type) {
				case *ast.FuncLit:
					return false
				case *ast.BinaryExpr:
					if m.Op == token.LAND || m.Op == token.LOR {
						ast.Inspect(m.X, visit)
						return false
					}
				case *ast.CallExpr:
					if sid, ok := st.siteOf[m]; ok {
						sids = append(sids, sid)
					}
				}
				return true
			}
			ast.Inspect(s, visit)
			if len(sids) == 0 {
				continue
			}
			r := &StmtRec{Kind: "stmt", ID: st.in.nextStmt, File: st.rel, Line: st.pos(s.Pos()).Line, Sites: sids}
			st.in.nextStmt++
			st.in.stmts = append(st.in.stmts, r)
			st.add(st.off(s.End()), "; __goracle.D("+strconv.Itoa(r.ID)+")")
			st.hooks++ // the file needs the import
		}
		return true
	})
}
