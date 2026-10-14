// Command helper loads a Go workspace once with go/packages and emits, for every
// call site in an in-repo file, its static callee as go/types sees it.
//
// Research probe for the codedoc_go design (track real-repos), not production code.
//
// Outputs (in -out):
//
//	sites.jsonl       one record per *ast.CallExpr in an in-repo file (deduplicated across package variants)
//	ifaces.jsonl      one record per distinct interface type used at an interface-method call site,
//	                  with candidate implementers (types.Implements)
//	summary.json      counts, timings, memory
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"go/ast"
	"go/token"
	"go/types"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	"golang.org/x/tools/go/packages"
)

type Pos struct {
	File   string `json:"file"`   // repo-relative for in-repo files, else absolute
	Line   int    `json:"line"`   // 1-based
	Col    int    `json:"col"`    // 1-based byte column
	Char16 int    `json:"char16"` // 0-based UTF-16 column (LSP default encoding)
	Off    int    `json:"off"`    // byte offset
	InRepo bool   `json:"in_repo,omitempty"`
}

type Target struct {
	Kind     string `json:"kind"`          // func, method, ifacemethod, tpmethod, type, builtin, var:<kind>
	Pkg      string `json:"pkg,omitempty"` // package path ("" for universe)
	Name     string `json:"name"`          // F, T.m, I.m, builtin name
	Loc      string `json:"loc"`           // same-module, other-repo-module, stdlib, third-party, universe, unknown
	Pos      *Pos   `json:"pos,omitempty"`
	TestFile bool   `json:"test_file,omitempty"`
}

type Site struct {
	Pos
	CallOff  int     `json:"call_off"` // offset of the CallExpr's Lparen (dedupe key)
	Shape    string  `json:"shape"`    // ident, selector, other
	FunNode  string  `json:"fun_node"`
	Class    string  `json:"class"`
	Sub      string  `json:"sub,omitempty"`
	Recv     string  `json:"recv,omitempty"`  // value|pointer (method decl receiver)
	Depth    int     `json:"depth,omitempty"` // embedding depth for promoted methods / interface via embedded field
	Generic  string  `json:"generic,omitempty"`
	Stmt     string  `json:"stmt,omitempty"` // go|defer
	TestFile bool    `json:"test_file,omitempty"`
	Module   string  `json:"module"`
	Pkg      string  `json:"pkg"`
	Target   *Target `json:"target,omitempty"`
	Iface    string  `json:"iface,omitempty"`
	Variants int     `json:"variants"`
	Conflict string  `json:"conflict,omitempty"`
	Note     string  `json:"note,omitempty"`
	variant  string
	normal   bool
	ifaceT   types.Type
	ifaceV   *packages.Package
}

var (
	root      string
	fset      = token.NewFileSet()
	modOf     = map[string]*packages.Module{} // package path -> module (nil = std)
	stdPkgs   = map[string]bool{}
	fileCache = map[string][]byte{}
)

