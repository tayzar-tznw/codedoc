package resolve

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/tools/go/packages"
)

var trapMods = []string{"example.com/thirdparty/suite@v1.0.0", "example.com/thirdparty/zap@v1.0.0"}

func trapRepo(t *testing.T, px *proxy) string {
	t.Helper()
	repo := fixture(t, "trap")
	writeFile(t, filepath.Join(repo, "go.sum"), px.goSum(trapMods...))
	return repo
}

func trapOpts(repo string) *Options {
	return &Options{RepoRoot: repo, Dir: repo, Pattern: "./...", GoWork: "off", MainModules: []string{"example.com/trap"}}
}

// TestOfflineTrapsTriggered checks that the fixture really exercises loader
// traps 1 and 2 offline: go/packages attaches logutil/zap.go to the stub of
// the missing zap package (golang/go#39986).
func TestOfflineTrapsTriggered(t *testing.T) {
	px := buildProxy(t)
	repo := trapRepo(t, px)
	env := goEnv(t, nil)
	opts := trapOpts(repo)
	pr := newProgram(opts, loadEnv(opts, env))
	cfg := &packages.Config{Mode: loadMode, Tests: true, Dir: repo, Env: loadEnv(opts, env),
		BuildFlags: buildFlags(opts), Fset: pr.fset, ParseFile: pr.parseFile}
	roots, err := packages.Load(cfg, "./...")
	if err != nil {
		t.Fatal(err)
	}
	pr.index(roots)
	zapFile := filepath.Join(repo, "logutil", "zap.go")
	found := false
	for _, p := range pr.all {
		if p.PkgPath != "example.com/thirdparty/zap" {
			continue
		}
		if !pr.isStub(p) {
			t.Errorf("the offline zap package is not a stub: %+v", p)
		}
		for _, f := range p.GoFiles {
			if f == zapFile {
				found = true
				if _, ok := pr.accept(p, f); ok {
					t.Error("accept() took the attached file of a stub")
				}
				if !attached(p, f) {
					t.Error("attached() does not see the attachment")
				}
			}
		}
		// go/types names the nameless stub after its attached file; that name
		// collides with the real logutil import in zapp/app.go (trap 2).
		if p.Types == nil || p.Types.Name() != "logutil" {
			t.Errorf("stub types.Package name = %q, want logutil", p.Types.Name())
		}
	}
	if !found {
		t.Fatal("the fixture no longer triggers golang/go#39986: no stub holds logutil/zap.go")
	}
}

