package resolve

import (
	"fmt"
	"go/ast"
	"go/token"
	"go/types"
	"path/filepath"
	"strconv"
	"strings"

	"codedoc.local/gohelper/internal/extract"
	"golang.org/x/tools/go/packages"
)

// answer is one package variant's answer for one site.
type answer struct {
	rec     *siteRecord // nil for conversions and notcalls
	path    string      // real path of the site's file
	off     int         // byte offset of the callee token
	conv    bool
	notcall bool
	variant string // package ID
	home    bool   // the variant's directory holds the real file (not a symlink twin)
	normal  bool   // non-test variant

	// interface dispatch, resolved by computeIfaces
	ifaceT types.Type        // dispatch interface (or type-parameter constraint)
	ifaceP *packages.Package // the variant whose universe ifaceT belongs to
	method *types.Func       // the called interface or constraint method
}

// visitor walks one in-repo file of one package variant.
type visitor struct {
	pr   *program
	p    *packages.Package
	info *types.Info
	file *ast.File
	real string // real path of the file
	home bool
	errs bool // the variant has errors
}

// collect walks every accepted (file, package) pair.
func (pr *program) collect() {
	for _, p := range pr.all {
		if p.Module == nil || !p.Module.Main || p.TypesInfo == nil || isTestMain(p) {
			continue
		}
		for _, f := range p.Syntax {
			tf := pr.fset.File(f.FileStart)
			if tf == nil {
				continue
			}
			real, ok := pr.accept(p, tf.Name())
			if !ok {
				continue
			}
			fi := pr.files[real]
			if fi == nil {
				fi = &fileInfo{names: map[string]bool{}, packages: map[string]bool{}}
				pr.files[real] = fi
			}
			fi.names[tf.Name()] = true
			fi.packages[p.ID] = true
			v := &visitor{pr: pr, p: p, info: p.TypesInfo, file: f, real: real,
				home: pr.realpath(p.Dir) == filepath.Dir(real), errs: len(p.Errors) > 0}
			v.walk()
		}
	}
}

// walk classifies every site of the file. The sites come from
// extract.Sites, the enumeration `gohelper extract` uses too, so the
// (relpath, offset) join of C2 and C3 holds by construction.
func (v *visitor) walk() {
	for _, s := range extract.Sites(v.file) {
		a, k := v.newAnswer(s.Pos, s.End, s.Name, s.Kind, s.Shape)
		if s.Range != nil {
			v.rangeSite(s.Range, a)
		} else {
			v.callSite(s.Call, a)
		}
		v.add(k, a)
	}
	for _, spec := range v.file.Imports {
		v.importSpec(spec)
	}
}

// indexBase strips index expressions and parentheses: m[k][j] -> m.
func indexBase(e ast.Expr) ast.Expr {
	for {
		switch x := ast.Unparen(e).(type) {
		case *ast.IndexExpr:
			e = x.X
		case *ast.IndexListExpr:
			e = x.X
		default:
			return x
		}
	}
}

func (v *visitor) newAnswer(pos, end token.Pos, name, siteKind, shape string) (*answer, siteKey) {
	p := v.pr.fset.PositionFor(pos, false)
	rec := &siteRecord{
		Kind: "site", Path: v.real, Offset: p.Offset, Line: p.Line, Col: p.Column,
		EndCol: p.Column + int(end-pos), Name: name, SiteKind: siteKind, Shape: shape,
		PkgErrors: v.errs, Variants: []*Target{}, Build: v.pr.opts.BuildID,
	}
	a := &answer{rec: rec, path: v.real, off: p.Offset, variant: v.p.ID, home: v.home, normal: !isTestVariant(v.p)}
	return a, siteKey{v.real, p.Offset}
}

func (v *visitor) add(k siteKey, a *answer) {
	v.pr.sites[k] = append(v.pr.sites[k], a)
}

