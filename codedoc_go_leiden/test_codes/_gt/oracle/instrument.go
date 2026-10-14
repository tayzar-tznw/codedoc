package main

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
)

// rtDirName is the directory of the runtime module in the copy; the leading
// underscore keeps it out of ./... patterns.
const rtDirName = "_goracle_rt"

func cmdInstrument(args []string) {
	fs := flag.NewFlagSet("instrument", flag.ExitOnError)
	src := fs.String("src", "", "source tree (a module, a workspace or a fixture with several)")
	out := fs.String("out", "", "copy to create and rewrite")
	mode := fs.String("mode", "all", "column tags: all, needed or none")
	dirs := fs.String("dirs", "", "comma-separated package directories relative to --src (default: all)")
	tags := fs.String("tags", "", "comma-separated build tags of the build configuration")
	force := fs.Bool("force", false, "remove --out first when it exists")
	tests := fs.Bool("tests", true, "instrument _test.go files too")
	counters := fs.Bool("counters", true, "add completion and reach counters")
	check(fs.Parse(args))
	if *src == "" || *out == "" || fs.NArg() > 0 {
		fs.Usage()
		os.Exit(2)
	}
	if *mode != "all" && *mode != "needed" && *mode != "none" {
		check(fmt.Errorf("--mode must be all, needed or none, not %q", *mode))
	}
	in, meta, err := instrumentTree(*src, *out, instrumentOptions{mode: *mode, dirs: splitList(*dirs),
		tags: splitList(*tags), force: *force, tests: *tests, counters: *counters})
	check(err)
	in.summary(os.Stdout, meta)
}

type instrumentOptions struct {
	mode            string
	dirs, tags      []string
	force           bool
	tests, counters bool
}

// instrumentTree copies src to out, rewrites the copy and writes its site table.
func instrumentTree(src, out string, o instrumentOptions) (*instrumenter, *MetaRec, error) {
	srcAbs, err := filepath.Abs(src)
	if err != nil {
		return nil, nil, err
	}
	if srcAbs, err = filepath.EvalSymlinks(srcAbs); err != nil {
		return nil, nil, err
	}
	outAbs, err := filepath.Abs(out)
	if err != nil {
		return nil, nil, err
	}
	if outAbs == srcAbs || strings.HasPrefix(outAbs, srcAbs+string(filepath.Separator)) {
		return nil, nil, fmt.Errorf("--out %s lies inside --src %s", outAbs, srcAbs)
	}
	if ents, err := os.ReadDir(outAbs); err == nil && len(ents) > 0 {
		if !o.force {
			return nil, nil, fmt.Errorf("--out %s exists and is not empty (use --force)", outAbs)
		}
		if err := os.RemoveAll(outAbs); err != nil {
			return nil, nil, err
		}
	}
	real := map[string]string{}
	if err := copyTree(srcAbs, outAbs, real); err != nil {
		return nil, nil, err
	}
	in := newInstrumenter(outAbs, o.mode, o.tags)
	in.tests, in.done, in.real = o.tests, o.counters, real
	in.run(o.dirs)
	if err := in.plumb(); err != nil {
		return nil, nil, err
	}
	meta := in.meta(srcAbs)
	writeJSONL(sitesPathOf(outAbs), in.records(meta))
	return in, meta, nil
}

