package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// A synthetic site table (root /r, files a.go and c.go) and the hits of one
// run, for report, match and judge.
func syntheticTable(t *testing.T, dir string) string {
	t.Helper()
	recs := []any{
		&MetaRec{Kind: "meta", Version: 1, Root: "/r", Mode: "all", RT: rtModule},
		&FileRec{Kind: "file", File: "a.go", Abs: "/r/a.go", Pkg: "p", Status: "instrumented", Build: true},
		&FileRec{Kind: "file", File: "c.go", Abs: "/r/c.go", Pkg: "p", Status: "skipped:cgo"},
		&FuncRec{Kind: "func", Fid: 0, Pkg: "p", QName: "p.F", RTName: "p.F", Qualname: "F", Name: "F", File: "a.go", Line: 3, Col: 6, Parent: -1, Hooked: true},
		&FuncRec{Kind: "func", Fid: 1, Pkg: "p", QName: "p.G", RTName: "p.G", Qualname: "G", Name: "G", File: "a.go", Line: 5, Col: 6, Parent: -1, Hooked: true},
		&FuncRec{Kind: "func", Fid: 2, Pkg: "p", QName: "p.H", RTName: "p.H", Qualname: "H", Name: "H", File: "a.go", Line: 7, Col: 6, Parent: -1},
		&FuncRec{Kind: "func", Fid: 3, Pkg: "p", QName: "p.T.M", RTName: "p.T.M", Qualname: "T.M", Name: "M", Recv: "T", File: "a.go", Line: 9, Col: 10, Parent: -1, Hooked: true},
		&FuncRec{Kind: "func", Fid: 4, Pkg: "p", QName: "p.(*U).M", RTName: "p.(*U).M", Qualname: "U.M", Name: "M", Recv: "U", Ptr: true, File: "a.go", Line: 11, Col: 12, Parent: -1, Hooked: true},
		&FuncRec{Kind: "func", Fid: 5, Pkg: "p", QName: "p.W", RTName: "p.W", Qualname: "W", Name: "W", File: "a.go", Line: 15, Col: 6, EndLine: 25, Parent: -1, Hooked: true, HasDefer: true, ExitLines: []int{20, 25}, Defers: []int{10, 11}},
		&FuncRec{Kind: "func", Fid: 6, Pkg: "p", QName: "p.W.func1", RTName: "p.W.func1", Qualname: "W.func1", Name: "func", File: "a.go", Line: 18, Col: 9, EndLine: 18, Lit: true, Parent: 5, Hooked: true},
		&FuncRec{Kind: "func", Fid: 7, Pkg: "p", QName: "p.V.M", RTName: "p.V.M", Qualname: "V.M", Name: "M", Recv: "V", File: "a.go", Line: 13, Col: 10, Parent: -1, Hooked: true},
		&FuncRec{Kind: "func", Fid: 8, Pkg: "p", QName: "p.Z", RTName: "p.Z", Qualname: "Z", Name: "Z", File: "a.go", Line: 60, Col: 6, EndLine: 62, Parent: -1, Hooked: true, HasDefer: true, ExitLines: []int{62}, Defers: []int{14}},
		&FuncRec{Kind: "func", Fid: 9, Pkg: "p", QName: "p.Q", RTName: "p.Q", Qualname: "Q", Name: "Q", File: "a.go", Line: 80, Col: 6, EndLine: 80, Parent: -1, Hooked: true},
		&FuncRec{Kind: "func", Fid: 10, Pkg: "p", QName: "p.Q.func1", RTName: "p.Q.func1", Qualname: "Q.func1", Name: "func", File: "a.go", Line: 80, Col: 15, EndLine: 80, Lit: true, Parent: 9, Hooked: true},
		&FileRec{Kind: "file", File: "l.go", Abs: "/r/l.go", Real: "a.go", Pkg: "q", Status: "instrumented", Build: true},
		&IMethodRec{Kind: "imethod", Pkg: "p", Iface: "I", Name: "M", Qualname: "I.M", QName: "p.I.M", File: "a.go", Line: 30, Col: 2},
		&SiteRec{Kind: "site", Sid: 1, File: "a.go", Line: 40, Col: 5, Name: "F", Form: "ident", LLine: 40, LCol: 6, Fid: 0, Stmt: "call", Tagged: true},
		&SiteRec{Kind: "site", Sid: 2, File: "a.go", Line: 41, Col: 5, Name: "G", Form: "ident", LLine: 41, LCol: 6, Fid: 0, Stmt: "call", Tagged: true},
		&SiteRec{Kind: "site", Sid: 3, File: "a.go", Line: 42, Col: 7, Name: "M", Form: "selector", LLine: 42, LCol: 8, Fid: 0, Stmt: "call", Tagged: true},
		&SiteRec{Kind: "site", Sid: 4, File: "a.go", Line: 43, Col: 5, Name: "F", Form: "ident", LLine: 43, LCol: 6, Fid: 0, Stmt: "go", GoLine: 43, Tagged: true},
		&SiteRec{Kind: "site", Sid: 5, File: "a.go", Line: 40, Col: 20, Name: "G", Form: "ident", LLine: 40, LCol: 21, Fid: 0, Stmt: "call", Tagged: true},
		&SiteRec{Kind: "site", Sid: 6, File: "a.go", Line: 45, Col: 5, Name: "F", Form: "ident", LLine: 45, LCol: 6, Fid: 0, Stmt: "call", Tagged: true},
		&SiteRec{Kind: "site", Sid: 7, File: "a.go", Line: 44, Col: 5, Name: "F", Form: "ident", LLine: 44, LCol: 6, Fid: 0, Stmt: "go", GoLine: 44, Tagged: true},
		&SiteRec{Kind: "site", Sid: 10, File: "a.go", Line: 16, Col: 8, Name: "G", Form: "ident", LLine: 16, LCol: 9, Fid: 5, Stmt: "defer", Tagged: true},
		&SiteRec{Kind: "site", Sid: 11, File: "a.go", Line: 17, Col: 8, Name: "F", Form: "ident", LLine: 17, LCol: 9, Fid: 5, Stmt: "defer", Tagged: true},
		&SiteRec{Kind: "site", Sid: 12, File: "a.go", Line: 50, Col: 5, Name: "G", Form: "ident", LLine: 50, LCol: 6, Fid: 0, Stmt: "call"},
		&SiteRec{Kind: "site", Sid: 13, File: "a.go", Line: 51, Col: 17, Name: "F", Form: "range", LLine: 51, LCol: 2, Fid: 0, Stmt: "range", Tagged: true, Hot: "range"},
		&SiteRec{Kind: "site", Sid: 14, File: "a.go", Line: 61, Col: 8, Name: "F", Form: "ident", LLine: 61, LCol: 9, Fid: 8, Stmt: "defer", Tagged: true},
		&SiteRec{Kind: "site", Sid: 15, File: "a.go", Line: 61, Col: 20, Name: "G", Form: "ident", LLine: 61, LCol: 21, Fid: 8, Stmt: "call", Tagged: true},
		&SiteRec{Kind: "site", Sid: 16, File: "a.go", Line: 70, Col: 5, Name: "F", Form: "ident", LLine: 70, LCol: 6, Fid: 0, Stmt: "call", Tagged: true},
		&SiteRec{Kind: "site", Sid: 17, File: "l.go", Line: 46, Col: 5, Name: "F", Form: "ident", LLine: 46, LCol: 6, Fid: 0, Stmt: "call", Tagged: true},
		&SiteRec{Kind: "site", Sid: 19, File: "a.go", Line: 46, Col: 5, Name: "F", Form: "ident", LLine: 46, LCol: 6, Fid: 0, Stmt: "call", Tagged: true},
		&HotRec{Kind: "hot", File: "a.go", Line: 62, Why: "exit"},
		&HotRec{Kind: "hot", File: "a.go", Line: 20, Why: "exit"},
		&HotRec{Kind: "hot", File: "a.go", Line: 25, Why: "exit"},
		&HotRec{Kind: "hot", File: "a.go", Line: 52, Why: "range"},
		&StmtRec{Kind: "stmt", ID: 0, File: "a.go", Line: 40, Sites: []int{1}},
		&StmtRec{Kind: "stmt", ID: 1, File: "a.go", Line: 41, Sites: []int{2}},
		&StmtRec{Kind: "reach", ID: 2, File: "a.go", Line: 16, Sites: []int{10}},
		&StmtRec{Kind: "reach", ID: 3, File: "a.go", Line: 17, Sites: []int{11}},
		&StmtRec{Kind: "stmt", ID: 4, File: "a.go", Line: 42, Sites: []int{3}},
		&StmtRec{Kind: "stmt", ID: 5, File: "a.go", Line: 40, Sites: []int{5}},
		&StmtRec{Kind: "stmt", ID: 6, File: "a.go", Line: 45, Sites: []int{6}},
		&StmtRec{Kind: "reach", ID: 7, File: "a.go", Line: 44, Sites: []int{7}},
		&StmtRec{Kind: "reach", ID: 8, File: "a.go", Line: 61, Sites: []int{14}},
		&StmtRec{Kind: "stmt", ID: 9, File: "a.go", Line: 70, Sites: []int{16}},
	}
	path := filepath.Join(dir, "sites.jsonl")
	writeJSONL(path, recs)
	hits := `{"kind":"meta","pid":1}
{"kind":"call","fid":0,"callee":"p.F","caller":"p.X","file":"/r/a.go#1","line":40,"n":3}
{"kind":"call","fid":1,"callee":"p.G","caller":"p.X","file":"/r/a.go#2","line":41,"n":2}
{"kind":"call","fid":0,"callee":"p.F","caller":"p.X","file":"/r/a.go#2","line":41,"n":1}
{"kind":"call","fid":3,"callee":"p.T.M","caller":"p.X","file":"/r/a.go#3","line":42,"n":1}
{"kind":"call","fid":4,"callee":"p.(*U).M","caller":"p.X","file":"/r/a.go#3","line":42,"n":1}
{"kind":"go","fid":0,"callee":"p.F","caller":"p.X","file":"/r/a.go","line":43,"n":1}
{"kind":"call","fid":1,"callee":"p.G","caller":"p.W","file":"/r/a.go","line":20,"n":1}
{"kind":"call","fid":0,"callee":"p.F","caller":"p.W","file":"/r/a.go","line":25,"n":1}
{"kind":"call","fid":1,"callee":"p.G","caller":"p.W","file":"/r/a.go","line":22,"n":1}
{"kind":"call","fid":1,"callee":"p.G","caller":"p.X","file":"/r/a.go","line":50,"n":1}
{"kind":"call","fid":0,"callee":"p.F","caller":"p.X","file":"/r/a.go","line":52,"n":1}
{"kind":"call","fid":0,"callee":"p.F","caller":"sort.x","file":"/go/src/sort/x.go","line":5,"n":1}
{"kind":"call","fid":1,"callee":"p.G","caller":"p.X","file":"/r/a.go#5","line":40,"n":1}
{"kind":"call","fid":0,"callee":"p.F","caller":"p.Z","file":"/r/a.go#15","line":61,"n":1,"defer":true}
{"kind":"call","fid":7,"callee":"p.V.M","caller":"runtime.gopanic","file":"/go/src/runtime/panic.go","line":900,"n":2}
{"kind":"panic","fid":7,"callee":"p.V.M","stack":["p.W","p.X"],"n":2}
{"kind":"call","fid":0,"callee":"p.F","caller":"p.X","file":"/r/a.go#16","line":70,"n":1}
{"kind":"call","fid":1,"callee":"p.G","caller":"p.X","file":"/r/l.go#17","line":46,"n":1}
{"kind":"call","fid":0,"callee":"p.F","caller":"p.X","file":"/r/a.go#19","line":46,"n":1}
{"kind":"done","fid":0,"n":3}
{"kind":"done","fid":1,"n":5}
{"kind":"done","fid":2,"n":1}
{"kind":"done","fid":4,"n":2}
{"kind":"done","fid":5,"n":2}
{"kind":"done","fid":6,"n":1}
{"kind":"done","fid":7,"n":1}
{"kind":"done","fid":8,"n":1}
`
	hitsDir := filepath.Join(dir, "hits")
	if err := os.MkdirAll(hitsDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(hitsDir, "hits-1.jsonl"), []byte(hits), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func runReport(t *testing.T) (dir, sites, obs string) {
	t.Helper()
	dir = t.TempDir()
	sites = syntheticTable(t, dir)
	tb := loadTable(sites)
	recs, st, sum := attribute(tb, hitFiles(filepath.Join(dir, "hits")))
	obs = filepath.Join(dir, "obs.jsonl")
	writeJSONL(obs, append([]any{&ObsMeta{Kind: "meta", Sites: sites, Root: "/r"}}, recs...))
	var buf bytes.Buffer
	printReport(&buf, tb, st, sum)
	if !strings.Contains(buf.String(), "range-implicit:1") || !strings.Contains(buf.String(), "no-site-at-line:1") ||
		!strings.Contains(buf.String(), "1 sort.x") || !strings.Contains(buf.String(), "2 runtime.gopanic") {
		t.Errorf("report text:\n%s", buf.String())
	}
	return
}

func TestReportAttribution(t *testing.T) {
	_, sites, obs := runReport(t)
	set := loadObs(obs)
	bySite, byGroup := set.bySite, set.byGroup
	tb := loadTable(sites)
	type want struct {
		via         string
		hits        uint64
		exec, reach int64
		callees     string
	}
	cases := map[int]want{
		1:  {"tag", 3, 3, -1, "p.F"},
		2:  {"tag", 3, 5, -1, "p.F,p.G"},
		3:  {"tag", 2, 2, -1, "p.T.M,p.(*U).M"},
		4:  {"go", 1, -1, -1, "p.F"},
		5:  {"tag", 1, 2, -1, "p.G"},
		6:  {"exec-only", 0, 1, -1, ""},
		7:  {"reach-only", 0, -1, 1, ""},
		10: {"reach-only", 0, -1, 1, ""},
		12: {"line", 1, -1, -1, "p.G"},
		14: {"reach-only", 0, -1, 1, ""},
		16: {"tag", 1, 0, -1, "p.F"},
		17: {"tag", 1, -1, -1, "p.G"},
		19: {"tag", 1, -1, -1, "p.F"},
	}
	for sid, w := range cases {
		o := bySite[sid]
		if o == nil {
			t.Errorf("site %d: no observation", sid)
			continue
		}
		var names []string
		for _, c := range o.Callees {
			names = append(names, c.QName)
		}
		if o.Via != w.via || o.Hits != w.hits || o.Exec != w.exec || o.Reach != w.reach || strings.Join(names, ",") != w.callees {
			t.Errorf("site %d: %+v callees %v, want %+v", sid, o, names, w)
		}
	}
	if bySite[11] != nil || bySite[13] != nil || bySite[15] != nil {
		t.Errorf("unexecuted sites observed: %+v %+v %+v", bySite[11], bySite[13], bySite[15])
	}
	g := byGroup[5]
	if g == nil || g.Via != "defer-group" || g.Exec != -1 || g.Hits != 2 || len(g.Callees) != 2 {
		t.Fatalf("defer group %+v", g)
	}
	// the deferreturn-run entry at a tagged position goes to Z's group, not to site 15
	if z := byGroup[8]; z == nil || z.Via != "defer-single" || len(z.Callees) != 1 || z.Callees[0].QName != "p.F" {
		t.Errorf("deferreturn routing %+v", z)
	}
	if set.panicked[[2]int{5, 7}] != 2 || len(set.panicked) != 1 { // W (fid 5) has defers; X has none
		t.Errorf("panic-run entries %v", set.panicked)
	}
	if v := viewOf(tb, tb.sites[11], bySite, byGroup); !v.group || v.reach != 0 || v.obs != g {
		t.Errorf("view of an unexecuted defer %+v", v)
	}
}

func writeEdges(t *testing.T, dir string, edges []string) string {
	t.Helper()
	path := filepath.Join(dir, "edges.jsonl")
	if err := os.WriteFile(path, []byte(strings.Join(edges, "\n")+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestMatchVerdicts(t *testing.T) {
	dir, _, obs := runReport(t)
	edges := []struct{ edge, want string }{
		{`{"file":"a.go","line":40,"col":5,"name":"F","target":{"file":"a.go","line":3}}`, "CONFIRMED"},
		{`{"file":"a.go","line":40,"col":5,"name":"F","target":{"file":"a.go","qualname":"G"}}`, "WRONG-OTHER"},
		{`{"file":"a.go","line":41,"col":5,"name":"G","target":{"qname":"p.G"}}`, "WRONG-MULTI"},
		{`{"file":"a.go","line":40,"col":20,"name":"G","target":{"qname":"p.G"}}`, "WRONG-MULTI"},
		{`{"file":"a.go","line":40,"col":20,"name":"G","target":{"qname":"p.F"}}`, "WRONG-OTHER"},
		{`{"file":"a.go","line":42,"col":7,"name":"M","target":{"file":"a.go","qualname":"I.M"}}`, "IFACE-CONSISTENT"},
		{`{"file":"a.go","line":42,"col":7,"name":"M","target":{"qname":"io.Runner.Run","kind":"iface-method"}}`, "IFACE-OTHER"},
		{`{"file":"a.go","line":41,"col":5,"name":"G","target":{"file":"a.go","line":30}}`, "IFACE-OTHER"},
		{`{"file":"a.go","line":43,"col":5,"name":"F","target":{"qname":"p.F"}}`, "CONFIRMED-NOEXEC"},
		{`{"file":"a.go","line":16,"col":8,"name":"G","target":{"qname":"p.G"}}`, "DEFER-GROUP-CONSISTENT"},
		{`{"file":"a.go","line":16,"col":8,"name":"G","target":{"qname":"p.W.func1"}}`, "DEFER-GROUP-OTHER"},
		{`{"file":"a.go","line":17,"col":8,"name":"F","target":{"qname":"p.W.func1"}}`, "UNEXERCISED"},
		{`{"file":"a.go","line":45,"col":5,"name":"F","target":{"qname":"p.F"}}`, "WRONG-OTHER"},
		{`{"file":"a.go","line":45,"col":5,"name":"F","target":{"qname":"p.H"}}`, "UNVERIFIABLE-TARGET"},
		{`{"file":"a.go","line":50,"col":5,"name":"G","target":{"qname":"p.G"}}`, "CONFIRMED-NOEXEC"},
		{`{"file":"a.go","line":51,"col":17,"name":"F","target":{"qname":"p.F"}}`, "UNEXERCISED"},
		{`{"file":"a.go","line":99,"col":1,"name":"F","target":{"qname":"p.F"}}`, "NO-SITE"},
		{`{"file":"a.go","line":40,"col":5,"name":"F","target":{"file":"a.go","qualname":"Nope"}}`, "NO-TARGET"},
		{`{"file":"a.go","line":40,"col":99,"name":"F","target":{"qname":"p.F"}}`, "CONFIRMED"},
		{`{"file":"a.go","line":61,"col":8,"name":"F","target":{"qname":"p.F"}}`, "CONFIRMED-NOEXEC"},
		{`{"file":"a.go","line":61,"col":20,"name":"G","target":{"qname":"p.G"}}`, "UNEXERCISED"},
		{`{"file":"a.go","line":16,"col":8,"name":"G","target":{"qname":"p.V.M"}}`, "UNEXERCISED"},
		{`{"file":"a.go","line":45,"col":5,"name":"F","target":{"file":"a.go","qualname":"I.M"}}`, "UNEXERCISED"},
		{`{"file":"a.go","line":42,"col":7,"name":"M","target":{"qname":"p.V.M","kind":"iface-method"}}`, "WRONG-OTHER"},
		{`{"file":"a.go","line":70,"col":5,"name":"F","target":{"qname":"p.F"}}`, "CONFIRMED-NOEXEC"},
		{`{"file":"a.go","line":80,"col":1,"name":"Q","target":{"file":"a.go","line":80}}`, "NO-SITE"},
		{`{"file":"a.go","line":40,"col":5,"name":"F","target":{"file":"a.go","line":80}}`, "NO-TARGET"},
		{`{"file":"a.go","line":40,"col":5,"name":"F","target":{"file":"a.go","line":80,"col":15}}`, "WRONG-OTHER"},
		{`{"file":"a.go","line":46,"col":5,"name":"F","target":{"qname":"p.F"}}`, "WRONG-MULTI"},
		{`{"file":"a.go","line":42,"col":7,"name":"M","target":{"file":"a.go","qualname":"I.M"},"candidates":[{"qname":"p.T.M"}]}`, "IFACE-OTHER"},
		{`{"file":"a.go","line":42,"col":7,"name":"M","target":{"file":"a.go","qualname":"I.M"},"candidates":[{"qname":"p.T.M"},{"file":"a.go","line":11,"col":12}]}`, "IFACE-CONSISTENT"},
		{`{"file":"a.go","line":42,"col":7,"name":"M","target":{"file":"a.go","qualname":"I.M"},"candidates":[{"qname":"q.Gone"}]}`, "IFACE-OTHER"},
		{`{"file":"a.go","line":42,"col":7,"name":"M","target":{"file":"a.go","qualname":"I.M"},"candidates":[]}`, "IFACE-OTHER"},
		{`{"file":"a.go","line":42,"col":7,"name":"M","target":{"file":"a.go","qualname":"I.M"},"candidates":[{"file":"a.go","line":9,"col":10},{"file":"a.go","line":12,"qualname":"Gone"}]}`, "NO-TARGET"},
		{`{"file":"a.go","line":42,"col":7,"name":"M","target":{"file":"a.go","qualname":"I.M"},"candidates":[{"file":"a.go","qualname":"I.M"}]}`, "IFACE-CONSISTENT"},
		{`{"file":"a.go","line":42,"col":7,"name":"M","target":{"file":"a.go","qualname":"I.M"},"candidates":[{"qname":"p.J.M","via_embedded_iface":true}]}`, "IFACE-CONSISTENT"},
		{`{"file":"/r/a.go","line":42,"col":7,"name":"M","target":{"file":"/r/a.go","qualname":"I.M"},"candidates":[{"file":"/r/a.go","line":9},{"file":"/r/a.go","line":11}]}`, "IFACE-CONSISTENT"},
	}
	var lines []string
	for _, e := range edges {
		lines = append(lines, e.edge)
	}
	path := writeEdges(t, dir, lines)
	out := filepath.Join(dir, "verdicts.jsonl")
	var buf bytes.Buffer
	if rc := cmdMatch([]string{"--obs", obs, "--records", path, "--out", out}, &buf); rc != 1 {
		t.Errorf("exit %d, want 1 (wrong verdicts present)", rc)
	}
	var got []string
	readJSONL(out, func(b []byte) {
		var v Verdict
		if err := json.Unmarshal(b, &v); err != nil {
			t.Fatal(err)
		}
		got = append(got, v.Verdict)
	})
	for i, e := range edges {
		if i >= len(got) || got[i] != e.want {
			t.Errorf("edge %d %s: got %v, want %s", i, e.edge, got[i:i+1], e.want)
		}
	}
	if !strings.Contains(buf.String(), "summary: CONFIRMED=2 CONFIRMED-NOEXEC=4 DEFER-GROUP-CONSISTENT=1 ") {
		t.Errorf("summary line:\n%s", buf.String())
	}
}

type jcase struct {
	ID       string          `json:"id"`
	File     string          `json:"file"`
	Line     int             `json:"line"`
	Col      int             `json:"col"`
	Token    string          `json:"token"`
	SiteKind string          `json:"site_kind"`
	Entry    *string         `json:"entry"`
	Expect   json.RawMessage `json:"expect"`
}

func TestJudgeVerdicts(t *testing.T) {
	dir, _, obs := runReport(t)
	run, other, bad := "run", "other", "bad"
	tg := func(q string) string { return `{"qualname":"` + q + `","module":"m","kind":"method","file":"a.go"}` }
	exp := func(status, targets, runtime, extra string) json.RawMessage {
		return json.RawMessage(`{"status":"` + status + `","targets":[` + targets + `],"candidates":[],"runtime_targets":[` +
			runtime + `],"distractors":[],"static_target":null,"open_world":null,"external":null,"reason":null` + extra + `}`)
	}
	cases := []struct {
		c    jcase
		want string
	}{
		{jcase{"C01", "a.go", 40, 5, "F", "call", &run, exp("resolved", tg("F"), "", "")}, "CONFIRMED"},
		{jcase{"C02", "a.go", 40, 5, "F", "call", &run, exp("resolved", tg("G"), "", "")}, "MISS"},
		{jcase{"C03", "a.go", 41, 5, "G", "call", &run, exp("resolved", tg("F"), tg("G"), "")}, "MISS"},
		{jcase{"C04", "a.go", 41, 5, "G", "call", &run, exp("resolved", tg("F"), tg("G"), `,"open_world":true`)}, "CONFIRMED-NOEXEC"},
		{jcase{"C05", "a.go", 42, 7, "M", "call", &run, exp("resolved", tg("I.M"), tg("T.M")+","+tg("U.M"), "")}, "IFACE-CONSISTENT"},
		{jcase{"C06", "a.go", 42, 7, "M", "call", &run, exp("resolved", tg("I.M"), tg("T.M"), "")}, "IFACE-OTHER"},
		{jcase{"C07", "a.go", 42, 7, "M", "call", &run, exp("resolved", tg("I.M"), tg("T.M")+","+tg("U.M")+","+tg("V.M"), "")}, "MISS"},
		{jcase{"C08", "a.go", 43, 5, "F", "go", &run, exp("resolved", tg("F"), "", "")}, "CONFIRMED-NOEXEC"},
		{jcase{"C09", "a.go", 16, 8, "G", "defer", &run, exp("resolved", tg("G"), "", "")}, "DEFER-GROUP-CONSISTENT"},
		{jcase{"C10", "a.go", 16, 8, "G", "defer", &run, exp("resolved", tg("W.func1"), "", "")}, "DEFER-GROUP-OTHER"},
		{jcase{"C11", "a.go", 17, 8, "F", "defer", &run, exp("resolved", tg("F"), "", "")}, "NOT-RUN"},
		{jcase{"C12", "a.go", 17, 8, "F", "defer", &bad, exp("resolved", tg("F"), "", "")}, "ENTRY-FAILED"},
		{jcase{"C13", "a.go", 17, 8, "F", "defer", &other, exp("resolved", tg("F"), "", "")}, "SKIPPED"},
		{jcase{"C14", "a.go", 17, 8, "F", "defer", nil, exp("resolved", tg("F"), "", "")}, "UNEXEC"},
		{jcase{"C15", "a.go", 45, 5, "F", "call", &run, exp("external", "", "", `,"external":{"module":"std"}`)}, "CONSISTENT"},
		{jcase{"C16", "a.go", 45, 5, "F", "call", &run, exp("resolved", tg("F"), "", "")}, "MISS"},
		{jcase{"C17", "a.go", 40, 5, "F", "call", &run, exp("dynamic", "", "", "")}, "MISS"},
		{jcase{"C18", "a.go", 40, 5, "F", "call", &run, exp("dynamic", "", tg("F"), "")}, "CONSISTENT"},
		{jcase{"C19", "a.go", 41, 5, "G", "call", &run, exp("resolved", tg("F"), tg("G"), `,"open_world":true,"distractors":[`+tg("G")+`]`)}, "MISS"},
		{jcase{"C20", "a.go", 40, 5, "F", "call", &run, exp("resolved", tg("Nope"), "", "")}, "TABLE-MISMATCH"},
		{jcase{"C21", "a.go", 40, 6, "F", "call", &run, exp("resolved", tg("F"), "", "")}, "NO-SITE"},
		{jcase{"C22", "c.go", 3, 2, "F", "call", &run, exp("resolved", tg("F"), "", "")}, "UNVERIFIABLE"},
		{jcase{"C23", "a.go", 45, 5, "F", "call", &run, exp("resolved", tg("H"), "", "")}, "UNVERIFIABLE"},
		{jcase{"C24", "a.go", 44, 5, "F", "go", &run, exp("resolved", tg("F"), "", "")}, "UNVERIFIABLE"},
		{jcase{"C25", "a.go", 40, 5, "F", "defer", &run, exp("resolved", tg("F"), "", "")}, "NO-SITE"},
		{jcase{"C26", "a.go", 45, 5, "F", "call", &run, exp("builtin", "", "", "")}, "CONSISTENT"},
		{jcase{"C27", "a.go", 61, 8, "F", "defer", &run, exp("resolved", tg("F"), "", "")}, "CONFIRMED-NOEXEC"},
		{jcase{"C28", "a.go", 16, 8, "G", "defer", &run, exp("resolved", tg("V.M"), "", "")}, "UNVERIFIABLE"},
		{jcase{"C29", "a.go", 45, 5, "F", "call", &run, exp("resolved", tg("I.M"), "", "")}, "UNVERIFIABLE"},
		{jcase{"C30", "a.go", 70, 5, "F", "call", &run, exp("resolved", tg("F"), "", "")}, "CONFIRMED-NOEXEC"},
		{jcase{"C31", "a.go", 51, 17, "F", "range", &run, exp("external", "", "", `,"external":{"module":"std"}`)}, "UNVERIFIABLE"},
		{jcase{"C32", "a.go", 51, 17, "F", "range", &run, exp("resolved", tg("F"), "", "")}, "NOT-RUN"},
		{jcase{"C33", "a.go", 16, 8, "G", "defer", &run, exp("resolved", tg("G"), "", `,"distractors":[`+tg("F")+`]`)}, "DEFER-GROUP-CONSISTENT"},
		{jcase{"C34", "a.go", 16, 8, "G", "defer", &run, exp("resolved", tg("I.M"), "", "")}, "UNVERIFIABLE"},
		{jcase{"C35", "a.go", 16, 8, "G", "defer", &run, exp("external", "", "", `,"external":{"module":"std"}`)}, "UNVERIFIABLE"},
		{jcase{"C36", "a.go", 41, 5, "G", "call", &run, exp("resolved", tg("F"), tg("G"), `,"open_world":true,"candidates":[`+tg("Nowhere")+`]`)}, "TABLE-MISMATCH"},
		{jcase{"C37", "a.go", 46, 5, "F", "call", &run, exp("resolved", tg("F"), "", "")}, "MISS"},
		{jcase{"C38", "a.go", 46, 5, "F", "call", &run, exp("ambiguous", "", tg("F")+","+tg("G"), `,"candidates":[`+tg("F")+","+tg("G")+`]`)}, "CONFIRMED-NOEXEC"},
		{jcase{"C39", "a.go", 42, 7, "M", "call", &run, exp("resolved", tg("I.M"), tg("T.M"), `,"candidates":[`+tg("U.M")+`]`)}, "IFACE-CONSISTENT"},
		{jcase{"C40", "a.go", 45, 5, "F", "call", &run, exp("ambiguous", "", "", `,"candidates":[`+tg("F")+`]`)}, "UNVERIFIABLE"},
		{jcase{"C41", "a.go", 40, 9, "byte", "call", &run, exp("conversion", "", "", "")}, "UNVERIFIABLE"},
		{jcase{"C42", "a.go", 44, 5, "F", "go", &run, exp("external", "", "", `,"external":{"module":"std"}`)}, "CONSISTENT-NOEXEC"},
		{jcase{"C43", "a.go", 41, 5, "G", "call", &run, exp("external", "", "", `,"external":{"module":"std"}`)}, "MISS"},
	}
	var list []jcase
	for _, c := range cases {
		list = append(list, c.c)
	}
	blob, err := json.Marshal(map[string]any{"fixture": "synthetic", "cases": list})
	if err != nil {
		t.Fatal(err)
	}
	mpath := filepath.Join(dir, "manifest.json")
	if err := os.WriteFile(mpath, blob, 0o644); err != nil {
		t.Fatal(err)
	}
	out := filepath.Join(dir, "verdicts.json")
	var buf bytes.Buffer
	if rc := cmdJudge([]string{"--obs", obs, "--manifest", mpath, "--ran", "run", "--failed", "bad", "--out", out}, &buf); rc != 1 {
		t.Errorf("exit %d, want 1", rc)
	}
	var res struct {
		Cases []Verdict `json:"cases"`
		Fail  int       `json:"fail"`
	}
	data, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, &res); err != nil {
		t.Fatal(err)
	}
	for i, c := range cases {
		if res.Cases[i].Verdict != c.want {
			t.Errorf("%s: %s (%s), want %s", c.c.ID, res.Cases[i].Verdict, res.Cases[i].Detail, c.want)
		}
	}
	if res.Fail != 17 {
		t.Errorf("fail count %d\n%s", res.Fail, buf.String())
	}
}
