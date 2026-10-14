// Command countcalls parses Go files (syntax only) and prints, per file, the number of
// *ast.CallExpr, the //go:build expression and whether the file parsed.
package main

import (
	"bufio"
	"fmt"
	"go/ast"
	"go/build/constraint"
	"go/parser"
	"go/token"
	"os"
	"strings"
)

func main() {
	fset := token.NewFileSet()
	sc := bufio.NewScanner(os.Stdin)
	for sc.Scan() {
		path := strings.TrimSpace(sc.Text())
		if path == "" {
			continue
		}
		f, err := parser.ParseFile(fset, path, nil, parser.ParseComments|parser.SkipObjectResolution)
		n := 0
		build := ""
		if f != nil {
			ast.Inspect(f, func(x ast.Node) bool {
				if _, ok := x.(*ast.CallExpr); ok {
					n++
				}
				return true
			})
			for _, cg := range f.Comments {
				if cg.Pos() > f.Package {
					break
				}
				for _, c := range cg.List {
					if constraint.IsGoBuild(c.Text) {
						build = strings.TrimPrefix(c.Text, "//go:build ")
					}
				}
			}
		}
		ok := "ok"
		if err != nil {
			ok = "parse-error"
		}
		fmt.Printf("%s\t%d\t%s\t%s\n", path, n, ok, build)
	}
}