func cmdScan(args []string) {
	fs := flag.NewFlagSet("scan", flag.ExitOnError)
	root := fs.String("root", "", "tree to scan (nothing is written into it)")
	dirs := fs.String("dirs", "", "comma-separated package directories relative to --root (default: all)")
	mode := fs.String("mode", "all", "column tags: all, needed or none")
	tags := fs.String("tags", "", "comma-separated build tags")
	out := fs.String("out", "", "site table (default: stdout)")
	check(fs.Parse(args))
	if *root == "" || fs.NArg() > 0 {
		fs.Usage()
		os.Exit(2)
	}
	abs, err := filepath.Abs(*root)
	check(err)
	in := newInstrumenter(abs, *mode, splitList(*tags))
	in.dry = true
	in.run(splitList(*dirs))
	meta := in.meta("")
	recs := in.records(meta)
	if *out == "" {
		w := newEncoder(os.Stdout)
		for _, r := range recs {
			check(w.Encode(r))
		}
		return
	}
	writeJSONL(*out, recs)
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

// skipDir reports directories the go command skips for ./... patterns.
func skipDir(name string) bool {
	return strings.HasPrefix(name, ".") || strings.HasPrefix(name, "_") || name == "testdata" || name == "vendor"
}

// run instruments every package directory under the root (or the listed ones).
func (in *instrumenter) run(dirs []string) {
	var pkgDirs []string
	if len(dirs) > 0 {
		for _, d := range dirs {
			pkgDirs = append(pkgDirs, filepath.Join(in.root, filepath.FromSlash(d)))
		}
	} else {
		ig := ignoreSet{}
		check(filepath.WalkDir(in.root, func(path string, d fs.DirEntry, err error) error {
			if err != nil {
				return err
			}
			if d.IsDir() {
				if path != in.root && skipDir(d.Name()) {
					return filepath.SkipDir
				}
				if ig.ignored(path, in.root) {
					in.ignored = append(in.ignored, path)
					return filepath.SkipDir
				}
				pkgDirs = append(pkgDirs, path)
			}
			return nil
		}))
	}
	sort.Strings(pkgDirs)
	for _, d := range pkgDirs {
		in.pkgDir(d)
	}
	// rt packs fid+1 into 23 bits of a key word and counts statements in a
	// fixed array of 1<<22
	if in.nextFid >= 1<<23-1 || in.nextStmt >= 1<<22 {
		check(fmt.Errorf("too many functions (%d) or counters (%d) for package rt", in.nextFid, in.nextStmt))
	}
}

// ignoreSet answers whether a directory lies in an `ignore` directory of its
// module's go.mod (spec R21: not scanned, like testdata). Per the go.mod
// reference, a path starting with ./ is relative to the module root; any
// other path matches a directory with that path at any depth in the module.
type ignoreSet map[string][]string // module root -> ignore paths

func (ig ignoreSet) ignored(dir, top string) bool {
	root := ""
	for d := dir; ; d = filepath.Dir(d) {
		if _, err := os.Stat(filepath.Join(d, "go.mod")); err == nil {
			root = d
			break
		}
		if d == top || filepath.Dir(d) == d {
			return false
		}
	}
	specs, ok := ig[root]
	if !ok {
		data, err := os.ReadFile(filepath.Join(root, "go.mod"))
		if err != nil {
			return false
		}
		specs = ignoreLines(string(data))
		ig[root] = specs
	}
	rel, err := filepath.Rel(root, dir)
	if err != nil || rel == "." {
		return false
	}
	rel = filepath.ToSlash(rel)
	for _, spec := range specs {
		if strings.HasPrefix(spec, "./") {
			if p := strings.TrimSuffix(strings.TrimPrefix(spec, "./"), "/"); rel == p {
				return true
			}
		} else if p := strings.TrimSuffix(spec, "/"); p != "" && (rel == p || strings.HasSuffix(rel, "/"+p)) {
			return true
		}
	}
	return false
}

// ignoreLines returns the paths of the ignore directives of a go.mod.
func ignoreLines(gomod string) []string {
	var out []string
	block := false
	for _, line := range strings.Split(gomod, "\n") {
		if i := strings.Index(line, "//"); i >= 0 {
			line = line[:i]
		}
		line = strings.TrimSpace(line)
		rest, isIgnore := strings.CutPrefix(line, "ignore")
		isIgnore = isIgnore && (rest == "" || strings.ContainsAny(rest[:1], " \t(\"`"))
		rest = strings.TrimSpace(rest)
		switch {
		case block && line == ")":
			block = false
		case block && line != "":
			out = append(out, strings.Trim(line, "\"`"))
		case isIgnore && rest == "(":
			block = true
		case isIgnore && rest != "":
			out = append(out, strings.Trim(rest, "\"`"))
		}
	}
	return out
}

func (in *instrumenter) meta(src string) *MetaRec {
	skipped := 0
	for _, f := range in.files {
		if f.Status != "instrumented" {
			skipped++
		}
	}
	tags := in.ctx.BuildTags
	if tags == nil {
		tags = []string{}
	}
	return &MetaRec{Kind: "meta", Version: 1, Src: src, Root: in.root, Mode: in.tagMode, RT: rtModule,
		Go: runtime.Version(), GOOS: in.ctx.GOOS, GOARCH: in.ctx.GOARCH, CGO: in.ctx.CgoEnabled, Tags: tags,
		Funcs: len(in.funcs), Sites: len(in.sites), Skipped: skipped}
}

func (in *instrumenter) records(meta *MetaRec) []any {
	recs := []any{meta}
	for _, r := range in.files {
		recs = append(recs, r)
	}
	for _, r := range in.funcs {
		recs = append(recs, r)
	}
	for _, r := range in.imethods {
		recs = append(recs, r)
	}
	for _, r := range in.sites {
		recs = append(recs, r)
	}
	for _, r := range in.hots {
		recs = append(recs, r)
	}
	for _, r := range in.stmts {
		recs = append(recs, r)
	}
	return recs
}

func (in *instrumenter) summary(w io.Writer, meta *MetaRec) {
	nf, nt, covered, reached := 0, 0, 0, 0
	for _, r := range in.files {
		if r.Status == "instrumented" {
			nf++
		}
	}
	for _, s := range in.sites {
		if s.Tagged {
			nt++
		}
	}
	for _, r := range in.stmts {
		if r.Kind == "stmt" {
			covered += len(r.Sites)
		} else {
			reached += len(r.Sites)
		}
	}
	fmt.Fprintf(w, "instrumented %d of %d files in %d modules: %d functions, %d call sites, %d tagged (mode %s), "+
		"%d sites under completion counters, %d under reach counters; %d go.mod ignore directories skipped; table %s\n",
		nf, len(in.files), len(in.modRoots), len(in.funcs), len(in.sites), nt, meta.Mode, covered, reached,
		len(in.ignored), sitesPathOf(in.root))
}

// copyTree copies src to dst. Directory symlinks stay symlinks; a symlink to
// a file becomes a regular copy (so each path is instrumented on its own),
// and real records its target relative to src when the target lies inside.
// .git, .hg and .svn are not copied.
func copyTree(src, dst string, real map[string]string) error {
	return filepath.WalkDir(src, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(src, path)
		target := filepath.Join(dst, rel)
		if d.IsDir() {
			if path != src && (d.Name() == ".git" || d.Name() == ".hg" || d.Name() == ".svn") {
				return filepath.SkipDir
			}
			return os.MkdirAll(target, 0o755)
		}
		info, err := os.Lstat(path)
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			resolved, err := filepath.EvalSymlinks(path)
			if err != nil {
				return nil // dangling symlink: the go command ignores it too
			}
			st, err := os.Stat(resolved)
			if err != nil {
				return err
			}
			if st.IsDir() {
				link, err := os.Readlink(path)
				if err != nil {
					return err
				}
				return os.Symlink(link, target)
			}
			if r, err := filepath.Rel(src, resolved); err == nil && !strings.HasPrefix(r, "..") {
				real[filepath.ToSlash(rel)] = filepath.ToSlash(r)
			}
			return copyFile(resolved, target, st.Mode())
		}
		if !info.Mode().IsRegular() {
			return nil
		}
		return copyFile(path, target, info.Mode())
	})
}

