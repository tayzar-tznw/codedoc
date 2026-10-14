// Package extract implements `gohelper extract` (protocol version 1, C2):
// go/parser declarations, call sites and imports per file, as entities.json
// FileRecords (C3). Standard library only.
package extract

import (
	"bufio"
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"math"
	"os"
	"path"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// UsageError marks an invalid invocation (exit code 2). Run returns other
// errors only when writing the output fails.
type UsageError struct{ Msg string }

func (e *UsageError) Error() string { return e.Msg }

func usagef(format string, args ...any) error {
	return &UsageError{Msg: fmt.Sprintf(format, args...)}
}

type fileLine struct {
	Kind    string      `json:"kind"`
	Relpath string      `json:"relpath"`
	Record  *FileRecord `json:"record,omitempty"`
	Error   string      `json:"error,omitempty"`
}

type summaryLine struct {
	Kind    string  `json:"kind"`
	Files   int     `json:"files"`
	Errors  int     `json:"errors"`
	Seconds float64 `json:"seconds"`
}

// Run implements `gohelper extract --root ABS --files-from FILE [--goos
// --goarch --tags --cgo --out FILE]`. It writes one "file" line per listed
// path in list order (a file that cannot be read or parsed gets an "error"
// field instead of a record), then a "summary" line. An invalid invocation
// returns a *UsageError before anything is written.
func Run(args []string, stdout io.Writer) error {
	fl := flag.NewFlagSet("extract", flag.ContinueOnError)
	fl.SetOutput(io.Discard)
	root := fl.String("root", "", "repo root")
	filesFrom := fl.String("files-from", "", "file listing repo-relative POSIX paths, one per line")
	goos := fl.String("goos", "linux", "GOOS of the primary build configuration")
	goarch := fl.String("goarch", "amd64", "GOARCH of the primary build configuration")
	tags := fl.String("tags", "", "comma-separated build tags")
	cgo := fl.String("cgo", "0", "1 when cgo is enabled")
	out := fl.String("out", "", "write the JSON lines to this file instead of stdout")
	if err := fl.Parse(args); err != nil {
		return usagef("%v", err)
	}
	switch {
	case fl.NArg() > 0:
		return usagef("unexpected arguments: %q", fl.Args())
	case *root == "":
		return usagef("--root is required")
	case *filesFrom == "":
		return usagef("--files-from is required")
	}
	cgoOn, err := strconv.ParseBool(*cgo)
	if err != nil {
		return usagef("--cgo: %q is not 0 or 1", *cgo)
	}
	absRoot, err := filepath.Abs(*root)
	if err != nil {
		return usagef("--root: %v", err)
	}
	if fi, err := os.Stat(absRoot); err != nil || !fi.IsDir() {
		return usagef("--root %s is not a directory", *root)
	}
	list, err := os.ReadFile(*filesFrom)
	if err != nil {
		return usagef("--files-from: %v", err)
	}
	w := stdout
	var outFile *os.File
	if *out != "" {
		if outFile, err = os.Create(*out); err != nil {
			return usagef("--out: %v", err)
		}
		defer outFile.Close() // on an early return; closed and checked below
		w = outFile
	}

	start := time.Now()
	results := extractAll(absRoot, relpaths(list), newBuildConfig(*goos, *goarch, splitTags(*tags), cgoOn))
	resolveOwners(results)

	// Encoding costs about as much as parsing: encode in parallel, write in
	// list order.
	encoded := make([][]byte, len(results))
	encErrs := make([]error, len(results))
	forEach(len(results), func(i int) {
		r := results[i]
		encoded[i], encErrs[i] = encodeLine(fileLine{Kind: "file", Relpath: r.relpath, Record: r.record, Error: r.err})
	})
	bw := bufio.NewWriterSize(w, 1<<20)
	errs := 0
	for i, r := range results {
		if encErrs[i] != nil {
			return encErrs[i]
		}
		if r.err != "" {
			errs++
		}
		if _, err := bw.Write(encoded[i]); err != nil {
			return err
		}
	}
	secs := math.Round(time.Since(start).Seconds()*1000) / 1000
	sum, err := encodeLine(summaryLine{Kind: "summary", Files: len(results), Errors: errs, Seconds: secs})
	if err != nil {
		return err
	}
	if _, err := bw.Write(sum); err != nil {
		return err
	}
	if err := bw.Flush(); err != nil {
		return err
	}
	if outFile != nil {
		return outFile.Close()
	}
	return nil
}

// relpaths reads the --files-from list: one path per line, CRLF tolerated,
// blank lines and repeats dropped.
func relpaths(list []byte) []string {
	var out []string
	seen := map[string]bool{}
	for _, l := range bytes.Split(list, []byte("\n")) {
		p := strings.TrimSuffix(string(l), "\r")
		if p != "" && !seen[p] {
			seen[p] = true
			out = append(out, p)
		}
	}
	return out
}

func splitTags(s string) []string {
	return strings.FieldsFunc(s, func(r rune) bool { return r == ',' || r == ' ' })
}

// extractAll extracts the files in parallel; results keep the list order.
func extractAll(root string, rels []string, bc *buildConfig) []*fileResult {
	results := make([]*fileResult, len(rels))
	mods := &moduleFinder{root: root, memo: map[string]*module{}}
	var todo []int
	owners := make([]*module, len(rels))
	for i, rel := range rels {
		if !cleanRelpath(rel) {
			results[i] = &fileResult{relpath: rel, err: "not a clean repo-relative path"}
			continue
		}
		dir := path.Dir(rel)
		if dir == "." {
			dir = ""
		}
		owners[i] = mods.find(dir)
		todo = append(todo, i)
	}
	// Largest files first, so that a big file does not start last and set
	// the wall time.
	size := make([]int64, len(rels))
	for _, i := range todo {
		if fi, err := os.Stat(filepath.Join(root, filepath.FromSlash(rels[i]))); err == nil {
			size[i] = fi.Size()
		}
	}
	sort.SliceStable(todo, func(a, b int) bool { return size[todo[a]] > size[todo[b]] })
	forEach(len(todo), func(k int) {
		i := todo[k]
		src, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(rels[i])))
		if err != nil {
			results[i] = &fileResult{relpath: rels[i], err: err.Error()}
			return
		}
		results[i] = extractSource(rels[i], src, bc, owners[i])
	})
	return results
}

