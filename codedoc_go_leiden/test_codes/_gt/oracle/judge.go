package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"
)

// The judge applies the ground-truth rule of spec §9.3 (C11) to the
// observations: observed ⊆ targets ∪ runtime_targets, and every runtime
// target observed. Candidates and the static target are not allowed callees
// on their own: an implementer or variant that runs belongs in
// runtime_targets. Interface cases (the target is an interface
// method) are IFACE-CONSISTENT when every observed callee is a documented
// runtime target and a method of the interface method's name. Defer sites in
// a function with two or more defers are judged at group level.
//
// Interface and ambiguous cases allow the candidates too (spec R6, codedoc_
// python's rule for ambiguous sites): observed ⊆ candidates ∪
// runtime_targets.
//
// Verdicts: CONFIRMED, CONFIRMED-NOEXEC, IFACE-CONSISTENT,
// DEFER-GROUP-CONSISTENT, CONSISTENT (a completion counter proves the site
// ran without entering anything undocumented; for external cases: exec >
// hits and no in-repo callee) and CONSISTENT-NOEXEC (the same, proven only by
// a reach counter or the callees) pass; MISS, IFACE-OTHER, DEFER-GROUP-OTHER,
// NO-SITE, TABLE-MISMATCH, NOT-RUN and ENTRY-FAILED fail; UNEXEC (no entry),
// SKIPPED (entry not run) and UNVERIFIABLE (file or target not instrumented,
// nothing observable) say nothing.

type manifest struct {
	Fixture string  `json:"fixture"`
	Cases   []mcase `json:"cases"`
}

type mcase struct {
	ID       string  `json:"id"`
	File     string  `json:"file"`
	Line     int     `json:"line"`
	Col      int     `json:"col"`
	Token    string  `json:"token"`
	SiteKind string  `json:"site_kind"`
	Entry    *string `json:"entry"`
	Expect   mexpect `json:"expect"`
}

type mtarget struct {
	Qualname string `json:"qualname"`
	Module   string `json:"module"`
	Kind     string `json:"kind"`
	File     string `json:"file"`
	Line     int    `json:"line"`
	Col      int    `json:"col"`
}

type mexpect struct {
	Status         string    `json:"status"`
	Targets        []mtarget `json:"targets"`
	Candidates     []mtarget `json:"candidates"`
	RuntimeTargets []mtarget `json:"runtime_targets"`
	Distractors    []mtarget `json:"distractors"`
	StaticTarget   *mtarget  `json:"static_target"`
	OpenWorld      *bool     `json:"open_world"`
	External       *struct{} `json:"external"`
}

var passVerdicts = map[string]bool{"CONFIRMED": true, "CONFIRMED-NOEXEC": true, "IFACE-CONSISTENT": true,
	"DEFER-GROUP-CONSISTENT": true, "CONSISTENT": true, "CONSISTENT-NOEXEC": true}
var failVerdicts = map[string]bool{"MISS": true, "IFACE-OTHER": true, "DEFER-GROUP-OTHER": true, "NO-SITE": true,
	"TABLE-MISMATCH": true, "NOT-RUN": true, "ENTRY-FAILED": true}

// expected is a target mapped onto the site table.
type expected struct {
	t  mtarget
	fn *FuncRec    // a function or closure
	im *IMethodRec // an interface method
}

func (e expected) label() string { return e.t.Qualname + " (" + e.t.File + ")" }

type judgeCtx struct {
	t        *table
	idx      siteIndex
	bySite   map[int]*Obs
	byGroup  map[int]*Obs
	panicked map[[2]int]uint64 // (deferring function, callee) -> entries runtime.gopanic or runtime.Goexit ran
}