// callSite classifies a call; a call of a type is a conversion.
func (v *visitor) callSite(call *ast.CallExpr, a *answer) {
	fun := ast.Unparen(call.Fun)
	if tv, ok := v.info.Types[fun]; ok && tv.IsType() {
		a.rec, a.conv = nil, true
		return
	}
	v.classify(fun, a)
}

// rangeSite classifies the implicit call of `for x := range f` over a
// function or method value (spec §4.3). A range over a known non-func type is
// a notcall; when X's type is unknown (a missing module) the site is unknown,
// so it counts as unresolved rather than vanishing (ruling 2026-10-14).
func (v *visitor) rangeSite(rs *ast.RangeStmt, a *answer) {
	x := ast.Unparen(rs.X)
	tv, ok := v.info.Types[x]
	switch {
	case !ok || tv.Type == nil || isInvalid(tv.Type):
		a.rec.Class = "unknown"
	case tv.IsType() || !isFuncType(tv.Type):
		a.rec, a.notcall = nil, true
	default:
		v.classify(x, a)
	}
}

// classify fills the record for a callee expression (parentheses removed).
func (v *visitor) classify(fun ast.Expr, a *answer) {
	rec := a.rec
	if lit, ok := fun.(*ast.FuncLit); ok {
		rec.Class = "funclit"
		rec.Target = v.closure(lit)
		return
	}
	tv, ok := v.info.Types[fun]
	if ok && tv.IsBuiltin() {
		rec.Class = "builtin"
		return
	}
	if id := calleeIdent(fun); id != nil {
		if _, ok := v.info.Uses[id].(*types.Builtin); ok {
			rec.Class = "builtin"
			return
		}
	}
	if !ok || tv.Type == nil || isInvalid(tv.Type) {
		rec.Class = "unknown"
		return
	}
	switch f := fun.(type) {
	case *ast.Ident:
		v.ident(f, a)
	case *ast.SelectorExpr:
		v.selector(f, a)
	case *ast.IndexExpr, *ast.IndexListExpr:
		v.index(fun, a)
	default:
		v.value(a, "other")
	}
}

func calleeIdent(e ast.Expr) *ast.Ident {
	switch x := e.(type) {
	case *ast.Ident:
		return x
	case *ast.SelectorExpr:
		return x.Sel
	}
	return nil
}

func (v *visitor) value(a *answer, kind string) {
	a.rec.Class = "value"
	a.rec.ValueKind = strp(kind)
}

func varKind(obj *types.Var) string {
	switch obj.Kind() {
	case types.PackageVar:
		return "pkgvar"
	case types.LocalVar:
		return "local"
	case types.ResultVar:
		return "result"
	case types.FieldVar:
		return "field"
	}
	return "param"
}

func (v *visitor) ident(id *ast.Ident, a *answer) {
	switch obj := v.info.Uses[id].(type) {
	case *types.Func:
		v.funcCallee(obj, a)
	case *types.Var:
		v.value(a, varKind(obj))
		if obj.Kind() == types.LocalVar {
			v.aliasProof(obj, a)
		}
	default:
		a.rec.Class = "unknown"
	}
}

func (v *visitor) selector(sel *ast.SelectorExpr, a *answer) {
	if s, ok := v.info.Selections[sel]; ok {
		switch s.Kind() {
		case types.MethodVal:
			v.methodSel(s, a, false)
		case types.MethodExpr:
			v.methodSel(s, a, true)
		case types.FieldVal:
			v.value(a, "field")
			a.rec.PromotedDepth = len(s.Index()) - 1
			if a.rec.PromotedDepth > 0 && v.pr.embedInvalid(s.Recv(), a.rec.PromotedDepth) {
				a.rec.EmbedInvalid = true
			}
		}
		return
	}
	// qualified identifier pkg.Name
	switch obj := v.info.Uses[sel.Sel].(type) {
	case *types.Func:
		v.funcCallee(obj, a)
	case *types.Var:
		v.value(a, varKind(obj))
	default:
		a.rec.Class = "unknown"
	}
}

