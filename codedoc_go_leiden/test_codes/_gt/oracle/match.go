package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"path/filepath"
	"sort"
	"strings"
)

// Edge is one resolver answer to judge: the site (file, line and 1-based
// byte column of the callee token, plus its name) and the target declaration,
// given by position (file + line of the name identifier, or of the func
// keyword for a closure), by C3 qualified name (file + qualname) or by
// runtime-style name (qname, type parameters dropped).
type Edge struct {
	File   string `json:"file"`
	Line   int    `json:"line"`
	Col    int    `json:"col"`
	Name   string `json:"name"`
	Target Target `json:"target"`
	// Candidates are the resolver's implementers of an interface edge
	// (spec R6): when given, they are the set every observed callee must
	// belong to.
	Candidates []Target `json:"candidates,omitempty"`
	Note       string   `json:"note,omitempty"`
}

// Target names a declaration. Kind "iface-method" (also "iface",
// "interface_method") marks an interface method; a target found among the
// table's interface methods is one too.
type Target struct {
	File     string `json:"file,omitempty"`
	Line     int    `json:"line,omitempty"`
	Col      int    `json:"col,omitempty"`
	Qualname string `json:"qualname,omitempty"`
	QName    string `json:"qname,omitempty"`
	Kind     string `json:"kind,omitempty"`
	// ViaEmbedded marks a candidate reached through an embedded interface
	// field (spec §5.4 second dispatch): any implementer may run behind it.
	ViaEmbedded bool `json:"via_embedded_iface,omitempty"`
}

// candSet is the implementer set of an interface edge (spec R50/R6).
type candSet struct {
	given   bool       // the record has a "candidates" key (an empty list included)
	fns     []*FuncRec // candidates found in the table
	open    bool       // a second dispatch: any method of the name may run
	missing []string   // candidates in instrumented files that match no function
}

func (t *table) candidates(list []Target) candSet {
	cs := candSet{given: list != nil}
	for _, ct := range list {
		ct.File = t.relPath(ct.File)
		if ct.ViaEmbedded {
			cs.open = true
			continue
		}
		fn, im := t.resolveTarget(ct)
		switch {
		case im != nil:
			cs.open = true // an interface method as candidate is a second dispatch
		case fn != nil:
			cs.fns = append(cs.fns, fn)
		default:
			// a candidate in a file the oracle does not instrument is never
			// observed; one in an instrumented file must be found
			if f := t.byRel[ct.File]; f != nil && f.Status == "instrumented" {
				cs.missing = append(cs.missing, fmt.Sprintf("%s:%d %s", ct.File, ct.Line, ct.Qualname))
			}
		}
	}
	return cs
}

// relPath makes an absolute path under the copy or the source tree relative
// to it, the form the table uses.
func (t *table) relPath(p string) string {
	if !filepath.IsAbs(p) || t.meta == nil {
		return p
	}
	for _, root := range []string{t.meta.Root, t.meta.Src} {
		if root == "" {
			continue
		}
		if rel, err := filepath.Rel(root, p); err == nil && !strings.HasPrefix(rel, "..") {
			return filepath.ToSlash(rel)
		}
	}
	return p
}

// Verdict is one judged edge (match) or case (judge).
type Verdict struct {
	Verdict  string   `json:"verdict"`
	ID       string   `json:"id,omitempty"`
	File     string   `json:"file"`
	Line     int      `json:"line"`
	Col      int      `json:"col"`
	Name     string   `json:"name,omitempty"`
	Target   *Target  `json:"target,omitempty"`
	Observed []string `json:"observed"`
	Exec     int64    `json:"exec"`
	Reach    int64    `json:"reach"`
	Hits     uint64   `json:"hits"`
	Detail   string   `json:"detail,omitempty"`
	Note     string   `json:"note,omitempty"`
}

// wrongVerdicts are the verdicts that count as wrong (spec §9.4).
var wrongVerdicts = map[string]bool{"WRONG-OTHER": true, "WRONG-MULTI": true, "IFACE-OTHER": true,
	"DEFER-GROUP-OTHER": true}

func normQName(q string) string {
	// pkg.(*T[...]).M and pkg.(*T).M are the same declaration
	return unescapeName(strings.ReplaceAll(q, "[...]", ""))
}

func isIfaceKind(k string) bool {
	return k == "iface-method" || k == "iface" || k == "interface_method"
}

// siteIndex finds sites by file, line and name; a copied symlink also answers
// for its target file.
type siteIndex map[string][]*SiteRec