func TestOfflineTraps(t *testing.T) {
	px := buildProxy(t)
	repo := trapRepo(t, px)
	dl := resolveRun(t, trapOpts(repo), goEnv(t, map[string]string{"GOPROXY": px.url}))
	for _, e := range dl.errors {
		t.Errorf("download mode: unexpected error %+v", e)
	}
	off := resolveRun(t, trapOpts(repo), goEnv(t, nil))

	// Trap 3: o.SetT resolves to suite.Suite.SetT at depth 1 in download mode;
	// offline suite.Suite is invalid and the selection would fall to Inner.SetT.
	s := dl.site(t, "shadow/shadow.go", "return o.SetT(nil)", "SetT")
	if s.Class != "method" || s.PromotedDepth != 1 || s.Target == nil || s.Target.Module != "example.com/thirdparty/suite" || s.EmbedInvalid {
		t.Errorf("download o.SetT: %+v %+v", s, s.Target)
	}
	s = off.site(t, "shadow/shadow.go", "return o.SetT(nil)", "SetT")
	if !s.EmbedInvalid || s.Target != nil || s.Class != "method" || s.PromotedDepth != 2 || !s.PkgErrors {
		t.Errorf("offline o.SetT: want embed_invalid with no target, got %+v %+v", s, s.Target)
	}
	// Mid has no invalid embedded field: its promoted SetT stays resolved.
	s = off.site(t, "shadow/shadow.go", "return m.SetT(nil)", "SetT")
	if s.EmbedInvalid || s.Target == nil {
		t.Errorf("offline m.SetT: %+v", s)
	}
	off.wantTarget(t, "offline m.SetT", s.Target, "shadow/shadow.go", "func (Inner) SetT(", "SetT")
	// An embedded interface whose method set is incomplete (embeds the missing
	// zap.Core) shadows Deep.Write at depth 2 in download mode.
	s = dl.site(t, "shadow/shadow.go", `o.Write("x")`, "Write")
	if s.Class != "iface" || s.PromotedDepth != 1 || s.Target == nil || s.Target.Module != "example.com/thirdparty/zap" {
		t.Errorf("download o.Write: %+v %+v", s, s.Target)
	}
	s = off.site(t, "shadow/shadow.go", `o.Write("x")`, "Write")
	if !s.EmbedInvalid || s.Target != nil {
		t.Errorf("offline o.Write: want embed_invalid with no target, got %+v %+v", s, s.Target)
	}

	// Trap 1: the attached file belongs to its own package only.
	fr := off.files["logutil/zap.go"]
	if fr == nil || len(fr.Packages) != 1 || fr.Packages[0] != "example.com/trap/logutil" {
		t.Errorf("offline file record for logutil/zap.go: %+v", fr)
	}
	s = off.site(t, "logutil/zap.go", "return Helper()", "Helper")
	if s.Class != "func" || len(s.Variants) != 0 {
		t.Errorf("offline Helper(): %+v", s)
	}
	off.wantTarget(t, "offline Helper()", s.Target, "logutil/helper.go", "func Helper()", "Helper")

	// Trap 2: the stub took the name logutil; a target in it is rejected.
	s = off.site(t, "zapp/app.go", "_ = logutil.CreateLogger()", "CreateLogger")
	if s.Target != nil && !s.Stub {
		off.wantTarget(t, "offline logutil.CreateLogger", s.Target, "logutil/zap.go", "func CreateLogger()", "CreateLogger")
	}
	if !s.Stub || s.Target != nil {
		t.Errorf("offline logutil.CreateLogger: want stub=true, target null; got %+v %+v", s, s.Target)
	}
	// Gate G4. In a package with import errors, iface candidates are suppressed.
	s = dl.site(t, "shadow/shadow.go", "return g.Greet()", "Greet")
	if s.Iface == nil || s.Iface.Suppressed || len(s.Iface.Candidates) != 1 {
		t.Errorf("download g.Greet: %+v", s.Iface)
	}
	s = off.site(t, "shadow/shadow.go", "return g.Greet()", "Greet")
	if s.Target == nil || s.Iface == nil || !s.Iface.Suppressed || len(s.Iface.Candidates) != 0 || !s.PkgErrors {
		t.Errorf("offline g.Greet: want the static target with suppressed candidates, got %+v %+v", s, s.Iface)
	}
	// An interface whose method set involves an invalid type is suppressed even
	// where the site's package has no errors; other interfaces there are not.
	s = dl.site(t, "user/user.go", "return l.Log(nil)", "Log")
	if s.Iface == nil || s.Iface.Suppressed || len(s.Iface.Candidates) != 1 || s.Iface.Candidates[0].Type != "MyLog" {
		t.Errorf("download l.Log: %+v", s.Iface)
	}
	s = off.site(t, "user/user.go", "return l.Log(nil)", "Log")
	if s.PkgErrors {
		t.Errorf("offline user package unexpectedly has errors: %+v", s)
	}
	if s.Iface == nil || !s.Iface.Suppressed || len(s.Iface.Candidates) != 0 {
		t.Errorf("offline l.Log: want suppressed candidates, got %+v", s.Iface)
	}
	s = off.site(t, "user/user.go", "return n.Name()", "Name")
	if s.Iface == nil || s.Iface.Suppressed || len(s.Iface.Candidates) != 1 {
		t.Errorf("offline n.Name: want candidates, got %+v", s.Iface)
	}
	// Trap 3 across packages (spec-review probe p2, R1): crossa has no load
	// errors, but crossb.S embeds the missing zap.Sugar next to Mid.
	s = dl.site(t, "crossa/a.go", `x.Info("x")`, "Info")
	if s.Target == nil || s.Target.Module != "example.com/thirdparty/zap" || s.PromotedDepth != 2 {
		t.Errorf("download crossa x.Info: %+v %+v", s, s.Target)
	}
	s = off.site(t, "crossa/a.go", `x.Info("x")`, "Info")
	if !s.EmbedInvalid || s.Target != nil || s.PkgErrors {
		t.Errorf("offline crossa x.Info: want embed_invalid, no target, no package errors; got %+v %+v", s, s.Target)
	}
	// Range candidates: download mode has a notcall for the slice and a site
	// for the func; offline both types are unknown, so both are unknown sites
	// (unresolved, never a notcall; ruling 2026-10-14).
	lv, _, _ := at(t, filepath.Join(dl.root, "shadow/shadow.go"), "for range zap.Levels {", "Levels")
	sq, _, _ := at(t, filepath.Join(dl.root, "shadow/shadow.go"), "for x := range zap.Seq {", "Seq")
	if !dl.notcalls["shadow/shadow.go"][lv] {
		t.Error("download range over a slice: want notcall")
	}
	if s := dl.sites["shadow/shadow.go"][sq]; s == nil || s.SiteKind != "range" || s.Class != "func" || s.Target == nil || s.Target.Name != "Seq" {
		t.Errorf("download range over zap.Seq: %+v", s)
	}
	for _, o := range []int{lv, sq} {
		if s := off.sites["shadow/shadow.go"][o]; s == nil || s.SiteKind != "range" || s.Class != "unknown" || s.Target != nil {
			t.Errorf("offline range candidate at %d: want an unknown range site, got %+v", o, s)
		}
	}
	// Trap 3 also covers an embedded stub copy of an in-repo type: offline
	// logutil.Thing in stubemb resolves to the zap stub's copy, which lacks the
	// SetT declared in logutil/helper.go (reviewer probe p2).
	s = dl.site(t, "stubemb/s.go", "return o.SetT(nil)", "SetT")
	if s.Target == nil || s.PromotedDepth != 1 {
		t.Errorf("download stubemb o.SetT: %+v", s)
	} else {
		dl.wantTarget(t, "download stubemb o.SetT", s.Target, "logutil/helper.go", "func (Thing) SetT(", "SetT")
	}
	s = off.site(t, "stubemb/s.go", "return o.SetT(nil)", "SetT")
	if !s.EmbedInvalid || s.Target != nil {
		t.Errorf("offline stubemb o.SetT: want embed_invalid, got %+v %+v", s, s.Target)
	}
	s = off.site(t, "stubemb/s.go", "return f(nil)", "f")
	if s.Alias != nil || !s.EmbedInvalid {
		t.Errorf("offline stubemb alias: want no alias, embed_invalid; got %+v %+v", s, s.Alias)
	}
	// A pointer-embedded alias of the stub copy (reviewer probe p4).
	for _, fn := range []string{"FP(o OuterPtr)", "FV(o OuterVal)"} {
		snippet := "func " + fn + " string { return o.SetT(nil) }"
		off0, _, _ := at(t, filepath.Join(dl.root, "stubalias/s.go"), snippet, "SetT")
		d, o := dl.sites["stubalias/s.go"][off0], off.sites["stubalias/s.go"][off0]
		if d == nil || d.Target == nil || d.PromotedDepth != 1 {
			t.Errorf("download %s: %+v", fn, d)
		} else {
			dl.wantTarget(t, "download "+fn, d.Target, "logutil/helper.go", "func (Thing) SetT(", "SetT")
		}
		if o == nil || !o.EmbedInvalid || o.Target != nil {
			t.Errorf("offline %s: want embed_invalid with no target, got %+v", fn, o)
		}
	}
	// G3 for candidates and implements: offline, shadow.Outer and
	// stubemb.Outer seem to implement user2.Setter through Inner.SetT, but at
	// run time a shallower SetT wins.
	s = dl.site(t, "user2/user2.go", "return s.SetT(nil)", "SetT")
	if s.Iface == nil || s.Iface.Suppressed {
		t.Fatalf("download user2 s.SetT: %+v", s.Iface)
	}
	s = off.site(t, "user2/user2.go", "return s.SetT(nil)", "SetT")
	if s.PkgErrors || s.Iface == nil || !s.Iface.Suppressed || len(s.Iface.Candidates) != 0 {
		t.Errorf("offline user2 s.SetT: want suppressed candidates, got pkg_errors=%v %+v", s.PkgErrors, s.Iface)
	}
	implPair := func(r *result, typ, iface string) *implementsRecord {
		for _, e := range r.impls {
			if e.Type.Pkg+"."+e.Type.Name == typ && e.Iface.Pkg+"."+e.Iface.Name == iface {
				return e
			}
		}
		return nil
	}
	if e := implPair(dl, "example.com/trap/shadow.Outer", "example.com/trap/user2.SetterT"); e == nil || !e.PointerReceiver || e.Incomplete {
		t.Errorf("download implements shadow.Outer -> user2.SetterT: %+v", e)
	}
	for _, typ := range []string{"example.com/trap/shadow.Outer", "example.com/trap/stubemb.Outer"} {
		if e := implPair(off, typ, "example.com/trap/user2.SetterT"); e != nil && !e.Incomplete {
			t.Errorf("offline implements %s -> user2.SetterT must be marked incomplete: %+v", typ, e)
		}
	}
	// Every embedded field gets an embed record; offline suite.Suite is invalid.
	var embedded []*Target
	for _, e := range off.embeds {
		if e.Type.Pkg == "example.com/trap/shadow" && e.Type.Name == "Outer" {
			embedded = append(embedded, e.Embedded)
		}
	}
	if len(embedded) != 2 || embedded[0] == nil || embedded[0].Name != "Mid" || embedded[1] != nil {
		t.Errorf("offline embeds of shadow.Outer: %+v", embedded)
	}
	assertNoChangedAnswers(t, dl, off)
}