// mapTarget finds a target in the table. A class target (a type) is never
// entered and maps to nothing. problem is set when a function target is
// missing from the table of an instrumented file (the C3 names of gt_lib and
// goracle disagree), unverifiable when its file is not instrumented.
func (j *judgeCtx) mapTarget(tg mtarget) (e expected, problem, unverifiable string) {
	e.t = tg
	if tg.Kind == "class" {
		return e, "", ""
	}
	if fns := j.t.funcByQual(tg.File, tg.Qualname); len(fns) > 0 {
		sort.Slice(fns, func(a, b int) bool { return fns[a].Fid < fns[b].Fid })
		e.fn = fns[0]
		return e, "", ""
	}
	if im := j.t.imethodByQual(tg.File, tg.Qualname); im != nil {
		e.im = im
		return e, "", ""
	}
	if f := j.t.byRel[tg.File]; f == nil || f.Status != "instrumented" {
		status := "not scanned"
		if f != nil {
			status = f.Status
		}
		return e, "", fmt.Sprintf("target file %s is %s", tg.File, status)
	}
	return e, fmt.Sprintf("target %s is not in the oracle's table of %s", tg.Qualname, tg.File), ""
}

// observedIn reports whether a callee is one of the expected functions.
func (j *judgeCtx) observedIn(c *Callee, list []expected) bool {
	for _, e := range list {
		if j.t.sameDecl(c, e.fn) {
			return true
		}
	}
	return false
}

func (j *judgeCtx) seen(e expected, o *Obs) bool {
	if o == nil || e.fn == nil {
		return false
	}
	for _, c := range o.Callees {
		if j.t.sameDecl(c, e.fn) {
			return true
		}
	}
	return false
}

func labels(list []expected) string {
	var out []string
	for _, e := range list {
		out = append(out, e.label())
	}
	return strings.Join(out, ", ")
}

// mergeViews joins the views of a position that several sites share (a file
// and its copied symlink).
func mergeViews(t *table, views []siteView) siteView {
	if len(views) == 1 {
		return views[0]
	}
	out := siteView{reach: -1}
	var merged *Obs
	for _, v := range views {
		if v.reach >= 0 {
			if out.reach < 0 {
				out.reach = 0
			}
			out.reach += v.reach
		}
		out.group = out.group || v.group
		if v.obs == nil {
			continue
		}
		if merged == nil {
			cp := *v.obs
			cp.Callees = nil
			merged = &cp
			merged.Hits = 0
			merged.Exec = -1
		}
		if v.obs.Exec >= 0 {
			if merged.Exec < 0 {
				merged.Exec = 0
			}
			merged.Exec += v.obs.Exec
		}
		merged.Hits += v.obs.Hits
	next:
		for _, c := range v.obs.Callees {
			for _, have := range merged.Callees {
				if have.Fid == c.Fid || (t.funcs[have.Fid] != nil && t.sameDecl(c, t.funcs[have.Fid])) {
					have.N += c.N
					continue next
				}
			}
			cc := *c
			merged.Callees = append(merged.Callees, &cc)
		}
	}
	out.obs = merged
	return out
}

