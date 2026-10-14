package main

import (
	"fmt"
	"go/ast"
	"go/build"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"slices"
	"sort"
	"strconv"
	"strings"
)

// Rewrite rules (line numbers never change):
//   - hook: "__goracle.E(fid);" right after the { of every function body and
//     func literal (not in //go:nosplit functions);
//   - import: "; import __goracle "<rt>"" after the package name;
//   - site id: "/*line ABS#SID:L:C*/(/*line ABS:L:C+1*/" around a call's "(",
//     and around the for keyword of a range statement, whose implicit iterator
//     call takes the for keyword's position;
//   - completion counter: "; __goracle.D(id)" after each simple statement of a
//     statement list (never after panic);
//   - reach counter: "__goracle.D(id); " before each such simple statement,
//     each return, if, for, range, switch, go and defer statement and before
//     panic(...);
//   - run counter: "; defer __goracle.D(id)" after each defer statement of a
//     function with two or more defers.

type edit struct {
	off  int
	prio int // at the same offset, 0 sorts before 1: a counter goes before a line directive
	seq  int
	text string
}

type instrumenter struct {
	root     string
	tagMode  string
	tests    bool
	done     bool
	dry      bool // scan only: no file is written
	ctx      build.Context
	nextFid  int
	nextSid  int
	nextStmt int
	funcs    []*FuncRec
	imethods []*IMethodRec
	sites    []*SiteRec
	files    []*FileRec
	hots     []*HotRec
	stmts    []*StmtRec
	modRoots map[string]bool // module roots with at least one instrumented file
	real     map[string]string
	ignored  []string // directories skipped by a go.mod ignore directive
}

func newInstrumenter(root, tagMode string, tags []string) *instrumenter {
	ctx := build.Default
	ctx.GOOS, ctx.GOARCH, ctx.CgoEnabled, ctx.Compiler = "linux", "amd64", false, "gc"
	ctx.BuildTags = tags
	return &instrumenter{root: root, tagMode: tagMode, tests: true, done: true, ctx: ctx,
		modRoots: map[string]bool{}, real: map[string]string{}}
}

// modulePath finds the go.mod above dir (not above the root) and returns the
// module root and the import path of dir.
func (in *instrumenter) modulePath(dir string) (string, string, error) {
	for d := dir; ; d = filepath.Dir(d) {
		data, err := os.ReadFile(filepath.Join(d, "go.mod"))
		if err == nil {
			mod := modLine(string(data))
			if mod == "" {
				return "", "", fmt.Errorf("%s/go.mod has no module line", d)
			}
			rel, _ := filepath.Rel(d, dir)
			if rel == "." {
				return d, mod, nil
			}
			return d, mod + "/" + filepath.ToSlash(rel), nil
		}
		if d == in.root || filepath.Dir(d) == d {
			return "", "", fmt.Errorf("no go.mod above %s", dir)
		}
	}
}

func modLine(gomod string) string {
	for _, line := range strings.Split(gomod, "\n") {
		if i := strings.Index(line, "//"); i >= 0 {
			line = line[:i]
		}
		line = strings.TrimSpace(line)
		if rest, ok := strings.CutPrefix(line, "module"); ok && (rest == "" || rest[0] == ' ' || rest[0] == '\t' || rest[0] == '"') {
			return strings.Trim(strings.TrimSpace(rest), "\"`")
		}
	}
	return ""
}