func main() {
	var (
		patterns   = flag.String("patterns", "work", "comma-separated patterns")
		dir        = flag.String("dir", "", "directory to run go list in (default -root)")
		out        = flag.String("out", "out", "output directory")
		tests      = flag.Bool("tests", true, "load tests")
		impls      = flag.Bool("impls", true, "compute interface implementers")
		tagsFlag   = flag.String("tags", "", "build tags (passed as -tags)")
		exportDeps = flag.Bool("exportdeps", false, "type-check only workspace roots from source; dependencies from export data (no NeedDeps)")
	)
	flag.StringVar(&root, "root", "", "repo root (absolute)")
	flag.Parse()
	if root == "" {
		fmt.Fprintln(os.Stderr, "need -root")
		os.Exit(2)
	}
	root, _ = filepath.Abs(root)
	if *dir == "" {
		*dir = root
	}
	if err := os.MkdirAll(*out, 0o755); err != nil {
		panic(err)
	}
	summary := map[string]any{}
	cfg := &packages.Config{
		Mode: packages.NeedName | packages.NeedFiles | packages.NeedSyntax | packages.NeedTypes |
			packages.NeedTypesInfo | packages.NeedDeps | packages.NeedImports | packages.NeedModule,
		Tests: *tests,
		Dir:   *dir,
		Fset:  fset,
	}
	if *exportDeps {
		cfg.Mode &^= packages.NeedDeps
	}
	if *tagsFlag != "" {
		cfg.BuildFlags = []string{"-tags=" + *tagsFlag}
	}
	t0 := time.Now()
	roots, err := packages.Load(cfg, strings.Split(*patterns, ",")...)
	loadDur := time.Since(t0)
	if err != nil {
		fmt.Fprintln(os.Stderr, "load error:", err)
		os.Exit(1)
	}
	summary["load_seconds"] = loadDur.Seconds()
	summary["vmhwm_after_load_mb"] = vmHWM()
	fmt.Fprintf(os.Stderr, "loaded %d roots in %.1fs, VmHWM %d MB\n", len(roots), loadDur.Seconds(), vmHWM())

	// all packages (roots + deps)
	var all []*packages.Package
	packages.Visit(roots, nil, func(p *packages.Package) { all = append(all, p) })
	nErrPkgs, nErrs := 0, 0
	errSamples := []string{}
	for _, p := range all {
		if p.Module != nil {
			modOf[p.PkgPath] = p.Module
		} else if !strings.HasSuffix(p.ID, ".test") && !strings.Contains(strings.SplitN(p.PkgPath, "/", 2)[0], ".") {
			stdPkgs[p.PkgPath] = true
		}
		if len(p.Errors) > 0 {
			nErrPkgs++
			nErrs += len(p.Errors)
			if len(errSamples) < 40 {
				errSamples = append(errSamples, p.ID+": "+p.Errors[0].Error())
			}
		}
	}
	summary["packages_total"] = len(all)
	summary["roots"] = len(roots)
	summary["packages_with_errors"] = nErrPkgs
	summary["errors_total"] = nErrs
	summary["error_samples"] = errSamples

	// in-repo packages: those with at least one file under root
	sites := map[string]map[int]*Site{} // relfile -> call_off -> site
	type modstat struct{ pkgs, files int }
	inRepoPkgIDs := map[string]bool{}
	inRepoPkgPaths := map[string]bool{}
	modules := map[string]bool{}
	fileSet := map[string]bool{}
	testFileSet := map[string]bool{}
	nGo, nDefer := map[string]int{}, map[string]int{}
	funcValues, methodValues := 0, 0
	goSeen := map[string]bool{}
	valSeen := map[string]bool{}
	bogus := map[string]int{}
	bogusSamples := []string{}
	for _, p := range all {
		if strings.HasSuffix(p.ID, ".test") || p.TypesInfo == nil {
			continue
		}
		for i, f := range p.Syntax {
			fname := fset.File(f.Pos()).Name()
			if i < len(p.CompiledGoFiles) {
				fname = p.CompiledGoFiles[i]
			}
			rel, ok := relInRepo(fname)
			if !ok {
				continue
			}
			// go/packages attaches the importing file to a package that failed to load
			// (workaround for golang/go#39986 in golist.go). Accept in-repo files only
			// for packages of a workspace module.
			if p.Module == nil || !p.Module.Main {
				bogus[p.ID]++
				if len(bogusSamples) < 100000 {
					bogusSamples = append(bogusSamples, p.ID+" <- "+rel)
				}
				continue
			}
			inRepoPkgIDs[p.ID] = true
			inRepoPkgPaths[p.PkgPath] = true
			if p.Module != nil {
				modules[p.Module.Path] = true
			}
			fileSet[rel] = true
			isTest := strings.HasSuffix(rel, "_test.go")
			if isTest {
				testFileSet[rel] = true
			}
			normal := p.ID == p.PkgPath
			if sites[rel] == nil {
				sites[rel] = map[int]*Site{}
			}
			// go/defer statements and function/method values
			callFuns := map[ast.Expr]bool{}
			ast.Inspect(f, func(n ast.Node) bool {
				if c, ok := n.(*ast.CallExpr); ok {
					fn := ast.Unparen(c.Fun)
					callFuns[fn] = true
					switch x := fn.(type) {
					case *ast.SelectorExpr:
						callFuns[x.Sel] = true
					case *ast.IndexExpr:
						if id := calleeIdent(x.X); id != nil {
							callFuns[id] = true
							callFuns[ast.Unparen(x.X)] = true
						}
					case *ast.IndexListExpr:
						if id := calleeIdent(x.X); id != nil {
							callFuns[id] = true
							callFuns[ast.Unparen(x.X)] = true
						}
					}
				}
				return true
			})
			stmtOf := map[*ast.CallExpr]string{}
			ast.Inspect(f, func(n ast.Node) bool {
				switch s := n.(type) {
				case *ast.GoStmt:
					stmtOf[s.Call] = "go"
					k := rel + fmt.Sprint(s.Pos())
					if !goSeen[k] {
						goSeen[k] = true
						nGo[testKey(isTest)]++
					}
				case *ast.DeferStmt:
					stmtOf[s.Call] = "defer"
					k := rel + fmt.Sprint(s.Pos())
					if !goSeen[k] {
						goSeen[k] = true
						nDefer[testKey(isTest)]++
					}
				case *ast.SelectorExpr:
					if callFuns[s] {
						return true
					}
					if sel, ok := p.TypesInfo.Selections[s]; ok && sel.Kind() == types.MethodVal {
						k := rel + fmt.Sprint(s.Sel.Pos())
						if !valSeen[k] {
							valSeen[k] = true
							methodValues++
						}
					} else if obj, ok := p.TypesInfo.Uses[s.Sel].(*types.Func); ok && obj.Signature().Recv() == nil {
						k := rel + fmt.Sprint(s.Sel.Pos())
						if !valSeen[k] {
							valSeen[k] = true
							funcValues++
						}
					}
				case *ast.Ident:
					if callFuns[s] {
						return true
					}
					if obj, ok := p.TypesInfo.Uses[s].(*types.Func); ok && obj.Signature().Recv() == nil {
						k := rel + fmt.Sprint(s.Pos())
						if !valSeen[k] {
							valSeen[k] = true
							funcValues++
						}
					}
				}
				return true
			})
			ast.Inspect(f, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok {
					return true
				}
				s := classify(p, call)
				s.Stmt = stmtOf[call]
				s.TestFile = isTest
				s.File = rel
				s.InRepo = true
				s.variant = p.ID
				s.normal = normal
				s.Pkg = p.PkgPath
				if p.Module != nil {
					s.Module = p.Module.Path
				}
				key := fset.Position(call.Lparen).Offset
				s.CallOff = key
				prev := sites[rel][key]
				if prev == nil {
					s.Variants = 1
					sites[rel][key] = s
				} else {
					prev.Variants++
					if sig(prev) != sig(s) {
						c := prev.variant + "=" + sig(prev) + " | " + s.variant + "=" + sig(s)
						if prev.Conflict == "" {
							prev.Conflict = c
						}
						if s.normal && !prev.normal { // prefer the normal variant's answer
							s.Variants = prev.Variants
							s.Conflict = prev.Conflict
							sites[rel][key] = s
						}
					}
				}
				return true
			})
		}
	}
	summary["rejected_bogus_packages"] = len(bogus)
	summary["rejected_bogus_file_attachments"] = func() int {
		n := 0
		for _, v := range bogus {
			n += v
		}
		return n
	}()
	summary["rejected_bogus_samples"] = bogusSamples
	summary["inrepo_packages_variants"] = len(inRepoPkgIDs)
	summary["inrepo_package_paths"] = len(inRepoPkgPaths)
	summary["inrepo_modules"] = len(modules)
	summary["inrepo_files_loaded"] = len(fileSet)
	summary["inrepo_test_files_loaded"] = len(testFileSet)
	summary["go_stmts"] = nGo
	summary["defer_stmts"] = nDefer
	summary["func_values_not_called"] = funcValues
	summary["method_values_not_called"] = methodValues

	// flatten
	var flat []*Site
	for _, m := range sites {
		for _, s := range m {
			flat = append(flat, s)
		}
	}
	sort.Slice(flat, func(i, j int) bool {
		if flat[i].File != flat[j].File {
			return flat[i].File < flat[j].File
		}
		return flat[i].CallOff < flat[j].CallOff
	})

	// interface implementers
	t1 := time.Now()
	var ifaceRecs map[string]*IfaceRec
	if *impls {
		ifaceRecs = implementers(all, flat)
		summary["impls_seconds"] = time.Since(t1).Seconds()
	}

	// counts
	byClass := map[string]int{}
	byClassTest := map[string]int{}
	conflicts := 0
	conflictSamples := []string{}
	for _, s := range flat {
		k := s.Class
		if s.Sub != "" {
			k += "/" + s.Sub
		}
		byClass[k]++
		byClass["TOTAL"]++
		if s.TestFile {
			byClassTest[k]++
			byClassTest["TOTAL"]++
		}
		if s.Generic != "" {
			byClass["generic:"+s.Generic]++
		}
		if s.Class == "method" {
			byClass["method-recv:"+s.Recv]++
			if s.Depth > 0 {
				byClass[fmt.Sprintf("method-promoted-depth:%d", s.Depth)]++
			}
		}
		if s.Class == "iface" && s.Depth > 0 {
			byClass["iface-via-embedded-field"]++
		}
		if s.Stmt != "" {
			byClass["stmt:"+s.Stmt+":"+s.Class]++
		}
		if s.Conflict != "" {
			conflicts++
			if len(conflictSamples) < 30 {
				conflictSamples = append(conflictSamples, fmt.Sprintf("%s:%d: %s", s.File, s.Line, s.Conflict))
			}
		}
	}
	summary["sites_by_class"] = byClass
	summary["sites_by_class_testfiles"] = byClassTest
	summary["variant_conflicts"] = conflicts
	summary["variant_conflict_samples"] = conflictSamples
	summary["vmhwm_end_mb"] = vmHWM()
	summary["total_seconds"] = time.Since(t0).Seconds()
	summary["go_version"] = runtime.Version()
	summary["patterns"] = *patterns
	summary["tests"] = *tests

	writeJSONL(filepath.Join(*out, "sites.jsonl"), flat)
	if ifaceRecs != nil {
		keys := make([]string, 0, len(ifaceRecs))
		for k := range ifaceRecs {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		recs := make([]*IfaceRec, 0, len(keys))
		for _, k := range keys {
			recs = append(recs, ifaceRecs[k])
		}
		writeJSONL(filepath.Join(*out, "ifaces.jsonl"), recs)
	}
	b, _ := json.MarshalIndent(summary, "", "  ")
	os.WriteFile(filepath.Join(*out, "summary.json"), b, 0o644)
	fmt.Fprintf(os.Stderr, "sites=%d files=%d total %.1fs VmHWM %d MB\n", len(flat), len(fileSet), time.Since(t0).Seconds(), vmHWM())
}