func (j *judgeCtx) judgeCase(c mcase) (string, string, siteView) {
	none := siteView{reach: -1}
	sites := j.t.lookupSites(j.idx, c.File, c.Line, c.Col, c.Token, c.SiteKind)
	if len(sites) == 0 {
		if f := j.t.byRel[c.File]; f == nil || f.Status != "instrumented" {
			status := "not scanned"
			if f != nil {
				status = f.Status
			}
			return "UNVERIFIABLE", fmt.Sprintf("%s is %s", c.File, status), none
		}
		if c.Expect.Status == "conversion" {
			// a conversion to a type literal ([]byte(s)) is no site of the
			// oracle: unobservable, which says nothing (ruling 2026-10-14)
			return "UNVERIFIABLE", "a conversion the oracle has no site for", none
		}
		return "NO-SITE", fmt.Sprintf("the oracle has no %s site %q at %s:%d:%d", c.SiteKind, c.Token, c.File, c.Line, c.Col), none
	}
	var views []siteView
	for _, s := range sites {
		views = append(views, viewOf(j.t, s, j.bySite, j.byGroup))
	}
	v := mergeViews(j.t, views)
	ex := c.Expect
	var targets, cands, runtime, distract []expected
	var problems, unverif []string
	// judged lists the targets the verdict rests on: an unmappable one of
	// them makes the case unverifiable; candidates, the static target and
	// distractors only have to exist
	mapAll := func(src []mtarget, dst *[]expected, judged bool) {
		for _, tg := range src {
			e, p, u := j.mapTarget(tg)
			if p != "" {
				problems = append(problems, p)
			}
			if u != "" && judged {
				unverif = append(unverif, u)
			}
			*dst = append(*dst, e)
		}
	}
	mapAll(ex.Targets, &targets, true)
	mapAll(ex.Candidates, &cands, false)
	mapAll(ex.RuntimeTargets, &runtime, true)
	mapAll(ex.Distractors, &distract, false)
	var static []expected
	if ex.StaticTarget != nil {
		mapAll([]mtarget{*ex.StaticTarget}, &static, false)
	}
	if len(problems) > 0 {
		return "TABLE-MISMATCH", strings.Join(problems, "; "), v
	}
	allowed := append(append([]expected{}, targets...), runtime...)
	iface := false
	for _, e := range targets {
		iface = iface || e.im != nil
	}
	if iface || ex.Status == "ambiguous" {
		allowed = append(allowed, cands...)
	}
	hookedExpected := false // an instrumented function must run at the site
	for _, e := range allowed {
		// an interface method alone expects nothing instrumented: in an open
		// world only an uninstrumented implementer may run
		if e.fn != nil && e.fn.Hooked {
			hookedExpected = true
		}
	}
	o := v.obs
	executed := o != nil && (len(o.Callees) > 0 || o.Exec > 0 || o.Reach > 0) || v.reach > 0
	if c.SiteKind == "defer" && v.reach == 0 {
		executed = false // the defer statement never ran; the group set comes from other defers
	}
	if !executed {
		_, counted := j.t.stmtOf[sites[0].Sid]
		_, reached := j.t.reachOf[sites[0].Sid]
		if !counted && !reached && !hookedExpected {
			// no counter covers the site (right operand of && or ||, an
			// else-if or case expression, a package-level initializer) and
			// nothing instrumented is expected: its execution is invisible
			return "UNVERIFIABLE", "no counter covers this site and no instrumented callee is expected", v
		}
		return "UNEXEC", "site not executed", v
	}
	if len(unverif) > 0 {
		return "UNVERIFIABLE", strings.Join(unverif, "; "), v
	}
	var callees []*Callee
	if o != nil {
		callees = o.Callees
	}
	names := "-"
	if o != nil && len(o.Callees) > 0 {
		names = strings.Join(calleeNames(o), ", ")
	}
	if !v.group {
		// a defer group's set also holds the callees of the other defers
		for _, d := range distract {
			if j.seen(d, o) {
				return "MISS", "distractor " + d.label() + " observed", v
			}
		}
	}
	var missing []expected
	for _, r := range runtime {
		if r.fn != nil && !j.seen(r, o) {
			missing = append(missing, r)
		}
	}
	unins := o != nil && o.Exec >= 0 && o.Exec > int64(o.Hits)

	if v.group {
		// two or more defers share the observations: only "every expected
		// function ran in the group" can be checked. The targets and runtime
		// targets must all have run (candidates are allowed, not required);
		// an interface method itself is never entered.
		var need []expected
		for _, e := range append(append([]expected{}, targets...), runtime...) {
			switch {
			case e.fn != nil && !e.fn.Hooked:
				return "UNVERIFIABLE", "the expected target has no hook; group observed " + names, v
			case e.fn != nil:
				need = append(need, e)
			}
		}
		if len(need) == 0 {
			return "UNVERIFIABLE", "nothing instrumented to look for in the defer group's set " + names, v
		}
		var absent []expected
		panicked := false
		for _, e := range need {
			if !j.seen(e, o) {
				absent = append(absent, e)
				for _, st := range sites {
					panicked = panicked || j.panicked[[2]int{st.Fid, e.fn.Fid}] > 0
				}
			}
		}
		if len(absent) == 0 {
			return "DEFER-GROUP-CONSISTENT", "group observed " + names, v
		}
		if panicked {
			return "UNVERIFIABLE", fmt.Sprintf("%s ran under runtime.gopanic, outside any group set; group observed %s", labels(absent), names), v
		}
		return "DEFER-GROUP-OTHER", fmt.Sprintf("%s not in the defer group's set %s", labels(absent), names), v
	}

	if len(callees) == 0 && (c.SiteKind == "defer" || c.SiteKind == "go") {
		// a deferred call that runs while the function panics is attributed
		// to runtime.gopanic, and a goroutine may not start before the
		// program exits: no callee proves nothing when one is expected
		for _, e := range allowed {
			if e.im != nil || (e.fn != nil && e.fn.Hooked) {
				return "UNVERIFIABLE", fmt.Sprintf("the %s statement ran (reach %d) but no instrumented callee was attributed to it", c.SiteKind, v.reach), v
			}
		}
	}
	switch ex.Status {
	case "resolved", "ambiguous", "external":
		if ex.Status == "external" && len(ex.Targets) == 0 {
			break
		}
		method := ""
		for _, e := range targets {
			if e.im != nil {
				method = e.im.Name
			}
		}
		var stray []*Callee
		for _, cl := range callees {
			if !j.observedIn(cl, allowed) {
				stray = append(stray, cl)
			}
		}
		if iface {
			for _, cl := range callees {
				if !ifaceCallee(j.t, cl, method) {
					return "IFACE-OTHER", fmt.Sprintf("%s is not a method %s; observed %s", cl.QName, method, names), v
				}
			}
			if len(stray) > 0 {
				return "IFACE-OTHER", fmt.Sprintf("undocumented implementer %s; observed %s", strayNames(stray), names), v
			}
			if len(missing) > 0 {
				return "MISS", fmt.Sprintf("runtime target %s not observed; observed %s", labels(missing), names), v
			}
			if unins && ex.OpenWorld != nil && !*ex.OpenWorld {
				return "IFACE-OTHER", fmt.Sprintf("an uninstrumented implementer ran (exec %d > hits %d) but open_world is false", o.Exec, o.Hits), v
			}
			if len(callees) == 0 {
				return "UNVERIFIABLE", "the site ran but no instrumented implementer was observed", v
			}
			return "IFACE-CONSISTENT", "observed " + names, v
		}
		if len(stray) > 0 {
			return "MISS", fmt.Sprintf("undocumented callee %s; observed %s", strayNames(stray), names), v
		}
		if len(missing) > 0 {
			return "MISS", fmt.Sprintf("runtime target %s not observed; observed %s", labels(missing), names), v
		}
		primary := targets
		if ex.Status == "ambiguous" {
			if len(callees) == 0 {
				// an anonymous interface may dispatch to an implementer
				// outside the instrumented code
				return "UNVERIFIABLE", "the site ran but no instrumented candidate was observed", v
			}
			primary = append(append([]expected{}, cands...), runtime...)
		}
		reached := false
		for _, e := range allowed {
			if j.seen(e, o) {
				reached = true
			}
		}
		unhooked := false
		for _, e := range allowed {
			if e.fn != nil && !e.fn.Hooked {
				unhooked = true
			}
		}
		if !reached {
			if unhooked {
				return "UNVERIFIABLE", "the expected target has no hook (assembly or nosplit); observed " + names, v
			}
			return "MISS", fmt.Sprintf("expected %s, observed %s (exec %d, hits %d)", labels(primary), names, execOf(o), hitsOf(o)), v
		}
		if unins && !unhooked && ex.Status != "ambiguous" && !(ex.OpenWorld != nil && *ex.OpenWorld) {
			return "MISS", fmt.Sprintf("an uninstrumented callee also ran (exec %d > hits %d); observed %s", o.Exec, o.Hits, names), v
		}
		if o.Exec >= 0 && o.Exec == int64(o.Hits) {
			return "CONFIRMED", "observed " + names, v
		}
		return "CONFIRMED-NOEXEC", "observed " + names, v // no counter, or fewer completions than entries
	}
	// external without targets, builtin, dynamic, unresolved: nothing
	// undocumented may run, and every runtime target must
	var stray []*Callee
	for _, cl := range callees {
		if !j.observedIn(cl, runtime) {
			stray = append(stray, cl)
		}
	}
	if len(stray) > 0 {
		return "MISS", fmt.Sprintf("in-repo callee %s ran at a %s site; observed %s", strayNames(stray), ex.Status, names), v
	}
	if len(missing) > 0 {
		return "MISS", fmt.Sprintf("runtime target %s not observed; observed %s", labels(missing), names), v
	}
	detail := fmt.Sprintf("%s: observed %s (exec %d, hits %d, reach %d)", ex.Status, names, execOf(o), hitsOf(o), v.reach)
	// a completion counter proves the run; an external case needs more
	// completions than instrumented entries (spec R6: exec > hits)
	if o != nil && o.Exec > 0 && o.Exec >= int64(o.Hits) && (ex.Status != "external" || o.Exec > int64(o.Hits)) {
		return "CONSISTENT", detail, v
	}
	return "CONSISTENT-NOEXEC", detail, v // only a reach counter or the callees prove the run
}

