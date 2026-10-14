package resolve

import (
	"archive/zip"
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"

	"golang.org/x/mod/sumdb/dirhash"
)

// tempRoot holds fixture copies shared by the tests of one run.
var tempRoot string

func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "resolve-test-")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if tempRoot, err = filepath.EvalSymlinks(dir); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	diag = io.Discard
	code := m.Run()
	os.RemoveAll(dir)
	os.Exit(code)
}

var (
	toolOnce sync.Once
	goBin    string
	goRoot   string
)

func toolchain(t *testing.T) (string, string) {
	t.Helper()
	toolOnce.Do(func() {
		p, err := exec.LookPath("go")
		if err != nil {
			return
		}
		goBin = p
		out, err := exec.Command(p, "env", "GOROOT").Output()
		if err == nil {
			goRoot = strings.TrimSpace(string(out))
		}
	})
	if goBin == "" || goRoot == "" {
		t.Fatal("no go toolchain on PATH")
	}
	return goBin, goRoot
}

// mkdir creates a fresh directory under tempRoot.
func mkdir(t *testing.T, name string) string {
	t.Helper()
	d, err := os.MkdirTemp(tempRoot, name+"-")
	if err != nil {
		t.Fatal(err)
	}
	return d
}

// goEnv is the spec §3.6 environment for an offline load with an empty
// module cache; extra entries override it.
func goEnv(t *testing.T, extra map[string]string) []string {
	t.Helper()
	bin, root := toolchain(t)
	gocache := os.Getenv("GOCACHE")
	if gocache == "" {
		gocache = filepath.Join(tempRoot, "gocache")
	}
	xdg := os.Getenv("XDG_CONFIG_HOME")
	if xdg == "" {
		xdg = filepath.Join(tempRoot, "xdg")
	}
	m := map[string]string{
		"PATH":            filepath.Dir(bin) + ":/usr/bin:/bin",
		"HOME":            os.Getenv("HOME"),
		"GOROOT":          root,
		"GOTOOLCHAIN":     "local",
		"GOENV":           "off",
		"GOFLAGS":         "-mod=readonly -modcacherw -buildvcs=false",
		"GOWORK":          "off",
		"GOPROXY":         "off",
		"GOSUMDB":         "off",
		"GOVCS":           "*:off",
		"GOAUTH":          "off",
		"CGO_ENABLED":     "0",
		"GOOS":            "linux",
		"GOARCH":          "amd64",
		"GOMODCACHE":      mkdir(t, "modcache"),
		"GOCACHE":         gocache,
		"GOPATH":          mkdir(t, "gopath"),
		"TMPDIR":          os.TempDir(),
		"GOTMPDIR":        os.TempDir(),
		"XDG_CONFIG_HOME": xdg,
	}
	for k, v := range extra {
		m[k] = v
	}
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	env := make([]string, 0, len(keys))
	for _, k := range keys {
		env = append(env, k+"="+m[k])
	}
	return env
}

// copyTree copies a directory tree.
func copyTree(t *testing.T, src, dst string) {
	t.Helper()
	err := filepath.WalkDir(src, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(src, p)
		target := filepath.Join(dst, rel)
		if d.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		b, err := os.ReadFile(p)
		if err != nil {
			return err
		}
		return os.WriteFile(target, b, 0o644)
	})
	if err != nil {
		t.Fatal(err)
	}
}

// fixture copies testdata/<name> into a fresh directory and returns it.
func fixture(t *testing.T, name string) string {
	t.Helper()
	dst := filepath.Join(mkdir(t, name), name)
	copyTree(t, filepath.Join("testdata", name), dst)
	return dst
}

// proxy is a file:// module proxy built from testdata/proxy.
type proxy struct {
	url  string
	sums map[string][]string // "path@version" -> go.sum lines
}

var (
	proxyOnce sync.Once
	theProxy  *proxy
	proxyErr  error
)

// buildProxy writes every module under testdata/proxy/<path>@<version> into
// a GOPROXY directory and computes its go.sum lines.
func buildProxy(t *testing.T) *proxy {
	t.Helper()
	proxyOnce.Do(func() { theProxy, proxyErr = makeProxy(filepath.Join(tempRoot, "proxy")) })
	if proxyErr != nil {
		t.Fatal(proxyErr)
	}
	return theProxy
}

