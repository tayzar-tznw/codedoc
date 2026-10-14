package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

// hit is one line of a hits-*.jsonl file written by package rt.
type hit struct {
	Kind   string   `json:"kind"` // meta, call, go, done
	Fid    int      `json:"fid"`  // done: the counter id
	Callee string   `json:"callee"`
	Caller string   `json:"caller"` // runtime name of the logical caller (go: the creator)
	File   string   `json:"file"`
	Line   int      `json:"line"`
	N      uint64   `json:"n"`
	Defer  bool     `json:"defer"` // run by runtime.deferreturn (rt saw it as the physical caller)
	Stack  []string `json:"stack"` // panic: logical frames above runtime.gopanic / runtime.Goexit
	Owner  string   `json:"owner"` // panic: the deferwrap closure that ran the entry, if one showed
}

// Callee is one function entered from a site.
type Callee struct {
	Fid      int    `json:"fid"`
	QName    string `json:"qname"`
	RTName   string `json:"rtname"`
	Qualname string `json:"qualname"`
	File     string `json:"file"`
	Line     int    `json:"line"`
	N        uint64 `json:"n"`
}

// Obs is the oracle's answer for one site (or one defer group).
type Obs struct {
	Kind    string    `json:"kind"` // site, defer-group
	Sid     int       `json:"sid,omitempty"`
	File    string    `json:"file"`
	Line    int       `json:"line"`
	Col     int       `json:"col,omitempty"`
	Name    string    `json:"name,omitempty"`
	Stmt    string    `json:"stmt,omitempty"`
	Form    string    `json:"form,omitempty"`
	Via     string    `json:"via"` // tag, line, go, defer-group, defer-single, exec-only, reach-only
	Fid     int       `json:"fid,omitempty"`
	QName   string    `json:"qname,omitempty"`
	Defers  []int     `json:"defer_sites,omitempty"`
	Exec    int64     `json:"exec"`  // completions of the enclosing simple statement; -1 = no completion counter
	Reach   int64     `json:"reach"` // starts of the enclosing return or control statement; -1 = no reach counter
	Run     int64     `json:"run"`   // completed runs of a deferred call; -1 = no run counter
	Hits    uint64    `json:"hits"`  // instrumented entries attributed to the site
	Callees []*Callee `json:"callees"`
}

// PanicRec counts the entries of a function (Fid) that runtime.gopanic or
// runtime.Goexit ran while Deferrer, a function with defers, was on the
// stack: deferred calls during a panic or Goexit, which no site or defer
// group can be credited with.
type PanicRec struct {
	Kind     string `json:"kind"` // "panic-deferred"
	Fid      int    `json:"fid"`
	QName    string `json:"qname"`
	Deferrer int    `json:"deferrer"`
	N        uint64 `json:"n"`
}

// ObsMeta is the first line of an observations file.
type ObsMeta struct {
	Kind  string   `json:"kind"` // "meta"
	Sites string   `json:"sites"`
	Root  string   `json:"root"`
	Hits  []string `json:"hits"`
	Files int      `json:"hit_files"`
}

type stats struct {
	callRecs, goRecs int
	byVia            map[string]int
	unattributed     map[string]uint64 // reason -> hits
	external         map[string]uint64 // caller function -> hits
	nameOK, nameBad  int
	nameRange        int // names the range-over-func rewrite gave (F-range1.1)
	nameBadEx        []string
}

type hitDirs []string

func (h *hitDirs) String() string { return strings.Join(*h, ",") }
func (h *hitDirs) Set(v string) error {
	*h = append(*h, splitList(v)...)
	return nil
}

