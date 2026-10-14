package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

// hit is one line of a hits-*.jsonl file written by package rt.
type hit struct {
	Kind   string `json:"kind"` // meta, call, go
	Fid    int    `json:"fid"`
	Callee string `json:"callee"` // runtime name of the entered function
	Caller string `json:"caller"` // runtime name of the logical caller (go: the creator)
	File   string `json:"file"`
	Line   int    `json:"line"`
	N      uint64 `json:"n"`
	Exe    string `json:"exe"`
	Args   string `json:"args"`
}

// Callee is one function entered from a site.
type Callee struct {
	Fid    int    `json:"fid"`
	QName  string `json:"qname"`
	RTName string `json:"rtname"`
	File   string `json:"file"`
	Line   int    `json:"line"`
	N      uint64 `json:"n"`
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
	Via     string    `json:"via"` // tag, line, go, defer-group, defer-single
	Fid     int       `json:"fid,omitempty"`
	QName   string    `json:"qname,omitempty"`
	Defers  []int     `json:"defer_sites,omitempty"`
	Exec    int64     `json:"exec"` // completions of the enclosing simple statement; -1 = not counted
	Hits    uint64    `json:"hits"` // instrumented entries attributed to the site
	Callees []*Callee `json:"callees"`
}

type table struct {
	files     map[string]*FileRec // abs -> file
	funcs     map[int]*FuncRec
	sites     map[int]*SiteRec
	untagged  map[string][]*SiteRec // abs:line -> untagged sites (by lparen line)
	goSites   map[string][]*SiteRec // abs:goline -> go statement sites
	exitFuncs map[string][]*FuncRec // abs:line -> functions with defers that exit there
	hot       map[string]string     // abs:line -> hot reason
	stmtOf    map[int]int           // sid -> stmt id
}

func lk(abs string, line int) string { return abs + ":" + strconv.Itoa(line) }

func loadTable(path string) *table {
	t := &table{files: map[string]*FileRec{}, funcs: map[int]*FuncRec{}, sites: map[int]*SiteRec{},
		untagged: map[string][]*SiteRec{}, goSites: map[string][]*SiteRec{}, exitFuncs: map[string][]*FuncRec{},
		hot: map[string]string{}, stmtOf: map[int]int{}}
	f, err := os.Open(path)
	check(err)
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<26)
	relAbs := map[string]string{}
	var hots []*HotRec
	for sc.Scan() {
		var probe struct{ Kind string }
		b := sc.Bytes()
		check(json.Unmarshal(b, &probe))
		switch probe.Kind {
		case "file":
			r := &FileRec{}
			check(json.Unmarshal(b, r))
			t.files[r.Abs] = r
			relAbs[r.File] = r.Abs
		case "func":
			r := &FuncRec{}
			check(json.Unmarshal(b, r))
			t.funcs[r.Fid] = r
		case "site":
			r := &SiteRec{}
			check(json.Unmarshal(b, r))
			t.sites[r.Sid] = r
		case "hot":
			r := &HotRec{}
			check(json.Unmarshal(b, r))
			hots = append(hots, r)
		case "stmt":
			r := &StmtRec{}
			check(json.Unmarshal(b, r))
			for _, sid := range r.Sites {
				t.stmtOf[sid] = r.ID
			}
		}
	}
	check(sc.Err())
	for _, h := range hots {
		t.hot[lk(relAbs[h.File], h.Line)] = h.Why
	}
	for _, s := range t.sites {
		abs := relAbs[s.File]
		if s.Hot != "" && t.hot[lk(abs, s.LLine)] == "" {
			t.hot[lk(abs, s.LLine)] = s.Hot
		}
		if !s.Tagged {
			t.untagged[lk(abs, s.LLine)] = append(t.untagged[lk(abs, s.LLine)], s)
		}
		if s.Stmt == "go" {
			t.goSites[lk(abs, s.GoLine)] = append(t.goSites[lk(abs, s.GoLine)], s)
		}
	}
	for _, fn := range t.funcs {
		abs := relAbs[fn.File]
		for _, l := range fn.ExitLines {
			t.exitFuncs[lk(abs, l)] = append(t.exitFuncs[lk(abs, l)], fn)
		}
	}
	return t
}

