package resolve

import (
	"go/ast"
	"go/token"
	"go/types"
	"math"
	"path/filepath"
	"regexp"
	"strconv"

	"golang.org/x/tools/go/packages"
)

// brokenDecl reports whether the package that declares t's base type may
// have lost declarations (R43): a method or an embedded field of a type
// there could be missing, so a member deeper in an embedding tree may
// wrongly win a selection. Types without a declaring package (the
// predeclared error) are whole.
func (pr *program) brokenDecl(t types.Type) bool {
	n, ok := types.Unalias(deref(types.Unalias(t))).(*types.Named)
	if !ok {
		return false
	}
	pkg := n.Origin().Obj().Pkg()
	if pkg == nil {
		return false
	}
	p := pr.byTypes[pkg]
	if p == nil {
		return true // not a package of this load: nothing vouches for it
	}
	return pr.brokenPkg(p)
}

// brokenPkg reports whether p may have lost declarations: it has syntax
// errors (the parser drops what an error swallows), or the type checker
// dropped declarations of a whole file (another package clause) or an
// embedded field of a struct type (a duplicate field name). Other errors
// keep every declaration in place: an import that cannot be resolved, an
// unknown name in a body or in a type, a method whose receiver names no type
// of the package. The result is cached per package variant.
func (pr *program) brokenPkg(p *packages.Package) bool {
	if b, ok := pr.broken[p]; ok {
		return b
	}
	b := declarationsLost(p)
	pr.broken[p] = b
	return b
}

func declarationsLost(p *packages.Package) bool {
	for _, e := range p.Errors {
		if e.Kind == packages.ParseError {
			return true
		}
	}
	if len(p.Errors) == 0 {
		return false
	}
	info := p.TypesInfo
	if info == nil {
		return true // errors and nothing to check them against
	}
	for _, f := range p.Syntax {
		if _, ok := info.Scopes[f]; !ok {
			return true // a file the type checker ignored
		}
	}
	return droppedEmbedded(p)
}

// droppedEmbedded reports whether the type checker dropped an embedded field
// of a struct type of p (a duplicate field name).
func droppedEmbedded(p *packages.Package) bool {
	info := p.TypesInfo
	for _, f := range p.Syntax {
		for _, d := range f.Decls {
			g, ok := d.(*ast.GenDecl)
			if !ok || g.Tok != token.TYPE {
				continue
			}
			for _, s := range g.Specs {
				ts, ok := s.(*ast.TypeSpec)
				if !ok {
					continue
				}
				st, ok := ts.Type.(*ast.StructType)
				if !ok {
					continue
				}
				tn, ok := info.Defs[ts.Name].(*types.TypeName)
				if !ok {
					continue
				}
				if u, ok := tn.Type().Underlying().(*types.Struct); ok && embeddedFields(u) < declaredEmbedded(st) {
					return true
				}
			}
		}
	}
	return false
}

// declaredEmbedded counts the embedded fields a struct type literal declares.
func declaredEmbedded(st *ast.StructType) int {
	n := 0
	for _, f := range st.Fields.List {
		if len(f.Names) == 0 {
			n++
		}
	}
	return n
}

func embeddedFields(st *types.Struct) int {
	n := 0
	for i := 0; i < st.NumFields(); i++ {
		if st.Field(i).Embedded() {
			n++
		}
	}
	return n
}

// typeOrMethod matches source text that may declare a type or a method.
var typeOrMethod = regexp.MustCompile(`\btype\b|\bfunc\s*\(`)

// lossBound says what a package that may have lost declarations (R43) could
// have lost that completes a type: declarations naming types of the packages
// it can name, itself and its imports, direct or not (paths); or anything
// (all), when the loss may have taken an import: a syntax error in a file's
// header, or errors without type information. The go command reads imports
// from the file headers, so an error after them leaves the import list whole.
type lossBound struct {
	all   bool
	paths map[string]bool
}

// lossBound returns p's bound, or nil when p cannot have lost a declaration
// that completes a type: its only losses are in files that declare no type
// and no method (for a file with syntax errors, whose text cannot).
func (pr *program) lossBound(p *packages.Package) *lossBound {
	b := &lossBound{paths: map[string]bool{}}
	add := func(q *packages.Package) {
		for path := range pr.universe(q) {
			b.paths[path] = true
		}
	}
	add(p)
	info := p.TypesInfo
	if info == nil {
		b.all = true
		return b
	}
	may := droppedEmbedded(p)
	files := map[string]*ast.File{}
	for _, f := range p.Syntax {
		files[filepath.Clean(pr.fset.PositionFor(f.Package, false).Filename)] = f
		if _, ok := info.Scopes[f]; ok || !declaresTypes(f) {
			continue
		}
		// a file the type checker ignored for its package clause: its
		// imports need not be p's
		may = true
		for _, spec := range f.Imports {
			path, err := strconv.Unquote(spec.Path.Value)
			if err != nil {
				b.all = true
				return b
			}
			if q := p.Imports[path]; q != nil {
				add(q)
			} else if q := pr.byPath[path]; q != nil {
				add(q)
			}
		}
	}
	for _, e := range p.Errors {
		if e.Kind != packages.ParseError {
			continue
		}
		file, line := splitPos(e.Pos)
		if file != "" && !filepath.IsAbs(file) {
			file = filepath.Join(pr.opts.Dir, file)
		}
		file = filepath.Clean(file)
		if text, ok := pr.typeText[file]; ok && !text {
			continue // nothing in this file can declare a type or a method
		}
		may = true
		if f := files[file]; f == nil || line < pr.firstDeclLine(f) {
			b.all = true
			return b
		}
	}
	if !may {
		return nil
	}
	return b
}