func (in *instrumenter) pkgDir(dir string) {
	ents, err := os.ReadDir(dir)
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
	if len(plain)+len(tests) == 0 {
		return
	}
	modRoot, ipath, err := in.modulePath(dir)
	if err != nil {
		for _, n := range append(plain, tests...) {
			rel, _ := filepath.Rel(in.root, filepath.Join(dir, n))
			in.files = append(in.files, &FileRec{Kind: "file", File: filepath.ToSlash(rel), Abs: filepath.Join(dir, n),
				Status: "skipped:no-module"})
		}
		return
	}
	sort.Strings(plain)
	sort.Strings(tests)
	// init.N numbering follows the compiler's file order: package files, then
	// in-package test files, counting compiled files only; the external test
	// package counts on its own.
	initN := map[string]*int{}
	for _, n := range append(plain, tests...) {
		if in.file(filepath.Join(dir, n), ipath, initN) {
			in.modRoots[modRoot] = true
		}
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
	in         *instrumenter
	fset       *token.FileSet
	tf         *token.File
	src        []byte
	abs        string
	rel        string
	pkg        string // import path of the package the file belongs to
	rtPkg      string // symbol prefix the runtime uses ("main" for package main)
	edits      []edit
	seq        int
	hooks      int
	initN      map[string]*int
	fsites     []*SiteRec
	lineCnt    map[int]int
	hot        map[int]string
	stack      []*funcCtx
	inRange    map[*ast.CallExpr]bool
	siteOf     map[*ast.CallExpr]int
	rangeOf    map[*ast.RangeStmt]int
	inDefer    map[*ast.CallExpr]bool     // calls in functions with defers: after a recovered panic their deferred calls report any line
	deferOf    map[*ast.CallExpr]*FuncRec // deferred call -> the function whose defer it is
	initK      int                        // init#k
	blankK     int                        // _#k
	pkgLits    int                        // <init>.funcN
	compiled   bool                       // the build configuration compiles this file
	unsafeName string                     // local name of the unsafe import, "" when not imported by name
}

type funcCtx struct {
	rec     *FuncRec
	litGen  int // closures directly inside (for the .funcN / .N suffix)
	isLit   bool
	retLine map[int]bool
}

// file instruments one file and reports whether it was rewritten.
func (in *instrumenter) file(path, ipath string, initN map[string]*int) bool {
	rel, _ := filepath.Rel(in.root, path)
	rel = filepath.ToSlash(rel)
	rec := &FileRec{Kind: "file", File: rel, Abs: path, Pkg: ipath, Real: in.real[rel]}
	in.files = append(in.files, rec)
	match, err := in.ctx.MatchFile(filepath.Dir(path), filepath.Base(path))
	rec.Build = err == nil && match
	src, err := os.ReadFile(path)
	check(err)
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, path, src, parser.ParseComments|parser.SkipObjectResolution)
	if err != nil {
		rec.Status = "skipped:parse"
		return false
	}
	for _, im := range f.Imports {
		if im.Path.Value == `"C"` {
			rec.Status = "skipped:cgo"
			return false
		}
	}
	if hasLineDirective(f) {
		rec.Status = "skipped:line-directives"
		return false
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
		siteOf: map[*ast.CallExpr]int{}, rangeOf: map[*ast.RangeStmt]int{}, inDefer: map[*ast.CallExpr]bool{},
		deferOf:  map[*ast.CallExpr]*FuncRec{},
		compiled: rec.Build}
	if !st.compiled {
		// a file the configuration does not compile must not shift the
		// runtime's init.N numbering of the compiled files
		st.initN = map[string]*int{}
	}
	for _, im := range f.Imports {
		if im.Path.Value == `"unsafe"` {
			st.unsafeName = "unsafe"
			if im.Name != nil {
				st.unsafeName = im.Name.Name
			}
		}
	}
	if st.initN[pkg] == nil {
		st.initN[pkg] = new(int)
	}

	// pass 1: lines that need column tags
	st.findHotLines(f)
	// pass 2: functions, hooks, interface methods and sites
	for _, d := range f.Decls {
		switch d := d.(type) {
		case *ast.FuncDecl:
			st.funcDecl(d)
		case *ast.GenDecl:
			st.genDecl(d)
		}
	}
	// pass 3: completion and reach counters
	if in.done {
		for _, d := range f.Decls {
			if fd, ok := d.(*ast.FuncDecl); ok && fd.Body != nil && !hasPragma(fd.Doc, "nosplit", "systemstack", "nowritebarrierrec") {
				st.counters(fd.Body)
			} else if gd, ok := d.(*ast.GenDecl); ok {
				ast.Inspect(gd, func(n ast.Node) bool {
					if fl, ok := n.(*ast.FuncLit); ok {
						st.counters(fl.Body)
						return false
					}
					return true
				})
			}
		}
	}
	var hl []int
	for l, why := range st.hot {
		if why != "multi" && why != "deferfn" {
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
	rec.Status = "instrumented"
	if len(st.edits) == 0 {
		return false
	}
	// tags alone need no import: a file without function bodies (only
	// package-level initializers) still gets its site ids
	if st.hooks > 0 {
		st.add(st.off(f.Name.End()), "; import __goracle "+strconv.Quote(rtModule))
	}
	sort.SliceStable(st.edits, func(i, j int) bool {
		a, b := st.edits[i], st.edits[j]
		if a.off != b.off {
			return a.off < b.off
		}
		if a.prio != b.prio {
			return a.prio < b.prio
		}
		return a.seq < b.seq
	})
	out := make([]byte, 0, len(src)+len(st.edits)*64)
	prev := 0
	for _, e := range st.edits {
		out = append(out, src[prev:e.off]...)
		out = append(out, e.text...)
		prev = e.off
	}
	out = append(out, src[prev:]...)
	if !in.dry {
		check(os.WriteFile(path, out, 0o644))
	}
	return st.hooks > 0
}

func (st *fileState) off(p token.Pos) int { return st.tf.Offset(p) }
func (st *fileState) pos(p token.Pos) token.Position {
	return st.fset.PositionFor(p, false)
}
func (st *fileState) add(off int, text string) { st.addPrio(off, 1, text) }
func (st *fileState) addPrio(off, prio int, text string) {
	st.seq++
	st.edits = append(st.edits, edit{off, prio, st.seq, text})
}

// isConversionForm reports callee expressions that are certainly types: type
// literals and pointers to them. (*T)(x) is not among them: syntax cannot
// tell it from the call (*fp)(x).
func isConversionForm(e ast.Expr) bool {
	switch e := e.(type) {
	case *ast.ParenExpr:
		return isConversionForm(e.X)
	case *ast.StarExpr:
		return isConversionForm(e.X)
	case *ast.ArrayType, *ast.ChanType, *ast.FuncType, *ast.MapType, *ast.InterfaceType, *ast.StructType:
		return true
	}
	return false
}

// compileTimeCall reports unsafe.Sizeof, Alignof and Offsetof: constants
// whose arguments are never evaluated. Calls in their arguments stay sites
// (they never run), but no counter covers them.
func (st *fileState) compileTimeCall(c *ast.CallExpr) bool {
	sel, ok := c.Fun.(*ast.SelectorExpr)
	if !ok || st.unsafeName == "" {
		return false
	}
	id, ok := sel.X.(*ast.Ident)
	return ok && id.Name == st.unsafeName && (sel.Sel.Name == "Sizeof" || sel.Sel.Name == "Alignof" || sel.Sel.Name == "Offsetof")
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
			for _, l := range st.rangeExits(body) {
				st.hot[l] = "exit"
			}
			st.hot[st.pos(body.Rbrace).Line] = "exit"
			ast.Inspect(body, func(n ast.Node) bool {
				switch n := n.(type) {
				case *ast.FuncLit:
					return false
				case *ast.CallExpr:
					st.inDefer[n] = true
				}
				return true
			})
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

// rangeExits lists the closing-brace lines of the range loops that enclose a
// return statement of body (not inside func literals). A return inside a
// range-over-func loop runs in the compiler's body closure; the function
// itself returns, and runs its deferred calls, at the closing brace of the
// loop (cmd/compile rangefunc rewrite: the checks after the iterator call).
// Over-including loops over slices or maps is harmless: only untagged
// entries at those lines go to the defer group.
func (st *fileState) rangeExits(body *ast.BlockStmt) []int {
	var lines []int
	var stack []ast.Node
	ast.Inspect(body, func(n ast.Node) bool {
		if n == nil {
			stack = stack[:len(stack)-1]
			return true
		}
		if _, ok := n.(*ast.FuncLit); ok {
			return false
		}
		stack = append(stack, n)
		if _, ok := n.(*ast.ReturnStmt); ok {
			for _, a := range stack {
				if r, ok := a.(*ast.RangeStmt); ok {
					lines = append(lines, st.pos(r.Body.Rbrace).Line)
				}
			}
		}
		return true
	})
	return lines
}

func recvName(fl *ast.FieldList) (base string, ptr, generic bool) {
	if fl == nil || len(fl.List) == 0 {
		return "", false, false
	}
	t := fl.List[0].Type
	for {
		p, ok := t.(*ast.ParenExpr)
		if !ok {
			break
		}
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

func (st *fileState) genDecl(d *ast.GenDecl) {
	if d.Tok == token.TYPE {
		for _, spec := range d.Specs {
			ts, ok := spec.(*ast.TypeSpec)
			if !ok {
				continue
			}
			it, ok := ts.Type.(*ast.InterfaceType)
			if !ok || it.Methods == nil {
				continue
			}
			for _, m := range it.Methods.List {
				if _, isFunc := m.Type.(*ast.FuncType); !isFunc {
					continue
				}
				for _, name := range m.Names {
					p := st.pos(name.Pos())
					st.in.imethods = append(st.in.imethods, &IMethodRec{Kind: "imethod", Pkg: st.pkg, Iface: ts.Name.Name,
						Name: name.Name, Qualname: ts.Name.Name + "." + name.Name,
						QName: st.pkg + "." + ts.Name.Name + "." + name.Name, File: st.rel, Line: p.Line, Col: p.Column})
				}
			}
		}
	}
	if d.Tok == token.CONST {
		return // constant expressions run no calls
	}
	st.walk(d, nil)
}

func (st *fileState) funcDecl(d *ast.FuncDecl) {
	var qname, rtname, qual, recv string
	var ptr bool
	name := d.Name.Name
	if d.Recv != nil {
		base, isPtr, generic := recvName(d.Recv)
		recv, ptr = base, isPtr
		g := ""
		if generic {
			g = "[...]"
		}
		if ptr {
			qname = st.pkg + ".(*" + base + ")." + name
			rtname = st.rtPkg + ".(*" + base + g + ")." + name
		} else {
			qname = st.pkg + "." + base + "." + name
			rtname = st.rtPkg + "." + base + g + "." + name
		}
		qual = base + "." + name
	} else {
		qname = st.pkg + "." + name
		rtname = st.rtPkg + "." + name
		qual = name
		if d.Type.TypeParams != nil && len(d.Type.TypeParams.List) > 0 {
			rtname += "[...]"
		}
		switch name {
		case "init":
			st.initK++
			qual = "init#" + strconv.Itoa(st.initK)
			if d.Body != nil {
				n := st.initN[st.pkg]
				qname = st.pkg + ".init." + strconv.Itoa(*n)
				rtname = st.rtPkg + ".init." + strconv.Itoa(*n)
				*n++
			}
		case "_":
			st.blankK++
			qual = "_#" + strconv.Itoa(st.blankK)
		}
	}
	p := st.pos(d.Name.Pos())
	end := p.Line
	if d.Body != nil {
		end = st.pos(d.Body.Rbrace).Line
	}
	rec := &FuncRec{Kind: "func", Fid: st.in.nextFid, Pkg: st.pkg, QName: qname, RTName: rtname, Qualname: qual,
		Name: name, Recv: recv, Ptr: ptr, File: st.rel, Line: p.Line, Col: p.Column, EndLine: end, Parent: -1}
	st.in.nextFid++
	st.in.funcs = append(st.in.funcs, rec)
	if d.Body == nil {
		return // assembly or linkname declaration: no hook, edges to it are unverifiable
	}
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
		for _, l := range st.rangeExits(b) {
			lines[l] = true
		}
		for l := range lines {
			rec.ExitLines = append(rec.ExitLines, l)
		}
		sort.Ints(rec.ExitLines)
	}
}

// walk visits n inside function ctx (nil at package level), recording call
// sites and range sites and descending into func literals as new functions.
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
		case *ast.RangeStmt:
			st.rangeSite(m, ctx)
		case *ast.ArrayType:
			// a call in an array length is a constant, never a run-time call (spec §4.3)
			st.walk(m.Elt, ctx)
			return false
		case *ast.CallExpr:
			st.site(m, ctx, stmtOf[m], goLine[m])
		}
		return true
	})
}

