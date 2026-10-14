package resolve

import (
	"fmt"
	"go/types"
	"path/filepath"
	"sort"
	"strings"

	"golang.org/x/tools/go/packages"
)

// typeEntry is a package-level named type of the loaded program, deduplicated
// across package variants (the non-test variant is preferred).
type typeEntry struct {
	key    string // package path + "." + name
	tn     *types.TypeName
	p      *packages.Package
	twinTN *types.TypeName // the test variant's twin, which may have test-only methods
	twinP  *packages.Package
	inRepo bool
	test   bool // declared in a _test.go file
	iface  bool // underlying interface (never a dynamic type)
	peer   bool // declared under --peer-dirs
}

// ifaceJob is one dispatch interface and its implementers, shared by sites.
type ifaceJob struct {
	t         types.Type
	p         *packages.Package
	recvType  string
	impls     []*impl
	openWorld bool
	invalid   bool // gate G4: no candidate set can be trusted; no candidates at all
	noMethods bool
	cands     map[string][]*Candidate // by called method Id
}

type impl struct {
	e        *typeEntry
	tn       *types.TypeName
	val      bool // T implements (else only *T)
	testOnly bool
}

// typeIndex is built once per load.
type typeIndex struct {
	entries []*typeEntry
	byKey   map[string]*typeEntry
	byName  map[string][]*typeEntry // method Id -> concrete non-generic types with it in the method set of *T
	losses  []*lossBound            // in-repo packages that may have lost declarations (R43)
}

func (pr *program) buildTypeIndex() *typeIndex {
	ix := &typeIndex{byKey: map[string]*typeEntry{}, byName: map[string][]*typeEntry{}}
	for _, p := range pr.all {
		if p.Types == nil || isTestMain(p) || pr.isStub(p) {
			continue
		}
		// Candidate universe (ruling 2026-10-13): types of real packages whose
		// declaring file is under the repo root and under the module's (or
		// its replacement's) directory, whatever Module.Main says.
		modDirs := pr.moduleDirs(p)
		if len(modDirs) > 0 && pr.brokenPkg(p) && p.Dir != "" && pr.inRepoFile(pr.realpath(p.Dir), modDirs) {
			if b := pr.lossBound(p); b != nil {
				ix.losses = append(ix.losses, b)
			}
		}
		scope := p.Types.Scope()
		for _, name := range scope.Names() {
			tn, ok := scope.Lookup(name).(*types.TypeName)
			if !ok || tn.IsAlias() {
				continue
			}
			named, ok := tn.Type().(*types.Named)
			if !ok || named.TypeParams().Len() > 0 {
				continue
			}
			if _, ok := named.Underlying().(*types.Pointer); ok {
				continue
			}
			pos := pr.fset.PositionFor(tn.Pos(), false)
			if !pos.IsValid() || attached(p, pos.Filename) {
				continue
			}
			key := p.PkgPath + "." + name
			e := ix.byKey[key]
			if e != nil {
				if isTestVariant(e.p) && !isTestVariant(p) {
					e.twinTN, e.twinP = e.tn, e.p
					e.tn, e.p = tn, p
				} else if isTestVariant(p) && e.twinTN == nil && !isTestVariant(e.p) {
					e.twinTN, e.twinP = tn, p
				}
				continue
			}
			e = &typeEntry{key: key, tn: tn, p: p, test: strings.HasSuffix(pos.Filename, "_test.go")}
			// Real paths matter only for repo and peer classification.
			if len(modDirs) > 0 || len(pr.peerDirs) > 0 {
				real := pr.realpath(pos.Filename)
				e.inRepo = pr.inRepoFile(real, modDirs)
				e.peer = pr.inPeer(real)
			}
			_, e.iface = named.Underlying().(*types.Interface)
			ix.byKey[key] = e
			ix.entries = append(ix.entries, e)
		}
	}
	sort.Slice(ix.entries, func(i, j int) bool { return ix.entries[i].key < ix.entries[j].key })
	for _, e := range ix.entries {
		if e.iface {
			continue
		}
		seen := map[string]bool{}
		for _, tn := range []*types.TypeName{e.tn, e.twinTN} {
			if tn == nil {
				continue
			}
			ms := pr.msets.MethodSet(types.NewPointer(tn.Type()))
			for i := 0; i < ms.Len(); i++ {
				id := ms.At(i).Obj().Id()
				if !seen[id] {
					seen[id] = true
					ix.byName[id] = append(ix.byName[id], e)
				}
			}
		}
	}
	return ix
}