func copyFile(from, to string, mode fs.FileMode) error {
	in, err := os.Open(from)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(to, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, mode.Perm()|0o200)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

// plumb makes the runtime package available to every module with
// instrumented files: the rt module is written to COPY/_goracle_rt; each such
// go.mod gets "require <rt> v0.0.0" and "replace <rt> => <abs dir>" (an
// absolute path, so workspace members agree on it); a vendor/modules.txt of
// such a module, or of a workspace (go.work with a vendor directory), gets the
// matching entries and a vendored copy of rt.
func (in *instrumenter) plumb() error {
	rtDir := filepath.Join(in.root, rtDirName)
	if err := os.MkdirAll(rtDir, 0o755); err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(rtDir, "go.mod"), []byte("module "+rtModule+"\n\ngo "+rtGoVersion+"\n"), 0o644); err != nil {
		return err
	}
	if err := writeRT(rtDir); err != nil {
		return err
	}
	var roots []string
	for r := range in.modRoots {
		roots = append(roots, r)
	}
	sort.Strings(roots)
	for _, r := range roots {
		if err := addRequire(filepath.Join(r, "go.mod"), rtDir); err != nil {
			return err
		}
		if err := vendorRT(filepath.Join(r, "vendor"), rtDir); err != nil {
			return err
		}
	}
	// workspace vendor directories
	return filepath.WalkDir(in.root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() && path != in.root && skipDir(d.Name()) {
			return filepath.SkipDir
		}
		if !d.IsDir() && d.Name() == "go.work" {
			if _, err := os.Stat(filepath.Join(filepath.Dir(path), "vendor", "modules.txt")); err == nil {
				return vendorRT(filepath.Join(filepath.Dir(path), "vendor"), rtDir)
			}
		}
		return nil
	})
}

func addRequire(gomod, rtDir string) error {
	data, err := os.ReadFile(gomod)
	if err != nil {
		return err
	}
	if strings.Contains(string(data), rtModule) {
		return nil
	}
	text := string(data)
	if text != "" && !strings.HasSuffix(text, "\n") {
		text += "\n"
	}
	text += "\nrequire " + rtModule + " v0.0.0\n\nreplace " + rtModule + " => " + rtDir + "\n"
	return os.WriteFile(gomod, []byte(text), 0o644)
}

// vendorRT adds rt to a vendor directory that has a modules.txt: the module
// line with its replacement, the explicit marker and go version, the package,
// and the wildcard replacement line the consistency check looks up.
func vendorRT(vendorDir, rtDir string) error {
	txt := filepath.Join(vendorDir, "modules.txt")
	data, err := os.ReadFile(txt)
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if strings.Contains(string(data), rtModule) {
		return nil
	}
	text := string(data)
	if text != "" && !strings.HasSuffix(text, "\n") {
		text += "\n"
	}
	text += "# " + rtModule + " v0.0.0 => " + rtDir + "\n## explicit; go " + rtGoVersion + "\n" + rtModule + "\n" +
		"# " + rtModule + " => " + rtDir + "\n"
	if err := os.WriteFile(txt, []byte(text), 0o644); err != nil {
		return err
	}
	return writeRT(filepath.Join(vendorDir, filepath.FromSlash(rtModule)))
}

// writeRT writes the runtime package's sources into dir.
func writeRT(dir string) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(dir, "rt.go"), rtSource, 0o644); err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(dir, "exit.go"), rtExitSource, 0o644)
}