func (st *fileState) funcLit(fl *ast.FuncLit, ctx *funcCtx) {
	var qname, rtname, qual string
	if ctx == nil {
		// package-level initializer
		n := st.initN[st.pkg+"#glob"]
		if n == nil {
			n = new(int)
			st.initN[st.pkg+"#glob"] = n
		}
		*n++
		st.pkgLits++
		qname = st.pkg + ".init.func" + strconv.Itoa(*n)
		rtname = st.rtPkg + ".init.func" + strconv.Itoa(*n)
		qual = "<init>.func" + strconv.Itoa(st.pkgLits)
	} else {
		ctx.litGen++
		sep := ".func"
		if ctx.isLit {
			sep = "."
		}
		qname = ctx.rec.QName + sep + strconv.Itoa(ctx.litGen)
		rtname = ctx.rec.RTName + sep + strconv.Itoa(ctx.litGen)
		qual = ctx.rec.Qualname + ".func" + strconv.Itoa(ctx.litGen)
	}
	p := st.pos(fl.Type.Func)
	parent := -1
	if ctx != nil {
		parent = ctx.rec.Fid
	}
	rec := &FuncRec{Kind: "func", Fid: st.in.nextFid, Pkg: st.pkg, QName: qname, RTName: rtname, Qualname: qual,
		Name: "func", File: st.rel, Line: p.Line, Col: p.Column, EndLine: st.pos(fl.Body.Rbrace).Line, Lit: true,
		Parent: parent}
	st.in.nextFid++
	st.in.funcs = append(st.in.funcs, rec)
	st.walk(fl.Type, ctx)
	st.body(rec, fl.Body, true, true)
}

