// Command gohelper is CodeDoc's Go helper (protocol version 1).
//
//	gohelper version
//	gohelper extract --root ABS --files-from FILE [--goos --goarch --tags --cgo]
//	gohelper resolve --repo-root ABS --dir ABS --pattern work|./... [flags]
//
// Output is JSON Lines on stdout (or --out FILE); diagnostics go to stderr.
// The exit code is 0 unless the invocation itself is invalid (2); load errors
// are records, not exit codes.
package main

import (
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"runtime"
	"runtime/debug"
	"sort"

	"codedoc.local/gohelper/internal/extract"
	"codedoc.local/gohelper/internal/resolve"
)

const protocol = 1

// pinnedXTools is reported when the build info carries no module versions.
const pinnedXTools = "v0.52.0"

// sources is the helper's own source list; its hash is the revision.
//
//go:embed main.go go.mod go.sum vendor/modules.txt internal/resolve/*.go internal/extract/*.go
var sources embed.FS

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

func run(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		fmt.Fprintln(stderr, "usage: gohelper version | extract [flags] | resolve [flags]")
		return 2
	}
	var err error
	switch args[0] {
	case "version":
		if len(args) > 1 {
			fmt.Fprintln(stderr, "gohelper version: takes no arguments")
			return 2
		}
		err = writeVersion(stdout)
	case "extract":
		err = extract.Run(args[1:], stdout)
	case "resolve":
		err = resolve.Run(args[1:], stdout)
	default:
		fmt.Fprintf(stderr, "gohelper: unknown subcommand %q\n", args[0])
		return 2
	}
	if err != nil {
		fmt.Fprintf(stderr, "gohelper %s: %v\n", args[0], err)
		var resolveUsage *resolve.UsageError
		var extractUsage *extract.UsageError
		if errors.As(err, &resolveUsage) || errors.As(err, &extractUsage) {
			return 2
		}
		return 1 // writing the output failed
	}
	return 0
}

func writeVersion(w io.Writer) error {
	rev, err := revision()
	if err != nil {
		return err
	}
	rec := struct {
		Kind     string `json:"kind"`
		Protocol int    `json:"protocol"`
		Go       string `json:"go"`
		XTools   string `json:"x_tools"`
		Revision string `json:"revision"`
	}{"version", protocol, runtime.Version(), xToolsVersion(), rev}
	return json.NewEncoder(w).Encode(rec)
}

func xToolsVersion() string {
	if bi, ok := debug.ReadBuildInfo(); ok {
		for _, d := range bi.Deps {
			if d.Path == "golang.org/x/tools" {
				return d.Version
			}
		}
	}
	return pinnedXTools
}

// revision is the SHA-256 over "path sha256(content)\n" lines of the embedded
// sources, sorted by path.
func revision() (string, error) {
	var paths []string
	err := fs.WalkDir(sources, ".", func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !d.IsDir() {
			paths = append(paths, p)
		}
		return nil
	})
	if err != nil {
		return "", err
	}
	sort.Strings(paths)
	h := sha256.New()
	for _, p := range paths {
		b, err := sources.ReadFile(p)
		if err != nil {
			return "", err
		}
		sum := sha256.Sum256(b)
		fmt.Fprintf(h, "%s %s\n", p, hex.EncodeToString(sum[:]))
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}
