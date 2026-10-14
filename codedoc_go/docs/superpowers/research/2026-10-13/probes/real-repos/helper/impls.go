package main

import (
	"fmt"
	"go/types"
	"os"
	"sort"
	"strings"

	"golang.org/x/tools/go/packages"
)

type Impl struct {
	Type     string `json:"type"` // pkgpath.Name
	PtrOnly  bool   `json:"ptr_only,omitempty"`
	TestOnly bool   `json:"test_only,omitempty"`
	Fallback bool   `json:"fallback,omitempty"` // found by signature-string match only (variant identity mismatch)
	Module   string `json:"module,omitempty"`
	Pos      *Pos   `json:"pos,omitempty"`
}

type IfaceRec struct {
	Key              string   `json:"key"`
	Loc              string   `json:"loc"` // relative to the first site's module
	DeclPos          *Pos     `json:"decl,omitempty"`
	TestOnly         bool     `json:"test_only,omitempty"`
	Methods          int      `json:"methods"`
	Sites            int      `json:"sites"`
	SitesNonTest     int      `json:"sites_nontest"`
	RepoImpls        []Impl   `json:"repo_impls"`
	StdImpls         int      `json:"std_impls"`
	ThirdImpls       int      `json:"third_impls"`
	StdSample        []string `json:"std_sample,omitempty"`
	ThirdSample      []string `json:"third_sample,omitempty"`
	GenericUndecided int      `json:"generic_undecided,omitempty"`
	// per interface method: distinct concrete method declarations over all implementers
	MethodDecls      map[string]map[string]*Decl `json:"method_decls,omitempty"`
	AbstractVia      map[string]int              `json:"abstract_via_embedded_iface,omitempty"`
	Fallbacks        int                         `json:"fallbacks,omitempty"`
	ImplementsChecks int                         `json:"implements_checks"`
}

type Decl struct {
	Cls      string   `json:"cls"` // repo, stdlib, third-party
	TestFile bool     `json:"test_file,omitempty"`
	Types    []string `json:"types"`
}

type tinfo struct {
	key      string
	name     string
	path     string
	obj      *types.TypeName
	pkg      *packages.Package
	testOnly bool
	cls      string // repo, stdlib, third-party
	module   string
	generic  bool
}

var universes = map[*packages.Package]map[string]*types.Package{}

func universe(p *packages.Package) map[string]*types.Package {
	if u, ok := universes[p]; ok {
		return u
	}
	u := map[string]*types.Package{}
	var walk func(q *packages.Package)
	seen := map[*packages.Package]bool{}
	walk = func(q *packages.Package) {
		if seen[q] {
			return
		}
		seen[q] = true
		if q.Types != nil {
			u[q.PkgPath] = q.Types
		}
		for _, imp := range q.Imports {
			walk(imp)
		}
	}
	walk(p)
	universes[p] = u
	return u
}

func isTestVariant(p *packages.Package) bool { return p.ID != p.PkgPath }

