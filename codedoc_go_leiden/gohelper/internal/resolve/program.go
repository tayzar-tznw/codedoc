package resolve

import (
	"go/ast"
	"go/parser"
	"go/token"
	"go/types"
	"path/filepath"
	"sort"
	"strings"
	"sync"

	"golang.org/x/tools/go/packages"
	"golang.org/x/tools/go/types/typeutil"
)

// program holds one load and the indexes built over it.
type program struct {
	opts *Options
	fset *token.FileSet

	repoGiven string // --repo-root as given (cleaned)
	repoRoot  string // its real path
	goroot    string // real path of the toolchain root ("" if unknown)
	mainMods  map[string]bool
	peerDirs  []string // real paths

	hashMu     sync.Mutex
	parsedHash map[string]string // file name as loaded -> sha256 of the parsed bytes

	roots   []*packages.Package
	all     []*packages.Package
	byTypes map[*types.Package]*packages.Package
	byPath  map[string]*packages.Package // PkgPath -> preferred variant
	astOf   map[*token.File]*ast.File

	realMu    sync.Mutex
	realCache map[string]string

	linknames   map[*packages.Package]map[string]string
	broken      map[*packages.Package]bool // brokenPkg, per package variant
	universes   map[*packages.Package]map[string]*types.Package
	msets       typeutil.MethodSetCache
	universePos map[string]token.Position // declarations in $GOROOT/src/builtin/builtin.go, lazily

	// collected results
	files   map[string]*fileInfo
	sites   map[siteKey][]*answer
	imports map[siteKey]*importRecord
	facts   map[factsKey]map[*types.Var]*varFacts

	ifaceJobs map[string]*ifaceJob
	types     *typeIndex
}

type siteKey struct {
	path string
	off  int
}

type fileInfo struct {
	names    map[string]bool // file names that loaded this real path
	packages map[string]bool
}

func newProgram(opts *Options, env []string) *program {
	pr := &program{
		opts:       opts,
		fset:       token.NewFileSet(),
		repoGiven:  filepath.Clean(opts.RepoRoot),
		mainMods:   map[string]bool{},
		parsedHash: map[string]string{},
		byTypes:    map[*types.Package]*packages.Package{},
		byPath:     map[string]*packages.Package{},
		astOf:      map[*token.File]*ast.File{},
		realCache:  map[string]string{},
		linknames:  map[*packages.Package]map[string]string{},
		broken:     map[*packages.Package]bool{},
		universes:  map[*packages.Package]map[string]*types.Package{},
		files:      map[string]*fileInfo{},
		sites:      map[siteKey][]*answer{},
		imports:    map[siteKey]*importRecord{},
		facts:      map[factsKey]map[*types.Var]*varFacts{},
		ifaceJobs:  map[string]*ifaceJob{},
	}
	pr.repoRoot = pr.realpath(pr.repoGiven)
	for _, m := range opts.MainModules {
		pr.mainMods[m] = true
	}
	for _, d := range opts.PeerDirs {
		pr.peerDirs = append(pr.peerDirs, pr.realpath(filepath.Clean(d)))
	}
	if g := goroot(env, opts.Dir); g != "" {
		pr.goroot = pr.realpath(filepath.Clean(g))
	}
	return pr
}

// realpath resolves symlinks; a path that cannot be resolved is returned cleaned.
func (pr *program) realpath(p string) string {
	if p == "" {
		return ""
	}
	pr.realMu.Lock()
	r, ok := pr.realCache[p]
	pr.realMu.Unlock()
	if ok {
		return r
	}
	r, err := filepath.EvalSymlinks(p)
	if err != nil {
		r = filepath.Clean(p)
	} else if !filepath.IsAbs(r) {
		if abs, err := filepath.Abs(r); err == nil {
			r = abs
		}
	}
	pr.realMu.Lock()
	pr.realCache[p] = r
	pr.realMu.Unlock()
	return r
}

func under(path, dir string) bool {
	if dir == "" {
		return false
	}
	return path == dir || strings.HasPrefix(path, dir+string(filepath.Separator))
}

func (pr *program) maybeInRepo(filename string) bool {
	return under(filename, pr.repoGiven) || under(filename, pr.repoRoot)
}

func isTestVariant(p *packages.Package) bool { return p.ID != p.PkgPath }

