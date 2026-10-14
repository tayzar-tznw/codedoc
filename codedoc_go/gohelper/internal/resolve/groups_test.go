package resolve

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeFile(t *testing.T, path, body string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestWorkspaceGroupPatternWork(t *testing.T) {
	repo := fixture(t, "ws")
	// The repo's own go.work must never be used: make it unusable.
	writeFile(t, filepath.Join(repo, "go.work"), "go 1.26\n\nuse ./missing\n")
	ctrl := mkdir(t, "ctrl")
	gowork := filepath.Join(ctrl, "go.work")
	writeFile(t, gowork, "go 1.26\n\nuse (\n\t"+filepath.Join(repo, "a")+"\n\t"+filepath.Join(repo, "b")+"\n)\n")
	before := treeHash(t, repo)

	r := resolveRun(t, &Options{RepoRoot: repo, Dir: repo, Pattern: "work", GoWork: gowork,
		MainModules: []string{"example.com/a", "example.com/b"}}, goEnv(t, nil))

	if after := treeHash(t, repo); after != before {
		t.Errorf("the load changed the repo:\nbefore %s\nafter  %s", before, after)
	}
	for _, e := range r.errors {
		t.Errorf("unexpected error: %+v", e)
	}
	if r.summary.Packages != 2 {
		t.Errorf("packages = %d, want 2", r.summary.Packages)
	}
	s := r.site(t, "a/app/app.go", "return lib.Hello()", "Hello")
	if s.Class != "func" {
		t.Fatalf("lib.Hello: %+v", s)
	}
	r.wantTarget(t, "lib.Hello", s.Target, "b/lib/lib.go", "func Hello()", "Hello")
	if s.Target.Module != "example.com/b" || s.Target.Pkg != "example.com/b/lib" {
		t.Errorf("lib.Hello module/pkg: %+v", s.Target)
	}
	if r.files["a/app/app.go"] == nil || r.files["b/lib/lib.go"] == nil {
		t.Errorf("file records: %v", r.files)
	}
	im := r.imports["a/app/app.go"]
	found := false
	for _, rec := range im {
		if rec.ImportPath == "example.com/b/lib" {
			found = true
			if rec.TargetDir != filepath.Join(r.root, "b", "lib") || rec.Module != "example.com/b" || rec.Std || rec.Error != nil {
				t.Errorf("import record: %+v", rec)
			}
		}
	}
	if !found {
		t.Error("no import record for example.com/b/lib")
	}
}

func TestModfileUsesAlternateGoSum(t *testing.T) {
	px := buildProxy(t)
	repo := fixture(t, "single")
	if _, err := os.Stat(filepath.Join(repo, "go.sum")); !os.IsNotExist(err) {
		t.Fatal("the fixture module must have no go.sum")
	}
	ctrl := mkdir(t, "ctrl")
	gomod, err := os.ReadFile(filepath.Join(repo, "go.mod"))
	if err != nil {
		t.Fatal(err)
	}
	writeFile(t, filepath.Join(ctrl, "go.mod"), string(gomod))
	writeFile(t, filepath.Join(ctrl, "go.sum"), px.goSum("example.com/dep@v1.0.0"))
	repoBefore, ctrlBefore := treeHash(t, repo), treeHash(t, ctrl)

	env := goEnv(t, map[string]string{"GOPROXY": px.url})
	opts := &Options{RepoRoot: repo, Dir: repo, Pattern: "./...", GoWork: "off",
		ModFile: filepath.Join(ctrl, "go.mod"), MainModules: []string{"example.com/single"}}
	r := resolveRun(t, opts, env)

	if treeHash(t, repo) != repoBefore {
		t.Error("the load changed the module directory")
	}
	if treeHash(t, ctrl) != ctrlBefore {
		t.Error("the load changed the control directory")
	}
	for _, e := range r.errors {
		t.Errorf("unexpected error: %+v", e)
	}
	s := r.site(t, "main.go", "println(dep.Do())", "Do")
	if s.Class != "func" || s.Target == nil || !strings.HasSuffix(s.Target.Path, "/example.com/dep@v1.0.0/dep.go") ||
		s.Target.Module != "example.com/dep" || s.Target.Version != "v1.0.0" {
		t.Fatalf("dep.Do with -modfile: %+v %+v", s, s.Target)
	}

	// Without -modfile the module's own (absent) go.sum applies and the
	// dependency fails to load: the alternate go.sum is what made it work.
	opts2 := *opts
	opts2.ModFile = ""
	r2 := resolveRun(t, &opts2, goEnv(t, map[string]string{"GOPROXY": px.url}))
	s2 := r2.site(t, "main.go", "println(dep.Do())", "Do")
	if s2.Class != "unknown" || s2.Target != nil {
		t.Errorf("dep.Do without -modfile: %+v", s2)
	}
	missing := false
	for _, e := range r2.errors {
		if strings.Contains(e.Msg, "missing go.sum entry") {
			missing = true
		}
	}
	if !missing {
		t.Errorf("want a missing go.sum entry error, got %+v", r2.errors)
	}
	if treeHash(t, repo) != repoBefore {
		t.Error("the second load changed the module directory")
	}
}

func TestLoadFailureIsARecord(t *testing.T) {
	repo := fixture(t, "ws")
	ctrl := mkdir(t, "ctrl")
	gowork := filepath.Join(ctrl, "go.work")
	writeFile(t, gowork, "go 1.26\n\nuse "+filepath.Join(repo, "nowhere")+"\n")
	r := resolveRun(t, &Options{RepoRoot: repo, Dir: repo, Pattern: "work", GoWork: gowork}, goEnv(t, nil))
	if len(r.errors) == 0 || r.summary.Errors == 0 || len(r.sites) != 0 {
		t.Errorf("want an error record and no sites: %+v %+v", r.errors, r.summary)
	}
}

// TestCandidateUniverseIncludesReplacedRepoModules: in a single-module group,
// a type of a replaced in-repo module is an in-repo candidate (ruling
// 2026-10-13), not an open-world implementer; its files carry no sites.
func TestCandidateUniverseIncludesReplacedRepoModules(t *testing.T) {
	repo := fixture(t, "repl")
	r := resolveRun(t, &Options{RepoRoot: repo, Dir: filepath.Join(repo, "a"), Pattern: "./...", GoWork: "off",
		MainModules: []string{"example.com/a"}}, goEnv(t, nil))
	for _, e := range r.errors {
		t.Errorf("unexpected error: %+v", e)
	}
	s := r.site(t, "a/main.go", "return s.Area()", "Area")
	if s.Iface == nil || s.Iface.OpenWorld || s.Iface.Suppressed || len(s.Iface.Candidates) != 1 {
		t.Fatalf("s.Area: %+v", s.Iface)
	}
	c := s.Iface.Candidates[0]
	path, line, col := r.decl(t, "b/lib/lib.go", "func (Square) Area()", "Area")
	if c.Path != path || c.Line != line || c.Col != col || c.Type != "Square" || c.Pkg != "example.com/b/lib" {
		t.Errorf("candidate: %+v", c)
	}
	found := false
	for _, e := range r.impls {
		if e.Type.Pkg == "example.com/b/lib" && e.Type.Name == "Square" && e.Iface.Name == "Shape" {
			found = true
		}
	}
	if !found {
		t.Error("missing implements lib.Square -> main.Shape")
	}
	if len(r.sites["b/lib/lib.go"]) != 0 || r.files["b/lib/lib.go"] != nil {
		t.Error("the replaced module's files must carry no sites in this group")
	}
}

// TestPeerDirImplementsAndEmbeds: implements records cover interfaces under
// --peer-dirs; embeds cover embedded peer types; peer types are never in-repo
// candidates and make the interface open-world.
func TestPeerDirImplementsAndEmbeds(t *testing.T) {
	base := fixture(t, "peer")
	repo, peer := filepath.Join(base, "app"), filepath.Join(base, "shared")
	r := resolveRun(t, &Options{RepoRoot: repo, Dir: repo, Pattern: "./...", GoWork: "off",
		MainModules: []string{"example.com/app"}, PeerDirs: []string{peer}}, goEnv(t, nil))
	for _, e := range r.errors {
		t.Errorf("unexpected error: %+v", e)
	}
	impl := false
	for _, e := range r.impls {
		if e.Type.Name == "Impl" && e.Iface.Name == "Handler" && strings.HasPrefix(e.Iface.Path, peer+"/") && !e.PointerReceiver {
			impl = true
		}
		if e.Type.Name == "PeerImpl" {
			t.Errorf("a peer type is not an in-repo concrete type: %+v", e)
		}
	}
	if !impl {
		t.Errorf("missing implements main.Impl -> api.Handler (peer): %+v", r.impls)
	}
	emb := false
	for _, e := range r.embeds {
		if e.Type.Name == "Local" && e.Embedded != nil && e.Embedded.Qualname == "Base" && strings.HasPrefix(e.Embedded.Path, peer+"/") {
			emb = true
		}
	}
	if !emb {
		t.Errorf("missing embed main.Local -> api.Base (peer): %+v", r.embeds)
	}
	s := r.site(t, "main.go", "return h.Serve()", "Serve")
	if s.Target == nil || s.Target.Qualname != "Handler.Serve" || !strings.HasPrefix(s.Target.Path, peer+"/") {
		t.Errorf("h.Serve target: %+v", s.Target)
	}
	if s.Iface == nil || !s.Iface.OpenWorld || len(s.Iface.Candidates) != 1 || s.Iface.Candidates[0].Type != "Impl" {
		t.Errorf("h.Serve iface: %+v", s.Iface)
	}
	s = r.site(t, "main.go", "Local{}.Hello()", "Hello")
	if s.Target == nil || s.Target.Qualname != "Base.Hello" || s.PromotedDepth != 1 {
		t.Errorf("Local{}.Hello: %+v %+v", s, s.Target)
	}
}