// TestPartialCacheNeverChangesAnswers fills the module cache with suite only
// (zap stays missing) and compares the offline answers with download mode.
func TestPartialCacheNeverChangesAnswers(t *testing.T) {
	px := buildProxy(t)
	repo := trapRepo(t, px)
	dl := resolveRun(t, trapOpts(repo), goEnv(t, map[string]string{"GOPROXY": px.url}))

	env := goEnv(t, map[string]string{"GOPROXY": px.url})
	cache := getEnv(env, "GOMODCACHE")
	cmd := exec.Command(goBin, "mod", "download", "example.com/thirdparty/suite@v1.0.0")
	cmd.Dir = mkdir(t, "outside")
	cmd.Env = append(env, "GOFLAGS=-modcacherw")
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("go mod download: %v\n%s", err, out)
	}
	if _, err := os.Stat(filepath.Join(cache, "example.com", "thirdparty", "zap@v1.0.0")); err == nil {
		t.Fatal("zap must stay missing")
	}
	off := resolveRun(t, trapOpts(repo), goEnv(t, map[string]string{"GOMODCACHE": cache}))
	s := off.site(t, "shadow/shadow.go", "return o.SetT(nil)", "SetT")
	if s.EmbedInvalid || s.Target == nil || s.Target.Module != "example.com/thirdparty/suite" {
		t.Errorf("partial cache o.SetT (suite present): %+v %+v", s, s.Target)
	}
	assertNoChangedAnswers(t, dl, off)
}