// isTestMain reports the generated test main package ("p.test", package
// main), whose files live in the build cache, not in its directory. A real
// package path may end in ".test" too.
func isTestMain(p *packages.Package) bool {
	if !strings.HasSuffix(p.ID, ".test") || p.Name != "main" || len(p.GoFiles) == 0 {
		return false
	}
	for _, f := range p.GoFiles {
		if filepath.Dir(f) == filepath.Clean(p.Dir) {
			return false
		}
	}
	return true
}

// index builds the package maps over the whole load.
func (pr *program) index(roots []*packages.Package) {
	pr.roots = roots
	packages.Visit(roots, nil, func(p *packages.Package) { pr.all = append(pr.all, p) })
	sort.Slice(pr.all, func(i, j int) bool { return pr.all[i].ID < pr.all[j].ID })
	for _, p := range pr.all {
		if p.Types != nil {
			pr.byTypes[p.Types] = p
		}
		if isTestMain(p) {
			continue
		}
		if prev, ok := pr.byPath[p.PkgPath]; !ok || (isTestVariant(prev) && !isTestVariant(p)) {
			pr.byPath[p.PkgPath] = p
		}
		for _, f := range p.Syntax {
			if tf := pr.fset.File(f.FileStart); tf != nil {
				if _, ok := pr.astOf[tf]; !ok {
					pr.astOf[tf] = f
				}
			}
		}
	}
}

func hasListError(p *packages.Package) bool {
	for _, e := range p.Errors {
		if e.Kind == packages.ListError {
			return true
		}
	}
	return false
}

// isStd reports whether p is a real standard-library package.
func (pr *program) isStd(p *packages.Package) bool {
	if p.Module != nil || p.Dir == "" || pr.goroot == "" {
		return false
	}
	if hasListError(p) {
		return false
	}
	if p.PkgPath != "unsafe" && len(p.GoFiles) == 0 {
		return false
	}
	return under(pr.realpath(p.Dir), filepath.Join(pr.goroot, "src"))
}

