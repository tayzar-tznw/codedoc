package resolve

import (
	"go/ast"
	"go/token"
	"go/types"

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