func cmdReport(args []string) {
	fs := flag.NewFlagSet("report", flag.ExitOnError)
	copyDir := fs.String("copy", "", "instrumented copy (its site table is COPY/_goracle/sites.jsonl)")
	sitesPath := fs.String("sites", "", "site table (default: from --copy)")
	var dirs hitDirs
	fs.Var(&dirs, "hits", "directory with hits-*.jsonl (repeat or comma-separate for several runs)")
	out := fs.String("out", "", "observations (default: COPY/_goracle/obs.jsonl)")
	check(fs.Parse(args))
	if (*copyDir == "" && *sitesPath == "") || len(dirs) == 0 || fs.NArg() > 0 {
		fs.Usage()
		os.Exit(2)
	}
	if *sitesPath == "" {
		*sitesPath = sitesPathOf(*copyDir)
	}
	if *out == "" {
		if *copyDir == "" {
			check(fmt.Errorf("--out is required without --copy"))
		}
		*out = filepath.Join(*copyDir, "_goracle", "obs.jsonl")
	}
	t := loadTable(*sitesPath)
	var paths []string
	for _, d := range dirs {
		paths = append(paths, hitFiles(d)...)
	}
	abs, _ := filepath.Abs(*sitesPath)
	meta := &ObsMeta{Kind: "meta", Sites: abs, Root: t.meta.Root, Hits: dirs, Files: len(paths)}
	recs, st, summary := attribute(t, paths)
	writeJSONL(*out, append([]any{meta}, recs...))
	printReport(os.Stdout, t, st, summary)
}

// runtimeNames indexes the functions with defers by the names the runtime
// gave them: the names the hits report for the entered functions, plus the
// predicted runtime name. The runtime's own name differs from the prediction
// for the body closures of range-over-func loops (F-range1.1 for the first
// func literal of the loop body of F) and for package main in a test binary
// (its import path instead of "main"). named reports whether a function goes
// by a runtime name.
func runtimeNames(t *table, paths []string) (map[string][]*FuncRec, func(fn *FuncRec, name string) bool) {
	seen := map[int]map[string]bool{}
	for _, path := range paths {
		f, err := os.Open(path)
		check(err)
		sc := bufio.NewScanner(f)
		sc.Buffer(make([]byte, 1<<20), 1<<26)
		for sc.Scan() {
			var h struct {
				Kind   string `json:"kind"`
				Fid    int    `json:"fid"`
				Callee string `json:"callee"`
			}
			if len(sc.Bytes()) == 0 || json.Unmarshal(sc.Bytes(), &h) != nil || h.Callee == "" {
				continue
			}
			if h.Kind == "call" || h.Kind == "go" || h.Kind == "panic" {
				if seen[h.Fid] == nil {
					seen[h.Fid] = map[string]bool{}
				}
				seen[h.Fid][unescapeName(h.Callee)] = true
			}
		}
		check(sc.Err())
		f.Close()
	}
	named := func(fn *FuncRec, name string) bool {
		name = unescapeName(name)
		return name == unescapeName(fn.RTName) || seen[fn.Fid][name]
	}
	byName := map[string][]*FuncRec{}
	for _, fn := range t.funcs {
		if !fn.HasDefer {
			continue
		}
		names := map[string]bool{unescapeName(fn.RTName): true}
		for n := range seen[fn.Fid] {
			names[n] = true
		}
		for n := range names {
			byName[n] = append(byName[n], fn)
		}
	}
	return byName, named
}

// deferwrapParent returns the runtime name of the function that made a
// deferred call, from the name of its deferwrap closure (F.deferwrap1 ->
// F), or "".
func deferwrapParent(name string) string {
	if i := strings.LastIndex(name, ".deferwrap"); i > 0 {
		return unescapeName(name[:i])
	}
	return ""
}

// hitFiles lists hits-*.jsonl in dir, plus snapshots of processes that died
// before their exit hook.
func hitFiles(dir string) []string {
	matches, _ := filepath.Glob(filepath.Join(dir, "hits-*.jsonl"))
	snaps, _ := filepath.Glob(filepath.Join(dir, "snap-*.jsonl"))
	have := map[string]bool{}
	for _, m := range matches {
		have[strings.TrimPrefix(filepath.Base(m), "hits-")] = true
	}
	for _, s := range snaps {
		if !have[strings.TrimPrefix(filepath.Base(s), "snap-")] {
			matches = append(matches, s)
		}
	}
	sort.Strings(matches)
	return matches
}