type stats struct {
	callRecs, goRecs int
	byVia            map[string]int
	unattributed     map[string]uint64 // reason -> hits
	external         map[string]uint64 // caller function -> hits
	nameOK, nameBad  int
	nameBadEx        []string
	ambiguousLine    int
}

func cmdReport(args []string) {
	fs := flag.NewFlagSet("report", flag.ExitOnError)
	sitesPath := fs.String("sites", "", "site table from instrument")
	hitsDir := fs.String("hits", "", "directory with hits-*.jsonl")
	out := fs.String("out", "", "observations (JSON lines)")
	fs.Parse(args)
	if *sitesPath == "" || *hitsDir == "" || *out == "" {
		fs.Usage()
		os.Exit(2)
	}
	t := loadTable(*sitesPath)
	st := &stats{byVia: map[string]int{}, unattributed: map[string]uint64{}, external: map[string]uint64{}}
	siteObs := map[int]*Obs{}
	groupObs := map[int]*Obs{}
	rtSeen := map[int]string{}
	done := map[int]uint64{}

	addCallee := func(o *Obs, fid int, n uint64) {
		for _, c := range o.Callees {
			if c.Fid == fid {
				c.N += n
				return
			}
		}
		fn := t.funcs[fid]
		c := &Callee{Fid: fid, N: n}
		if fn != nil {
			c.QName, c.RTName, c.File, c.Line = fn.QName, fn.RTName, fn.File, fn.Line
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
		o.Hits += n
		addCallee(o, fid, n)
		st.byVia[via]++
	}
	toGroup := func(fn *FuncRec, fid int, n uint64) {
		o := groupObs[fn.Fid]
		if o == nil {
			o = &Obs{Kind: "defer-group", File: fn.File, Line: fn.Line, Via: "defer-group", Fid: fn.Fid, QName: fn.QName,
				Defers: fn.Defers}
			if len(fn.Defers) == 1 {
				o.Via = "defer-single"
			}
			groupObs[fn.Fid] = o
		}
		addCallee(o, fid, n)
		st.byVia[o.Via]++
	}

	matches, _ := filepath.Glob(filepath.Join(*hitsDir, "hits-*.jsonl"))
	snaps, _ := filepath.Glob(filepath.Join(*hitsDir, "snap-*.jsonl"))
	have := map[string]bool{}
	for _, m := range matches {
		have[strings.TrimPrefix(filepath.Base(m), "hits-")] = true
	}
	for _, s := range snaps {
		if !have[strings.TrimPrefix(filepath.Base(s), "snap-")] {
			matches = append(matches, s) // process died before its exit hook
		}
	}
	sort.Strings(matches)
	for _, path := range matches {
		f, err := os.Open(path)
		check(err)
		sc := bufio.NewScanner(f)
		sc.Buffer(make([]byte, 1<<20), 1<<26)
		for sc.Scan() {
			var h hit
			check(json.Unmarshal(sc.Bytes(), &h))
			if h.Kind == "meta" {
				continue
			}
			if h.Kind == "done" {
				done[h.Fid] += h.N
				continue
			}
			if fn := t.funcs[h.Fid]; fn != nil && h.Callee != "" {
				if _, ok := rtSeen[h.Fid]; !ok {
					rtSeen[h.Fid] = h.Callee
					if h.Callee == fn.RTName {
						st.nameOK++
					} else {
						st.nameBad++
						if len(st.nameBadEx) < 40 {
							st.nameBadEx = append(st.nameBadEx, fmt.Sprintf("expected %s observed %s (%s:%d)", fn.RTName, h.Callee, fn.File, fn.Line))
						}
					}
				}
			}
			if h.Kind == "go" {
				st.goRecs++
				ss := t.goSites[lk(h.File, h.Line)]
				if len(ss) == 1 {
					toSite(ss[0], "go", h.Fid, h.N)
				} else if len(ss) > 1 {
					st.unattributed["go-ambiguous"] += h.N
				} else if _, ok := t.files[h.File]; ok {
					st.unattributed["go-no-site"] += h.N
				} else {
					st.external["(go) "+h.Caller] += h.N
				}
				continue
			}
			st.callRecs++
			if i := strings.LastIndexByte(h.File, '#'); i >= 0 {
				sid, err := strconv.Atoi(h.File[i+1:])
				s := t.sites[sid]
				if err != nil || s == nil || t.files[h.File[:i]] == nil {
					st.unattributed["bad-tag"] += h.N
					continue
				}
				toSite(s, "tag", h.Fid, h.N)
				continue
			}
			if _, ok := t.files[h.File]; !ok {
				st.external[h.Caller] += h.N
				continue
			}
			key := lk(h.File, h.Line)
			if fns := t.exitFuncs[key]; len(fns) > 0 && t.hot[key] == "exit" {
				fn := fns[0]
				for _, c := range fns {
					if c.RTName == h.Caller {
						fn = c
					}
				}
				toGroup(fn, h.Fid, h.N)
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
				st.ambiguousLine++
				st.unattributed["line-ambiguous"] += h.N
			default:
				st.unattributed["no-site-at-line"] += h.N
			}
		}
		check(sc.Err())
		f.Close()
	}

	// sites that completed without any attributed entry still answer "executed"
	stmtSites := map[int][]int{}
	for sid, id := range t.stmtOf {
		stmtSites[id] = append(stmtSites[id], sid)
	}
	execOnly := 0
	for id, n := range done {
		for _, sid := range stmtSites[id] {
			if siteObs[sid] == nil {
				s := t.sites[sid]
				siteObs[sid] = &Obs{Kind: "site", Sid: sid, File: s.File, Line: s.Line, Col: s.Col, Name: s.Name,
					Stmt: s.Stmt, Form: s.Form, Via: "exec-only"}
				execOnly++
			}
		}
		_ = n
	}
	short := 0
	for sid, ob := range siteObs {
		ob.Exec = -1
		if id, ok := t.stmtOf[sid]; ok {
			ob.Exec = int64(done[id])
			if ob.Exec > int64(ob.Hits) && len(ob.Callees) > 0 {
				short++
			}
		}
	}

	o, err := os.Create(*out)
	check(err)
	w := bufio.NewWriter(o)
	enc := json.NewEncoder(w)
	var sids []int
	for sid := range siteObs {
		sids = append(sids, sid)
	}
	sort.Ints(sids)
	multi, edges := 0, 0
	stmtCount := map[string]int{}
	for _, sid := range sids {
		ob := siteObs[sid]
		sort.Slice(ob.Callees, func(i, j int) bool { return ob.Callees[i].Fid < ob.Callees[j].Fid })
		edges += len(ob.Callees)
		if len(ob.Callees) > 1 {
			multi++
		}
		stmtCount[ob.Stmt]++
		check(enc.Encode(ob))
	}
	var gids []int
	for g := range groupObs {
		gids = append(gids, g)
	}
	sort.Ints(gids)
	for _, g := range gids {
		check(enc.Encode(groupObs[g]))
	}
	check(w.Flush())
	check(o.Close())

	total := len(t.sites)
	fmt.Printf("sites in table: %d; observed sites: %d (call %d, go %d, defer %d); site->callee edges: %d; sites with >1 callee: %d\n",
		total, len(siteObs), stmtCount["call"], stmtCount["go"], stmtCount["defer"], edges, multi)
	fmt.Printf("defer groups observed: %d\n", len(groupObs))
	counted := 0
	for _, ob := range siteObs {
		if ob.Exec >= 0 {
			counted++
		}
	}
	fmt.Printf("completion counters: %d observed sites carry one; %d sites executed without entering instrumented code (exec-only); %d sites with entries but exec > hits\n",
		counted, execOnly, short)
	fmt.Printf("records: call %d, go %d; attributions by route: %v\n", st.callRecs, st.goRecs, st.byVia)
	fmt.Printf("unattributed hits: %v\n", st.unattributed)
	fmt.Printf("runtime-name check: %d functions match the expected runtime name, %d differ\n", st.nameOK, st.nameBad)
	for _, e := range st.nameBadEx {
		fmt.Println("  name:", e)
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
	sort.Slice(ext, func(i, j int) bool { return ext[i].v > ext[j].v })
	fmt.Printf("hits from callers outside the instrumented files: %d over %d caller functions; top:\n", extTotal, len(ext))
	for i, e := range ext {
		if i >= 12 {
			break
		}
		fmt.Printf("  %10d %s\n", e.v, e.k)
	}
}