func testKey(t bool) string {
	if t {
		return "test"
	}
	return "nontest"
}

func sig(s *Site) string {
	t := ""
	if s.Target != nil {
		t = s.Target.Pkg + "." + s.Target.Name
	}
	return s.Class + "/" + s.Sub + "/" + t
}

func writeJSONL[T any](path string, recs []T) {
	f, err := os.Create(path)
	if err != nil {
		panic(err)
	}
	w := bufio.NewWriterSize(f, 1<<20)
	enc := json.NewEncoder(w)
	for _, r := range recs {
		enc.Encode(r)
	}
	w.Flush()
	f.Close()
}

func vmHWM() int {
	b, err := os.ReadFile("/proc/self/status")
	if err != nil {
		return -1
	}
	for _, line := range strings.Split(string(b), "\n") {
		if strings.HasPrefix(line, "VmHWM:") {
			var kb int
			fmt.Sscanf(strings.TrimSpace(strings.TrimPrefix(line, "VmHWM:")), "%d", &kb)
			return kb / 1024
		}
	}
	return -1
}

func relInRepo(fname string) (string, bool) {
	rel, err := filepath.Rel(root, fname)
	if err != nil || strings.HasPrefix(rel, "..") {
		return "", false
	}
	return filepath.ToSlash(rel), true
}