type reportSummary struct {
	sites, observed, edges, multi, groups, counted, execOnly, reachOnly, short int
	byStmt                                                                     map[string]int
}

// attribute maps every hit to a site or defer group (first rule that applies):
// a call that runtime.deferreturn ran goes to the defer group of its logical
// caller (whatever its position); a tagged file name goes to its site; an
// exit line of a function with defers goes to its defer group; a range line
// without a tag is range-implicit; a single untagged site on the line takes
// it; runtime.goexit goes to the go statement of the created-by line; a
// caller outside the instrumented files is external, and entries that
// runtime.gopanic or runtime.Goexit ran are counted per function.
func attribute(t *table, paths []string) ([]any, *stats, *reportSummary) {
	st := &stats{byVia: map[string]int{}, unattributed: map[string]uint64{}, external: map[string]uint64{}}
	deferFns, named := runtimeNames(t, paths)
	deferByFile := map[string][]*FuncRec{}
	for _, fn := range t.funcs {
		if fn.HasDefer {
			deferByFile[fn.File] = append(deferByFile[fn.File], fn)
		}
	}
	siteObs := map[int]*Obs{}
	groupObs := map[int]*Obs{}
	rtSeen := map[int]bool{}
	done := map[int]uint64{}
	panicked := map[[2]int]uint64{} // (deferrer, callee) -> entries run by gopanic or Goexit

	addCallee := func(o *Obs, fid int, n uint64) {
		o.Hits += n
		for _, c := range o.Callees {
			if c.Fid == fid {
				c.N += n
				return
			}
		}
		c := &Callee{Fid: fid, N: n}
		if fn := t.funcs[fid]; fn != nil {
			c.QName, c.RTName, c.Qualname, c.File, c.Line = fn.QName, fn.RTName, fn.Qualname, fn.File, fn.Line
		}
		o.Callees = append(o.Callees, c)
	}
	toSite := func(s *SiteRec, via string, fid int, n uint64) {
		o := siteObs[s.Sid]
		if o == nil {
			o = &Obs{Kind: "site", Sid: s.Sid, File: s.File, Line: s.Line, Col: s.Col, Name: s.Name, Stmt: s.Stmt,
				Form: s.Form, Via: via}
			siteObs[s.Sid] = o
		}
		addCallee(o, fid, n)
		st.byVia[via]++
	}
	toGroup := func(fn *FuncRec, via string, fid int, n uint64) {
		o := groupObs[fn.Fid]
		if o == nil {
			o = &Obs{Kind: "defer-group", File: fn.File, Line: fn.Line, Via: "defer-group", Fid: fn.Fid, QName: fn.QName,
				Defers: fn.Defers, Exec: -1, Reach: -1, Run: -1}
			if len(fn.Defers) == 1 {
				o.Via = "defer-single"
			}
			groupObs[fn.Fid] = o
		}
		addCallee(o, fid, n)
		if via == "" {
			via = o.Via
		}
		st.byVia[via]++
	}

	for _, path := range paths {
		f, err := os.Open(path)
		check(err)
		sc := bufio.NewScanner(f)
		sc.Buffer(make([]byte, 1<<20), 1<<26)
		for sc.Scan() {
			if len(sc.Bytes()) == 0 {
				continue
			}
			var h hit
			check(json.Unmarshal(sc.Bytes(), &h))
			switch h.Kind {
			case "meta":
				continue
			case "done":
				done[h.Fid] += h.N
				continue
			case "panic":
				// the function whose defer this is when its deferwrap closure
				// showed, else every function with defers on the stack (the
				// leniency can hide a wrong defer-group edge, never make one)
				owners := deferFns[deferwrapParent(h.Owner)]
				if len(owners) == 0 {
					for _, name := range h.Stack {
						owners = append(owners, deferFns[unescapeName(name)]...)
					}
				}
				for _, fn := range owners {
					panicked[[2]int{fn.Fid, h.Fid}] += h.N
				}
				continue
			}
			if fn := t.funcs[h.Fid]; fn != nil && h.Callee != "" && !rtSeen[h.Fid] {
				rtSeen[h.Fid] = true
				got, want := unescapeName(h.Callee), unescapeName(fn.RTName)
				switch {
				case got == want, strings.HasPrefix(want, "main.") && got == fn.Pkg+want[len("main"):]:
					st.nameOK++ // a test binary names package main by its import path
				case strings.Contains(got, "-range"):
					st.nameRange++ // a func literal in the body of a range-over-func loop
				default:
					st.nameBad++
					if len(st.nameBadEx) < 40 {
						st.nameBadEx = append(st.nameBadEx, fmt.Sprintf("expected %s observed %s (%s:%d)", fn.RTName, h.Callee, fn.File, fn.Line))
					}
				}
			}
			if h.Kind == "go" {
				st.goRecs++
				if f := t.file(h.File); f != nil {
					h.File = f.Abs
				}
				ss := t.goSites[lk(h.File, h.Line)]
				switch {
				case len(ss) == 1:
					toSite(ss[0], "go", h.Fid, h.N)
				case len(ss) > 1:
					st.unattributed["go-ambiguous"] += h.N
				case t.file(h.File) != nil:
					st.unattributed["go-no-site"] += h.N
				default:
					st.external["(go) "+h.Caller] += h.N
				}
				continue
			}
			st.callRecs++
			base := h.File
			if i := strings.LastIndexByte(base, '#'); i >= 0 {
				base = base[:i]
			}
			if h.Defer {
				var fn *FuncRec
				if f := t.file(base); f != nil {
					for _, c := range deferFns[unescapeName(h.Caller)] {
						if f.File == c.File && c.Line <= h.Line && h.Line <= c.EndLine && (fn == nil || c.Line > fn.Line) {
							fn = c
						}
					}
					if fn == nil {
						// a caller name that neither the table nor any hit
						// knows: the innermost function with defers around the line
						for _, c := range deferByFile[f.File] {
							if c.Line <= h.Line && h.Line <= c.EndLine && (fn == nil || c.Line > fn.Line) {
								fn = c
							}
						}
					}
				}
				if fn != nil {
					toGroup(fn, "deferreturn", h.Fid, h.N)
				} else {
					st.unattributed["deferreturn-no-function"] += h.N
				}
				continue
			}
			if i := strings.LastIndexByte(h.File, '#'); i >= 0 {
				sid, err := strconv.Atoi(h.File[i+1:])
				s := t.sites[sid]
				if err != nil || s == nil || t.file(h.File[:i]) == nil {
					st.unattributed["bad-tag"] += h.N
					continue
				}
				toSite(s, "tag", h.Fid, h.N)
				continue
			}
			f := t.file(h.File)
			if f == nil {
				st.external[h.Caller] += h.N
				continue
			}
			key := lk(f.Abs, h.Line)
			if fns := t.exitFuncs[key]; len(fns) > 0 && t.hot[key] == "exit" {
				fn := fns[0]
				for _, c := range fns {
					if named(c, h.Caller) {
						fn = c
					}
				}
				toGroup(fn, "", h.Fid, h.N)
				continue
			}
			if t.hot[key] == "range" {
				st.unattributed["range-implicit"] += h.N
				continue
			}
			ss := t.untagged[key]
			switch {
			case len(ss) == 1:
				toSite(ss[0], "line", h.Fid, h.N)
			case len(ss) > 1:
				st.unattributed["line-ambiguous"] += h.N
			default:
				st.unattributed["no-site-at-line"] += h.N
			}
		}
		check(sc.Err())
		f.Close()
	}

	sum := &reportSummary{sites: len(t.sites), groups: len(groupObs), byStmt: map[string]int{}}
	// sites whose counters ran without an attributed entry still answer "executed"
	for sid, id := range t.stmtOf {
		if done[id] > 0 && siteObs[sid] == nil {
			s := t.sites[sid]
			siteObs[sid] = &Obs{Kind: "site", Sid: sid, File: s.File, Line: s.Line, Col: s.Col, Name: s.Name,
				Stmt: s.Stmt, Form: s.Form, Via: "exec-only"}
			sum.execOnly++
		}
	}
	for sid, id := range t.reachOf {
		if done[id] > 0 && siteObs[sid] == nil {
			s := t.sites[sid]
			siteObs[sid] = &Obs{Kind: "site", Sid: sid, File: s.File, Line: s.Line, Col: s.Col, Name: s.Name,
				Stmt: s.Stmt, Form: s.Form, Via: "reach-only"}
			sum.reachOnly++
		}
	}
	for sid, ob := range siteObs {
		ob.Exec, ob.Reach, ob.Run = -1, -1, -1
		if id, ok := t.runOf[sid]; ok {
			ob.Run = int64(done[id])
		}
		if id, ok := t.stmtOf[sid]; ok {
			ob.Exec = int64(done[id])
			sum.counted++
			if ob.Exec > int64(ob.Hits) && len(ob.Callees) > 0 {
				sum.short++
			}
		}
		if id, ok := t.reachOf[sid]; ok {
			ob.Reach = int64(done[id])
		}
	}
	var sids []int
	for sid := range siteObs {
		sids = append(sids, sid)
	}
	sort.Ints(sids)
	var recs []any
	for _, sid := range sids {
		ob := siteObs[sid]
		sort.Slice(ob.Callees, func(i, j int) bool { return ob.Callees[i].Fid < ob.Callees[j].Fid })
		sum.edges += len(ob.Callees)
		if len(ob.Callees) > 1 {
			sum.multi++
		}
		sum.byStmt[ob.Stmt]++
		recs = append(recs, ob)
	}
	sum.observed = len(siteObs)
	var gids []int
	for g := range groupObs {
		gids = append(gids, g)
	}
	sort.Ints(gids)
	for _, g := range gids {
		ob := groupObs[g]
		sort.Slice(ob.Callees, func(i, j int) bool { return ob.Callees[i].Fid < ob.Callees[j].Fid })
		recs = append(recs, ob)
	}
	var pkeys [][2]int
	for k := range panicked {
		pkeys = append(pkeys, k)
	}
	sort.Slice(pkeys, func(i, j int) bool {
		return pkeys[i][0] < pkeys[j][0] || pkeys[i][0] == pkeys[j][0] && pkeys[i][1] < pkeys[j][1]
	})
	for _, k := range pkeys {
		r := &PanicRec{Kind: "panic-deferred", Deferrer: k[0], Fid: k[1], N: panicked[k]}
		if fn := t.funcs[k[1]]; fn != nil {
			r.QName = fn.QName
		}
		recs = append(recs, r)
	}
	return recs, st, sum
}

