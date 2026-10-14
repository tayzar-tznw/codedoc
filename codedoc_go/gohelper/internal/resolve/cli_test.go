package resolve

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestParseArgs(t *testing.T) {
	ok := []string{"--repo-root", "/r", "--dir", "/r", "--pattern", "./...", "--gowork", "off"}
	o, err := ParseArgs(append(ok, "--tags", "a, b,", "--cgo", "1", "--main-modules", "m1,m2", "--peer-dirs", "/p1,/p2", "--modfile=/c/go.mod"))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(o.Tags, ",") != "a,b" || !o.CGO || strings.Join(o.MainModules, ",") != "m1,m2" ||
		strings.Join(o.PeerDirs, ",") != "/p1,/p2" || o.ModFile != "/c/go.mod" || o.BuildID != "linux-amd64" || o.GOOS != "linux" {
		t.Errorf("parsed options: %+v", o)
	}
	if got := buildFlags(o); strings.Join(got, " ") != "-buildvcs=false -modfile=/c/go.mod -tags=a,b" {
		t.Errorf("build flags: %v", got)
	}
	env := loadEnv(o, []string{"GOOS=windows", "KEEP=1", "CGO_ENABLED=0"})
	if getEnv(env, "GOOS") != "linux" || getEnv(env, "CGO_ENABLED") != "1" || getEnv(env, "GOWORK") != "off" || getEnv(env, "KEEP") != "1" {
		t.Errorf("load env: %v", env)
	}
	bad := [][]string{
		{},
		{"--repo-root", "rel", "--dir", "/r", "--pattern", "./..."},
		{"--repo-root", "/r", "--pattern", "./..."},
		{"--repo-root", "/r", "--dir", "/r", "--pattern", "all"},
		{"--repo-root", "/r", "--dir", "/r", "--pattern", "work", "--gowork", "off"},
		{"--repo-root", "/r", "--dir", "/r", "--pattern", "./...", "--gowork", "/w/go.work", "--modfile", "/c/go.mod"},
		{"--repo-root", "/r", "--dir", "/r", "--pattern", "./...", "--gowork", "rel/go.work"},
		{"--repo-root", "/r", "--dir", "/r", "--pattern", "./...", "--cgo", "2"},
		{"--repo-root", "/r", "--dir", "/r", "--pattern", "./...", "--peer-dirs", "rel"},
		{"--repo-root", "/r", "--dir", "/r", "--pattern", "./...", "--unknown"},
		{"--repo-root", "/r", "--dir", "/r", "--pattern", "./...", "extra"},
	}
	for _, args := range bad {
		_, err := ParseArgs(args)
		var u *UsageError
		if !errors.As(err, &u) {
			t.Errorf("ParseArgs(%q) = %v, want a usage error", args, err)
		}
	}
}

func TestRunWritesOutFileAtomically(t *testing.T) {
	repo := fixture(t, "variants")
	out := filepath.Join(mkdir(t, "out"), "res.jsonl")
	args := []string{"--repo-root", repo, "--dir", repo, "--pattern", "./...", "--gowork", "off", "--out", out}
	// Run uses the process environment; give it the test environment.
	for _, kv := range goEnv(t, nil) {
		k, v, _ := strings.Cut(kv, "=")
		t.Setenv(k, v)
	}
	var stdout bytes.Buffer
	if err := Run(args, &stdout); err != nil {
		t.Fatal(err)
	}
	if stdout.Len() != 0 {
		t.Errorf("stdout must stay empty with --out, got %q", stdout.String())
	}
	b, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	r := parseOutput(t, repo, b)
	if r.summary.Packages != 1 || r.summary.Sites == 0 {
		t.Errorf("summary: %+v", r.summary)
	}
	if _, err := os.Stat(out + ".tmp"); !os.IsNotExist(err) {
		t.Error("temporary output left behind")
	}
}
