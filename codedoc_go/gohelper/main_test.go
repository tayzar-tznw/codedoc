package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func TestVersion(t *testing.T) {
	var out, errb bytes.Buffer
	if code := run([]string{"version"}, &out, &errb); code != 0 {
		t.Fatalf("exit %d: %s", code, errb.String())
	}
	var v struct {
		Kind     string `json:"kind"`
		Protocol int    `json:"protocol"`
		Go       string `json:"go"`
		XTools   string `json:"x_tools"`
		Revision string `json:"revision"`
	}
	dec := json.NewDecoder(&out)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&v); err != nil {
		t.Fatal(err)
	}
	if v.Kind != "version" || v.Protocol != 1 || v.Go != runtime.Version() || v.XTools != "v0.52.0" || len(v.Revision) != 64 {
		t.Errorf("version record: %+v", v)
	}
	rev, err := revision()
	if err != nil || rev != v.Revision {
		t.Errorf("revision is not deterministic: %q vs %q (%v)", rev, v.Revision, err)
	}
}

func TestExitCodes(t *testing.T) {
	cases := []struct {
		args []string
		code int
	}{
		{nil, 2},
		{[]string{"bogus"}, 2},
		{[]string{"version", "x"}, 2},
		{[]string{"resolve", "--pattern", "./..."}, 2},
		{[]string{"extract", "--root", "/nonexistent-dir-for-test"}, 2},
		{[]string{"extract", "--bogus-flag"}, 2},
	}
	for _, c := range cases {
		var out, errb bytes.Buffer
		if code := run(c.args, &out, &errb); code != c.code {
			t.Errorf("run(%q) = %d, want %d (%s)", c.args, code, c.code, errb.String())
		}
	}
}

func TestEmbeddedSources(t *testing.T) {
	for _, p := range []string{"main.go", "go.mod", "go.sum", "vendor/modules.txt", "internal/resolve/resolve.go", "internal/extract/sites.go"} {
		if _, err := sources.ReadFile(p); err != nil {
			t.Errorf("source %s not embedded: %v", p, err)
		}
	}
	b, _ := sources.ReadFile("vendor/modules.txt")
	if !strings.Contains(string(b), "# golang.org/x/tools v0.52.0") {
		t.Error("vendor/modules.txt does not pin x/tools v0.52.0")
	}
}

// failWriter fails every write, as a closed stdout would.
type failWriter struct{}

func (failWriter) Write([]byte) (int, error) { return 0, errors.New("broken pipe") }

func TestOutputFailureExitsOne(t *testing.T) {
	var errb bytes.Buffer
	dir := t.TempDir()
	list := filepath.Join(dir, "files.txt")
	if err := os.WriteFile(list, []byte("a.go\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "a.go"), []byte("package a\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if code := run([]string{"extract", "--root", dir, "--files-from", list}, failWriter{}, &errb); code != 1 {
		t.Errorf("extract with a failing stdout: exit %d, want 1 (%s)", code, errb.String())
	}
	if code := run([]string{"version"}, failWriter{}, &errb); code != 1 {
		t.Errorf("version with a failing stdout: exit %d, want 1", code)
	}
}
