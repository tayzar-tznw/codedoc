package extract

import (
	"bytes"
	"errors"
	"go/ast"
	"go/build"
	"go/build/constraint"
	"io"
	"path"
	"strconv"
	"strings"
)

// buildConfig is the primary build configuration (GOOS, GOARCH, tags, cgo).
type buildConfig struct {
	ctxt build.Context
}

// newBuildConfig starts from build.Default, so release tags (go1.x) and tool
// tags come from the toolchain, as for the go command.
func newBuildConfig(goos, goarch string, tags []string, cgo bool) *buildConfig {
	c := build.Default
	c.GOOS, c.GOARCH, c.CgoEnabled = goos, goarch, cgo
	c.BuildTags = append([]string(nil), tags...)
	c.UseAllFiles = false
	return &buildConfig{ctxt: c}
}

// included reports whether the go command compiles the file in this
// configuration: go/build's MatchFile (file name suffixes, //go:build or
// // +build lines), minus the files go/build's Import ignores after
// matching: cgo files when cgo is off, cgo in test files, and package
// "documentation". A file MatchFile cannot read is invalid, so not compiled.
func (bc *buildConfig) included(relpath string, src []byte, f *ast.File) bool {
	c := bc.ctxt
	c.OpenFile = func(string) (io.ReadCloser, error) { return io.NopCloser(bytes.NewReader(src)), nil }
	dir, name := path.Split(relpath)
	if ok, err := c.MatchFile(dir, name); !ok || err != nil {
		return false
	}
	if f.Name.Name == "documentation" {
		return false
	}
	for _, im := range f.Imports {
		if p, err := strconv.Unquote(im.Path.Value); err == nil && p == "C" {
			if !c.CgoEnabled || strings.HasSuffix(name, "_test.go") {
				return false
			}
		}
	}
	return true
}

// constraintText returns the build constraint that controls the file, as
// go/build reads it: the //go:build line, else the // +build lines of the
// header, ANDed. Parsed expressions are printed in //go:build syntax.
func constraintText(src []byte) string {
	header, goBuild, _ := fileHeader(src)
	if goBuild != nil {
		if x, err := constraint.Parse(string(goBuild)); err == nil {
			return x.String()
		}
		return strings.TrimSpace(string(goBuild[len("//go:build"):]))
	}
	var expr constraint.Expr
	for _, line := range bytes.Split(header, []byte("\n")) {
		line = bytes.TrimSpace(line)
		if !bytes.HasPrefix(line, slashSlash) || !bytes.Contains(line, []byte("+build")) || !constraint.IsPlusBuild(string(line)) {
			continue
		}
		x, err := constraint.Parse(string(line))
		if err != nil {
			continue // go/build skips it too
		}
		if expr == nil {
			expr = x
		} else {
			expr = &constraint.AndExpr{X: expr, Y: x}
		}
	}
	if expr == nil {
		return ""
	}
	return expr.String()
}

var (
	slashSlash         = []byte("//")
	slashStar          = []byte("/*")
	starSlash          = []byte("*/")
	goBuildComment     = []byte("//go:build")
	errMultipleGoBuild = errors.New("multiple //go:build comments")
)

// fileHeader is go/build's parseFileHeader (go1.27.2, src/go/build/build.go):
// it returns the leading run of comments and blank lines up to the last blank
// line before the first non-comment line (where // +build lines count), and
// the //go:build line. On a second //go:build line it returns the first one
// and an error; go/build then rejects the file.
func fileHeader(content []byte) (trimmed, goBuild []byte, err error) {
	end := 0
	p := content
	ended := false       // found non-blank, non-// line, so stopped accepting //go:build lines
	inSlashStar := false // in /* */ comment

Lines:
	for len(p) > 0 {
		line := p
		if i := bytes.IndexByte(line, '\n'); i >= 0 {
			line, p = line[:i], p[i+1:]
		} else {
			p = p[len(p):]
		}
		line = bytes.TrimSpace(line)
		if len(line) == 0 && !ended { // Blank line
			end = len(content) - len(p)
			continue Lines
		}
		if !bytes.HasPrefix(line, slashSlash) { // Not comment line
			ended = true
		}

		if !inSlashStar && isGoBuildComment(line) {
			if goBuild != nil {
				return content[:end], goBuild, errMultipleGoBuild
			}
			goBuild = line
		}

	Comments:
		for len(line) > 0 {
			if inSlashStar {
				if i := bytes.Index(line, starSlash); i >= 0 {
					inSlashStar = false
					line = bytes.TrimSpace(line[i+len(starSlash):])
					continue Comments
				}
				continue Lines
			}
			if bytes.HasPrefix(line, slashSlash) {
				continue Lines
			}
			if bytes.HasPrefix(line, slashStar) {
				inSlashStar = true
				line = bytes.TrimSpace(line[len(slashStar):])
				continue Comments
			}
			// Found non-comment text.
			break Lines
		}
	}

	return content[:end], goBuild, nil
}

func isGoBuildComment(line []byte) bool {
	if !bytes.HasPrefix(line, goBuildComment) {
		return false
	}
	line = bytes.TrimSpace(line)
	rest := line[len(goBuildComment):]
	return len(rest) == 0 || len(bytes.TrimSpace(rest)) < len(rest)
}