// isStub reports whether p is not a real workspace, dependency or standard
// package: typically the stand-in that go/packages creates for a failed
// import (loader trap 2).
func (pr *program) isStub(p *packages.Package) bool {
	if p == nil {
		return true
	}
	if p.Module != nil {
		return p.Dir == ""
	}
	return !pr.isStd(p)
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

// attached reports whether file was added to p by the go/packages work-around
// for golang/go#39986, which puts an importing file into the package that
// failed to load (loader trap 1).
func attached(p *packages.Package, file string) bool {
	if p.Dir == "" {
		return true
	}
	return filepath.Dir(file) != filepath.Clean(p.Dir) && contains(p.GoFiles, file)
}

// realObj reports whether obj is declared in a real package, in one of that
// package's own files. Universe objects are real.
func (pr *program) realObj(obj types.Object) bool {
	pkg := obj.Pkg()
	if pkg == nil {
		return true
	}
	p := pr.byTypes[pkg]
	if pr.isStub(p) {
		return false
	}
	pos := pr.fset.PositionFor(obj.Pos(), false)
	if !pos.IsValid() {
		return false
	}
	return !attached(p, pos.Filename)
}

// accept decides whether (file, p) is an in-repo pair: a workspace main
// module package, in --main-modules when given, whose own directory holds the
// file, with the file's real path under the repo root (loader trap 1).
func (pr *program) accept(p *packages.Package, file string) (string, bool) {
	if p.Module == nil || !p.Module.Main || p.TypesInfo == nil || isTestMain(p) {
		return "", false
	}
	if len(pr.mainMods) > 0 && !pr.mainMods[p.Module.Path] {
		return "", false
	}
	if p.Dir == "" || filepath.Dir(file) != filepath.Clean(p.Dir) {
		return "", false
	}
	real := pr.realpath(file)
	if !under(real, pr.repoRoot) {
		return "", false
	}
	return real, true
}

func (pr *program) inPeer(real string) bool {
	for _, d := range pr.peerDirs {
		if under(real, d) {
			return true
		}
	}
	return false
}

// fileOf returns the syntax tree that declares pos.
func (pr *program) fileOf(pos token.Pos) *ast.File {
	tf := pr.fset.File(pos)
	if tf == nil {
		return nil
	}
	return pr.astOf[tf]
}

// funcDecl returns the declaration of a package-level function or method.
func (pr *program) funcDecl(f *types.Func) *ast.FuncDecl {
	file := pr.fileOf(f.Pos())
	if file == nil {
		return nil
	}
	for _, d := range file.Decls {
		if fd, ok := d.(*ast.FuncDecl); ok && fd.Name.Pos() == f.Pos() {
			return fd
		}
	}
	return nil
}

// linknameOf returns the target of a pull-style `//go:linkname local target`
// directive for f, from any file of f's package.
func (pr *program) linknameOf(f *types.Func) (string, bool) {
	p := pr.byTypes[f.Pkg()]
	if p == nil {
		return "", false
	}
	m, ok := pr.linknames[p]
	if !ok {
		m = map[string]string{}
		for _, file := range p.Syntax {
			for _, cg := range file.Comments {
				for _, c := range cg.List {
					if !strings.HasPrefix(c.Text, "//go:linkname ") {
						continue
					}
					fields := strings.Fields(c.Text)
					if len(fields) == 3 {
						m[fields[1]] = fields[2]
					}
				}
			}
		}
		pr.linknames[p] = m
	}
	t, ok := m[f.Name()]
	return t, ok
}

// lookupLinkname finds the object a linkname target names: "pkg.name",
// "pkg.T.m" or "pkg.(*T).m". It returns nil when the package is not loaded or
// the symbol is not found.
func (pr *program) lookupLinkname(s string) types.Object {
	slash := strings.LastIndex(s, "/")
	for i := slash + 1; i < len(s); i++ {
		if s[i] != '.' {
			continue
		}
		p := pr.byPath[s[:i]]
		if p == nil || p.Types == nil {
			continue
		}
		sym := s[i+1:]
		if !strings.ContainsAny(sym, ".()*") {
			if obj := p.Types.Scope().Lookup(sym); obj != nil {
				if _, ok := obj.(*types.Func); ok && pr.realObj(obj) {
					return obj
				}
			}
			continue
		}
		dot := strings.LastIndex(sym, ".")
		if dot < 0 {
			continue
		}
		recv, name := sym[:dot], sym[dot+1:]
		ptr := false
		if strings.HasPrefix(recv, "(*") && strings.HasSuffix(recv, ")") {
			recv, ptr = recv[2:len(recv)-1], true
		}
		tn, ok := p.Types.Scope().Lookup(recv).(*types.TypeName)
		if !ok {
			continue
		}
		var t types.Type = tn.Type()
		if ptr {
			t = types.NewPointer(t)
		}
		if sel := pr.msets.MethodSet(t).Lookup(p.Types, name); sel != nil && pr.realObj(sel.Obj()) {
			return sel.Obj()
		}
	}
	return nil
}

// universeDecl returns the position of a predeclared name in
// $GOROOT/src/builtin/builtin.go, the declaration gopls reports for it: a
// type ("error", "int"), or "error.Error" for the error interface's method.
func (pr *program) universeDecl(name string) (token.Position, bool) {
	if pr.universePos == nil {
		pr.universePos = map[string]token.Position{}
		if pr.goroot == "" {
			return token.Position{}, false
		}
		path := filepath.Join(pr.goroot, "src", "builtin", "builtin.go")
		f, err := parser.ParseFile(pr.fset, path, nil, parser.SkipObjectResolution)
		if err != nil {
			return token.Position{}, false
		}
		real := pr.realpath(path)
		at := func(id *ast.Ident) token.Position {
			p := pr.fset.PositionFor(id.Pos(), false)
			p.Filename = real
			return p
		}
		for _, d := range f.Decls {
			g, ok := d.(*ast.GenDecl)
			if !ok || g.Tok != token.TYPE {
				continue
			}
			for _, sp := range g.Specs {
				ts := sp.(*ast.TypeSpec)
				pr.universePos[ts.Name.Name] = at(ts.Name)
				if it, ok := ts.Type.(*ast.InterfaceType); ok {
					for _, m := range it.Methods.List {
						for _, n := range m.Names {
							pr.universePos[ts.Name.Name+"."+n.Name] = at(n)
						}
					}
				}
			}
		}
	}
	p, ok := pr.universePos[name]
	return p, ok
}