// calleeOf returns the callee token of a call the way extract.Sites does
// (master plan C2, helper-extract ruling): see calleeToken; for any other
// callee, conversions to type literals included, the token is the call's "("
// and the site has no name. The outer call of f()(x) is no site there;
// goracle keeps it, as form callresult, so that its entries are attributed.
func calleeOf(c *ast.CallExpr) (name, form string, p token.Pos) {
	if name, form, p, ok := calleeToken(c.Fun); ok {
		return name, form, p
	}
	if _, ok := ast.Unparen(c.Fun).(*ast.CallExpr); ok {
		return "", "callresult", c.Lparen
	}
	if isConversionForm(c.Fun) {
		return "", "conversion", c.Lparen // never entered
	}
	if _, ok := c.Fun.(*ast.ParenExpr); ok {
		return "", "paren", c.Lparen
	}
	return "", "other", c.Lparen
}

// calleeToken returns the identifier of an ident, selector or index callee
// (an index base that is an identifier or a selector: G[T](x), m[k](x)) or
// the func keyword of a func literal. Parentheses are transparent.
func calleeToken(e ast.Expr) (name, form string, p token.Pos, ok bool) {
	switch x := ast.Unparen(e).(type) {
	case *ast.Ident:
		return x.Name, "ident", x.Pos(), true
	case *ast.SelectorExpr:
		return x.Sel.Name, "selector", x.Sel.Pos(), true
	case *ast.IndexExpr:
		return indexToken(x.X)
	case *ast.IndexListExpr:
		return indexToken(x.X)
	case *ast.FuncLit:
		return "func", "funclit", x.Type.Func, true
	}
	return "", "", token.NoPos, false
}

