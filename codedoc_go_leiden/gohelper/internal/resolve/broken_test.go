package resolve

import (
	"sort"
	"testing"

	"golang.org/x/tools/go/packages"
)

func brkOpts(repo string) *Options {
	return &Options{RepoRoot: repo, Dir: repo, Pattern: "./...", GoWork: "off", MainModules: []string{"example.com/brk"}}
}

// TestBrokenPackages: which packages may have lost declarations (R43): a
// syntax error, a file the type checker ignored for its package clause, an
// embedded field dropped as a duplicate. Type errors that drop nothing do not
// count: an import that cannot be resolved, a receiver naming no type.
func TestBrokenPackages(t *testing.T) {
	repo := fixture(t, "brk")
	opts := brkOpts(repo)
	env := loadEnv(opts, goEnv(t, nil))
	pr := newProgram(opts, env)
	cfg := &packages.Config{Mode: loadMode, Tests: true, Dir: repo, Env: env,
		BuildFlags: buildFlags(opts), Fset: pr.fset, ParseFile: pr.parseFile}
	roots, err := packages.Load(cfg, "./...")
	if err != nil {
		t.Fatal(err)
	}
	pr.index(roots)
	want := map[string]bool{
		"example.com/brk/p": true, "example.com/brk/pcx": true, "example.com/brk/d": true,
		"example.com/brk/bi": true, "example.com/brk/t": false, "example.com/brk/imp": false,
		"example.com/brk/inv": false, "example.com/brk/r": false, "example.com/brk/o": false,
		"example.com/brk/pf": false, "example.com/brk/ifc": false, "example.com/brk/q": false,
	}
	for path, broken := range want {
		p := pr.byPath[path]
		if p == nil {
			t.Fatalf("package %s not loaded", path)
		}
		if got := pr.brokenPkg(p); got != broken {
			t.Errorf("brokenPkg(%s) = %v, want %v (errors %v)", path, got, broken, p.Errors)
		}
	}
	for _, path := range []string{"example.com/brk/t", "example.com/brk/imp", "example.com/brk/inv"} {
		if len(pr.byPath[path].Errors) == 0 {
			t.Errorf("the fixture no longer gives %s a type error", path)
		}
	}
}