// index handles G[T](x), pkg.G[T](x), x.M[T](y) and m[k](x).
func (v *visitor) index(fun ast.Expr, a *answer) {
	var base ast.Expr
	switch x := fun.(type) {
	case *ast.IndexExpr:
		base = ast.Unparen(x.X)
	case *ast.IndexListExpr:
		base = ast.Unparen(x.X)
	}
	switch b := base.(type) {
	case *ast.Ident:
		if fn, ok := v.info.Uses[b].(*types.Func); ok {
			v.funcCallee(fn, a)
			return
		}
	case *ast.SelectorExpr:
		if s, ok := v.info.Selections[b]; ok && s.Kind() != types.FieldVal {
			v.methodSel(s, a, s.Kind() == types.MethodExpr)
			return
		}
		if _, ok := v.info.Selections[b]; !ok {
			if fn, ok := v.info.Uses[b.Sel].(*types.Func); ok {
				v.funcCallee(fn, a)
				return
			}
		}
	}
	v.value(a, "index")
}

// funcCallee classifies a call of a package-level function.
func (v *visitor) funcCallee(fn *types.Func, a *answer) {
	rec := a.rec
	orig := fn.Origin()
	if orig.Signature().Recv() != nil {
		rec.Class = "method"
		v.concrete(orig, a)
		return
	}
	rec.Class = "func"
	if !v.pr.realObj(orig) {
		rec.Stub = true
		return
	}
	if d := v.pr.funcDecl(orig); d != nil && d.Body == nil {
		if ln, ok := v.pr.linknameOf(orig); ok {
			rec.Class = "linkname"
			rec.Linkname = strp(ln)
			if obj := v.pr.lookupLinkname(ln); obj != nil {
				rec.Target = v.pr.target(obj)
			}
			return
		}
		rec.Asm = true
	}
	rec.Target = v.pr.target(orig)
}

// concrete fills a concrete method target.
func (v *visitor) concrete(m *types.Func, a *answer) {
	if !v.pr.realObj(m) {
		a.rec.Stub = true
		return
	}
	if d := v.pr.funcDecl(m); d != nil && d.Body == nil {
		a.rec.Asm = true
	}
	a.rec.Target = v.pr.target(m)
}

// methodSel classifies a method value or method expression selection.
func (v *visitor) methodSel(s *types.Selection, a *answer, isExpr bool) {
	rec := a.rec
	m := s.Obj().(*types.Func).Origin()
	depth := len(s.Index()) - 1
	rec.PromotedDepth = depth
	recv := s.Recv()
	if tp, ok := types.Unalias(deref(recv)).(*types.TypeParam); ok {
		rec.Class = "typeparam"
		if !v.pr.realObj(m) {
			rec.Stub = true
			return
		}
		rec.Target = v.pr.target(m)
		if isUniverseError(tp.Constraint()) {
			rec.Class = "builtin" // as for a plain error receiver
			return
		}
		a.ifaceT, a.ifaceP, a.method = tp.Constraint(), v.p, m
		return
	}
	rec.Class = "method"
	if isInterface(m.Signature().Recv().Type()) {
		rec.Class = "iface"
	}
	// Trap 3 / G3: a member of an embedded field whose members are unknown
	// could shadow the selected one at run time.
	if depth > 0 && v.pr.embedInvalid(recv, depth) {
		rec.EmbedInvalid = true
		return
	}
	// Trap 2: a stub's copy of a type lacks the methods of the real type's
	// other files, so the selection may differ from the real program's.
	if !v.pr.realRecv(recv) {
		rec.Stub = true
		return
	}
	if m.Pkg() == nil {
		// Error of the predeclared error interface: class builtin, no
		// candidates (ruling 2026-10-13).
		rec.Class = "builtin"
		rec.Target = v.pr.target(m)
		return
	}
	if rec.Class == "iface" {
		if !v.pr.realObj(m) {
			rec.Stub = true
			return
		}
		rec.Target = v.pr.target(m)
		a.ifaceT, a.ifaceP, a.method = dispatchType(recv, s.Index()), v.p, m
		return
	}
	v.concrete(m, a)
}