func indexToken(base ast.Expr) (name, form string, p token.Pos, ok bool) {
	switch x := ast.Unparen(base).(type) {
	case *ast.Ident:
		return x.Name, "index", x.Pos(), true
	case *ast.SelectorExpr:
		return x.Sel.Name, "index", x.Sel.Pos(), true
	}
	return "", "", token.NoPos, false
}

func (st *fileState) newSite(s *SiteRec) *SiteRec {
	s.Kind, s.Sid, s.File = "site", st.in.nextSid, st.rel
	st.in.nextSid++
	st.in.sites = append(st.in.sites, s)
	st.fsites = append(st.fsites, s)
	return s
}

func (st *fileState) tag(s *SiteRec, off, width int, lp token.Position) {
	if st.in.tagMode == "all" || (st.in.tagMode == "needed" && s.Hot != "") {
		s.Tagged = true
		st.add(off, fmt.Sprintf("/*line %s#%d:%d:%d*/", st.abs, s.Sid, lp.Line, lp.Column))
		st.add(off+width, fmt.Sprintf("/*line %s:%d:%d*/", st.abs, lp.Line, lp.Column+width))
	}
}

func (st *fileState) site(c *ast.CallExpr, ctx *funcCtx, stmt string, goLine int) {
	if stmt == "" {
		stmt = "call"
	}
	name, form, np := calleeOf(c)
	p := st.pos(np)
	lp := st.pos(c.Lparen)
	fid := -1
	if ctx != nil {
		fid = ctx.rec.Fid
	}
	s := st.newSite(&SiteRec{Line: p.Line, Col: p.Column, Name: name, Form: form, LLine: lp.Line, LCol: lp.Column,
		Fid: fid, Stmt: stmt, GoLine: goLine, Hot: st.hot[lp.Line], InRange: st.inRange[c]})
	if s.Hot == "" && st.inDefer[c] {
		s.Hot = "deferfn"
	}
	st.siteOf[c] = s.Sid
	if stmt == "defer" && ctx != nil {
		ctx.rec.Defers = append(ctx.rec.Defers, s.Sid)
		st.deferOf[c] = ctx.rec
	}
	st.tag(s, st.off(c.Lparen), 1, lp)
}