func printReport(w io.Writer, t *table, st *stats, s *reportSummary) {
	fmt.Fprintf(w, "sites in table: %d; observed sites: %d (call %d, go %d, defer %d, range %d); site->callee edges: %d; sites with >1 callee: %d\n",
		s.sites, s.observed, s.byStmt["call"], s.byStmt["go"], s.byStmt["defer"], s.byStmt["range"], s.edges, s.multi)
	fmt.Fprintf(w, "defer groups observed: %d\n", s.groups)
	fmt.Fprintf(w, "completion counters: %d observed sites carry one; %d sites executed without entering instrumented code (exec-only), %d more seen by reach counters only; %d sites with entries but exec > hits\n",
		s.counted, s.execOnly, s.reachOnly, s.short)
	fmt.Fprintf(w, "records: call %d, go %d; attributions by route: %v\n", st.callRecs, st.goRecs, sortedMap(st.byVia))
	fmt.Fprintf(w, "unattributed hits: %v\n", sortedMap(st.unattributed))
	fmt.Fprintf(w, "runtime-name check: %d functions match the expected runtime name, %d differ, %d renamed by the range-over-func rewrite\n",
		st.nameOK, st.nameBad, st.nameRange)
	if st.nameBad > 0 {
		fmt.Fprintf(w, "WARNING: %d entered functions run under a runtime name the table does not predict; "+
			"defer groups and panic credit use the names the hits report\n", st.nameBad)
	}
	for _, e := range st.nameBadEx {
		fmt.Fprintln(w, "  name:", e)
	}
	type kv struct {
		k string
		v uint64
	}
	var ext []kv
	var extTotal uint64
	for k, v := range st.external {
		ext = append(ext, kv{k, v})
		extTotal += v
	}
	sort.Slice(ext, func(i, j int) bool { return ext[i].v > ext[j].v || ext[i].v == ext[j].v && ext[i].k < ext[j].k })
	fmt.Fprintf(w, "hits from callers outside the instrumented files: %d over %d caller functions; top:\n", extTotal, len(ext))
	for i, e := range ext {
		if i >= 12 {
			break
		}
		fmt.Fprintf(w, "  %10d %s\n", e.v, e.k)
	}
}