func newSiteIndex(t *table) siteIndex {
	idx := siteIndex{}
	for _, s := range t.sites {
		idx[fmt.Sprintf("%s:%d:%s", s.File, s.Line, s.Name)] = append(idx[fmt.Sprintf("%s:%d:%s", s.File, s.Line, s.Name)], s)
		if f := t.byRel[s.File]; f != nil && f.Real != "" {
			k := fmt.Sprintf("%s:%d:%s", f.Real, s.Line, s.Name)
			idx[k] = append(idx[k], s)
		}
	}
	for _, v := range idx {
		sort.Slice(v, func(i, j int) bool { return v[i].Sid < v[j].Sid })
	}
	return idx
}

// lookupSites returns the sites at (file, line, col, name): the site of file
// itself and the sites of copied symlinks whose target is file. Spec §5.1
// (G1) treats them as one site, keyed by real path; a copy compiled in
// another package is a variant of it, so their observations are merged.
func (t *table) lookupSites(idx siteIndex, file string, line, col int, name string, stmt string) []*SiteRec {
	var out []*SiteRec
	for _, c := range idx[fmt.Sprintf("%s:%d:%s", file, line, name)] {
		if c.Col == col && (stmt == "" || c.Stmt == stmt) {
			out = append(out, c)
		}
	}
	return out
}

// resolveTarget maps a target to a function or an interface method.
func (t *table) resolveTarget(tg Target) (*FuncRec, *IMethodRec) {
	inFile := func(file string) bool {
		if tg.File == "" || file == tg.File {
			return true
		}
		f := t.byRel[file]
		return f != nil && f.Real == tg.File
	}
	if tg.File != "" && tg.Line > 0 {
		var found []*FuncRec
		for _, fn := range t.funcs {
			if fn.Line == tg.Line && inFile(fn.File) && (tg.Col == 0 || fn.Col == tg.Col) &&
				(tg.Qualname == "" || fn.Qualname == tg.Qualname) {
				found = append(found, fn)
			}
		}
		// several functions on one line (a one-line closure): the position
		// alone does not say which one; a copied symlink repeats the same one
		sort.Slice(found, func(i, j int) bool { return found[i].Fid < found[j].Fid })
		for _, fn := range found[min(1, len(found)):] {
			if fn.Qualname != found[0].Qualname || fn.Col != found[0].Col {
				return nil, nil
			}
		}
		if len(found) > 0 {
			return found[0], nil
		}
		for _, m := range t.imethods {
			if m.Line == tg.Line && inFile(m.File) && (tg.Col == 0 || m.Col == tg.Col) {
				return nil, m
			}
		}
	}
	if tg.File != "" && tg.Qualname != "" {
		if fns := t.funcByQual(tg.File, tg.Qualname); len(fns) > 0 {
			sort.Slice(fns, func(i, j int) bool { return fns[i].Fid < fns[j].Fid })
			return fns[0], nil
		}
		if m := t.imethodByQual(tg.File, tg.Qualname); m != nil {
			return nil, m
		}
	}
	if tg.QName != "" {
		want := normQName(tg.QName)
		for _, fn := range t.funcs {
			if normQName(fn.QName) == want {
				return fn, nil
			}
		}
		for _, m := range t.imethods {
			if normQName(m.QName) == want {
				return nil, m
			}
		}
	}
	return nil, nil
}

// sameDecl reports whether a callee is the target function: the same fid,
// or the same declaration in a copied symlink.
func (t *table) sameDecl(c *Callee, fn *FuncRec) bool {
	if fn == nil {
		return false
	}
	if c.Fid == fn.Fid {
		return true
	}
	cf := t.funcs[c.Fid]
	if cf == nil || cf.Line != fn.Line || cf.Col != fn.Col || cf.Qualname != fn.Qualname {
		return false
	}
	return realOf(t, cf.File) == realOf(t, fn.File)
}

func realOf(t *table, file string) string {
	if f := t.byRel[file]; f != nil && f.Real != "" {
		return f.Real
	}
	return file
}

func calleeNames(obs *Obs) []string {
	var names []string
	for _, c := range obs.Callees {
		names = append(names, c.QName)
	}
	sort.Strings(names)
	return names
}

// ifaceCallee reports whether a callee can be the dynamic target of a call of
// interface method name: a method (it has a receiver) of that name.
func ifaceCallee(t *table, c *Callee, name string) bool {
	fn := t.funcs[c.Fid]
	return fn != nil && fn.Recv != "" && fn.Name == name
}

