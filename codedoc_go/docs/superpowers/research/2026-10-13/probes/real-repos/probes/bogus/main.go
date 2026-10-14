// Probe: how does an in-repo call resolve to an object in a stub package offline?
package main

import (
	"fmt"
	"go/ast"
	"go/types"
	"os"
	"strings"

	"golang.org/x/tools/go/packages"
)

func main() {
	cfg := &packages.Config{Mode: packages.NeedName | packages.NeedFiles | packages.NeedSyntax | packages.NeedTypes |
		packages.NeedTypesInfo | packages.NeedDeps | packages.NeedImports | packages.NeedModule, Dir: os.Args[1], Tests: len(os.Args) > 3}
	pkgs, err := packages.Load(cfg, os.Args[2])
	if err != nil {
		panic(err)
	}
	packages.Visit(pkgs, nil, func(p *packages.Package) {
		if strings.Contains(p.PkgPath, "logutil") || p.PkgPath == "go.uber.org/zap" {
			fmt.Printf("PKG id=%s path=%s name=%s mod=%v files=%v types=%p typesPath=%s nerr=%d\n", p.ID, p.PkgPath, p.Name, p.Module != nil, p.GoFiles, p.Types, p.Types.Path(), len(p.Errors))
			if obj := p.Types.Scope().Lookup("CreateDefaultZapLogger"); obj != nil {
				fmt.Printf("   scope CreateDefaultZapLogger pkg=%s\n", obj.Pkg().Path())
			}
			for path, imp := range p.Imports {
				if path == "go.uber.org/zap" {
					fmt.Printf("   import %s -> id=%s types=%p\n", path, imp.ID, imp.Types)
				}
			}
		}
	})
	for _, p := range pkgs {
		if p.PkgPath != "go.etcd.io/etcd/client/v3" {
			continue
		}
		for _, f := range p.Syntax {
			ast.Inspect(f, func(n ast.Node) bool {
				if sel, ok := n.(*ast.SelectorExpr); ok && sel.Sel.Name == "CreateDefaultZapLogger" {
					obj := p.TypesInfo.Uses[sel.Sel]
					x := sel.X.(*ast.Ident)
					pn := p.TypesInfo.Uses[x]
					fmt.Printf("USE in %s: obj=%v pkg=%s ; qualifier %s -> %v", p.ID, obj, obj.Pkg().Path(), x.Name, pn)
					if pn, ok := pn.(*types.PkgName); ok {
						fmt.Printf(" imported=%p path=%s", pn.Imported(), pn.Imported().Path())
					}
					fmt.Println()
				}
				return true
			})
		}
	}
}