func sortedMap[V int | uint64](m map[string]V) string {
	var keys []string
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var parts []string
	for _, k := range keys {
		parts = append(parts, fmt.Sprintf("%s:%v", k, m[k]))
	}
	return "map[" + strings.Join(parts, " ") + "]"
}

// obsSet is an observations file: site observations by sid, defer groups by
// fid, panic- or Goexit-run entries by (deferring function, callee), and the
// meta line.
type obsSet struct {
	meta     *ObsMeta
	bySite   map[int]*Obs
	byGroup  map[int]*Obs
	panicked map[[2]int]uint64
}

func loadObs(path string) *obsSet {
	o := &obsSet{bySite: map[int]*Obs{}, byGroup: map[int]*Obs{}, panicked: map[[2]int]uint64{}}
	readJSONL(path, func(b []byte) {
		var probe struct{ Kind string }
		check(json.Unmarshal(b, &probe))
		switch probe.Kind {
		case "meta":
			o.meta = &ObsMeta{}
			check(json.Unmarshal(b, o.meta))
		case "site":
			r := &Obs{}
			check(json.Unmarshal(b, r))
			o.bySite[r.Sid] = r
		case "defer-group":
			r := &Obs{}
			check(json.Unmarshal(b, r))
			o.byGroup[r.Fid] = r
		case "panic-deferred":
			r := &PanicRec{}
			check(json.Unmarshal(b, r))
			o.panicked[[2]int{r.Deferrer, r.Fid}] += r.N
		}
	})
	return o
}