// realRecv reports whether the receiver's base type is declared in a real
// package (not in a stub's copy of an attached file).
func (pr *program) realRecv(recv types.Type) bool {
	if n, ok := types.Unalias(deref(types.Unalias(recv))).(*types.Named); ok {
		return pr.realObj(n.Origin().Obj())
	}
	return true
}

// dispatchType is the interface a call dispatches on: the receiver's static
// type, or the first embedded interface field on the selection path.
func dispatchType(recv types.Type, index []int) types.Type {
	t := recv
	if isInterface(t) {
		return t
	}
	for _, i := range index[:len(index)-1] {
		st, ok := deref(t).Underlying().(*types.Struct)
		if !ok || i >= st.NumFields() {
			return nil
		}
		t = st.Field(i).Type()
		if isInterface(t) {
			return t
		}
	}
	return nil
}

// closure is the target of a func literal: its `func` keyword.
func (v *visitor) closure(lit *ast.FuncLit) *Target {
	p := v.pr.fset.PositionFor(lit.Type.Func, false)
	t := &Target{Path: v.pr.realpath(p.Filename), Line: p.Line, Col: p.Column, Name: "func",
		Pkg: v.p.PkgPath, Obj: "closure"}
	if v.p.Types != nil {
		t.Pkg = v.p.Types.Path()
	}
	if v.p.Module != nil {
		t.Module, t.Version = v.p.Module.Path, moduleVersion(v.p.Module)
	}
	return t
}

func (v *visitor) importSpec(spec *ast.ImportSpec) {
	path, err := strconv.Unquote(spec.Path.Value)
	if err != nil {
		return
	}
	p := v.pr.fset.PositionFor(spec.Path.Pos(), false)
	k := siteKey{v.real, p.Offset}
	if prev, ok := v.pr.imports[k]; ok && prev != nil {
		return
	}
	rec := &importRecord{Kind: "import", Path: v.real, Offset: p.Offset, Line: p.Line, Col: p.Column, ImportPath: path}
	switch ip := v.p.Imports[path]; {
	case path == "C":
		rec.Module = "C"
	case ip == nil:
		rec.Error = strp("no package for import " + strconv.Quote(path))
	default:
		if ip.Dir != "" {
			rec.TargetDir = v.pr.realpath(ip.Dir)
		}
		if ip.Module != nil {
			rec.Module, rec.Version = ip.Module.Path, moduleVersion(ip.Module)
		}
		rec.Std = v.pr.isStd(ip)
		if msg := firstListError(ip); msg != "" {
			rec.Error = strp(msg)
		} else if v.pr.isStub(ip) {
			rec.Error = strp("package not found")
		}
	}
	v.pr.imports[k] = rec
}

func firstListError(p *packages.Package) string {
	for _, e := range p.Errors {
		if e.Kind == packages.ListError {
			return e.Msg
		}
	}
	return ""
}

func moduleVersion(m *packages.Module) string {
	if m.Replace != nil {
		return m.Replace.Version
	}
	return m.Version
}

// target describes a declaration. Generic objects must be origins already.
func (pr *program) target(obj types.Object) *Target {
	t := &Target{Name: obj.Name(), Qualname: obj.Name()}
	switch o := obj.(type) {
	case *types.Func:
		t.Obj = "func"
		if recv := o.Signature().Recv(); recv != nil {
			t.Obj = "method"
			if isInterface(recv.Type()) {
				t.Obj = "iface_method"
			}
			t.Recv = recvString(recv.Type())
			t.Qualname = methodQualname(o)
		}
	case *types.Var:
		t.Obj = "var"
	case *types.TypeName:
		t.Obj = "type"
	case *types.Builtin:
		t.Obj = "builtin"
	}
	if obj.Pkg() == nil {
		// Predeclared: a type such as error, or error.Error (class builtin).
		key := obj.Name()
		if t.Obj == "iface_method" {
			key, t.Obj = t.Qualname, "builtin"
		}
		if p, ok := pr.universeDecl(key); ok {
			t.Path, t.Line, t.Col = p.Filename, p.Line, p.Column
		}
		return t
	}
	t.Pkg = obj.Pkg().Path()
	if p := pr.byTypes[obj.Pkg()]; p != nil && p.Module != nil {
		t.Module, t.Version = p.Module.Path, moduleVersion(p.Module)
	}
	if pos := pr.fset.PositionFor(obj.Pos(), false); pos.IsValid() {
		t.Path, t.Line, t.Col = pr.realpath(pos.Filename), pos.Line, pos.Column
	}
	return t
}

