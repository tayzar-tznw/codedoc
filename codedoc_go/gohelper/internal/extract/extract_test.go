package extract

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
)

// line is one decoded output line.
type line struct {
	Kind    string      `json:"kind"`
	Relpath string      `json:"relpath"`
	Record  *FileRecord `json:"record"`
	Error   string      `json:"error"`
	Files   int         `json:"files"`
	Errors  int         `json:"errors"`
	Seconds *float64    `json:"seconds"`
}

func fixtureFiles(t *testing.T, root string) []string {
	t.Helper()
	var rels []string
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err == nil && !d.IsDir() && strings.HasSuffix(p, ".go") {
			rel, _ := filepath.Rel(root, p)
			rels = append(rels, filepath.ToSlash(rel))
		}
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	sort.Strings(rels)
	return rels
}

func writeList(t *testing.T, rels []string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "files.txt")
	if err := os.WriteFile(p, []byte(strings.Join(rels, "\n")+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

func runLines(t *testing.T, args ...string) ([]line, string) {
	t.Helper()
	var out bytes.Buffer
	if err := Run(args, &out); err != nil {
		t.Fatalf("Run: %v", err)
	}
	return decode(t, out.Bytes()), out.String()
}

func decode(t *testing.T, data []byte) []line {
	t.Helper()
	var lines []line
	sc := bufio.NewScanner(bytes.NewReader(data))
	sc.Buffer(make([]byte, 1<<20), 1<<26)
	for sc.Scan() {
		var l line
		if err := json.Unmarshal(sc.Bytes(), &l); err != nil {
			t.Fatalf("bad JSON line %q: %v", sc.Text(), err)
		}
		lines = append(lines, l)
	}
	return lines
}

func byRelpath(lines []line) map[string]line {
	m := map[string]line{}
	for _, l := range lines {
		if l.Kind == "file" {
			m[l.Relpath] = l
		}
	}
	return m
}

// ownerOf is the receiver's owner_relpath, with " unresolved" appended when
// receiver_unresolved is set.
func ownerOf(t *testing.T, files map[string]line, relpath, qualname string) string {
	t.Helper()
	rec := files[relpath].Record
	if rec == nil {
		t.Fatalf("%s: no record (%s)", relpath, files[relpath].Error)
	}
	for _, fn := range rec.Funcs {
		if fn.Qualname == qualname {
			if fn.Receiver.OwnerRelpath == nil {
				t.Fatalf("%s %s: owner_relpath is null", relpath, qualname)
			}
			o := *fn.Receiver.OwnerRelpath
			if fn.Receiver.Unresolved {
				o += " unresolved"
			}
			return o
		}
	}
	t.Fatalf("%s: no func %s", relpath, qualname)
	return ""
}

func fixtureRoot(t *testing.T) string {
	t.Helper()
	root, err := filepath.Abs("testdata/repo")
	if err != nil {
		t.Fatal(err)
	}
	return root
}

func TestRunWritesOneLinePerFileInInputOrderThenASummary(t *testing.T) {
	root := fixtureRoot(t)
	rels := fixtureFiles(t, root)
	// Input order is kept, not sorted.
	rels[0], rels[len(rels)-1] = rels[len(rels)-1], rels[0]
	lines, raw := runLines(t, "--root", root, "--files-from", writeList(t, rels))
	if len(lines) != len(rels)+1 {
		t.Fatalf("got %d lines for %d files", len(lines), len(rels))
	}
	for i, rel := range rels {
		if lines[i].Kind != "file" || lines[i].Relpath != rel {
			t.Errorf("line %d: %s %s, want file %s", i, lines[i].Kind, lines[i].Relpath, rel)
		}
	}
	sum := lines[len(lines)-1]
	if sum.Kind != "summary" || sum.Files != len(rels) || sum.Errors != 1 || sum.Seconds == nil || *sum.Seconds < 0 {
		t.Errorf("summary %+v", sum)
	}
	if strings.Contains(raw, `\u003c`) || !strings.Contains(raw, `"qualname":"<package>"`) {
		t.Error("qualnames must not be HTML-escaped (<package>, <init>)")
	}
	files := byRelpath(lines)
	if l := files["broken/clause.go"]; l.Record != nil || !strings.Contains(l.Error, "broken/clause.go:1:1") {
		t.Errorf("broken/clause.go: %+v", l)
	}
	if !strings.Contains(raw, `{"kind":"file","relpath":"broken/clause.go","error":`) {
		t.Error("error records carry no record field")
	}
	late := files["broken/late.go"].Record
	if late == nil || len(late.SyntaxErrors) == 0 || late.SyntaxErrors[0] != 7 || len(late.Funcs) != 2 {
		t.Errorf("broken/late.go: %+v", late)
	}
}

func TestRunModulesPackagesAndOrigins(t *testing.T) {
	root := fixtureRoot(t)
	lines, _ := runLines(t, "--root", root, "--files-from", writeList(t, fixtureFiles(t, root)))
	files := byRelpath(lines)
	for rel, want := range map[string][3]string{
		"fix.go":                  {"example.com/fix", "example.com/fix", "package"},
		"billing/fee.go":          {"example.com/fix", "example.com/fix/billing", "package"},
		"billing/fee_test.go":     {"example.com/fix", "example.com/fix/billing", "test"},
		"billing/fee_ext_test.go": {"example.com/fix", "example.com/fix/billing_test", "xtest"},
		"cmd/tool/main.go":        {"example.com/fix", "example.com/fix/cmd/tool", "command"},
		"sub/inner/x.go":          {"example.com/fix/sub", "example.com/fix/sub/inner", "package"},
		"other/fee.go":            {"example.com/fix", "example.com/fix/other", "package"},
		"billing/twin_windows.go": {"example.com/fix", "example.com/fix/billing", "package"},
	} {
		r := files[rel].Record
		if r == nil || r.Module != want[0] || r.Package != want[1] || r.Origin != want[2] {
			t.Errorf("%s: %+v, want module %s package %s origin %s", rel, r, want[0], want[1], want[2])
		}
	}
	if r := files["billing/gen.go"].Record; !r.Generated {
		t.Error("billing/gen.go is generated")
	}
	for rel, want := range map[string]bool{
		"billing/fee.go": true, "billing/twin_linux.go": true, "billing/twin_windows.go": false,
		"billing/cgo.go": false, "billing/ghost_darwin.go": false,
	} {
		if got := files[rel].Record.BuildIncluded; got != want {
			t.Errorf("%s: build_included %v, want %v", rel, got, want)
		}
	}
}

func TestRunReceiverOwners(t *testing.T) {
	root := fixtureRoot(t)
	list := writeList(t, fixtureFiles(t, root))
	for _, c := range []struct {
		goos string
		want map[[2]string]string
	}{
		{"linux", map[[2]string]string{
			{"billing/fee.go", "FeeCalculator.Calculate"}:       "billing/fee.go",
			{"billing/fee_methods.go", "FeeCalculator.Total"}:   "billing/fee.go",
			{"billing/fee_test.go", "FeeCalculator.testHelper"}: "billing/fee.go",
			{"billing/fee_ext_test.go", "FeeCalculator.Ext"}:    "billing/fee_ext_test.go",
			{"billing/fee_ext_test.go", "Twin.X"}:               "billing/fee_ext_test.go unresolved",
			{"billing/twin_linux.go", "Twin.Own"}:               "billing/twin_linux.go",
			{"billing/twin_windows.go", "Twin.Own"}:             "billing/twin_windows.go",
			{"billing/twin_use.go", "Twin.Use"}:                 "billing/twin_linux.go",
			{"billing/ghost_use.go", "Ghost.Boo"}:               "billing/ghost_darwin.go",
			{"billing/nowhere.go", "Missing.X"}:                 "billing/nowhere.go unresolved",
			{"other/fee.go", "FeeCalculator.Other"}:             "other/fee.go",
		}},
		{"windows", map[[2]string]string{
			{"billing/twin_use.go", "Twin.Use"}:               "billing/twin_windows.go",
			{"billing/ghost_use.go", "Ghost.Boo"}:             "billing/ghost_windows.go",
			{"billing/twin_linux.go", "Twin.Own"}:             "billing/twin_linux.go",
			{"billing/fee_methods.go", "FeeCalculator.Total"}: "billing/fee.go",
		}},
		{"darwin", map[[2]string]string{
			{"billing/twin_use.go", "Twin.Use"}:   "billing/twin_linux.go",
			{"billing/ghost_use.go", "Ghost.Boo"}: "billing/ghost_darwin.go",
		}},
	} {
		lines, _ := runLines(t, "--root", root, "--files-from", list, "--goos", c.goos)
		files := byRelpath(lines)
		for k, want := range c.want {
			if got := ownerOf(t, files, k[0], k[1]); got != want {
				t.Errorf("%s %s %s: owner %v, want %v", c.goos, k[0], k[1], got, want)
			}
		}
	}
}

func TestRunBuildFlags(t *testing.T) {
	root := fixtureRoot(t)
	list := writeList(t, []string{"billing/cgo.go"})
	for _, c := range []struct {
		args []string
		want bool
	}{
		{nil, false},
		{[]string{"--cgo", "1"}, true},
		{[]string{"--cgo", "true"}, true},
		{[]string{"--cgo", "0"}, false},
	} {
		lines, _ := runLines(t, append([]string{"--root", root, "--files-from", list}, c.args...)...)
		if got := lines[0].Record.BuildIncluded; got != c.want {
			t.Errorf("%v: build_included %v, want %v", c.args, got, c.want)
		}
	}
	tagged := filepath.Join(t.TempDir(), "tagged")
	if err := os.MkdirAll(tagged, 0o755); err != nil {
		t.Fatal(err)
	}
	src := "//go:build integration && extra\n\npackage p\n"
	if err := os.WriteFile(filepath.Join(tagged, "a.go"), []byte(src), 0o644); err != nil {
		t.Fatal(err)
	}
	list = writeList(t, []string{"a.go"})
	for _, c := range []struct {
		tags string
		want bool
	}{{"", false}, {"integration", false}, {"integration,extra", true}, {"integration extra", true}} {
		lines, _ := runLines(t, "--root", tagged, "--files-from", list, "--tags", c.tags)
		if got := lines[0].Record.BuildIncluded; got != c.want {
			t.Errorf("tags %q: build_included %v, want %v", c.tags, got, c.want)
		}
	}
}

func TestRunFilesWithoutAModule(t *testing.T) {
	root, _ := filepath.Abs("testdata/nomod")
	lines, _ := runLines(t, "--root", root, "--files-from", writeList(t, []string{"loose/a.go"}))
	if r := lines[0].Record; r == nil || r.Module != "" || r.Package != "" || r.PackageName != "loose" {
		t.Errorf("record %+v", r)
	}
}

func TestRunBadPathsAreErrorRecordsAndDuplicatesAreDropped(t *testing.T) {
	root := fixtureRoot(t)
	rels := []string{"fix.go", "missing.go", "../outside.go", "/abs.go", "./fix.go", "billing", "fix.go", "", "fix.go\r"}
	lines, _ := runLines(t, "--root", root, "--files-from", writeList(t, rels))
	var got []string
	for _, l := range lines[:len(lines)-1] {
		state := "ok"
		if l.Record == nil {
			state = "error"
			if l.Error == "" {
				t.Errorf("%s: an error record needs a message", l.Relpath)
			}
		}
		got = append(got, l.Relpath+":"+state)
	}
	want := []string{"fix.go:ok", "missing.go:error", "../outside.go:error", "/abs.go:error", "./fix.go:error", "billing:error"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v", got, want)
	}
	if sum := lines[len(lines)-1]; sum.Files != 6 || sum.Errors != 5 {
		t.Errorf("summary %+v", sum)
	}
}

func TestRunEmptyList(t *testing.T) {
	lines, _ := runLines(t, "--root", fixtureRoot(t), "--files-from", writeList(t, nil))
	if len(lines) != 1 || lines[0].Kind != "summary" || lines[0].Files != 0 || lines[0].Errors != 0 {
		t.Errorf("lines %+v", lines)
	}
}

func TestRunOutFile(t *testing.T) {
	root := fixtureRoot(t)
	out := filepath.Join(t.TempDir(), "entities.jsonl")
	var stdout bytes.Buffer
	if err := Run([]string{"--root", root, "--files-from", writeList(t, []string{"fix.go"}), "--out", out}, &stdout); err != nil {
		t.Fatal(err)
	}
	if stdout.Len() != 0 {
		t.Errorf("stdout must stay empty with --out, got %q", stdout.String())
	}
	data, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	if lines := decode(t, data); len(lines) != 2 || lines[0].Record == nil || lines[1].Kind != "summary" {
		t.Errorf("out file lines %+v", lines)
	}
}

func TestRunInvalidInvocations(t *testing.T) {
	root := fixtureRoot(t)
	list := writeList(t, []string{"fix.go"})
	for name, args := range map[string][]string{
		"no root":             {"--files-from", list},
		"no files-from":       {"--root", root},
		"missing list":        {"--root", root, "--files-from", filepath.Join(t.TempDir(), "nope.txt")},
		"bad cgo":             {"--root", root, "--files-from", list, "--cgo", "maybe"},
		"unknown flag":        {"--root", root, "--files-from", list, "--bogus"},
		"positional args":     {"--root", root, "--files-from", list, "extra"},
		"root is not a dir":   {"--root", filepath.Join(root, "fix.go"), "--files-from", list},
		"root does not exist": {"--root", filepath.Join(root, "nope"), "--files-from", list},
		"out not creatable":   {"--root", root, "--files-from", list, "--out", filepath.Join(root, "nope", "x.jsonl")},
	} {
		var out bytes.Buffer
		var usage *UsageError
		if err := Run(args, &out); !errors.As(err, &usage) {
			t.Errorf("%s: want a *UsageError, got %v", name, err)
		}
		if out.Len() != 0 {
			t.Errorf("%s: no output expected on an invalid invocation, got %q", name, out.String())
		}
	}
}

type failingWriter struct{}

func (failingWriter) Write([]byte) (int, error) { return 0, errors.New("disk full") }

func TestRunWriteFailureIsNotAUsageError(t *testing.T) {
	args := []string{"--root", fixtureRoot(t), "--files-from", writeList(t, []string{"fix.go"})}
	var usage *UsageError
	if err := Run(args, failingWriter{}); err == nil || errors.As(err, &usage) {
		t.Errorf("want a plain write error, got %v", err)
	}
}

func TestModulePath(t *testing.T) {
	for src, want := range map[string]string{
		"module example.com/m\n\ngo 1.26\n":              "example.com/m",
		"// comment\nmodule example.com/m // trailing\n": "example.com/m",
		"module \"example.com/quoted\"\n":                "example.com/quoted",
		"module `example.com/raw`\n":                     "example.com/raw",
		"module (\n\texample.com/block\n)\n":             "example.com/block",
		"module(\n\texample.com/block\n)\n":              "example.com/block",
		"\xef\xbb\xbfmodule example.com/bom\n":           "example.com/bom",
		"go 1.26\nmodule\texample.com/tab\r\n":           "example.com/tab",
		"go 1.26\n":                                      "",
		"modules example.com/no\n":                       "",
	} {
		if got := modulePath([]byte(src)); got != want {
			t.Errorf("%q: got %q, want %q", src, got, want)
		}
	}
}