// judgeEdge applies the verdict rules of spec §9.4 to one resolver edge at
// site s. method is the interface method's name when iface is set, and cs
// the edge's implementer set; panicked counts the entries that
// runtime.gopanic or runtime.Goexit ran, by (deferring function, callee).
func judgeEdge(t *table, s *SiteRec, v siteView, target *FuncRec, iface bool, method string, cs candSet,
	panicked map[[2]int]uint64) (string, string) {
	o := v.obs
	if !iface && target == nil {
		return "NO-TARGET", "" // no declaration of the table matches the target: fix the record, not a verdict
	}
	if o == nil || (len(o.Callees) == 0 && o.Exec <= 0) {
		return "UNEXERCISED", ""
	}
	names := calleeNames(o)
	if s.Stmt == "defer" && v.reach == 0 {
		return "UNEXERCISED", strings.Join(names, ", ") // this defer statement never ran
	}
	if iface {
		if v.group {
			// other defers of the function share the group set
			for _, c := range o.Callees {
				if ifaceCallee(t, c, method) {
					return "DEFER-GROUP-CONSISTENT", strings.Join(names, ", ")
				}
			}
			return "UNEXERCISED", strings.Join(names, ", ")
		}
		if len(o.Callees) == 0 {
			return "UNEXERCISED", "no instrumented callee observed" // nothing to compare
		}
		// an interface method has no body; the edge is consistent when every
		// instrumented callee is a method of that name and one of the
		// resolver's candidates (spec R50, R6). Implementations outside the
		// instrumented code are allowed: the world is open. Without a
		// candidates key, or behind a second dispatch, the name decides. A
		// method of a generic type is never a candidate (spec §5.4 offers
		// non-generic types only), so the name decides for it too.
		var notes []string
		for _, c := range o.Callees {
			if !ifaceCallee(t, c, method) {
				return "IFACE-OTHER", strings.Join(names, ", ")
			}
			if !cs.given || cs.open {
				continue
			}
			known := false
			for _, fn := range cs.fns {
				known = known || t.sameDecl(c, fn)
			}
			switch {
			case known:
			case strings.Contains(t.funcs[c.Fid].RTName, "[...]"):
				notes = append(notes, c.QName+" is a generic implementer, outside the candidate universe")
			case len(cs.missing) > 0:
				return "NO-TARGET", c.QName + " is not among the candidates, and candidates " +
					strings.Join(cs.missing, ", ") + " match no function of the table"
			default:
				return "IFACE-OTHER", strings.Join(names, ", ") + "; " + c.QName + " is not among the candidates"
			}
		}
		detail := strings.Join(names, ", ")
		if len(notes) > 0 {
			detail += "; " + strings.Join(notes, "; ")
		}
		return "IFACE-CONSISTENT", detail
	}
	if !target.Hooked {
		// the target records nothing: an observed callee still proves a
		// different function ran, counts prove nothing
		if len(o.Callees) > 0 && !v.group {
			return "WRONG-OTHER", strings.Join(names, ", ")
		}
		return "UNVERIFIABLE-TARGET", strings.Join(names, ", ")
	}
	hit := false
	for _, c := range o.Callees {
		if t.sameDecl(c, target) {
			hit = true
		}
	}
	if v.group {
		if hit {
			return "DEFER-GROUP-CONSISTENT", strings.Join(names, ", ")
		}
		if panicked[[2]int{s.Fid, target.Fid}] > 0 {
			// the target ran while a function was panicking: that entry is
			// in no group set
			return "UNEXERCISED", strings.Join(names, ", ") + "; the target ran under runtime.gopanic"
		}
		return "DEFER-GROUP-OTHER", strings.Join(names, ", ")
	}
	// entries that reached no instrumented function: the statement completed
	// more often than instrumented code was entered
	unins := int64(0)
	if o.Exec >= 0 && o.Exec > int64(o.Hits) {
		unins = o.Exec - int64(o.Hits)
		names = append(names, fmt.Sprintf("<%d uninstrumented>", unins))
	}
	detail := strings.Join(names, ", ")
	others := len(o.Callees) > 1 || (len(o.Callees) == 1 && !hit)
	switch {
	case others && hit:
		return "WRONG-MULTI", detail
	case len(o.Callees) > 0 && !hit:
		return "WRONG-OTHER", detail
	case hit && unins > 0:
		return "WRONG-MULTI", detail
	case !hit && unins > 0:
		return "WRONG-OTHER", detail
	case hit && o.Exec >= 0 && o.Exec == int64(o.Hits):
		return "CONFIRMED", detail
	case hit:
		// no completion counter, or fewer completions than entries (a
		// panic or exit after the entry): entries outside instrumented code
		// cannot be ruled out
		return "CONFIRMED-NOEXEC", detail
	}
	return "UNEXERCISED", detail
}