func content(fname string) []byte {
	b, ok := fileCache[fname]
	if !ok {
		b, _ = os.ReadFile(fname)
		fileCache[fname] = b
	}
	return b
}

func mkPos(p token.Pos) *Pos {
	if !p.IsValid() {
		return nil
	}
	tp := fset.Position(p)
	ps := &Pos{Line: tp.Line, Col: tp.Column, Off: tp.Offset}
	if rel, ok := relInRepo(tp.Filename); ok {
		ps.File = rel
		ps.InRepo = true
	} else {
		ps.File = tp.Filename
	}
	b := content(tp.Filename)
	lineStart := tp.Offset - (tp.Column - 1)
	if b != nil && lineStart >= 0 && tp.Offset <= len(b) {
		n := 0
		for seg := b[lineStart:tp.Offset]; len(seg) > 0; {
			r, size := utf8.DecodeRune(seg)
			if r >= 0x10000 {
				n += 2
			} else {
				n++
			}
			seg = seg[size:]
		}
		ps.Char16 = n
	} else {
		ps.Char16 = tp.Column - 1
	}
	return ps
}

// location of a package path relative to the caller's module
func locOf(pkgPath, callerMod string) string {
	pkgPath = strings.TrimSuffix(pkgPath, "_test")
	if pkgPath == "" {
		return "universe"
	}
	if stdPkgs[pkgPath] {
		return "stdlib"
	}
	m, ok := modOf[pkgPath]
	if !ok {
		if m2, ok2 := modOf[pkgPath+"_test"]; ok2 {
			m = m2
		} else {
			return "missing"
		}
	}
	if m == nil {
		return "missing"
	}
	if m.Main {
		if m.Path == callerMod {
			return "same-module"
		}
		return "other-repo-module"
	}
	return "third-party"
}

