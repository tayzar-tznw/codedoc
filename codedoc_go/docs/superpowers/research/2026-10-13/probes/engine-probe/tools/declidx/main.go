// declidx prints every declared name in the Go files under the given roots
// (build tags ignored) as JSON lines: file, 1-based line, 1-based byte
// column, kind and a runtime-style qualified name. It is the probe's stand-in
// for CodeDoc's tree-sitter declaration index.
package main

import (
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/mod/modfile"
)

type decl struct {
	File  string `json:"file"`
	Line  int    `json:"line"`
	Col   int    `json:"col"`
	Kind  string `json:"kind"`
	QName string `json:"qname"`
	Body  *bool  `json:"has_body,omitempty"`
	End   int    `json:"end_line,omitempty"`
}

var enc = json.NewEncoder(os.Stdout)

func modPath(dir string) (string, string) {
	for d := dir; ; d = filepath.Dir(d) {
		if b, err := os.ReadFile(filepath.Join(d, "go.mod")); err == nil {
			return d, modfile.ModulePath(b)
		}
		if filepath.Dir(d) == d {
			return "", ""
		}
	}
}

func recvName(fd *ast.FuncDecl) string {
	if fd.Recv == nil || len(fd.Recv.List) == 0 {
		return ""
	}
	t := fd.Recv.List[0].Type
	ptr := false
	if s, ok := t.(*ast.StarExpr); ok {
		ptr, t = true, s.X
	}
	switch x := t.(type) {
	case *ast.IndexExpr:
		t = x.X
	case *ast.IndexListExpr:
		t = x.X
	}
	name := t.(*ast.Ident).Name
	if ptr {
		return "(*" + name + ")"
	}
	return name
}

func main() {
	fset := token.NewFileSet()
	for _, root := range os.Args[1:] {
		filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
			if err != nil || d.IsDir() || !strings.HasSuffix(path, ".go") {
				return nil
			}
			f, err := parser.ParseFile(fset, path, nil, parser.SkipObjectResolution)
			if err != nil {
				fmt.Fprintln(os.Stderr, err)
				return nil
			}
			dir := filepath.Dir(path)
			mdir, mpath := modPath(dir)
			pkg := mpath
			if rel, _ := filepath.Rel(mdir, dir); rel != "." {
				pkg = mpath + "/" + filepath.ToSlash(rel)
			}
			if gr := os.Getenv("DECLIDX_GOROOT"); gr != "" && strings.HasPrefix(dir, gr+"/src/") {
				pkg = filepath.ToSlash(dir[len(gr)+5:])
			}
			if strings.Contains(path, "/vendor/") {
				pkg = filepath.ToSlash(dir[strings.LastIndex(dir, "/vendor/")+8:])
			}
			if f.Name.Name == "main" {
				pkg = "main"
			}
			if strings.HasSuffix(f.Name.Name, "_test") {
				pkg += "_test"
			}
			emit := func(id *ast.Ident, kind, q string, body *bool) {
				p := fset.Position(id.Pos())
				enc.Encode(decl{p.Filename, p.Line, p.Column, kind, q, body, 0})
			}
			var walkFunc func(scope string, n ast.Node)
			walkFunc = func(scope string, n ast.Node) {
				lit := 0
				ast.Inspect(n, func(m ast.Node) bool {
					switch x := m.(type) {
					case *ast.FuncLit:
						lit++
						p := fset.Position(x.Pos())
						q := fmt.Sprintf("%s.func@%d", scope, p.Line)
						enc.Encode(decl{p.Filename, p.Line, p.Column, "funclit", q, nil, fset.Position(x.End()).Line})
						for _, fl := range []*ast.FieldList{x.Type.Params, x.Type.Results} {
							if fl != nil {
								for _, fld := range fl.List {
									for _, nm := range fld.Names {
										emit(nm, "local", q+"."+nm.Name, nil)
									}
								}
							}
						}
					case *ast.AssignStmt:
						if x.Tok == token.DEFINE {
							for _, l := range x.Lhs {
								if id, ok := l.(*ast.Ident); ok {
									emit(id, "local", scope+"."+id.Name, nil)
								}
							}
						}
					case *ast.RangeStmt:
						if x.Tok == token.DEFINE {
							for _, l := range []ast.Expr{x.Key, x.Value} {
								if id, ok := l.(*ast.Ident); ok {
									emit(id, "local", scope+"."+id.Name, nil)
								}
							}
						}
					case *ast.DeclStmt:
						if gd, ok := x.Decl.(*ast.GenDecl); ok {
							for _, sp := range gd.Specs {
								if vs, ok := sp.(*ast.ValueSpec); ok {
									for _, nm := range vs.Names {
										emit(nm, "local", scope+"."+nm.Name, nil)
									}
								}
							}
						}
					case *ast.TypeSwitchStmt:
						if as, ok := x.Assign.(*ast.AssignStmt); ok {
							for _, l := range as.Lhs {
								if id, ok := l.(*ast.Ident); ok {
									emit(id, "local", scope+"."+id.Name, nil)
								}
							}
						}
					}
					return true
				})
			}
			initN := 0
			for _, dcl := range f.Decls {
				switch d := dcl.(type) {
				case *ast.FuncDecl:
					q := pkg + "." + d.Name.Name
					kind := "func"
					if r := recvName(d); r != "" {
						q, kind = pkg+"."+r+"."+d.Name.Name, "method"
					}
					if d.Name.Name == "init" && d.Recv == nil {
						q = fmt.Sprintf("%s.init#%s#%d", pkg, filepath.Base(path), initN)
						initN++
					}
					hb := d.Body != nil
					p := fset.Position(d.Name.Pos())
					enc.Encode(decl{p.Filename, p.Line, p.Column, kind, q, &hb, fset.Position(d.End()).Line})
					for _, fl := range []*ast.FieldList{d.Recv, d.Type.Params, d.Type.Results} {
						if fl != nil {
							for _, fld := range fl.List {
								for _, nm := range fld.Names {
									emit(nm, "local", q+"."+nm.Name, nil)
								}
							}
						}
					}
					if d.Body != nil {
						walkFunc(q, d.Body)
					}
				case *ast.GenDecl:
					for _, sp := range d.Specs {
						switch s := sp.(type) {
						case *ast.TypeSpec:
							tq := pkg + "." + s.Name.Name
							emit(s.Name, "type", tq, nil)
							switch t := s.Type.(type) {
							case *ast.InterfaceType:
								for _, m := range t.Methods.List {
									for _, nm := range m.Names {
										emit(nm, "ifacemethod", tq+"."+nm.Name, nil)
									}
								}
							case *ast.StructType:
								for _, fld := range t.Fields.List {
									for _, nm := range fld.Names {
										emit(nm, "field", tq+"."+nm.Name, nil)
									}
									if len(fld.Names) == 0 { // embedded
										et := fld.Type
										if st, ok := et.(*ast.StarExpr); ok {
											et = st.X
										}
										var id *ast.Ident
										switch e := et.(type) {
										case *ast.Ident:
											id = e
										case *ast.SelectorExpr:
											id = e.Sel
										}
										if id != nil {
											emit(id, "embedded", tq+"."+id.Name, nil)
										}
									}
								}
							}
						case *ast.ValueSpec:
							for i, nm := range s.Names {
								kind := "var"
								if d.Tok == token.CONST {
									kind = "const"
								}
								emit(nm, kind, pkg+"."+nm.Name, nil)
								if i < len(s.Values) {
									walkFunc(pkg+".init", s.Values[i])
								}
							}
						}
					}
				}
			}
			return nil
		})
	}
}