// rangeSite records the implicit iterator call of a range statement. The
// compiler gives that call the position of the for keyword, so the tag goes
// around "for". Its callee token follows the call rule of extract.Sites
// (calleeToken: spec §4.3, C2); a range expression of another shape, where
// extract has no site, gets a site with no name at the expression.
func (st *fileState) rangeSite(r *ast.RangeStmt, ctx *funcCtx) {
	if r.X == nil {
		return
	}
	name, np := "", r.X.Pos()
	if n, _, p, ok := calleeToken(r.X); ok {
		name, np = n, p
	}
	p := st.pos(np)
	fp := st.pos(r.For)
	fid := -1
	if ctx != nil {
		fid = ctx.rec.Fid
	}
	s := st.newSite(&SiteRec{Line: p.Line, Col: p.Column, Name: name, Form: "range", LLine: fp.Line, LCol: fp.Column,
		Fid: fid, Stmt: "range", Hot: "range"})
	st.rangeOf[r] = s.Sid
	st.tag(s, st.off(r.For), len("for"), fp)
}

// unconditional lists the sites of nodes that run whenever the enclosing
// statement runs: not under the right operand of && or ||, not inside func
// literals.
func (st *fileState) unconditional(nodes ...ast.Node) []int {
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
			if st.compileTimeCall(m) {
				return false
			}
		}
		return true
	}
	for _, n := range nodes {
		if n != nil {
			ast.Inspect(n, visit)
		}
	}
	return sids
}

func isPanic(s ast.Stmt) bool {
	if es, ok := s.(*ast.ExprStmt); ok {
		if c, ok := es.X.(*ast.CallExpr); ok {
			if id, ok := c.Fun.(*ast.Ident); ok && id.Name == "panic" {
				return true
			}
		}
	}
	return false
}

// counters puts a completion counter after every simple statement of a
// statement list that holds unconditional call sites, and a reach counter
// before it and before every return and control statement (and panic) that
// does: a statement whose call panics or never returns has started all the
// same.
func (st *fileState) counters(body ast.Node) {
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
		for idx, outer := range list {
			s := outer
			if l, ok := s.(*ast.LabeledStmt); ok {
				s = l.Stmt
			}
			var reach []int
			switch x := s.(type) {
			case *ast.ExprStmt, *ast.AssignStmt, *ast.IncDecStmt, *ast.SendStmt, *ast.DeclStmt:
				if isPanic(s) {
					// panic(...) is a terminating statement; nothing may follow it
					reach = st.startSites(s, st.unconditional(s), true)
					break
				}
				if d, ok := s.(*ast.DeclStmt); ok {
					if g, ok := d.Decl.(*ast.GenDecl); !ok || g.Tok != token.VAR {
						continue
					}
				}
				sids := st.unconditional(s)
				if len(sids) == 0 {
					continue
				}
				r := &StmtRec{Kind: "stmt", ID: st.in.nextStmt, File: st.rel, Line: st.pos(s.Pos()).Line, Sites: sids}
				st.in.nextStmt++
				st.in.stmts = append(st.in.stmts, r)
				st.add(st.off(s.End()), "; __goracle.D("+strconv.Itoa(r.ID)+")")
				st.hooks++ // the file needs the import
				// a call may never return (os.Exit, log.Fatal, t.Fatal, a panic
				// that is recovered further up): a reach counter proves the
				// statement started
				reach = st.startSites(s, sids, idx == len(list)-1)
			case *ast.ReturnStmt:
				for _, e := range x.Results {
					reach = append(reach, st.unconditional(e)...)
				}
			case *ast.IfStmt:
				reach = st.unconditional(x.Init, x.Cond)
			case *ast.SwitchStmt:
				reach = st.unconditional(x.Init, x.Tag)
			case *ast.TypeSwitchStmt:
				reach = st.unconditional(x.Init, x.Assign)
			case *ast.ForStmt:
				reach = st.unconditional(x.Init, x.Cond)
			case *ast.RangeStmt:
				reach = st.unconditional(x.X)
				if sid, ok := st.rangeOf[x]; ok {
					reach = append(reach, sid)
				}
			case *ast.GoStmt:
				reach = st.unconditional(x.Call)
			case *ast.DeferStmt:
				reach = st.unconditional(x.Call)
			}
			if len(reach) == 0 {
				continue
			}
			r := &StmtRec{Kind: "reach", ID: st.in.nextStmt, File: st.rel, Line: st.pos(s.Pos()).Line, Sites: reach}
			st.in.nextStmt++
			st.in.stmts = append(st.in.stmts, r)
			// break and continue need their label on the loop, switch or
			// select itself; any other label is a goto target, and the
			// counter goes after it so that goto runs the counter too
			at := outer.Pos()
			switch s.(type) {
			case *ast.ForStmt, *ast.RangeStmt, *ast.SwitchStmt, *ast.TypeSwitchStmt, *ast.SelectStmt:
			default:
				at = s.Pos()
			}
			st.addPrio(st.off(at), 0, "__goracle.D("+strconv.Itoa(r.ID)+"); ")
			st.hooks++
			if d, ok := s.(*ast.DeferStmt); ok {
				st.runCounter(d)
			}
		}
		return true
	})
}