func objTarget(kind string, obj types.Object, name string, callerMod string) *Target {
	t := &Target{Kind: kind, Name: name}
	if obj.Pkg() == nil {
		t.Loc = "universe"
		return t
	}
	t.Pkg = obj.Pkg().Path()
	t.Loc = locOf(t.Pkg, callerMod)
	t.Pos = mkPos(obj.Pos())
	if t.Pos != nil && strings.HasSuffix(t.Pos.File, "_test.go") {
		t.TestFile = true
	}
	return t
}

func deref(t types.Type) types.Type {
	if p, ok := types.Unalias(t).(*types.Pointer); ok {
		return p.Elem()
	}
	return t
}

func isInterface(t types.Type) bool {
	_, ok := types.Unalias(t).Underlying().(*types.Interface)
	return ok
}

func recvName(f *types.Func) string {
	r := f.Signature().Recv()
	if r == nil {
		return f.Name()
	}
	t := deref(r.Type())
	switch tt := types.Unalias(t).(type) {
	case *types.Named:
		return tt.Obj().Name() + "." + f.Name()
	case *types.TypeParam:
		return tt.Obj().Name() + "." + f.Name()
	}
	return "(" + types.TypeString(t, func(*types.Package) string { return "" }) + ")." + f.Name()
}

func calleeIdent(e ast.Expr) *ast.Ident {
	switch x := ast.Unparen(e).(type) {
	case *ast.Ident:
		return x
	case *ast.SelectorExpr:
		return x.Sel
	}
	return nil
}