// moduleDirs returns the real directories of p's module and of its
// replacement, when one of them lies under the repo root.
func (pr *program) moduleDirs(p *packages.Package) []string {
	if p.Module == nil {
		return nil
	}
	var dirs []string
	for _, m := range []*packages.Module{p.Module, p.Module.Replace} {
		if m != nil && m.Dir != "" {
			if d := pr.realpath(m.Dir); under(d, pr.repoRoot) {
				dirs = append(dirs, d)
			}
		}
	}
	return dirs
}

// inRepoFile: under the repo root and a module directory, and not inside a
// vendor or testdata directory of the repo.
func (pr *program) inRepoFile(real string, modDirs []string) bool {
	if !under(real, pr.repoRoot) {
		return false
	}
	inMod := false
	for _, d := range modDirs {
		if under(real, d) {
			inMod = true
		}
	}
	if !inMod {
		return false
	}
	rel, err := filepath.Rel(pr.repoRoot, real)
	if err != nil {
		return false
	}
	for _, part := range strings.Split(filepath.ToSlash(rel), "/") {
		if part == "vendor" || part == "testdata" {
			return false
		}
	}
	return true
}

// candidatesOf intersects the method-name postings of an interface.
func (ix *typeIndex) candidatesOf(it *types.Interface) []*typeEntry {
	var cands []*typeEntry
	for i := 0; i < it.NumMethods(); i++ {
		post := ix.byName[it.Method(i).Id()]
		if i == 0 {
			cands = append([]*typeEntry(nil), post...)
			continue
		}
		in := make(map[*typeEntry]bool, len(post))
		for _, e := range post {
			in[e] = true
		}
		kept := cands[:0]
		for _, e := range cands {
			if in[e] {
				kept = append(kept, e)
			}
		}
		cands = kept
	}
	return cands
}

// universe maps package paths to the types reachable from p, so that a type
// of one package variant can be looked up in another variant's world.
func (pr *program) universe(p *packages.Package) map[string]*types.Package {
	if u, ok := pr.universes[p]; ok {
		return u
	}
	u := map[string]*types.Package{}
	seen := map[*packages.Package]bool{}
	var walk func(q *packages.Package)
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
	pr.universes[p] = u
	return u
}

// align returns the type named like t in the universe of into, or t.
func (pr *program) align(t types.Type, into *packages.Package) types.Type {
	n, ok := types.Unalias(t).(*types.Named)
	if !ok || n.TypeArgs().Len() > 0 || !packageLevel(n.Obj()) {
		return t
	}
	tp := pr.universe(into)[n.Obj().Pkg().Path()]
	if tp == nil || tp == n.Obj().Pkg() {
		return t
	}
	if obj, ok := tp.Scope().Lookup(n.Obj().Name()).(*types.TypeName); ok && obj.Pos() == n.Obj().Pos() {
		return obj.Type()
	}
	return t
}

// packageLevel reports whether a type name is declared at package scope.
func packageLevel(tn *types.TypeName) bool {
	return tn.Pkg() != nil && tn.Parent() == tn.Pkg().Scope()
}

// alignName returns the type name declared like tn in the universe of into
// (a test variant's twin carries the test-only methods), or tn.
func (pr *program) alignName(tn *types.TypeName, into *packages.Package) *types.TypeName {
	if tn.Pkg() == nil {
		return tn
	}
	tp := pr.universe(into)[tn.Pkg().Path()]
	if tp == nil || tp == tn.Pkg() {
		return tn
	}
	if obj, ok := tp.Scope().Lookup(tn.Name()).(*types.TypeName); ok && obj.Pos() == tn.Pos() {
		return obj
	}
	return tn
}

