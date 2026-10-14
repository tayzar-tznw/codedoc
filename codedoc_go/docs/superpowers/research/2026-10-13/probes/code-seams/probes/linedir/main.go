package main

import (
	"fmt"
	"go/parser"
	"go/token"
)

const src = "package p\n\n//line gram.y:100\nfunc F() { G() }\n\nfunc G() {}\n"

func main() {
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, "p.go", src, parser.ParseComments)
	if err != nil {
		panic(err)
	}
	for _, d := range f.Decls {
		p := d.Pos()
		fmt.Printf("Position (adjusted): %v   PositionFor(p, false): %v\n", fset.Position(p), fset.PositionFor(p, false))
	}
}
