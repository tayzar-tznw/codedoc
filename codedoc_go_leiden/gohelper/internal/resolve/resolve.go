// Package resolve implements `gohelper resolve` (contract C2, protocol 1):
// one go/packages + go/types load of a group, then one JSON line per call
// site, conversion, import, embedding, implementation pair and load error.
package resolve

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"

	"golang.org/x/tools/go/packages"
)

// UsageError marks an invalid invocation (exit code 2).
type UsageError struct{ Msg string }

func (e *UsageError) Error() string { return e.Msg }

func usagef(format string, args ...any) error {
	return &UsageError{Msg: fmt.Sprintf(format, args...)}
}

// Options are the resolve flags (C2).
type Options struct {
	RepoRoot    string   // --repo-root, absolute
	Dir         string   // --dir, absolute cwd of the load
	Pattern     string   // --pattern: "work" or "./..."
	GoWork      string   // --gowork: absolute path, "off", or "" (inherit)
	ModFile     string   // --modfile: absolute path or ""
	GOOS        string   // --goos
	GOARCH      string   // --goarch
	Tags        []string // --tags a,b
	CGO         bool     // --cgo 0|1
	BuildID     string   // --build-id
	MainModules []string // --main-modules m1,m2
	PeerDirs    []string // --peer-dirs ABS,ABS
	Out         string   // --out FILE ("" = stdout)
}

// diag receives the one-line timing summary (stderr; tests silence it).
var diag io.Writer = os.Stderr

// loadMode is fixed by C2 and spec §5.1.
const loadMode = packages.NeedName | packages.NeedFiles | packages.NeedSyntax | packages.NeedTypes |
	packages.NeedTypesInfo | packages.NeedDeps | packages.NeedImports | packages.NeedModule

// Run executes `gohelper resolve` with the process environment.
func Run(args []string, stdout io.Writer) error {
	opts, err := ParseArgs(args)
	if err != nil {
		return err
	}
	if opts.Out == "" {
		return Resolve(opts, os.Environ(), stdout)
	}
	tmp := opts.Out + ".tmp"
	f, err := os.Create(tmp)
	if err != nil {
		return usagef("--out: %v", err)
	}
	if err := Resolve(opts, os.Environ(), f); err != nil {
		f.Close()
		os.Remove(tmp)
		return err
	}
	if err := f.Close(); err != nil {
		os.Remove(tmp)
		return err
	}
	return os.Rename(tmp, opts.Out)
}