func cmdMatch(args []string, stdout io.Writer) int {
	fs := flag.NewFlagSet("match", flag.ExitOnError)
	obsPath := fs.String("obs", "", "observations from report")
	recordsPath := fs.String("records", "", "resolver edges (JSON lines)")
	sitesPath := fs.String("sites", "", "site table (default: from the observations' meta line)")
	out := fs.String("out", "", "verdicts (JSON lines)")
	check(fs.Parse(args))
	if *obsPath == "" || *recordsPath == "" || fs.NArg() > 0 {
		fs.Usage()
		return 2
	}
	obs := loadObs(*obsPath)
	if *sitesPath == "" {
		if obs.meta == nil || obs.meta.Sites == "" {
			check(fmt.Errorf("%s has no meta line; pass --sites", *obsPath))
		}
		*sitesPath = obs.meta.Sites
	}
	t := loadTable(*sitesPath)
	idx := newSiteIndex(t)
	counts := map[string]int{}
	var verdicts []any
	readJSONL(*recordsPath, func(b []byte) {
		e := &Edge{}
		check(json.Unmarshal(b, e))
		e.File, e.Target.File = t.relPath(e.File), t.relPath(e.Target.File)
		vd := &Verdict{File: e.File, Line: e.Line, Col: e.Col, Name: e.Name, Target: &e.Target, Note: e.Note,
			Exec: -1, Reach: -1, Observed: []string{}}
		sites := t.lookupSites(idx, e.File, e.Line, e.Col, e.Name, "")
		if len(sites) == 0 {
			// the column may come from another convention: a unique name on
			// the line of the file itself is enough
			var own []*SiteRec
			for _, c := range idx[fmt.Sprintf("%s:%d:%s", e.File, e.Line, e.Name)] {
				if c.File == e.File {
					own = append(own, c)
				}
			}
			if len(own) == 1 {
				sites = own
			}
		}
		if len(sites) == 0 {
			vd.Verdict = "NO-SITE"
		} else {
			target, im := t.resolveTarget(e.Target)
			// the kind marks an interface method only when the table does
			// not know the target (an interface outside the instrumented tree)
			iface := im != nil || (target == nil && isIfaceKind(e.Target.Kind))
			method := ""
			switch {
			case im != nil:
				method = im.Name
			case target != nil:
				method = target.Name
			default:
				q := e.Target.Qualname
				if q == "" {
					q = e.Target.QName
				}
				method = q[strings.LastIndexByte(q, '.')+1:]
			}
			var views []siteView
			for _, st := range sites {
				views = append(views, viewOf(t, st, obs.bySite, obs.byGroup))
			}
			v := mergeViews(t, views)
			vd.Verdict, vd.Detail = judgeEdge(t, sites[0], v, target, iface, method, t.candidates(e.Candidates), obs.panicked)
			if v.obs != nil {
				vd.Observed = calleeNames(v.obs)
				vd.Exec, vd.Hits = v.obs.Exec, v.obs.Hits
			}
			vd.Reach = v.reach
		}
		counts[vd.Verdict]++
		verdicts = append(verdicts, vd)
		tname := e.Target.Qualname
		if tname == "" {
			tname = e.Target.QName
		}
		if tname == "" {
			tname = fmt.Sprintf("%s:%d", e.Target.File, e.Target.Line)
		}
		fmt.Fprintf(stdout, "%-22s %s:%d:%d %s -> %s  observed: %s %s\n", vd.Verdict, e.File, e.Line, e.Col, e.Name,
			tname, vd.Detail, e.Note)
	})
	if *out != "" {
		writeJSONL(*out, verdicts)
	}
	fmt.Fprintln(stdout, "summary:", summaryLine(counts))
	for v := range counts {
		if wrongVerdicts[v] {
			return 1
		}
	}
	return 0
}

func summaryLine(counts map[string]int) string {
	var keys []string
	for k := range counts {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var parts []string
	for _, k := range keys {
		parts = append(parts, fmt.Sprintf("%s=%d", k, counts[k]))
	}
	return strings.Join(parts, " ")
}