func classify(p *packages.Package, call *ast.CallExpr) *Site {
	info := p.TypesInfo
	callerMod := ""
	if p.Module != nil {
		callerMod = p.Module.Path
	}
	fun := ast.Unparen(call.Fun)
	s := &Site{FunNode: strings.TrimPrefix(fmt.Sprintf("%T", fun), "*ast.")}
	tok := fun.Pos()
	switch f := fun.(type) {
	case *ast.Ident:
		s.Shape = "ident"
		tok = f.Pos()
	case *ast.SelectorExpr:
		s.Shape = "selector"
		tok = f.Sel.Pos()
	case *ast.IndexExpr:
		s.Shape = "other"
		if id := calleeIdent(f.X); id != nil {
			tok = id.Pos()
		}
	case *ast.IndexListExpr:
		s.Shape = "other"
		if id := calleeIdent(f.X); id != nil {
			tok = id.Pos()
		}
	default:
		s.Shape = "other"
	}
	s.Pos = *mkPos(tok)

	if id := calleeIdent(fun); id != nil {
		if b, ok := info.Uses[id].(*types.Builtin); ok {
			s.Class = "builtin"
			s.Target = &Target{Kind: "builtin", Name: b.Name(), Loc: "universe"}
			if b.Pkg() != nil || id.Name != b.Name() {
				s.Target.Loc = "stdlib"
			}
			if sel, ok := fun.(*ast.SelectorExpr); ok {
				if x, ok := sel.X.(*ast.Ident); ok {
					s.Target.Pkg = x.Name // unsafe
					s.Target.Loc = "stdlib"
				}
			}
			return s
		}
	}
	tv, ok := info.Types[fun]
	if !ok || tv.Type == nil || tv.Type == types.Typ[types.Invalid] {
		s.Class = "unknown"
		// is there at least an object for the callee ident?
		if id := calleeIdent(fun); id != nil {
			if obj := info.Uses[id]; obj != nil {
				s.Note = fmt.Sprintf("invalid-type obj=%T", obj)
			}
		}
		return s
	}
	if tv.IsType() {
		s.Class = "conversion"
		id := calleeIdent(fun)
		if id == nil {
			if ix, ok := fun.(*ast.IndexExpr); ok {
				id = calleeIdent(ix.X)
			} else if ix, ok := fun.(*ast.IndexListExpr); ok {
				id = calleeIdent(ix.X)
			}
		}
		if id != nil {
			if obj, ok := info.Uses[id].(*types.TypeName); ok {
				s.Target = objTarget("type", obj, obj.Name(), callerMod)
				s.Sub = s.Target.Loc
				return s
			}
		}
		s.Sub = "type-literal"
		return s
	}
	if tv.IsBuiltin() {
		s.Class = "builtin"
		if id := calleeIdent(fun); id != nil {
			s.Target = &Target{Kind: "builtin", Name: id.Name, Loc: "universe"}
			if sel, ok := fun.(*ast.SelectorExpr); ok {
				if x, ok := sel.X.(*ast.Ident); ok {
					s.Target.Pkg = x.Name // unsafe
					s.Target.Loc = "stdlib"
				}
			}
		}
		return s
	}
	switch f := fun.(type) {
	case *ast.Ident:
		classifyObj(s, info, f, info.Uses[f], callerMod, false)
	case *ast.SelectorExpr:
		if sel, ok := info.Selections[f]; ok {
			switch sel.Kind() {
			case types.MethodVal:
				m := sel.Obj().(*types.Func)
				orig := m.Origin()
				s.Depth = len(sel.Index()) - 1
				recvT := deref(sel.Recv())
				if _, isTP := types.Unalias(recvT).(*types.TypeParam); isTP {
					s.Class = "typeparam"
					s.Target = objTarget("tpmethod", orig, recvName(orig), callerMod)
					s.Sub = s.Target.Loc
					return s
				}
				if isInterface(orig.Signature().Recv().Type()) {
					s.Class = "iface"
					s.Target = objTarget("ifacemethod", orig, recvName(orig), callerMod)
					// dispatch interface: static receiver type, or the embedded field's type
					it := recvT
					if !isInterface(it) {
						t := sel.Recv()
						for _, idx := range sel.Index()[:len(sel.Index())-1] {
							st, ok := deref(t).Underlying().(*types.Struct)
							if !ok {
								break
							}
							t = st.Field(idx).Type()
							if isInterface(t) {
								break
							}
						}
						it = t
					}
					s.ifaceT = it
					s.ifaceV = p
					s.Iface, s.Sub = ifaceKey(it, callerMod)
					return s
				}
				s.Class = "method"
				s.Target = objTarget("method", orig, recvName(orig), callerMod)
				s.Sub = s.Target.Loc
				if _, ptr := types.Unalias(orig.Signature().Recv().Type()).(*types.Pointer); ptr {
					s.Recv = "pointer"
				} else {
					s.Recv = "value"
				}
				if named, ok := types.Unalias(deref(orig.Signature().Recv().Type())).(*types.Named); ok && named.TypeParams().Len() > 0 {
					s.Generic = "method-of-generic-type"
				}
				return s
			case types.FieldVal:
				s.Class = "dynamic"
				s.Sub = "field"
				s.Target = objTarget("var:field", sel.Obj(), sel.Obj().Name(), callerMod)
				return s
			case types.MethodExpr:
				m := sel.Obj().(*types.Func).Origin()
				s.Class = "methodexpr"
				if isInterface(m.Signature().Recv().Type()) {
					s.Sub = "interface"
				} else {
					s.Sub = "concrete"
				}
				s.Target = objTarget("method", m, recvName(m), callerMod)
				return s
			}
		}
		// qualified identifier pkg.Name
		classifyObj(s, info, f.Sel, info.Uses[f.Sel], callerMod, true)
	case *ast.IndexExpr, *ast.IndexListExpr:
		var x ast.Expr
		if ix, ok := f.(*ast.IndexExpr); ok {
			x = ix.X
		} else {
			x = f.(*ast.IndexListExpr).X
		}
		id := calleeIdent(x)
		if id != nil {
			if _, inst := info.Instances[id]; inst {
				classifyObj(s, info, id, info.Uses[id], callerMod, false)
				s.Generic = "explicit"
				return s
			}
		}
		s.Class = "dynamic"
		s.Sub = "index"
	case *ast.FuncLit:
		s.Class = "funclit"
	case *ast.CallExpr:
		s.Class = "dynamic"
		s.Sub = "call-result"
	case *ast.TypeAssertExpr:
		s.Class = "dynamic"
		s.Sub = "type-assert"
	case *ast.StarExpr:
		s.Class = "dynamic"
		s.Sub = "deref"
	default:
		s.Class = "dynamic"
		s.Sub = "other:" + s.FunNode
	}
	return s
}