// runCounter defers a counter right after a defer statement of a function
// with two or more defers. Deferred calls run last in, first out, so it runs
// right before the statement's deferred call, and only if the statement
// registered that call (its arguments evaluated without a panic): a deferred
// call that never ran (never registered, os.Exit, a goroutine still blocked
// at exit) leaves it at 0, while the reach counter before the statement
// proves only that the statement started.
func (st *fileState) runCounter(d *ast.DeferStmt) {
	rec, sid := st.deferOf[d.Call], st.siteOf[d.Call]
	if rec == nil || len(rec.Defers) < 2 {
		return
	}
	r := &StmtRec{Kind: "run", ID: st.in.nextStmt, File: st.rel, Line: st.pos(d.Pos()).Line, Sites: []int{sid}}
	st.in.nextStmt++
	st.in.stmts = append(st.in.stmts, r)
	st.add(st.off(d.End()), "; defer __goracle.D("+strconv.Itoa(r.ID)+")")
	st.hooks++
}

// startSites lists the sites that a reach counter before simple statement s
// vouches for: the call that runs first in evaluation order, which starts
// whenever s starts, and, for the last statement of a block or a panic, also
// the outermost call, which may never return (os.Exit(code()), t.Fatal(...)).
// Any other call of s does not start when an earlier one panics.
func (st *fileState) startSites(s ast.Stmt, sids []int, last bool) []int {
	var out []int
	add := func(c *ast.CallExpr) {
		sid, ok := st.siteOf[c]
		if !ok || !slices.Contains(sids, sid) || slices.Contains(out, sid) {
			return
		}
		out = append(out, sid)
	}
	if c := st.firstCall(s); c != nil {
		add(c)
	}
	if es, ok := s.(*ast.ExprStmt); ok && last {
		if c, ok := ast.Unparen(es.X).(*ast.CallExpr); ok {
			add(c)
		}
	}
	return out
}

// firstCall returns the call of n that runs first: a call runs after its
// function value and its arguments, operands run left to right, and the
// right operand of && and ||, func literal bodies, array lengths and the
// arguments of unsafe.Sizeof, Alignof and Offsetof do not run first (or at
// all).
func (st *fileState) firstCall(n ast.Node) *ast.CallExpr {
	var first *ast.CallExpr
	var visit func(n ast.Node) bool
	visit = func(n ast.Node) bool {
		switch x := n.(type) {
		case nil, *ast.FuncLit, *ast.ArrayType:
			return false
		case *ast.CallExpr:
			if st.compileTimeCall(x) {
				return false
			}
			if visit(x.Fun) {
				return true
			}
			for _, a := range x.Args {
				if visit(a) {
					return true
				}
			}
			first = x
			return true
		case *ast.BinaryExpr:
			if visit(x.X) {
				return true
			}
			if x.Op == token.LAND || x.Op == token.LOR {
				return false
			}
			return visit(x.Y)
		}
		found := false
		ast.Inspect(n, func(c ast.Node) bool {
			if c == n {
				return true
			}
			if !found && c != nil {
				found = visit(c)
			}
			return false
		})
		return found
	}
	visit(n)
	return first
}