// implementsAligned checks T and *T against the interface I, after moving
// one of them into the other's universe when a test variant is involved. It
// returns the type name and the interface it checked, for the method lookups
// that follow.
func (pr *program) implementsAligned(tn *types.TypeName, tp *packages.Package, I types.Type, ip *packages.Package) (*types.TypeName, *types.Interface, bool, bool) {
	It := I
	if tp != ip {
		if isTestVariant(tp) {
			It = pr.align(I, tp)
		} else if ip != nil && isTestVariant(ip) {
			tn = pr.alignName(tn, ip)
		}
	}
	it, ok := It.Underlying().(*types.Interface)
	if !ok {
		return tn, nil, false, false
	}
	T := tn.Type()
	val := types.Implements(T, it)
	ptr := val || types.Implements(types.NewPointer(T), it)
	return tn, it, val, ptr
}

// implVerdict says whether a type implements an interface, as far as the
// load can tell.
type implVerdict int

const (
	implNo      implVerdict = iota // not an implementer
	implYes                        // an implementer, on sound method facts
	implUnknown                    // the method facts are not sound (G3, G4, R43)
	implRefused                    // go/types says yes, but a winning method has another signature (R52)
)

// implementer decides whether the type tn of package variant tp implements
// the interface I of variant ip. It returns the type name it checked,
// whether T itself (not only *T) implements I, and the verdict. Every method
// is checked (implSoundness): go/types reports a type with an embedded field
// of invalid type as implementing every interface (go/types hasAllMethods),
// whatever its methods are (R52). A no from go/types is weighed too
// (refusal).
func (pr *program) implementer(tn *types.TypeName, tp *packages.Package, I types.Type, ip *packages.Package) (*types.TypeName, bool, implVerdict) {
	tn, it, val, ptr := pr.implementsAligned(tn, tp, I, ip)
	if !ptr {
		val, v := pr.refusal(tn, it)
		return tn, val, v
	}
	v := pr.implSoundness(tn, val, it)
	if val && v == implUnknown && hasInvalidEmbeddedFields(tn.Type(), nil) {
		// The yes for T may come only from that field; the methods may be on *T.
		if pv := pr.implSoundness(tn, false, it); pv != implUnknown {
			return tn, false, pv
		}
	}
	return tn, val, v
}

// refusal weighs a no from go/types, which compares signatures with
// types.Identical: no type is identical to an invalid one, so a winning
// method whose signature involves an invalid type (a parameter type from a
// module missing offline) may have the interface method's signature after
// all. The verdict is then unknown, unless another method settles it: one
// missing from the method set of *T, or a winning method of another, valid
// signature. (A type with an embedded field of invalid type does not get
// here: go/types says yes, and implSoundness decides.) val reports whether T
// itself, not only *T, has the methods.
func (pr *program) refusal(tn *types.TypeName, it *types.Interface) (bool, implVerdict) {
	if it == nil {
		return false, implNo
	}
	T := tn.Type()
	ms, vs := pr.msets.MethodSet(types.NewPointer(T)), pr.msets.MethodSet(T)
	val, v := true, implNo
	for i := 0; i < it.NumMethods(); i++ {
		m := it.Method(i)
		sel := ms.Lookup(m.Pkg(), m.Name())
		if sel == nil {
			return false, implNo
		}
		if involvesInvalid(sel.Obj().Type(), map[types.Type]bool{}) {
			v = implUnknown
		} else if !types.Identical(sel.Obj().Type(), m.Type()) {
			return false, implNo
		}
		val = val && vs.Lookup(m.Pkg(), m.Name()) != nil
	}
	return val && v == implUnknown, v
}

// hasInvalidEmbeddedFields reports whether t is a struct, or a pointer to
// one, with an embedded field of invalid type, directly or through embedded
// structs: the condition under which go/types stops checking methods.
func hasInvalidEmbeddedFields(t types.Type, seen map[*types.Struct]bool) bool {
	t = types.Unalias(t)
	if p, ok := t.(*types.Pointer); ok {
		t = types.Unalias(p.Elem())
	}
	st, ok := t.Underlying().(*types.Struct)
	if !ok || seen[st] {
		return false
	}
	if seen == nil {
		seen = map[*types.Struct]bool{}
	}
	seen[st] = true
	for i := 0; i < st.NumFields(); i++ {
		f := st.Field(i)
		if f.Embedded() && (isInvalid(f.Type()) || hasInvalidEmbeddedFields(f.Type(), seen)) {
			return true
		}
	}
	return false
}