func makeProxy(dir string) (*proxy, error) {
	px := &proxy{url: "file://" + dir, sums: map[string][]string{}}
	src := filepath.Join("testdata", "proxy")
	var mods []string
	err := filepath.WalkDir(src, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() && strings.Contains(d.Name(), "@") {
			mods = append(mods, p)
			return filepath.SkipDir
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	for _, m := range mods {
		rel, _ := filepath.Rel(src, m)
		rel = filepath.ToSlash(rel)
		at := strings.LastIndex(rel, "@")
		path, version := rel[:at], rel[at+1:]
		vdir := filepath.Join(dir, filepath.FromSlash(path), "@v")
		if err := os.MkdirAll(vdir, 0o755); err != nil {
			return nil, err
		}
		gomod, err := os.ReadFile(filepath.Join(m, "go.mod"))
		if err != nil {
			return nil, err
		}
		files := map[string]string{
			"list":            version + "\n",
			version + ".info": fmt.Sprintf("{\"Version\":%q,\"Time\":\"2026-01-01T00:00:00Z\"}\n", version),
			version + ".mod":  string(gomod),
		}
		for name, body := range files {
			if err := os.WriteFile(filepath.Join(vdir, name), []byte(body), 0o644); err != nil {
				return nil, err
			}
		}
		zipPath := filepath.Join(vdir, version+".zip")
		if err := writeModZip(zipPath, m, path+"@"+version); err != nil {
			return nil, err
		}
		h1, err := dirhash.HashZip(zipPath, dirhash.Hash1)
		if err != nil {
			return nil, err
		}
		modH1, err := dirhash.Hash1([]string{"go.mod"}, func(string) (io.ReadCloser, error) {
			return io.NopCloser(bytes.NewReader(gomod)), nil
		})
		if err != nil {
			return nil, err
		}
		px.sums[path+"@"+version] = []string{
			fmt.Sprintf("%s %s %s", path, version, h1),
			fmt.Sprintf("%s %s/go.mod %s", path, version, modH1),
		}
	}
	return px, nil
}

func writeModZip(zipPath, srcDir, prefix string) error {
	f, err := os.Create(zipPath)
	if err != nil {
		return err
	}
	zw := zip.NewWriter(f)
	err = filepath.WalkDir(srcDir, func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		rel, _ := filepath.Rel(srcDir, p)
		w, err := zw.Create(prefix + "/" + filepath.ToSlash(rel))
		if err != nil {
			return err
		}
		b, err := os.ReadFile(p)
		if err != nil {
			return err
		}
		_, err = w.Write(b)
		return err
	})
	if cerr := zw.Close(); err == nil {
		err = cerr
	}
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	return err
}

// goSum is the go.sum body for the given proxy modules.
func (px *proxy) goSum(mods ...string) string {
	var lines []string
	for _, m := range mods {
		lines = append(lines, px.sums[m]...)
	}
	sort.Strings(lines)
	return strings.Join(lines, "\n") + "\n"
}

// result is the parsed output of one resolve run.
type result struct {
	root     string
	files    map[string]*fileRecord // by relpath
	sites    map[string]map[int]*siteRecord
	convs    map[string]map[int]bool
	notcalls map[string]map[int]bool
	imports  map[string]map[int]*importRecord
	embeds   []*embedRecord
	impls    []*implementsRecord
	errors   []*errorRecord
	summary  *summaryRecord
	lines    []string
}

func (r *result) rel(path string) string {
	rel, err := filepath.Rel(r.root, path)
	if err != nil {
		return path
	}
	return filepath.ToSlash(rel)
}

// resolveRun runs Resolve in-process and parses its output.
func resolveRun(t *testing.T, opts *Options, env []string) *result {
	t.Helper()
	if opts.BuildID == "" {
		opts.BuildID = "linux-amd64"
	}
	if opts.GOOS == "" {
		opts.GOOS, opts.GOARCH = "linux", "amd64"
	}
	var buf bytes.Buffer
	if err := Resolve(opts, env, &buf); err != nil {
		t.Fatalf("Resolve: %v", err)
	}
	return parseOutput(t, opts.RepoRoot, buf.Bytes())
}

func parseOutput(t *testing.T, root string, data []byte) *result {
	t.Helper()
	real, err := filepath.EvalSymlinks(root)
	if err != nil {
		t.Fatal(err)
	}
	r := &result{root: real, files: map[string]*fileRecord{}, sites: map[string]map[int]*siteRecord{},
		convs: map[string]map[int]bool{}, notcalls: map[string]map[int]bool{}, imports: map[string]map[int]*importRecord{}}
	sc := bufio.NewScanner(bytes.NewReader(data))
	sc.Buffer(make([]byte, 1<<20), 1<<26)
	for sc.Scan() {
		line := sc.Text()
		r.lines = append(r.lines, line)
		var head struct{ Kind string }
		if err := json.Unmarshal([]byte(line), &head); err != nil {
			t.Fatalf("bad JSON line %q: %v", line, err)
		}
		dec := func(v any) {
			d := json.NewDecoder(strings.NewReader(line))
			d.DisallowUnknownFields()
			if err := d.Decode(v); err != nil {
				t.Fatalf("decode %s: %v (%s)", head.Kind, err, line)
			}
		}
		switch head.Kind {
		case "file":
			var f fileRecord
			dec(&f)
			r.files[r.rel(f.Path)] = &f
		case "site":
			var s siteRecord
			dec(&s)
			rel := r.rel(s.Path)
			if r.sites[rel] == nil {
				r.sites[rel] = map[int]*siteRecord{}
			}
			if _, dup := r.sites[rel][s.Offset]; dup {
				t.Fatalf("duplicate site %s:%d", rel, s.Offset)
			}
			r.sites[rel][s.Offset] = &s
		case "conversion":
			var c conversionRecord
			dec(&c)
			rel := r.rel(c.Path)
			if r.convs[rel] == nil {
				r.convs[rel] = map[int]bool{}
			}
			r.convs[rel][c.Offset] = true
		case "notcall":
			var c notcallRecord
			dec(&c)
			rel := r.rel(c.Path)
			if r.notcalls[rel] == nil {
				r.notcalls[rel] = map[int]bool{}
			}
			r.notcalls[rel][c.Offset] = true
		case "import":
			var im importRecord
			dec(&im)
			rel := r.rel(im.Path)
			if r.imports[rel] == nil {
				r.imports[rel] = map[int]*importRecord{}
			}
			r.imports[rel][im.Offset] = &im
		case "embed":
			var e embedRecord
			dec(&e)
			r.embeds = append(r.embeds, &e)
		case "implements":
			var e implementsRecord
			dec(&e)
			r.impls = append(r.impls, &e)
		case "error":
			var e errorRecord
			dec(&e)
			r.errors = append(r.errors, &e)
		case "summary":
			var s summaryRecord
			dec(&s)
			r.summary = &s
		default:
			t.Fatalf("unknown record kind %q", head.Kind)
		}
	}
	if r.summary == nil {
		t.Fatal("no summary record")
	}
	if last := r.lines[len(r.lines)-1]; !strings.Contains(last, `"kind":"summary"`) {
		t.Fatalf("summary is not the last record: %s", last)
	}
	return r
}

// at returns the byte offset, line and column of the first occurrence of tok
// inside the unique occurrence of snippet in the file.
func at(t *testing.T, path, snippet, tok string) (off, line, col int) {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	src := string(b)
	i := strings.Index(src, snippet)
	if i < 0 {
		t.Fatalf("%s: snippet %q not found", path, snippet)
	}
	if strings.Contains(src[i+1:], snippet) {
		t.Fatalf("%s: snippet %q is not unique", path, snippet)
	}
	j := wordIndex(snippet, tok)
	if j < 0 {
		t.Fatalf("token %q not in snippet %q", tok, snippet)
	}
	off = i + j
	line = 1 + strings.Count(src[:off], "\n")
	col = off - strings.LastIndex(src[:off], "\n")
	return off, line, col
}

func isIdentByte(c byte) bool {
	return c == '_' || c >= '0' && c <= '9' || c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= 0x80
}

// wordIndex finds tok in s as a whole identifier where tok begins or ends
// with an identifier character.
func wordIndex(s, tok string) int {
	for from := 0; from <= len(s)-len(tok); {
		j := strings.Index(s[from:], tok)
		if j < 0 {
			return -1
		}
		j += from
		end := j + len(tok)
		okL := j == 0 || !isIdentByte(tok[0]) || !isIdentByte(s[j-1])
		okR := end == len(s) || !isIdentByte(tok[len(tok)-1]) || !isIdentByte(s[end])
		if okL && okR {
			return j
		}
		from = j + 1
	}
	return -1
}

// site returns the site record at tok inside snippet of relpath.
func (r *result) site(t *testing.T, relpath, snippet, tok string) *siteRecord {
	t.Helper()
	off, _, _ := at(t, filepath.Join(r.root, relpath), snippet, tok)
	s := r.sites[relpath][off]
	if s == nil {
		t.Fatalf("no site at %s:%d (%q / %q)", relpath, off, snippet, tok)
	}
	return s
}

// decl returns the position of a declaration name as a Target-like triple.
func (r *result) decl(t *testing.T, relpath, snippet, tok string) (string, int, int) {
	t.Helper()
	path := filepath.Join(r.root, relpath)
	_, line, col := at(t, path, snippet, tok)
	return path, line, col
}

// wantTarget checks that tgt names the declaration at tok in snippet.
func (r *result) wantTarget(t *testing.T, what string, tgt *Target, relpath, snippet, tok string) {
	t.Helper()
	if tgt == nil {
		t.Fatalf("%s: target is null, want %s %q", what, relpath, tok)
	}
	path, line, col := r.decl(t, relpath, snippet, tok)
	if tgt.Path != path || tgt.Line != line || tgt.Col != col || tgt.Name != tok {
		t.Fatalf("%s: target %s:%d:%d %q, want %s:%d:%d %q", what, tgt.Path, tgt.Line, tgt.Col, tgt.Name, path, line, col, tok)
	}
}

// treeHash fingerprints every file under dir (paths, modes and contents).
func treeHash(t *testing.T, dir string) string {
	t.Helper()
	var b strings.Builder
	err := filepath.WalkDir(dir, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(dir, p)
		fmt.Fprintf(&b, "%s %v %d", rel, info.Mode(), info.ModTime().UnixNano())
		if info.Mode().IsRegular() {
			c, err := os.ReadFile(p)
			if err != nil {
				return err
			}
			fmt.Fprintf(&b, " %x", c)
		}
		b.WriteByte('\n')
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return b.String()
}