// ParseArgs parses and validates the resolve flags.
func ParseArgs(args []string) (*Options, error) {
	fs := flag.NewFlagSet("resolve", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	o := &Options{}
	var tags, cgo, mains, peers string
	fs.StringVar(&o.RepoRoot, "repo-root", "", "")
	fs.StringVar(&o.Dir, "dir", "", "")
	fs.StringVar(&o.Pattern, "pattern", "", "")
	fs.StringVar(&o.GoWork, "gowork", "", "")
	fs.StringVar(&o.ModFile, "modfile", "", "")
	fs.StringVar(&o.GOOS, "goos", "linux", "")
	fs.StringVar(&o.GOARCH, "goarch", "amd64", "")
	fs.StringVar(&tags, "tags", "", "")
	fs.StringVar(&cgo, "cgo", "0", "")
	fs.StringVar(&o.BuildID, "build-id", "", "")
	fs.StringVar(&mains, "main-modules", "", "")
	fs.StringVar(&peers, "peer-dirs", "", "")
	fs.StringVar(&o.Out, "out", "", "")
	if err := fs.Parse(args); err != nil {
		return nil, usagef("%v", err)
	}
	if fs.NArg() > 0 {
		return nil, usagef("unexpected arguments: %q", fs.Args())
	}
	if o.RepoRoot == "" || !filepath.IsAbs(o.RepoRoot) {
		return nil, usagef("--repo-root must be an absolute path")
	}
	if o.Dir == "" || !filepath.IsAbs(o.Dir) {
		return nil, usagef("--dir must be an absolute path")
	}
	switch o.Pattern {
	case "work", "./...":
	default:
		return nil, usagef("--pattern must be work or ./...")
	}
	if o.GoWork != "" && o.GoWork != "off" && !filepath.IsAbs(o.GoWork) {
		return nil, usagef("--gowork must be an absolute path or off")
	}
	if o.ModFile != "" && !filepath.IsAbs(o.ModFile) {
		return nil, usagef("--modfile must be an absolute path")
	}
	if o.ModFile != "" && o.GoWork != "off" {
		return nil, usagef("--modfile needs --gowork off (the go command rejects -modfile in workspace mode)")
	}
	if o.Pattern == "work" && o.GoWork == "off" {
		return nil, usagef("--pattern work needs a workspace (--gowork)")
	}
	switch cgo {
	case "0":
	case "1":
		o.CGO = true
	default:
		return nil, usagef("--cgo must be 0 or 1")
	}
	if o.GOOS == "" || o.GOARCH == "" {
		return nil, usagef("--goos and --goarch must not be empty")
	}
	o.Tags = splitList(tags)
	o.MainModules = splitList(mains)
	o.PeerDirs = splitList(peers)
	for _, d := range o.PeerDirs {
		if !filepath.IsAbs(d) {
			return nil, usagef("--peer-dirs entries must be absolute: %q", d)
		}
	}
	if o.BuildID == "" {
		o.BuildID = o.GOOS + "-" + o.GOARCH
	}
	return o, nil
}

func splitList(s string) []string {
	var out []string
	for _, p := range strings.Split(s, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// Resolve loads the group described by opts with the given environment and
// writes the records to w. Load failures are records; the error result is
// only for output failures.
func Resolve(opts *Options, env []string, w io.Writer) error {
	t0 := time.Now()
	out := newWriter(w)
	env = loadEnv(opts, env)
	pr := newProgram(opts, env)

	cfg := &packages.Config{
		Mode:       loadMode,
		Tests:      true,
		Dir:        opts.Dir,
		Env:        env,
		BuildFlags: buildFlags(opts),
		Fset:       pr.fset,
		ParseFile:  pr.parseFile,
	}
	roots, err := packages.Load(cfg, opts.Pattern)
	loadSeconds := time.Since(t0).Seconds()
	if err != nil {
		out.write(&errorRecord{Kind: "error", Msg: "load failed: " + err.Error()})
		out.write(&summaryRecord{Kind: "summary", Errors: 1, LoadSeconds: loadSeconds,
			PeakRSSBytes: peakRSS(), Seconds: time.Since(t0).Seconds()})
		return out.flush()
	}
	t1 := time.Now()
	pr.index(roots)
	pr.collect()
	t2 := time.Now()
	pr.computeIfaces()
	t3 := time.Now()
	sum := pr.emit(out)
	sum.LoadSeconds = loadSeconds
	sum.PeakRSSBytes = peakRSS()
	sum.Seconds = time.Since(t0).Seconds()
	out.write(sum)
	err = out.flush()
	fmt.Fprintf(diag, "gohelper resolve: load %.2fs, sites %.2fs, interfaces %.2fs, output %.2fs; %d sites, %d conversions, %d packages\n",
		loadSeconds, t2.Sub(t1).Seconds(), t3.Sub(t2).Seconds(), time.Since(t3).Seconds(), sum.Sites, sum.Conversions, sum.Packages)
	return err
}

// loadEnv overrides the build configuration and workspace in env.
func loadEnv(o *Options, env []string) []string {
	env = setEnv(env, "GOOS", o.GOOS)
	env = setEnv(env, "GOARCH", o.GOARCH)
	cgo := "0"
	if o.CGO {
		cgo = "1"
	}
	env = setEnv(env, "CGO_ENABLED", cgo)
	if o.GoWork != "" {
		env = setEnv(env, "GOWORK", o.GoWork)
	}
	return env
}

func buildFlags(o *Options) []string {
	flags := []string{"-buildvcs=false"}
	if o.ModFile != "" {
		flags = append(flags, "-modfile="+o.ModFile)
	}
	if len(o.Tags) > 0 {
		flags = append(flags, "-tags="+strings.Join(o.Tags, ","))
	}
	return flags
}

func setEnv(env []string, key, value string) []string {
	out := make([]string, 0, len(env)+1)
	for _, kv := range env {
		if !strings.HasPrefix(kv, key+"=") {
			out = append(out, kv)
		}
	}
	return append(out, key+"="+value)
}

func getEnv(env []string, key string) string {
	v := ""
	for _, kv := range env {
		if strings.HasPrefix(kv, key+"=") {
			v = kv[len(key)+1:]
		}
	}
	return v
}

// goroot returns the toolchain root: $GOROOT when set, else `go env GOROOT`.
func goroot(env []string, dir string) string {
	if g := getEnv(env, "GOROOT"); g != "" {
		return g
	}
	cmd := exec.Command("go", "env", "GOROOT")
	cmd.Env = env
	cmd.Dir = dir
	b, err := cmd.Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

// parseFile is the loader's ParseFile hook. It records the SHA-256 of the
// exact bytes parsed for files that may be in the repo.
func (pr *program) parseFile(fset *token.FileSet, filename string, src []byte) (*ast.File, error) {
	if pr.maybeInRepo(filename) {
		sum := sha256.Sum256(src)
		pr.hashMu.Lock()
		pr.parsedHash[filename] = hex.EncodeToString(sum[:])
		pr.hashMu.Unlock()
	}
	return parser.ParseFile(fset, filename, src, parser.AllErrors|parser.ParseComments|parser.SkipObjectResolution)
}

// writer streams JSON lines.
type writer struct {
	bw  *bufio.Writer
	enc *json.Encoder
	err error
}

func newWriter(w io.Writer) *writer {
	bw := bufio.NewWriterSize(w, 1<<20)
	enc := json.NewEncoder(bw)
	enc.SetEscapeHTML(false)
	return &writer{bw: bw, enc: enc}
}

func (w *writer) write(v any) {
	if w.err == nil {
		w.err = w.enc.Encode(v)
	}
}

// writeAll encodes records in parallel chunks and writes them in order.
func (w *writer) writeAll(recs []any) {
	const chunk = 4096
	n := (len(recs) + chunk - 1) / chunk
	bufs := make([]bytes.Buffer, n)
	errs := make([]error, n)
	var wg sync.WaitGroup
	sem := make(chan struct{}, runtime.GOMAXPROCS(0))
	for i := 0; i < n; i++ {
		wg.Add(1)
		sem <- struct{}{}
		go func(i int) {
			defer func() { <-sem; wg.Done() }()
			enc := json.NewEncoder(&bufs[i])
			enc.SetEscapeHTML(false)
			for _, r := range recs[i*chunk : min((i+1)*chunk, len(recs))] {
				if err := enc.Encode(r); err != nil {
					errs[i] = err
					return
				}
			}
		}(i)
	}
	wg.Wait()
	for i := range bufs {
		if w.err == nil && errs[i] != nil {
			w.err = errs[i]
		}
		if w.err == nil {
			_, w.err = w.bw.Write(bufs[i].Bytes())
		}
	}
}

func (w *writer) flush() error {
	if w.err != nil {
		return w.err
	}
	return w.bw.Flush()
}
