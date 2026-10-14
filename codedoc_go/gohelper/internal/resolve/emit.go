package resolve

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"go/types"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"golang.org/x/tools/go/packages"
)

// changedDuringLoad replaces a file hash when two reads of one file within the
// load disagree; it never equals a real hash, so the file counts as stale.
const changedDuringLoad = "changed-during-load"

func sortKeys(keys []siteKey) {
	sort.Slice(keys, func(i, j int) bool {
		if keys[i].path != keys[j].path {
			return keys[i].path < keys[j].path
		}
		return keys[i].off < keys[j].off
	})
}

func sortedSet(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// better orders the variants of one site: the variant that loaded the real
// path, then the non-test variant, then the smallest package ID.
func better(a, b *answer) bool {
	if a.home != b.home {
		return a.home
	}
	if a.normal != b.normal {
		return a.normal
	}
	return a.variant < b.variant
}

func (pr *program) emit(out *writer) *summaryRecord {
	sum := &summaryRecord{Kind: "summary"}
	rootPkgs := map[string]bool{}
	for _, r := range pr.roots {
		if !isTestVariant(r) && !isTestMain(r) {
			rootPkgs[r.PkgPath] = true
		}
	}
	sum.Packages = len(rootPkgs)

	paths := make([]string, 0, len(pr.files))
	for p := range pr.files {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	for _, p := range paths {
		fi := pr.files[p]
		out.write(&fileRecord{Kind: "file", Path: p, SHA256: pr.fileHash(p, fi), Packages: sortedSet(fi.packages)})
	}

	keys := make([]siteKey, 0, len(pr.sites))
	for k := range pr.sites {
		keys = append(keys, k)
	}
	sortKeys(keys)
	recs := make([]any, len(keys))
	for i, k := range keys {
		recs[i] = pr.siteOutput(pr.sites[k])
		switch recs[i].(type) {
		case *siteRecord:
			sum.Sites++
		case *conversionRecord:
			sum.Conversions++
		case *notcallRecord:
			sum.Notcalls++
		}
	}
	out.writeAll(recs)

	ikeys := make([]siteKey, 0, len(pr.imports))
	for k := range pr.imports {
		ikeys = append(ikeys, k)
	}
	sortKeys(ikeys)
	for _, k := range ikeys {
		out.write(pr.imports[k])
	}
	for _, r := range pr.embedRecords() {
		out.write(r)
	}
	for _, r := range pr.implementsRecords() {
		out.write(r)
	}
	for _, r := range pr.errorRecords() {
		sum.Errors++
		out.write(r)
	}
	return sum
}

// siteOutput merges the variants' answers for one site into one record.
func (pr *program) siteOutput(as []*answer) any {
	best := as[0]
	for _, a := range as[1:] {
		if better(a, best) {
			best = a
		}
	}
	ids := map[string]bool{}
	errs := false
	for _, a := range as {
		ids[a.identity()] = true
		if a.rec != nil && a.rec.PkgErrors {
			errs = true
		}
	}
	if len(ids) == 1 {
		switch {
		case best.conv:
			return &conversionRecord{Kind: "conversion", Path: best.path, Offset: best.off}
		case best.notcall:
			return &notcallRecord{Kind: "notcall", Path: best.path, Offset: best.off, SiteKind: "range"}
		}
		best.rec.PkgErrors = errs
		best.rec.Iface = pr.ifaceInfo(best)
		return best.rec
	}
	// The variants disagree. A production conversion or notcall stands unless
	// another variant names a target; then that variant's site is reported.
	if best.rec == nil {
		var alt *answer
		for _, a := range as {
			if a.rec != nil && (a.rec.Target != nil || a.rec.Alias != nil) && (alt == nil || better(a, alt)) {
				alt = a
			}
		}
		if alt == nil {
			if best.conv {
				return &conversionRecord{Kind: "conversion", Path: best.path, Offset: best.off}
			}
			return &notcallRecord{Kind: "notcall", Path: best.path, Offset: best.off, SiteKind: "range"}
		}
		best = alt
	}
	seen := map[string]bool{}
	variants := []*Target{}
	embedInvalid, stub := false, false
	for _, a := range as {
		if a.rec == nil {
			continue
		}
		embedInvalid = embedInvalid || a.rec.EmbedInvalid
		stub = stub || a.rec.Stub
		t := a.rec.Target
		if t == nil {
			t = a.rec.Alias
		}
		if t == nil || seen[targetKey(t)] {
			continue
		}
		seen[targetKey(t)] = true
		variants = append(variants, t)
	}
	sort.Slice(variants, func(i, j int) bool { return targetKey(variants[i]) < targetKey(variants[j]) })
	rec := best.rec
	rec.Variants = variants
	rec.PkgErrors = errs
	if embedInvalid || stub {
		// Traps 2 and 3 per variant: the most conservative answer wins (R1).
		rec.EmbedInvalid = rec.EmbedInvalid || embedInvalid
		rec.Stub = rec.Stub || stub
		rec.Target, rec.Alias, rec.Iface = nil, nil, nil
		return rec
	}
	rec.Iface = pr.ifaceInfo(best)
	return rec
}

// fileHash is the SHA-256 of the bytes the load parsed for a real path.
func (pr *program) fileHash(real string, fi *fileInfo) string {
	h := ""
	for name := range fi.names {
		v, ok := pr.parsedHash[name]
		if !ok {
			continue
		}
		if h == "" {
			h = v
		} else if h != v {
			return changedDuringLoad
		}
	}
	if h != "" {
		return h
	}
	b, err := os.ReadFile(real)
	if err != nil {
		return changedDuringLoad
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// repoTypes yields the in-repo package-level named types of every accepted
// package variant, keyed by declaration position.
func (pr *program) repoTypes(fn func(p *packages.Package, tn *types.TypeName, ref *TypeRef)) {
	for _, p := range pr.all {
		if p.Module == nil || !p.Module.Main || p.Types == nil || isTestMain(p) {
			continue
		}
		scope := p.Types.Scope()
		for _, name := range scope.Names() {
			tn, ok := scope.Lookup(name).(*types.TypeName)
			if !ok || tn.IsAlias() {
				continue
			}
			pos := pr.fset.PositionFor(tn.Pos(), false)
			if !pos.IsValid() {
				continue
			}
			real, ok := pr.accept(p, pos.Filename)
			if !ok {
				continue
			}
			fn(p, tn, &TypeRef{Path: real, Line: pos.Line, Col: pos.Column, Name: tn.Name(), Pkg: tn.Pkg().Path()})
		}
	}
}

func typeRefKey(r *TypeRef) string {
	return r.Path + ":" + strconv.Itoa(r.Line) + ":" + strconv.Itoa(r.Col)
}

// embedRecords lists every embedded field of the in-repo types. The embedded
// type is null when it is invalid, a stub's copy, or differs between package
// variants.
func (pr *program) embedRecords() []*embedRecord {
	type slot struct {
		owner *TypeRef
		recs  map[string]*embedRecord
	}
	slots := map[string]*slot{}
	add := func(owner *TypeRef, idx string, t types.Type, ptr bool) {
		k := typeRefKey(owner) + "#" + idx
		s := slots[k]
		if s == nil {
			s = &slot{owner: owner, recs: map[string]*embedRecord{}}
			slots[k] = s
		}
		tgt := pr.embeddedTarget(t)
		id := targetKey(tgt) + "|" + strconv.FormatBool(ptr)
		if _, ok := s.recs[id]; !ok {
			s.recs[id] = &embedRecord{Kind: "embed", Type: owner, Embedded: tgt, Pointer: ptr}
		}
	}
	pr.repoTypes(func(p *packages.Package, tn *types.TypeName, ref *TypeRef) {
		switch u := tn.Type().Underlying().(type) {
		case *types.Struct:
			for i := 0; i < u.NumFields(); i++ {
				f := u.Field(i)
				if !f.Embedded() {
					continue
				}
				t, ptr := f.Type(), false
				if pt, ok := types.Unalias(t).(*types.Pointer); ok {
					t, ptr = pt.Elem(), true
				}
				add(ref, fmt.Sprintf("f%05d", i), t, ptr)
			}
		case *types.Interface:
			for i := 0; i < u.NumEmbeddeds(); i++ {
				// Only embedded interfaces are embeddings; union and type-set
				// elements of constraints and interface literals are not.
				if e := u.EmbeddedType(i); embeddedInterface(e) {
					add(ref, fmt.Sprintf("e%05d", i), e, false)
				}
			}
		}
	})
	keys := make([]string, 0, len(slots))
	for k := range slots {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var out []*embedRecord
	for _, k := range keys {
		s := slots[k]
		var r *embedRecord
		for _, x := range s.recs {
			r = x
		}
		if len(s.recs) > 1 {
			r = &embedRecord{Kind: "embed", Type: s.owner, Embedded: nil, Pointer: r.Pointer}
		}
		out = append(out, r)
	}
	return out
}

// embeddedInterface reports whether an element of an interface is an
// embedded interface: a named interface (comparable, error and any included),
// or an invalid type, which may have been one.
func embeddedInterface(e types.Type) bool {
	if a, ok := e.(*types.Alias); ok && a.Obj().Pkg() == nil {
		return true // any
	}
	switch x := types.Unalias(e).(type) {
	case *types.Named:
		return isInterface(x)
	case *types.Basic:
		return x.Kind() == types.Invalid
	}
	return false
}

// embeddedTarget is the declaration of an embedded type: a named type
// (origin of an instance), or a predeclared type in builtin.go; nil for
// invalid types and stub copies.
func (pr *program) embeddedTarget(t types.Type) *Target {
	if a, ok := t.(*types.Alias); ok && a.Obj().Pkg() == nil {
		// any, declared in builtin.go
		tgt := &Target{Name: a.Obj().Name(), Qualname: a.Obj().Name(), Obj: "type"}
		if p, ok := pr.universeDecl(a.Obj().Name()); ok {
			tgt.Path, tgt.Line, tgt.Col = p.Filename, p.Line, p.Column
		}
		return tgt
	}
	switch x := types.Unalias(t).(type) {
	case *types.Named:
		tn := x.Origin().Obj()
		if !pr.realObj(tn) {
			return nil
		}
		return pr.target(tn)
	case *types.Basic:
		if x.Kind() == types.Invalid {
			return nil
		}
		tgt := &Target{Name: x.Name(), Qualname: x.Name(), Obj: "type"}
		if p, ok := pr.universeDecl(x.Name()); ok {
			tgt.Path, tgt.Line, tgt.Col = p.Filename, p.Line, p.Column
		}
		return tgt
	}
	return nil
}

// implementsRecords lists (in-repo concrete type, in-repo or peer interface)
// pairs. Interfaces involving invalid types are skipped, and so are types
// whose winning method has another signature (R52). A pair whose method set
// facts are not sound (gate G4: a stub's method or invalid types; G3, R43: a
// promoted method that a lost or unknown member could shadow, or a method an
// embedded field of invalid type may provide; an interface whose package may
// have lost declarations) is marked incomplete, and Phase 1.6 does not edge it.
func (pr *program) implementsRecords() []*implementsRecord {
	ix := pr.types
	var out []*implementsRecord
	for _, ie := range ix.entries {
		if !ie.iface || !(ie.inRepo || ie.peer) {
			continue
		}
		it := ie.tn.Type().Underlying().(*types.Interface)
		if it.NumMethods() == 0 || !it.IsMethodSet() || involvesInvalid(it, map[types.Type]bool{}) {
			continue
		}
		iref := pr.typeRef(ie.tn)
		ifaceBroken := pr.brokenDecl(ie.tn.Type()) || pr.ifaceIncomplete(it, map[*types.Interface]bool{})
		for _, e := range ix.candidatesOf(it) {
			if !e.inRepo {
				continue
			}
			_, val, v := pr.implementer(e.tn, e.p, ie.tn.Type(), ie.p)
			if v == implNo || v == implRefused {
				continue
			}
			out = append(out, &implementsRecord{Kind: "implements", Type: pr.typeRef(e.tn), Iface: iref, PointerReceiver: !val,
				Incomplete: ifaceBroken || v == implUnknown})
		}
	}
	sort.Slice(out, func(i, j int) bool {
		a, b := typeRefKey(out[i].Type), typeRefKey(out[j].Type)
		if a != b {
			return a < b
		}
		return typeRefKey(out[i].Iface) < typeRefKey(out[j].Iface)
	})
	return out
}

func (pr *program) typeRef(tn *types.TypeName) *TypeRef {
	pos := pr.fset.PositionFor(tn.Pos(), false)
	return &TypeRef{Path: pr.realpath(pos.Filename), Line: pos.Line, Col: pos.Column, Name: tn.Name(), Pkg: tn.Pkg().Path()}
}

// errorRecords lists every package error of the load, deduplicated.
func (pr *program) errorRecords() []*errorRecord {
	seen := map[string]bool{}
	var out []*errorRecord
	for _, p := range pr.all {
		for _, e := range p.Errors {
			file, line := splitPos(e.Pos)
			if file != "" {
				if !filepath.IsAbs(file) {
					file = filepath.Join(pr.opts.Dir, file)
				}
				file = pr.realpath(file)
			}
			k := p.PkgPath + "\x00" + file + "\x00" + strconv.Itoa(line) + "\x00" + e.Msg
			if seen[k] {
				continue
			}
			seen[k] = true
			out = append(out, &errorRecord{Kind: "error", Pkg: p.PkgPath, Path: file, Line: line, Msg: e.Msg})
		}
	}
	return out
}

// splitPos splits "file:line:col" or "file:line" ("" and "-" have no file).
func splitPos(pos string) (string, int) {
	if pos == "" || pos == "-" {
		return "", 0
	}
	parts := strings.Split(pos, ":")
	n, nums := len(parts), 0
	for nums < 2 && n-nums-1 >= 1 {
		if _, err := strconv.Atoi(parts[n-nums-1]); err != nil {
			break
		}
		nums++
	}
	if nums == 0 {
		return pos, 0
	}
	line, _ := strconv.Atoi(parts[n-nums])
	return strings.Join(parts[:n-nums], ":"), line
}