// siteView is what match and judge read for one site: its own observation,
// or the observation of its defer group (group=true when the function has
// two or more defers, so only the group set is known). reach counts the
// starts of the site's statement and run the completed runs of a deferred
// call (-1: no such counter).
type siteView struct {
	obs   *Obs
	group bool
	reach int64
	run   int64
}

func viewOf(t *table, s *SiteRec, bySite, byGroup map[int]*Obs) siteView {
	v := siteView{obs: bySite[s.Sid], reach: -1, run: -1}
	if v.obs != nil {
		v.reach, v.run = v.obs.Reach, v.obs.Run
	} else {
		if _, ok := t.reachOf[s.Sid]; ok {
			v.reach = 0 // counted, never started
		}
		if _, ok := t.runOf[s.Sid]; ok {
			v.run = 0
		}
	}
	if s.Stmt == "defer" && s.Fid >= 0 {
		if g := byGroup[s.Fid]; g != nil {
			v.obs, v.group = g, len(g.Defers) > 1
		} else if v.obs != nil && len(v.obs.Callees) == 0 {
			// the defer statement ran (reach counter) but its call entered no
			// instrumented function
			v.obs = &Obs{Kind: "site", Sid: s.Sid, File: s.File, Line: s.Line, Exec: -1, Reach: v.obs.Reach, Run: v.obs.Run}
		}
	}
	return v
}

func newEncoder(w io.Writer) *json.Encoder { return json.NewEncoder(w) }