// forEach runs fn(0) ... fn(n-1) on GOMAXPROCS goroutines.
func forEach(n int, fn func(i int)) {
	var next atomic.Int64
	var wg sync.WaitGroup
	for range min(runtime.GOMAXPROCS(0), n) {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := int(next.Add(1)) - 1; i < n; i = int(next.Add(1)) - 1 {
				fn(i)
			}
		}()
	}
	wg.Wait()
}

// encodeLine is one JSON line; qualnames such as <package> stay unescaped.
func encodeLine(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	err := enc.Encode(v)
	return buf.Bytes(), err
}

func cleanRelpath(p string) bool {
	return p == path.Clean(p) && !path.IsAbs(p) && p != ".." && !strings.HasPrefix(p, "../")
}

// moduleFinder finds the go.mod that owns a directory: the deepest one at or
// above it, up to the repo root.
type moduleFinder struct {
	root string
	memo map[string]*module // directory -> owning module, nil = none
}

func (m *moduleFinder) find(dir string) *module {
	if mod, ok := m.memo[dir]; ok {
		return mod
	}
	var mod *module
	gomod := filepath.Join(m.root, filepath.FromSlash(dir), "go.mod")
	if fi, err := os.Stat(gomod); err == nil && !fi.IsDir() {
		data, _ := os.ReadFile(gomod)
		mod = &module{root: dir, path: modulePath(data)}
	} else if dir != "" {
		parent := path.Dir(dir)
		if parent == "." {
			parent = ""
		}
		mod = m.find(parent)
	}
	m.memo[dir] = mod
	return mod
}

// modulePath returns the path of a go.mod's module directive (also in block
// form), or "".
func modulePath(data []byte) string {
	data = bytes.TrimPrefix(data, []byte("\xef\xbb\xbf"))
	inBlock := false
	for _, l := range strings.Split(string(data), "\n") {
		if i := strings.Index(l, "//"); i >= 0 {
			l = l[:i]
		}
		f := strings.Fields(strings.NewReplacer("(", " ( ", ")", " ) ").Replace(l))
		switch {
		case len(f) == 0:
		case inBlock:
			if f[0] == ")" {
				return ""
			}
			return unquote(f[0])
		case f[0] == "module" && len(f) > 1:
			if f[1] != "(" {
				return unquote(f[1])
			}
			inBlock = true
		}
	}
	return ""
}

func unquote(s string) string {
	if u, err := strconv.Unquote(s); err == nil {
		return u
	}
	return s
}
