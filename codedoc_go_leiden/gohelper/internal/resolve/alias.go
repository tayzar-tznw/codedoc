package resolve

import (
	"go/ast"
	"go/token"
	"go/types"
)

// varFacts are the binding facts of one variable within one file, from go/types.
type varFacts struct {
	init     ast.Expr // the initializer of `v := x` or `var v = x`
	badInit  bool     // bound by range, by a multi-value call, or declared without a value
	assigned bool     // assigned again anywhere (also inside closures)
	addr     bool     // address taken, explicitly or by a pointer-receiver method
}

type factsKey struct {
	pkg  string
	file *ast.File
}

// varFactsOf computes the facts of every variable of one file, once per
// package variant.
func (v *visitor) varFactsOf() map[*types.Var]*varFacts {
	k := factsKey{v.p.ID, v.file}
	if m, ok := v.pr.facts[k]; ok {
		return m
	}
	m := map[*types.Var]*varFacts{}
	get := func(obj *types.Var) *varFacts {
		f := m[obj]
		if f == nil {
			f = &varFacts{}
			m[obj] = f
		}
		return f
	}
	info := v.info
	ast.Inspect(v.file, func(n ast.Node) bool {
		switch x := n.(type) {
		case *ast.AssignStmt:
			for i, lhs := range x.Lhs {
				id, ok := ast.Unparen(lhs).(*ast.Ident)
				if !ok {
					continue
				}
				if x.Tok == token.DEFINE {
					if obj, ok := info.Defs[id].(*types.Var); ok && obj != nil {
						f := get(obj)
						if len(x.Lhs) == len(x.Rhs) {
							f.init = x.Rhs[i]
						} else {
							f.badInit = true
						}
						continue
					}
				}
				if obj, ok := info.Uses[id].(*types.Var); ok {
					get(obj).assigned = true
				}
			}
		case *ast.ValueSpec:
			for i, id := range x.Names {
				obj, ok := info.Defs[id].(*types.Var)
				if !ok || obj == nil {
					continue
				}
				f := get(obj)
				if len(x.Values) == len(x.Names) {
					f.init = x.Values[i]
				} else {
					f.badInit = true
				}
			}
		case *ast.RangeStmt:
			for _, e := range []ast.Expr{x.Key, x.Value} {
				id, ok := ast.Unparen(e).(*ast.Ident)
				if e == nil || !ok {
					continue
				}
				if obj, ok := info.Defs[id].(*types.Var); ok && obj != nil {
					get(obj).badInit = true
				} else if obj, ok := info.Uses[id].(*types.Var); ok {
					get(obj).assigned = true
				}
			}
		case *ast.UnaryExpr:
			if x.Op == token.AND {
				if id, ok := ast.Unparen(x.X).(*ast.Ident); ok {
					if obj, ok := info.Uses[id].(*types.Var); ok {
						get(obj).addr = true
					}
				}
			}
		case *ast.SelectorExpr:
			id, ok := ast.Unparen(x.X).(*ast.Ident)
			if !ok {
				break
			}
			obj, ok := info.Uses[id].(*types.Var)
			if !ok {
				break
			}
			if s, ok := info.Selections[x]; ok && s.Kind() == types.MethodVal {
				if fn, ok := s.Obj().(*types.Func); ok && fn.Signature().Recv() != nil {
					if _, ptr := types.Unalias(fn.Signature().Recv().Type()).(*types.Pointer); ptr {
						if _, isPtr := types.Unalias(obj.Type()).(*types.Pointer); !isPtr {
							get(obj).addr = true
						}
					}
				}
			}
		}
		return true
	})
	v.pr.facts[k] = m
	return m
}