func implementers(all []*packages.Package, flat []*Site) map[string]*IfaceRec {
	// 1. named types
	types_ := map[string]*tinfo{}
	for _, p := range all {
		if strings.HasSuffix(p.ID, ".test") || p.Types == nil {
			continue
		}
		cls := "third-party"
		mod := ""
		if p.Module == nil {
			if !stdPkgs[p.PkgPath] {
				continue // stub of a package that failed to load (may hold attached in-repo files)
			}
			cls = "stdlib"
		} else {
			mod = p.Module.Path
			if p.Module.Main {
				cls = "repo"
			}
		}
		scope := p.Types.Scope()
		for _, name := range scope.Names() {
			tn, ok := scope.Lookup(name).(*types.TypeName)
			if !ok || tn.IsAlias() {
				continue
			}
			named, ok := tn.Type().(*types.Named)
			if !ok {
				continue
			}
			switch named.Underlying().(type) {
			case *types.Interface, *types.Pointer:
				continue
			}
			key := p.PkgPath + "." + name
			ti := types_[key]
			fname := fset.Position(tn.Pos()).Filename
			testOnly := strings.HasSuffix(fname, "_test.go")
			if ti == nil || (isTestVariant(ti.pkg) && !isTestVariant(p)) {
				types_[key] = &tinfo{key: key, name: name, path: p.PkgPath, obj: tn, pkg: p, testOnly: testOnly,
					cls: cls, module: mod, generic: named.TypeParams().Len() > 0}
			}
		}
	}
	// 2. method-name index
	index := map[string][]*tinfo{}
	gindex := map[string][]*tinfo{}
	nTypes := map[string]int{}
	for _, ti := range types_ {
		nTypes[ti.cls]++
		ms := types.NewMethodSet(types.NewPointer(ti.obj.Type()))
		for i := 0; i < ms.Len(); i++ {
			id := ms.At(i).Obj().Id()
			if ti.generic {
				gindex[id] = append(gindex[id], ti)
			} else {
				index[id] = append(index[id], ti)
			}
		}
	}
	fmt.Fprintf(os.Stderr, "named types: %v\n", nTypes)

	// 3. interfaces at call sites
	recs := map[string]*IfaceRec{}
	ifaceT := map[string]types.Type{}
	ifaceV := map[string]*packages.Package{}
	for _, s := range flat {
		if s.Class != "iface" {
			continue
		}
		r := recs[s.Iface]
		if r == nil {
			r = &IfaceRec{Key: s.Iface, Loc: s.Sub}
			recs[s.Iface] = r
		}
		r.Sites++
		if !s.TestFile {
			r.SitesNonTest++
		}
		if prev, ok := ifaceV[s.Iface]; !ok || (isTestVariant(prev) && !isTestVariant(s.ifaceV)) {
			ifaceT[s.Iface] = s.ifaceT
			ifaceV[s.Iface] = s.ifaceV
		}
	}
	qual := (*types.Package).Path
	for key, r := range recs {
		I := ifaceT[key]
		VI := ifaceV[key]
		iface, ok := types.Unalias(I).Underlying().(*types.Interface)
		if !ok {
			continue
		}
		r.Methods = iface.NumMethods()
		var named *types.Named
		if n, ok := types.Unalias(I).(*types.Named); ok {
			named = n
			r.DeclPos = mkPos(n.Obj().Pos())
			if r.DeclPos != nil && strings.HasSuffix(r.DeclPos.File, "_test.go") {
				r.TestOnly = true
			}
		}
		if iface.NumMethods() == 0 {
			continue
		}
		// candidate set: intersection of method-name postings
		var cands []*tinfo
		for i := 0; i < iface.NumMethods(); i++ {
			id := iface.Method(i).Id()
			post := index[id]
			if i == 0 {
				cands = append([]*tinfo(nil), post...)
				continue
			}
			set := map[*tinfo]bool{}
			for _, t := range post {
				set[t] = true
			}
			kept := cands[:0]
			for _, t := range cands {
				if set[t] {
					kept = append(kept, t)
				}
			}
			cands = kept
		}
		// generic types matching all names (undecided)
		{
			var g []*tinfo
			for i := 0; i < iface.NumMethods(); i++ {
				post := gindex[iface.Method(i).Id()]
				if i == 0 {
					g = append(g, post...)
					continue
				}
				set := map[*tinfo]bool{}
				for _, t := range post {
					set[t] = true
				}
				kept := g[:0]
				for _, t := range g {
					if set[t] {
						kept = append(kept, t)
					}
				}
				g = kept
			}
			r.GenericUndecided = len(g)
		}
		for _, ti := range cands {
			T := ti.obj.Type()
			Iuse := I
			if isTestVariant(ti.pkg) && named != nil && named.TypeArgs().Len() == 0 && named.Obj().Pkg() != nil {
				if tp, ok := universe(ti.pkg)[named.Obj().Pkg().Path()]; ok {
					if obj, ok := tp.Scope().Lookup(named.Obj().Name()).(*types.TypeName); ok {
						Iuse = obj.Type()
					}
				}
			} else if VI != nil && isTestVariant(VI) && !isTestVariant(ti.pkg) {
				if tp, ok := universe(VI)[ti.path]; ok {
					if obj, ok := tp.Scope().Lookup(ti.name).(*types.TypeName); ok {
						T = obj.Type()
					}
				}
			}
			ifc, ok := types.Unalias(Iuse).Underlying().(*types.Interface)
			if !ok {
				continue
			}
			r.ImplementsChecks++
			val := types.Implements(T, ifc)
			ptr := val || types.Implements(types.NewPointer(T), ifc)
			fallback := false
			if !ptr {
				// signature-string fallback (identity mismatch across test variants)
				if v, p := stringMatch(T, iface, qual); p {
					val, ptr, fallback = v, p, true
					r.Fallbacks++
				}
			}
			if !ptr {
				continue
			}
			// concrete declarations of each interface method for this implementer
			{
				recvT := T
				if !val {
					recvT = types.NewPointer(T)
				}
				ms := types.NewMethodSet(recvT)
				if r.MethodDecls == nil {
					r.MethodDecls = map[string]map[string]*Decl{}
					r.AbstractVia = map[string]int{}
				}
				for i := 0; i < iface.NumMethods(); i++ {
					m := iface.Method(i)
					sel := ms.Lookup(m.Pkg(), m.Name())
					if sel == nil {
						continue
					}
					f, ok := sel.Obj().(*types.Func)
					if !ok {
						continue
					}
					f = f.Origin()
					if isInterface(f.Signature().Recv().Type()) {
						r.AbstractVia[m.Name()]++
						continue
					}
					ps := mkPos(f.Pos())
					if ps == nil {
						continue
					}
					k := fmt.Sprintf("%s:%d:%d", ps.File, ps.Line-1, ps.Char16)
					if r.MethodDecls[m.Name()] == nil {
						r.MethodDecls[m.Name()] = map[string]*Decl{}
					}
					d := r.MethodDecls[m.Name()][k]
					if d == nil {
						cls := "third-party"
						switch locOf(f.Pkg().Path(), "") {
						case "stdlib":
							cls = "stdlib"
						case "same-module", "other-repo-module":
							cls = "repo"
						}
						d = &Decl{Cls: cls, TestFile: strings.HasSuffix(ps.File, "_test.go")}
						r.MethodDecls[m.Name()][k] = d
					}
					if len(d.Types) < 6 {
						d.Types = append(d.Types, ti.key)
					}
				}
			}
			switch ti.cls {
			case "repo":
				r.RepoImpls = append(r.RepoImpls, Impl{Type: ti.key, PtrOnly: !val, TestOnly: ti.testOnly,
					Fallback: fallback, Module: ti.module, Pos: mkPos(ti.obj.Pos())})
			case "stdlib":
				r.StdImpls++
				if len(r.StdSample) < 5 {
					r.StdSample = append(r.StdSample, ti.key)
				}
			default:
				r.ThirdImpls++
				if len(r.ThirdSample) < 5 {
					r.ThirdSample = append(r.ThirdSample, ti.key)
				}
			}
		}
		sort.Slice(r.RepoImpls, func(i, j int) bool { return r.RepoImpls[i].Type < r.RepoImpls[j].Type })
	}
	return recs
}

func sigString(f *types.Func, qual types.Qualifier) string {
	return types.TypeString(f.Signature(), qual)
}

func stringMatch(T types.Type, iface *types.Interface, qual types.Qualifier) (val, ptr bool) {
	check := func(ms *types.MethodSet) bool {
		for i := 0; i < iface.NumMethods(); i++ {
			m := iface.Method(i)
			sel := ms.Lookup(m.Pkg(), m.Name())
			if sel == nil {
				return false
			}
			f, ok := sel.Obj().(*types.Func)
			if !ok || sigString(f, qual) != sigString(m, qual) {
				return false
			}
		}
		return true
	}
	val = check(types.NewMethodSet(T))
	ptr = val || check(types.NewMethodSet(types.NewPointer(T)))
	return
}