// ifaceKey identifies a dispatch interface across sites.
func ifaceKey(t types.Type) string {
	t = types.Unalias(t)
	if n, ok := t.(*types.Named); ok {
		switch {
		case n.Obj().Pkg() == nil:
			return n.Obj().Name()
		case !packageLevel(n.Obj()):
			// a function-local type: its name is not unique in the package
			return fmt.Sprintf("local:%s@%d:%s", n.Obj().Pkg().Path(), n.Obj().Pos(), types.TypeString(n, (*types.Package).Path))
		}
		return types.TypeString(n, (*types.Package).Path)
	}
	k := "anon:" + types.TypeString(t, (*types.Package).Path)
	if it, ok := t.Underlying().(*types.Interface); ok {
		for i := 0; i < it.NumMethods(); i++ {
			if m := it.Method(i); !m.Exported() && m.Pkg() != nil {
				k += "@" + m.Pkg().Path()
				break
			}
		}
	}
	return k
}

// recvTypeString names a dispatch interface; it is "" for interface type
// literals, function-local named interfaces and inline constraints (R2).
func recvTypeString(t types.Type) string {
	n, ok := types.Unalias(t).(*types.Named)
	if !ok || (n.Obj().Pkg() != nil && !packageLevel(n.Obj())) {
		return ""
	}
	return types.TypeString(n, (*types.Package).Path)
}

// computeIfaces gathers the dispatch interfaces of all sites and computes
// their implementers once each.
func (pr *program) computeIfaces() {
	pr.types = pr.buildTypeIndex()
	keys := make([]siteKey, 0, len(pr.sites))
	for k := range pr.sites {
		keys = append(keys, k)
	}
	sortKeys(keys)
	for _, k := range keys {
		for _, a := range pr.sites[k] {
			if a.ifaceT == nil {
				continue
			}
			key := ifaceKey(a.ifaceT)
			job := pr.ifaceJobs[key]
			if job == nil {
				job = &ifaceJob{t: a.ifaceT, p: a.ifaceP, cands: map[string][]*Candidate{},
					recvType: recvTypeString(a.ifaceT)}
				pr.ifaceJobs[key] = job
			} else if isTestVariant(job.p) && !isTestVariant(a.ifaceP) {
				job.t, job.p = a.ifaceT, a.ifaceP
			}
		}
	}
	jobKeys := make([]string, 0, len(pr.ifaceJobs))
	for k := range pr.ifaceJobs {
		jobKeys = append(jobKeys, k)
	}
	sort.Strings(jobKeys)
	for _, k := range jobKeys {
		pr.runJob(pr.ifaceJobs[k])
	}
}

func (pr *program) runJob(job *ifaceJob) {
	it, ok := job.t.Underlying().(*types.Interface)
	if !ok || it.NumMethods() == 0 {
		job.noMethods = true
		return
	}
	// G4, R43: an interface whose package, or the package of an interface it
	// embeds, may have lost declarations may be missing methods, so a type
	// that seems to implement it may not. A declaration lost in an in-repo
	// package could also complete a type the search never meets (mayLose).
	if involvesInvalid(job.t.Underlying(), map[types.Type]bool{}) || pr.brokenDecl(job.t) ||
		pr.ifaceIncomplete(it, map[*types.Interface]bool{}) || pr.types.mayLose(it) {
		job.invalid = true
		return
	}
	for _, e := range pr.types.candidatesOf(it) {
		if !e.inRepo && job.openWorld {
			continue // the flag is already set
		}
		testOnly := e.test
		tn, val, v := pr.implementer(e.tn, e.p, job.t, job.p)
		if v == implNo && e.twinTN != nil {
			testOnly = true
			tn, val, v = pr.implementer(e.twinTN, e.twinP, job.t, job.p)
		}
		switch {
		case v == implNo || v == implRefused:
			continue // not an implementer (R52: a winning method has another signature)
		case !e.inRepo:
			job.openWorld = true
			continue
		case v == implUnknown:
			// G4: an in-repo type that may implement the interface leaves the
			// candidate set open, so none is offered.
			job.invalid = true
			return
		}
		job.impls = append(job.impls, &impl{e: e, tn: tn, val: val, testOnly: testOnly})
	}
}