// aliasProof applies the func-alias rule (spec §5.5) to a call through the
// local variable obj: bound exactly once, by its declaration, to a function,
// a method value or expression, or a func literal; never assigned again and
// never address-taken. A method value with an interface receiver behaves like
// an iface site.
func (v *visitor) aliasProof(obj *types.Var, a *answer) {
	f := v.varFactsOf()[obj]
	if f == nil || f.init == nil || f.badInit || f.assigned || f.addr {
		return
	}
	rec := a.rec
	switch e := ast.Unparen(f.init).(type) {
	case *ast.FuncLit:
		rec.Alias = v.closure(e)
	case *ast.Ident:
		if fn, ok := v.info.Uses[e].(*types.Func); ok {
			v.aliasFunc(fn, a)
		}
	case *ast.SelectorExpr:
		if s, ok := v.info.Selections[e]; ok {
			if s.Kind() != types.FieldVal {
				v.aliasMethod(s, a)
			}
		} else if fn, ok := v.info.Uses[e.Sel].(*types.Func); ok {
			v.aliasFunc(fn, a)
		}
	case *ast.IndexExpr, *ast.IndexListExpr:
		switch b := indexBase(e).(type) {
		case *ast.Ident:
			if fn, ok := v.info.Uses[b].(*types.Func); ok {
				v.aliasFunc(fn, a)
			}
		case *ast.SelectorExpr:
			if s, ok := v.info.Selections[b]; ok {
				if s.Kind() != types.FieldVal {
					v.aliasMethod(s, a)
				}
			} else if fn, ok := v.info.Uses[b.Sel].(*types.Func); ok {
				v.aliasFunc(fn, a)
			}
		}
	}
}

// aliasFunc proves an alias to a package-level function. Body-less
// declarations (assembly, linkname) give no proof.
func (v *visitor) aliasFunc(fn *types.Func, a *answer) {
	orig := fn.Origin()
	if orig.Signature().Recv() != nil {
		return
	}
	if !v.pr.realObj(orig) {
		a.rec.Stub = true
		return
	}
	if d := v.pr.funcDecl(orig); d == nil || d.Body == nil {
		return
	}
	a.rec.Alias = v.pr.target(orig)
}

// aliasMethod proves an alias to a method value or method expression; the
// selected method object decides (R36).
func (v *visitor) aliasMethod(s *types.Selection, a *answer) {
	m := s.Obj().(*types.Func).Origin()
	depth := len(s.Index()) - 1
	recv := s.Recv()
	if depth > 0 && v.pr.embedInvalid(recv, depth) {
		a.rec.EmbedInvalid = true
		return
	}
	if !v.pr.realObj(m) || !v.pr.realRecv(recv) {
		a.rec.Stub = true
		return
	}
	if tp, ok := types.Unalias(deref(recv)).(*types.TypeParam); ok {
		a.rec.Alias = v.pr.target(m)
		if !isUniverseError(tp.Constraint()) {
			a.ifaceT, a.ifaceP, a.method = tp.Constraint(), v.p, m
		}
		return
	}
	if m.Pkg() == nil {
		a.rec.Alias = v.pr.target(m) // error.Error: builtin, no candidates
		return
	}
	if isInterface(m.Signature().Recv().Type()) {
		a.rec.Alias = v.pr.target(m)
		a.ifaceT, a.ifaceP, a.method = dispatchType(recv, s.Index()), v.p, m
		return
	}
	if d := v.pr.funcDecl(m); d == nil || d.Body == nil {
		return
	}
	a.rec.Alias = v.pr.target(m)
}

// embedInvalid reports whether the embedding tree of recv has, at a depth no
// greater than depth, an embedded field whose members may be unknown: its
// type is invalid, is a stub's copy of a type, or its method set is
// incomplete. Such a member could shadow the selected one at run time
// (loader trap 3, gate G3, R1). So could a declaration dropped from the
// package of any type in the tree, the receiver's own included (R43).
func (pr *program) embedInvalid(recv types.Type, depth int) bool {
	type item struct {
		t     types.Type
		level int
	}
	start := types.Unalias(deref(types.Unalias(recv)))
	if pr.brokenDecl(start) {
		return true
	}
	st, ok := start.Underlying().(*types.Struct)
	if !ok {
		return false
	}
	var queue []item
	push := func(st *types.Struct, level int) {
		for i := 0; i < st.NumFields(); i++ {
			if f := st.Field(i); f.Embedded() {
				queue = append(queue, item{f.Type(), level})
			}
		}
	}
	push(st, 1)
	seen := map[types.Type]bool{start: true}
	for len(queue) > 0 {
		it := queue[0]
		queue = queue[1:]
		if pr.incompleteType(it.t) || pr.brokenDecl(it.t) {
			return true
		}
		if it.level >= depth {
			continue
		}
		base := types.Unalias(deref(types.Unalias(it.t)))
		if seen[base] {
			continue
		}
		seen[base] = true
		if st, ok := base.Underlying().(*types.Struct); ok {
			push(st, it.level+1)
		}
	}
	return false
}