// methodQualname is T.M (I.M for an interface method): the receiver's base
// type name without pointer and type parameters, then the method name; ""
// for a method of an interface literal, as for closures (R37).
func methodQualname(f *types.Func) string {
	recv := f.Signature().Recv()
	if recv == nil {
		return f.Name()
	}
	if base := strings.TrimPrefix(recvString(recv.Type()), "*"); base != "" {
		return base + "." + f.Name()
	}
	return ""
}

// isUniverseError reports whether t is the predeclared error type.
func isUniverseError(t types.Type) bool {
	return types.Identical(t, types.Universe.Lookup("error").Type())
}

func recvString(t types.Type) string {
	ptr := ""
	if p, ok := types.Unalias(t).(*types.Pointer); ok {
		ptr, t = "*", p.Elem()
	}
	switch tt := types.Unalias(t).(type) {
	case *types.Named:
		return ptr + tt.Obj().Name()
	case *types.TypeParam:
		return ptr + tt.Obj().Name()
	}
	return ptr
}

// deref removes one pointer; aliases are resolved before and after, so an
// embedded *Alias yields the aliased type itself.
func deref(t types.Type) types.Type {
	t = types.Unalias(t)
	if p, ok := t.(*types.Pointer); ok {
		return types.Unalias(p.Elem())
	}
	return t
}

func isInterface(t types.Type) bool {
	if t == nil {
		return false
	}
	_, ok := types.Unalias(t).Underlying().(*types.Interface)
	return ok
}

func isInvalid(t types.Type) bool {
	b, ok := types.Unalias(t).(*types.Basic)
	return ok && b.Kind() == types.Invalid
}

// isFuncType reports whether ranging over a value of type t calls it: its
// core type is a signature.
func isFuncType(t types.Type) bool {
	t = types.Unalias(t)
	if tp, ok := t.(*types.TypeParam); ok {
		it, ok := tp.Constraint().Underlying().(*types.Interface)
		if !ok || it.NumEmbeddeds() != 1 {
			return false
		}
		e := it.EmbeddedType(0)
		if u, ok := e.(*types.Union); ok {
			if u.Len() != 1 {
				return false
			}
			e = u.Term(0).Type()
		}
		_, ok = e.Underlying().(*types.Signature)
		return ok
	}
	_, ok := t.Underlying().(*types.Signature)
	return ok
}

// identity summarises an answer for the variant-disagreement check.
func (a *answer) identity() string {
	switch {
	case a.conv:
		return "conversion"
	case a.notcall:
		return "notcall"
	}
	r := a.rec
	var b strings.Builder
	fmt.Fprintf(&b, "%s|%s|%s|%t|%t|%t", r.Class, targetKey(r.Target), targetKey(r.Alias), r.Stub, r.EmbedInvalid, r.Asm)
	if r.Linkname != nil {
		b.WriteString("|ln=" + *r.Linkname)
	}
	if a.ifaceT != nil {
		b.WriteString("|if=" + ifaceKey(a.ifaceT))
	}
	return b.String()
}

func targetKey(t *Target) string {
	if t == nil {
		return "-"
	}
	return fmt.Sprintf("%s:%d:%d", t.Path, t.Line, t.Col)
}