// assertNoChangedAnswers: wherever the offline run names a target, it is the
// download-mode target (Review Focus 4).
func assertNoChangedAnswers(t *testing.T, dl, off *result) {
	t.Helper()
	compared := 0
	for rel, m := range off.sites {
		for o, s := range m {
			ref := dl.sites[rel][o]
			if ref == nil {
				// download knows it is no call; offline may only not know
				if (dl.notcalls[rel][o] || dl.convs[rel][o]) && s.Target == nil && s.Alias == nil {
					continue
				}
				t.Errorf("%s:%d: offline site missing in download mode", rel, o)
				continue
			}
			for _, pair := range [][2]*Target{{s.Target, ref.Target}, {s.Alias, ref.Alias}} {
				if pair[0] == nil {
					continue
				}
				compared++
				if pair[1] == nil || targetIdentity(pair[0]) != targetIdentity(pair[1]) {
					t.Errorf("%s:%d %s: offline target %+v differs from download %+v", rel, o, s.Name, pair[0], pair[1])
				}
			}
		}
	}
	if compared == 0 {
		t.Error("nothing compared")
	}
}

// targetIdentity ignores where the module cache lives: two runs with
// different GOMODCACHE directories name the same declaration.
func targetIdentity(t *Target) string {
	return t.Module + "@" + t.Version + " " + t.Pkg + " " + filepath.Base(t.Path) + " " + t.Name + " " +
		targetKey(&Target{Line: t.Line, Col: t.Col})
}