func execOf(o *Obs) int64 {
	if o == nil {
		return -1
	}
	return o.Exec
}

func hitsOf(o *Obs) uint64 {
	if o == nil {
		return 0
	}
	return o.Hits
}

func strayNames(cs []*Callee) string {
	var out []string
	for _, c := range cs {
		out = append(out, c.QName)
	}
	sort.Strings(out)
	return strings.Join(out, ", ")
}

func cmdJudge(args []string, stdout io.Writer) int {
	fs := flag.NewFlagSet("judge", flag.ExitOnError)
	obsPath := fs.String("obs", "", "observations from report")
	manifestPath := fs.String("manifest", "", "ground_truth/manifest.json")
	sitesPath := fs.String("sites", "", "site table (default: from the observations' meta line)")
	ran := fs.String("ran", "", "comma-separated entries that ran and exited 0 (default: every entry a case names)")
	failed := fs.String("failed", "", "comma-separated entries that failed")
	out := fs.String("out", "", "verdicts (JSON)")
	check(fs.Parse(args))
	if *obsPath == "" || *manifestPath == "" || fs.NArg() > 0 {
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
	data, err := os.ReadFile(*manifestPath)
	check(err)
	var m manifest
	check(json.Unmarshal(data, &m))
	ranSet, failedSet := map[string]bool{}, map[string]bool{}
	for _, e := range splitList(*ran) {
		ranSet[e] = true
	}
	for _, e := range splitList(*failed) {
		failedSet[e] = true
	}
	j := &judgeCtx{t: t, idx: newSiteIndex(t), bySite: obs.bySite, byGroup: obs.byGroup, panicked: obs.panicked}
	counts := map[string]int{}
	var verdicts []*Verdict
	for _, c := range m.Cases {
		verdict, detail, v := j.judgeCase(c)
		if verdict == "UNEXEC" && c.Entry != nil {
			switch {
			case failedSet[*c.Entry]:
				verdict, detail = "ENTRY-FAILED", "entry "+*c.Entry+" failed; "+detail
			case *ran != "" && !ranSet[*c.Entry]:
				verdict, detail = "SKIPPED", "entry "+*c.Entry+" not run"
			default:
				verdict, detail = "NOT-RUN", "no entry executed the site (the case names "+*c.Entry+")"
			}
		} else if verdict == "UNEXEC" {
			detail = "no entry (entry is null)"
		}
		counts[verdict]++
		vd := &Verdict{Verdict: verdict, ID: c.ID, File: c.File, Line: c.Line, Col: c.Col, Name: c.Token,
			Detail: detail, Exec: -1, Reach: v.reach, Observed: []string{}}
		if v.obs != nil {
			vd.Observed, vd.Exec, vd.Hits = calleeNames(v.obs), v.obs.Exec, v.obs.Hits
		}
		verdicts = append(verdicts, vd)
		fmt.Fprintf(stdout, "%-22s %-10s %s\n", verdict, c.ID, detail)
	}
	pass, fail := 0, 0
	for k, n := range counts {
		if passVerdicts[k] {
			pass += n
		}
		if failVerdicts[k] {
			fail += n
		}
	}
	fmt.Fprintf(stdout, "summary: %d cases: %d pass, %d fail, %d say nothing; %s\n", len(m.Cases), pass, fail,
		len(m.Cases)-pass-fail, summaryLine(counts))
	if *out != "" {
		blob, err := json.MarshalIndent(map[string]any{"fixture": m.Fixture, "summary": counts, "pass": pass,
			"fail": fail, "cases": verdicts}, "", " ")
		check(err)
		check(os.WriteFile(*out, append(blob, '\n'), 0o644))
	}
	if fail > 0 {
		return 1
	}
	return 0
}
