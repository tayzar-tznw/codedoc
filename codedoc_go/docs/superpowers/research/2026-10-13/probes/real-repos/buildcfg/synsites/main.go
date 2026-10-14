// Command synsites emits syntax-only call sites (callee token positions) for the files on stdin,
// in the helper's sites.jsonl shape, so that driver.py can query gopls at them.
package main

import (
	"bufio"
	"encoding/json"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
	"unicode/utf8"
)

type site struct {
	File    string `json:"file"`
	CallOff int    `json:"call_off"`
	Line    int    `json:"line"`
	Col     int    `json:"col"`
	Char16  int    `json:"char16"`
	Class   string `json:"class"`
	Shape   string `json:"shape"`
	Name    string `json:"name"`
}

func main() {
	root := os.Args[1]
	fset := token.NewFileSet()
	enc := json.NewEncoder(os.Stdout)
	sc := bufio.NewScanner(os.Stdin)
	for sc.Scan() {
		path := strings.TrimSpace(sc.Text())
		src, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		f, err := parser.ParseFile(fset, path, src, parser.SkipObjectResolution)
		if f == nil {
			continue
		}
		rel, _ := filepath.Rel(root, path)
		ast.Inspect(f, func(n ast.Node) bool {
			c, ok := n.(*ast.CallExpr)
			if !ok {
				return true
			}
			fun := ast.Unparen(c.Fun)
			tok := fun.Pos()
			shape, name := "other", ""
			switch x := fun.(type) {
			case *ast.Ident:
				shape, name = "ident", x.Name
			case *ast.SelectorExpr:
				shape, name, tok = "selector", x.Sel.Name, x.Sel.Pos()
			}
			p := fset.Position(tok)
			ls := p.Offset - (p.Column - 1)
			n16 := 0
			for seg := src[ls:p.Offset]; len(seg) > 0; {
				r, sz := utf8.DecodeRune(seg)
				if r >= 0x10000 {
					n16 += 2
				} else {
					n16++
				}
				seg = seg[sz:]
			}
			enc.Encode(site{File: filepath.ToSlash(rel), CallOff: fset.Position(c.Lparen).Offset, Line: p.Line, Col: p.Column, Char16: n16, Class: "syntax-only", Shape: shape, Name: name})
			return true
		})
	}
}