// TestMissingInModulePackage: an import of a package that does not exist in
// the main module gets a stub with the importing file attached; the stub is
// never an in-repo package and its answers never surface.
func TestMissingInModulePackage(t *testing.T) {
	repo := fixture(t, "ghost")
	opts := &Options{RepoRoot: repo, Dir: repo, Pattern: "./...", GoWork: "off", MainModules: []string{"example.com/ghost"}}
	env := goEnv(t, nil)
	pr := newProgram(opts, loadEnv(opts, env))
	cfg := &packages.Config{Mode: loadMode, Tests: true, Dir: repo, Env: loadEnv(opts, env),
		BuildFlags: buildFlags(opts), Fset: pr.fset, ParseFile: pr.parseFile}
	roots, err := packages.Load(cfg, "./...")
	if err != nil {
		t.Fatal(err)
	}
	pr.index(roots)
	cFile := filepath.Join(repo, "c", "c.go")
	attachedSeen := false
	for _, p := range pr.all {
		if p.PkgPath == "example.com/ghost/nothere" && contains(p.GoFiles, cFile) {
			attachedSeen = true
			if _, ok := pr.accept(p, cFile); ok || !pr.isStub(p) {
				t.Errorf("stub with attached c.go accepted: %+v", p)
			}
		}
	}
	if !attachedSeen {
		t.Fatal("fixture no longer triggers the attachment")
	}
	r := resolveRun(t, opts, goEnv(t, nil))
	fr := r.files["c/c.go"]
	if fr == nil || len(fr.Packages) != 1 || fr.Packages[0] != "example.com/ghost/c" {
		t.Errorf("file record c/c.go: %+v", fr)
	}
	s := r.site(t, "c/c.go", `return nothere.X() + local()`, "local")
	if s.Class != "func" || len(s.Variants) != 0 {
		t.Errorf("local(): %+v", s)
	}
	r.wantTarget(t, "local()", s.Target, "c/c.go", "func local()", "local")
	s = r.site(t, "c/c.go", `return nothere.X() + local()`, "X")
	if s.Class != "unknown" || s.Target != nil || !s.PkgErrors {
		t.Errorf("nothere.X(): %+v", s)
	}
}

// TestToolchainTooNewIsALoadError: a go line newer than the toolchain fails
// the load; the go command's message reaches an error record (R5).
func TestToolchainTooNewIsALoadError(t *testing.T) {
	repo := fixture(t, "newgo")
	r := resolveRun(t, &Options{RepoRoot: repo, Dir: repo, Pattern: "./...", GoWork: "off"}, goEnv(t, nil))
	found := false
	for _, e := range r.errors {
		if strings.Contains(e.Msg, "requires go >= 1.99") {
			found = true
		}
	}
	if !found || len(r.sites) != 0 || r.summary.Packages != 0 {
		t.Errorf("want an error record naming the go line, no sites, packages=0: %+v %+v", r.errors, r.summary)
	}
}