// implSoundness checks each method the interface needs in the method set of
// T (val) or *T: present, from a real declaration without invalid types
// (gate G4), not one a lost or unknown shallower member could hide (G3,
// R43), and with the interface method's signature (R52). A winning method of
// another signature refuses T; any other doubt is unknown.
func (pr *program) implSoundness(tn *types.TypeName, val bool, it *types.Interface) implVerdict {
	var recv types.Type = tn.Type()
	if !val {
		recv = types.NewPointer(recv)
	}
	if !pr.realRecv(recv) {
		return implUnknown
	}
	ms := pr.msets.MethodSet(recv)
	v := implYes
	for i := 0; i < it.NumMethods(); i++ {
		m := it.Method(i)
		sel := ms.Lookup(m.Pkg(), m.Name())
		if sel == nil {
			// go/types said yes only because an embedded field of invalid type
			// may have the method.
			v = implUnknown
			continue
		}
		if depth := len(sel.Index()) - 1; depth > 0 && pr.embedInvalid(recv, depth) {
			v = implUnknown
			continue
		}
		f := sel.Obj().(*types.Func).Origin()
		if !pr.realObj(f) || involvesInvalid(f.Type(), map[types.Type]bool{}) {
			v = implUnknown
			continue
		}
		if !types.Identical(sel.Obj().Type(), m.Type()) {
			return implRefused
		}
	}
	return v
}

// candidatesFor lists, for one called method, each implementer's method.
func (pr *program) candidatesFor(job *ifaceJob, m *types.Func) []*Candidate {
	if c, ok := job.cands[m.Id()]; ok {
		return c
	}
	out := []*Candidate{}
	for _, im := range job.impls {
		var recv types.Type = im.tn.Type()
		if !im.val {
			recv = types.NewPointer(recv)
		}
		sel := pr.msets.MethodSet(recv).Lookup(m.Pkg(), m.Name())
		if sel == nil {
			continue
		}
		f := sel.Obj().(*types.Func).Origin()
		pos := pr.fset.PositionFor(f.Pos(), false)
		if !pos.IsValid() {
			continue
		}
		real := pr.realpath(pos.Filename)
		out = append(out, &Candidate{
			Path: real, Line: pos.Line, Col: pos.Column, Name: f.Name(), Qualname: methodQualname(f), Pkg: f.Pkg().Path(),
			Type: im.tn.Name(), Pointer: !im.val,
			TestOnly:         im.testOnly || strings.HasSuffix(real, "_test.go"),
			ViaEmbeddedIface: isInterface(f.Signature().Recv().Type()),
		})
	}
	sort.Slice(out, func(i, j int) bool {
		a, b := out[i], out[j]
		if a.Path != b.Path {
			return a.Path < b.Path
		}
		if a.Line != b.Line {
			return a.Line < b.Line
		}
		if a.Col != b.Col {
			return a.Col < b.Col
		}
		return a.Type < b.Type
	})
	job.cands[m.Id()] = out
	return out
}

// ifaceInfo builds the iface field of an emitted site.
func (pr *program) ifaceInfo(a *answer) *IfaceInfo {
	rec := a.rec
	if a.ifaceT == nil {
		if (rec.Class == "iface" || rec.Class == "typeparam") && rec.Target != nil {
			return &IfaceInfo{Candidates: []*Candidate{}, OpenWorld: true, Suppressed: true}
		}
		return nil
	}
	job := pr.ifaceJobs[ifaceKey(a.ifaceT)]
	info := &IfaceInfo{RecvType: job.recvType, Candidates: []*Candidate{}}
	switch {
	case job.noMethods:
		return info
	case rec.PkgErrors || job.invalid:
		info.Suppressed, info.OpenWorld = true, true
		return info
	}
	info.Candidates = pr.candidatesFor(job, a.method)
	info.OpenWorld = job.openWorld
	return info
}
