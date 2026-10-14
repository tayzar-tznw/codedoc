package extract

import (
	"go/ast"
	"go/parser"
	"go/token"
	"sort"
)

// parseMode is the go/parser mode of the extractor. go/packages parses with
// AllErrors|ParseComments; SkipObjectResolution changes no node or position,
// so both see the same syntax tree.
const parseMode = parser.ParseComments | parser.AllErrors | parser.SkipObjectResolution

// Site is one call site as both `gohelper extract` and `gohelper resolve`
// enumerate it (spec §4.3). The byte offset of Pos is the join key between
// the two outputs (C2, C3).
//
// Every call expression is a site, conversions included, because syntax
// cannot tell T(x) from f(x). Not sites: the outer call of f()(x) (the inner
// call is one), calls inside array-type lengths and calls inside const
// declarations (both are evaluated at compile time). A range statement over
// an identifier, selector, index expression or func literal is a candidate
// site: syntax cannot tell a range over a func from a range over a slice.
type Site struct {
	Kind  string         // "call", "go", "defer" or "range"
	Shape string         // "ident", "selector", "index", "funclit", "paren" or "other"
	Name  string         // the callee token's text; "func" for funclit; "" for paren and other
	Pos   token.Pos      // the callee token
	End   token.Pos      // end of the callee token (exclusive)
	Call  *ast.CallExpr  // the call; nil for range sites
	Range *ast.RangeStmt // the range statement; nil for call sites
	Recv  ast.Expr       // the selector operand (selector shape, or index shape with a selector base), else nil
}

// Sites returns every site of f, ordered by token position. It applies no
// syntax-error rule: extract drops declarations from the first error line on,
// so it reports a subset of these in a file with errors.
func Sites(f *ast.File) []Site {
	var out []Site
	w := &walker{site: func(s Site) { out = append(out, s) }}
	for _, d := range f.Decls {
		switch d := d.(type) {
		case *ast.FuncDecl:
			if d.Body != nil {
				w.walk(d.Body)
			}
		case *ast.GenDecl:
			if d.Tok != token.VAR {
				continue
			}
			for _, spec := range d.Specs {
				if vs, ok := spec.(*ast.ValueSpec); ok {
					for _, v := range vs.Values {
						w.walk(v)
					}
				}
			}
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].Pos < out[j].Pos })
	return out
}

// walker visits the sites below a node in pre-order. Function bodies and
// package-level var initializers are walked by its callers; func literals are
// reported through enter/leave so callers can track the running function.
type walker struct {
	site  func(Site)
	enter func(*ast.FuncLit) bool // before a func literal's body; false skips the body
	leave func(*ast.FuncLit)      // after the body
	stmt  *ast.CallExpr           // the call of the go or defer statement being visited
	kind  string                  // "go" or "defer" for stmt
}

func (w *walker) walk(n ast.Node) {
	ast.Inspect(n, w.visit)
}

func (w *walker) visit(n ast.Node) bool {
	switch x := n.(type) {
	case *ast.ArrayType, *ast.StructType, *ast.FuncType, *ast.InterfaceType, *ast.MapType, *ast.ChanType:
		// Types hold no runtime call: the only expressions inside a type are
		// array lengths, which are constant.
		return false
	case *ast.GenDecl:
		// Local declarations: const values are constant, types hold no calls.
		return x.Tok == token.VAR
	case *ast.FuncLit:
		if w.enter == nil || w.enter(x) {
			w.walk(x.Body)
			if w.leave != nil {
				w.leave(x)
			}
		}
		return false
	case *ast.GoStmt:
		w.stmt, w.kind = x.Call, "go"
	case *ast.DeferStmt:
		w.stmt, w.kind = x.Call, "defer"
	case *ast.CallExpr:
		if s, ok := callSite(x); ok {
			if x == w.stmt {
				s.Kind = w.kind
			}
			w.site(s)
		}
	case *ast.RangeStmt:
		if s, ok := rangeSite(x); ok {
			w.site(s)
		}
	}
	return true
}

// callSite returns the site of a call, or false for the outer call of f()(x).
func callSite(call *ast.CallExpr) (Site, bool) {
	s := Site{Kind: "call", Call: call}
	if !callee(&s, call.Fun) {
		switch ast.Unparen(call.Fun).(type) {
		case *ast.CallExpr:
			return s, false
		}
		s.Shape = "other"
		if _, ok := call.Fun.(*ast.ParenExpr); ok {
			s.Shape = "paren"
		}
		s.Pos, s.End = call.Lparen, call.Lparen+1
	}
	return s, true
}

// rangeSite returns the candidate site of a range statement, if its range
// expression names a value the way a callee does.
func rangeSite(r *ast.RangeStmt) (Site, bool) {
	s := Site{Kind: "range", Range: r}
	return s, r.X != nil && callee(&s, r.X)
}

// callee fills the shape, token and receiver of a callee expression whose
// token is an identifier or a func keyword. Parentheses are transparent.
func callee(s *Site, fun ast.Expr) bool {
	switch x := ast.Unparen(fun).(type) {
	case *ast.Ident:
		s.Shape = "ident"
		s.setIdent(x)
	case *ast.SelectorExpr:
		s.Shape = "selector"
		s.setIdent(x.Sel)
		s.Recv = x.X
	case *ast.IndexExpr:
		return indexCallee(s, x.X)
	case *ast.IndexListExpr:
		return indexCallee(s, x.X)
	case *ast.FuncLit:
		s.Shape, s.Name = "funclit", "func"
		s.Pos, s.End = x.Type.Func, x.Type.Func+token.Pos(len("func"))
	default:
		return false
	}
	return true
}

// typeLiteral reports whether a callee, through parentheses and pointer
// stars, is a type literal: an array or slice, map, channel, function,
// interface or struct type. Its call is a conversion by syntax (spec R53). A
// named type is not: (*T)(x) may as well dereference a function pointer.
func typeLiteral(fun ast.Expr) bool {
	for {
		switch x := ast.Unparen(fun).(type) {
		case *ast.StarExpr:
			fun = x.X
		case *ast.ArrayType, *ast.MapType, *ast.ChanType, *ast.FuncType, *ast.InterfaceType, *ast.StructType:
			return true
		default:
			return false
		}
	}
}

// indexCallee handles G[T](x) and m[k](x): the token is the base identifier.
func indexCallee(s *Site, base ast.Expr) bool {
	switch x := ast.Unparen(base).(type) {
	case *ast.Ident:
		s.setIdent(x)
	case *ast.SelectorExpr:
		s.setIdent(x.Sel)
		s.Recv = x.X
	default:
		return false
	}
	s.Shape = "index"
	return true
}

func (s *Site) setIdent(id *ast.Ident) {
	s.Name, s.Pos, s.End = id.Name, id.Pos(), id.End()
}
