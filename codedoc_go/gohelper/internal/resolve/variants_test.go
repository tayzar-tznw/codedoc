package resolve

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestVariantsDisagree(t *testing.T) {
	repo := fixture(t, "variants")
	r := resolveRun(t, &Options{RepoRoot: repo, Dir: repo, Pattern: "./...", GoWork: "off",
		MainModules: []string{"example.com/variants"}}, goEnv(t, nil))
	const f = "p/p.go"
	s := r.site(t, f, "return o.Hook() + Plain()", "Hook")
	// production: the promoted inner.Hook; the test variant adds Outer.Hook
	if s.Class != "method" || s.PromotedDepth != 1 {
		t.Errorf("o.Hook: %+v", s)
	}
	r.wantTarget(t, "o.Hook", s.Target, f, "func (inner) Hook()", "Hook")
	if len(s.Variants) != 2 {
		t.Fatalf("variants = %+v, want 2", s.Variants)
	}
	path0, line0, col0 := r.decl(t, f, "func (inner) Hook()", "Hook")
	path1, line1, col1 := r.decl(t, "p/p_test.go", "func (Outer) Hook()", "Hook")
	want := map[string]bool{
		targetKey(&Target{Path: path0, Line: line0, Col: col0}): true,
		targetKey(&Target{Path: path1, Line: line1, Col: col1}): true,
	}
	for _, v := range s.Variants {
		if !want[targetKey(v)] {
			t.Errorf("unexpected variant %+v", v)
		}
	}
	s = r.site(t, f, "return o.Hook() + Plain()", "Plain")
	if len(s.Variants) != 0 {
		t.Errorf("Plain: variants %+v, want none", s.Variants)
	}
	fr := r.files[f]
	if fr == nil || strings.Join(fr.Packages, ",") != "example.com/variants/p,example.com/variants/p [example.com/variants/p.test]" {
		t.Errorf("file record: %+v", fr)
	}
}

func TestSymlinkedFileDedupe(t *testing.T) {
	repo := fixture(t, "sym")
	if err := os.Symlink(filepath.Join("..", "x", "common.go"), filepath.Join(repo, "y", "common.go")); err != nil {
		t.Fatal(err)
	}
	r := resolveRun(t, &Options{RepoRoot: repo, Dir: repo, Pattern: "./...", GoWork: "off",
		MainModules: []string{"example.com/sym"}}, goEnv(t, nil))
	if r.files["y/common.go"] != nil || len(r.sites["y/common.go"]) != 0 {
		t.Error("the symlink path must not appear; paths are real paths")
	}
	fr := r.files["x/common.go"]
	if fr == nil || strings.Join(fr.Packages, ",") != "example.com/sym/x,example.com/sym/y" {
		t.Fatalf("file record: %+v", fr)
	}
	const snippet = "strings.ToUpper(local())"
	s := r.site(t, "x/common.go", snippet, "ToUpper")
	if s.Class != "func" || len(s.Variants) != 0 {
		t.Errorf("ToUpper: %+v", s)
	}
	s = r.site(t, "x/common.go", snippet, "local")
	r.wantTarget(t, "local (production variant)", s.Target, "x/local.go", "func local()", "local")
	if len(s.Variants) != 2 {
		t.Fatalf("local: variants %+v, want x/local.go and y/local.go", s.Variants)
	}
	if !strings.HasSuffix(s.Variants[0].Path, "/x/local.go") || !strings.HasSuffix(s.Variants[1].Path, "/y/local.go") {
		t.Errorf("local variants: %+v %+v", s.Variants[0], s.Variants[1])
	}
	// conv(1) is a conversion in x (production) and a call in y: the site is
	// reported with y's target in variants, never silently dropped.
	off, _, _ := at(t, filepath.Join(r.root, "x/common.go"), "int(conv(1))", "conv")
	if r.convs["x/common.go"][off] {
		t.Error("conv(1) reported as a conversion although a variant calls a function")
	}
	s = r.sites["x/common.go"][off]
	if s == nil || s.Class != "func" || len(s.Variants) != 1 || !strings.HasSuffix(s.Variants[0].Path, "/y/conv.go") {
		t.Errorf("conv(1): %+v", s)
	}
}

// TestTwinVariantsMostConservative: one symlinked twin package resolves the
// call across an embedded alias of a missing type; offline that variant is
// embed_invalid, and the most conservative answer wins (R1).
func TestTwinVariantsMostConservative(t *testing.T) {
	px := buildProxy(t)
	repo := fixture(t, "twin")
	writeFile(t, filepath.Join(repo, "go.sum"), px.goSum("example.com/thirdparty/suite@v1.0.0"))
	if err := os.Symlink(filepath.Join("..", "x", "common.go"), filepath.Join(repo, "y", "common.go")); err != nil {
		t.Fatal(err)
	}
	opts := &Options{RepoRoot: repo, Dir: repo, Pattern: "./...", GoWork: "off", MainModules: []string{"example.com/tw"}}
	dl := resolveRun(t, opts, goEnv(t, map[string]string{"GOPROXY": px.url}))
	s := dl.site(t, "x/common.go", "return o.SetT(nil)", "SetT")
	dl.wantTarget(t, "download o.SetT", s.Target, "x/common.go", "func (Inner) SetT(", "SetT")
	if len(s.Variants) != 2 || s.EmbedInvalid {
		t.Errorf("download o.SetT variants: %+v", s.Variants)
	}
	off := resolveRun(t, opts, goEnv(t, nil))
	s = off.site(t, "x/common.go", "return o.SetT(nil)", "SetT")
	if !s.EmbedInvalid || s.Target != nil || len(s.Variants) == 0 {
		t.Errorf("offline o.SetT: want embed_invalid, no target, variants set; got %+v %+v", s, s.Target)
	}
}