// declaresTypes reports whether a file declares a type or a method.
func declaresTypes(f *ast.File) bool {
	for _, d := range f.Decls {
		switch d := d.(type) {
		case *ast.FuncDecl:
			if d.Recv != nil {
				return true
			}
		case *ast.GenDecl:
			if d.Tok == token.TYPE {
				return true
			}
		}
	}
	return false
}

// firstDeclLine is the line of a file's first declaration after its imports
// (a BadDecl does not count), or past the end when there is none.
func (pr *program) firstDeclLine(f *ast.File) int {
	for _, d := range f.Decls {
		switch d := d.(type) {
		case *ast.FuncDecl:
			return pr.fset.PositionFor(d.Pos(), false).Line
		case *ast.GenDecl:
			if d.Tok != token.IMPORT {
				return pr.fset.PositionFor(d.Pos(), false).Line
			}
		}
	}
	return math.MaxInt
}

// mayLose reports whether a declaration an in-repo package may have lost
// (R43) could supply a method of the interface it. A type missing a method,
// or the embedded field that promotes one, never meets the candidate search
// (candidatesOf), and neither does a type that embeds it, so the candidate
// set would look closed. A lost declaration can name only types of packages
// its own package can name, and an unexported method only of such a package.
func (ix *typeIndex) mayLose(it *types.Interface) bool {
	if len(ix.losses) == 0 {
		return false
	}
	need := make([]map[string]bool, it.NumMethods())
	for i := range need {
		need[i] = methodPkgs(it.Method(i))
	}
	for _, b := range ix.losses {
		if b.all {
			return true
		}
		for _, pkgs := range need {
			if b.names(pkgs) {
				return true
			}
		}
	}
	return false
}

// names reports whether every package of pkgs is within the bound.
func (b *lossBound) names(pkgs map[string]bool) bool {
	for path := range pkgs {
		if !b.paths[path] {
			return false
		}
	}
	return true
}

// methodPkgs lists the packages a declaration must be able to name to have
// method m with m's signature: those of the named types in the signature,
// those of unexported fields and methods of type literals in it, and m's own
// when m is unexported.
func methodPkgs(m *types.Func) map[string]bool {
	out := map[string]bool{}
	if !m.Exported() && m.Pkg() != nil {
		out[m.Pkg().Path()] = true
	}
	typePkgs(m.Type(), out, map[types.Type]bool{})
	return out
}

func typePkgs(t types.Type, out map[string]bool, seen map[types.Type]bool) {
	if t == nil || seen[t] {
		return
	}
	seen[t] = true
	switch x := t.(type) {
	case *types.Alias:
		typePkgs(types.Unalias(x), out, seen)
	case *types.Named:
		if p := x.Obj().Pkg(); p != nil {
			out[p.Path()] = true
		}
		for i := 0; i < x.TypeArgs().Len(); i++ {
			typePkgs(x.TypeArgs().At(i), out, seen)
		}
	case *types.TypeParam:
		if p := x.Obj().Pkg(); p != nil {
			out[p.Path()] = true
		}
	case *types.Pointer:
		typePkgs(x.Elem(), out, seen)
	case *types.Slice:
		typePkgs(x.Elem(), out, seen)
	case *types.Array:
		typePkgs(x.Elem(), out, seen)
	case *types.Chan:
		typePkgs(x.Elem(), out, seen)
	case *types.Map:
		typePkgs(x.Key(), out, seen)
		typePkgs(x.Elem(), out, seen)
	case *types.Signature:
		for _, tup := range []*types.Tuple{x.Params(), x.Results()} {
			for i := 0; i < tup.Len(); i++ {
				typePkgs(tup.At(i).Type(), out, seen)
			}
		}
	case *types.Struct:
		for i := 0; i < x.NumFields(); i++ {
			f := x.Field(i)
			if !f.Exported() && f.Pkg() != nil {
				out[f.Pkg().Path()] = true
			}
			typePkgs(f.Type(), out, seen)
		}
	case *types.Interface:
		for i := 0; i < x.NumExplicitMethods(); i++ {
			m := x.ExplicitMethod(i)
			if !m.Exported() && m.Pkg() != nil {
				out[m.Pkg().Path()] = true
			}
			typePkgs(m.Type(), out, seen)
		}
		for i := 0; i < x.NumEmbeddeds(); i++ {
			typePkgs(x.EmbeddedType(i), out, seen)
		}
	case *types.Union:
		for i := 0; i < x.Len(); i++ {
			typePkgs(x.Term(i).Type(), out, seen)
		}
	}
}