func classifyObj(s *Site, info *types.Info, id *ast.Ident, obj types.Object, callerMod string, qualified bool) {
	switch o := obj.(type) {
	case *types.Func:
		orig := o.Origin()
		if orig.Signature().Recv() != nil {
			// should not happen for idents
			s.Class = "method"
			s.Target = objTarget("method", orig, recvName(orig), callerMod)
			s.Sub = s.Target.Loc
			return
		}
		s.Class = "func"
		s.Target = objTarget("func", orig, orig.Name(), callerMod)
		s.Sub = s.Target.Loc
		if _, inst := info.Instances[id]; inst && s.Generic == "" {
			s.Generic = "implicit"
		}
	case *types.Var:
		s.Class = "dynamic"
		kind := "var"
		switch o.Kind() {
		case types.PackageVar:
			kind = "pkgvar"
		case types.LocalVar:
			kind = "local"
		case types.ParamVar, types.RecvVar, types.ResultVar:
			kind = "param"
		case types.FieldVar:
			kind = "field"
		}
		s.Sub = kind
		s.Target = objTarget("var:"+kind, o, o.Name(), callerMod)
	case nil:
		s.Class = "unknown"
		s.Note = "no-object"
	default:
		s.Class = "unknown"
		s.Note = fmt.Sprintf("obj=%T", obj)
	}
}

func ifaceKey(t types.Type, callerMod string) (string, string) {
	t = types.Unalias(t)
	if n, ok := t.(*types.Named); ok {
		if n.Obj().Pkg() == nil {
			return n.Obj().Name(), "universe"
		}
		k := n.Obj().Pkg().Path() + "." + n.Obj().Name()
		if n.TypeArgs().Len() > 0 {
			k = types.TypeString(n, (*types.Package).Path)
		}
		return k, locOf(n.Obj().Pkg().Path(), callerMod)
	}
	k := "anon:" + types.TypeString(t, (*types.Package).Path)
	// unexported method names are package-qualified: keep anonymous interfaces of different packages apart
	if it, ok := t.Underlying().(*types.Interface); ok {
		for i := 0; i < it.NumMethods(); i++ {
			if m := it.Method(i); !m.Exported() && m.Pkg() != nil {
				k += "@" + m.Pkg().Path()
				break
			}
		}
	}
	return k, "anonymous"
}