// incompleteType reports whether the members of an embedded field of type t
// may be unknown.
func (pr *program) incompleteType(t types.Type) bool {
	t = types.Unalias(deref(types.Unalias(t)))
	if isInvalid(t) {
		return true
	}
	if n, ok := t.(*types.Named); ok && !pr.realObj(n.Origin().Obj()) {
		return true
	}
	u := t.Underlying()
	if u == nil || isInvalid(u) {
		return true
	}
	if it, ok := u.(*types.Interface); ok {
		return pr.ifaceIncomplete(it, map[*types.Interface]bool{})
	}
	return false
}

// ifaceIncomplete reports whether an interface embeds an invalid type, a
// stub's copy of an interface or an interface of a package that may have lost
// declarations (R43), directly or through embedded interfaces.
func (pr *program) ifaceIncomplete(it *types.Interface, seen map[*types.Interface]bool) bool {
	if seen[it] {
		return false
	}
	seen[it] = true
	for i := 0; i < it.NumEmbeddeds(); i++ {
		e := types.Unalias(it.EmbeddedType(i))
		if isInvalid(e) {
			return true
		}
		if n, ok := e.(*types.Named); ok && (!pr.realObj(n.Origin().Obj()) || pr.brokenDecl(n)) {
			return true
		}
		switch u := e.Underlying().(type) {
		case *types.Interface:
			if pr.ifaceIncomplete(u, seen) {
				return true
			}
		case *types.Basic:
			if u.Kind() == types.Invalid {
				return true
			}
		}
	}
	return false
}

// involvesInvalid reports whether t mentions the invalid type anywhere.
func involvesInvalid(t types.Type, seen map[types.Type]bool) bool {
	if t == nil {
		return false
	}
	if seen[t] {
		return false
	}
	seen[t] = true
	switch x := types.Unalias(t).(type) {
	case *types.Basic:
		return x.Kind() == types.Invalid
	case *types.Pointer:
		return involvesInvalid(x.Elem(), seen)
	case *types.Slice:
		return involvesInvalid(x.Elem(), seen)
	case *types.Array:
		return involvesInvalid(x.Elem(), seen)
	case *types.Map:
		return involvesInvalid(x.Key(), seen) || involvesInvalid(x.Elem(), seen)
	case *types.Chan:
		return involvesInvalid(x.Elem(), seen)
	case *types.Signature:
		return tupleInvalid(x.Params(), seen) || tupleInvalid(x.Results(), seen)
	case *types.Struct:
		for i := 0; i < x.NumFields(); i++ {
			if involvesInvalid(x.Field(i).Type(), seen) {
				return true
			}
		}
	case *types.Interface:
		for i := 0; i < x.NumEmbeddeds(); i++ {
			if involvesInvalid(x.EmbeddedType(i), seen) {
				return true
			}
		}
		for i := 0; i < x.NumMethods(); i++ {
			if involvesInvalid(x.Method(i).Type(), seen) {
				return true
			}
		}
	case *types.Named:
		if isInvalid(x.Underlying()) {
			return true
		}
		for i := 0; i < x.TypeArgs().Len(); i++ {
			if involvesInvalid(x.TypeArgs().At(i), seen) {
				return true
			}
		}
	case *types.Union:
		for i := 0; i < x.Len(); i++ {
			if involvesInvalid(x.Term(i).Type(), seen) {
				return true
			}
		}
	}
	return false
}

func tupleInvalid(t *types.Tuple, seen map[types.Type]bool) bool {
	for i := 0; i < t.Len(); i++ {
		if involvesInvalid(t.At(i).Type(), seen) {
			return true
		}
	}
	return false
}