// TestR43PromotedSelectionsThroughBrokenPackages: a promoted selection is
// refused (embed_invalid) when a package declaring a type on its embedding
// path may have lost a declaration, the receiver's own package included
// (B2 review probe brk/); candidates and implements pairs through such types
// are refused too.
func TestR43PromotedSelectionsThroughBrokenPackages(t *testing.T) {
	repo := fixture(t, "brk")
	r := resolveRun(t, brkOpts(repo), goEnv(t, nil))
	const q = "q/q.go"
	for _, c := range []struct {
		snippet, why string
		depth        int
	}{
		{"return p.S{}.M()", "a syntax error in the receiver's package swallowed S.M", 1},
		{"return p.S.M(p.S{})", "method expression through the same type", 1},
		{"return pcx.S{}.M()", "S.M is in a file with another package clause", 1},
		{"return d.D{}.M()", "o.Wrapper.M (depth 1) was dropped with its duplicate field", 2},
	} {
		s := r.site(t, q, c.snippet, "M")
		if s.Class != "method" || s.PromotedDepth != c.depth || !s.EmbedInvalid || s.Target != nil {
			t.Errorf("%s (%s): want a refused promoted selection, got %+v %+v", c.snippet, c.why, s, s.Target)
		}
	}
	s := r.site(t, q, "return f()", "f")
	if s.Class != "value" || s.Alias != nil || !s.EmbedInvalid {
		t.Errorf("alias of p.S{}.M: want no proof, embed_invalid; got %+v %+v", s, s.Alias)
	}
	// Type errors that drop no declaration keep their answers: a receiver
	// naming no type (t), an unresolved import (imp); so do clean packages.
	for _, snippet := range []string{"return t.S{}.M()", "return imp.S{}.M()", "return r.Wrapper{}.M()"} {
		s := r.site(t, q, snippet, "M")
		if s.EmbedInvalid || s.PromotedDepth != 1 || s.PkgErrors {
			t.Errorf("%s: want a resolved promoted selection, got %+v", snippet, s)
		}
		r.wantTarget(t, snippet, s.Target, "r/r.go", "func (Base) M()", "M")
	}
	// The probe's shape with S.M in place resolves to S.M itself.
	s = r.site(t, q, "return pf.S{}.M()", "M")
	if s.EmbedInvalid || s.PromotedDepth != 0 {
		t.Errorf("pf.S{}.M(): %+v", s)
	}
	r.wantTarget(t, "pf.S{}.M()", s.Target, "pf/pf.go", "func (S) M()", "M")
	// Candidates: p.S, pcx.S and d.D may implement ifc.I through such paths, so
	// the candidate search is refused (G4); so is the search for an interface
	// of such a package, or one embedding it. The static targets stay.
	for _, c := range []struct{ snippet, rel, decl string }{
		{"return i.M()", "ifc/ifc.go", "type I interface{ M() string }"},
		{"return j.M()", "bi/bi.go", "type J interface{ M() string }"},
		{"return k.M()", "bi/bi.go", "type J interface{ M() string }"},
	} {
		s := r.site(t, q, c.snippet, "M")
		if s.Class != "iface" || s.Iface == nil || !s.Iface.Suppressed || len(s.Iface.Candidates) != 0 {
			t.Errorf("%s: want suppressed candidates, got %+v %+v", c.snippet, s, s.Iface)
		}
		r.wantTarget(t, c.snippet, s.Target, c.rel, c.decl, "M")
	}
	// Implements pairs through such types, or to such interfaces, are marked.
	impls := map[string]*implementsRecord{}
	for _, e := range r.impls {
		impls[e.Type.Pkg+"."+e.Type.Name+" "+e.Iface.Pkg+"."+e.Iface.Name] = e
	}
	want := map[string]bool{}
	for _, typ := range []string{"r.Base", "r.Wrapper", "o.Wrapper", "p.S", "t.S", "d.D", "imp.S", "pcx.S", "pf.S"} {
		want["example.com/brk/"+typ+" example.com/brk/ifc.I"] = map[string]bool{"p.S": true, "d.D": true, "pcx.S": true}[typ]
		want["example.com/brk/"+typ+" example.com/brk/bi.J"] = true
		want["example.com/brk/"+typ+" example.com/brk/ifc.K"] = true
	}
	for _, typ := range []string{"Good", "Mixed", "PtrGood"} {
		want["example.com/brk/inv."+typ+" example.com/brk/inv.Ready"] = false
	}
	for pair, incomplete := range want {
		e, ok := impls[pair]
		if !ok {
			t.Errorf("implements %s: missing", pair)
		} else if e.Incomplete != incomplete {
			t.Errorf("implements %s: incomplete = %v, want %v", pair, e.Incomplete, incomplete)
		}
	}
	var extra []string
	for pair := range impls {
		if _, ok := want[pair]; !ok {
			extra = append(extra, pair)
		}
	}
	sort.Strings(extra)
	if len(extra) > 0 {
		t.Errorf("unexpected implements pairs: %v", extra)
	}
}

// TestR52TypesWithAnInvalidEmbeddedField: go/types reports a type with an
// embedded field of invalid type as implementing every interface. maxTS
// (Prometheus storage/remote maxTimestamp, whose prometheus.Gauge is missing
// offline) and maxV (the same with a value receiver, which the helper used
// to pair) have Get() float64, so they implement Ready neither as a pair nor
// as a candidate: definitely not, so the candidate set stays closed. Mixed
// and PtrGood have Ready's Get themselves and do.
func TestR52TypesWithAnInvalidEmbeddedField(t *testing.T) {
	repo := fixture(t, "brk")
	r := resolveRun(t, brkOpts(repo), goEnv(t, nil))
	for _, e := range r.impls {
		if e.Type.Name == "maxTS" || e.Type.Name == "maxV" {
			t.Errorf("%s must not implement %s.%s: %+v", e.Type.Name, e.Iface.Pkg, e.Iface.Name, e)
		}
		if e.Type.Name == "PtrGood" && !e.PointerReceiver {
			t.Errorf("PtrGood implements %s through *PtrGood: %+v", e.Iface.Name, e)
		}
	}
	s := r.site(t, "q/q.go", "return x.Get()", "Get")
	if s.Class != "iface" || s.Iface == nil || s.Iface.Suppressed || s.Iface.OpenWorld {
		t.Fatalf("x.Get(): want a closed candidate set: %+v %+v", s, s.Iface)
	}
	var got []string
	for _, c := range s.Iface.Candidates {
		got = append(got, c.Qualname)
	}
	sort.Strings(got)
	if want := []string{"Good.Get", "Mixed.Get", "PtrGood.Get"}; !equalStrings(got, want) {
		t.Errorf("x.Get() candidates = %v, want %v", got, want)
	}
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
