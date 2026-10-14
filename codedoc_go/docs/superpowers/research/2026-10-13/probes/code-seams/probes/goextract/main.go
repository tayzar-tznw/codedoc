// goextract is a probe for the codedoc_go extractor decision (option b):
// go/parser + go/ast, standard library only. It reads file paths (one per
// line) on stdin and writes one JSON object per file on stdout: parse
// errors, package clause, build constraints, generated header, imports,
// declarations with name ranges, call sites with callee-token ranges,
// receivers, embedded fields, interface methods and type parameters.
//
// Positions are 1-based lines and 0-based BYTE columns (go/token columns
// minus one), so they compare directly with tree-sitter points.
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"go/ast"
	"go/build"
	"go/build/constraint"
	"go/parser"
	"go/scanner"
	"go/token"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type Pos struct {
	Line    int `json:"line"`
	Col     int `json:"col"`
	EndLine int `json:"end_line"`
	EndCol  int `json:"end_col"`
}

type Decl struct {
	Kind     string `json:"k"` // func|method|type|alias|var|const|field|embed|imethod|iembed|tparam
	Name     string `json:"name"`
	Parent   string `json:"parent,omitempty"` // type for field/embed/imethod/iembed; func/type for tparam
	Recv     string `json:"recv,omitempty"`   // receiver type text: T, *T, T[K], *T[K, V]
	RecvBase string `json:"recv_base,omitempty"`
	Ptr      bool   `json:"ptr,omitempty"`
	Generic  bool   `json:"generic,omitempty"` // declares type params, or receiver of a generic type
	Qual     string `json:"q,omitempty"`       // proposed qualname (funcs, methods, types)
	Pos
}

type Call struct {
	Name   string `json:"name"`           // callee token text ("" when there is none)
	Shape  string `json:"shape"`          // ident|selector|funclit|call|typelit|star|paren-other|other
	Inst   bool   `json:"inst,omitempty"` // callee wrapped in an index expression (instantiation or index)
	Caller string `json:"caller"`         // proposed qualname of the code that runs the call
	Recv   string `json:"recv,omitempty"` // selector operand when it is a plain identifier
	Line   int    `json:"line"`
	Col    int    `json:"col"`
	EndCol int    `json:"end_col"`
}

type Import struct {
	Path string `json:"path"`
	Name string `json:"name,omitempty"`
	Pos
}

type ErrItem struct {
	Line int    `json:"line"`
	Col  int    `json:"col"`
	Msg  string `json:"msg"`
}

type FileOut struct {
	Path       string    `json:"path"`
	Bytes      int       `json:"bytes"`
	ParseUS    int64     `json:"parse_us"`
	ExtractUS  int64     `json:"extract_us"`
	NErrors    int       `json:"nerrors"`
	Errors     []ErrItem `json:"errors,omitempty"`
	Package    string    `json:"pkg"`
	PkgLine    int       `json:"pkg_line"`
	PkgCol     int       `json:"pkg_col"`
	Generated  bool      `json:"generated"`
	GoBuild    string    `json:"go_build,omitempty"`
	PlusBuild  []string  `json:"plus_build,omitempty"`
	MatchLinux *bool     `json:"match_linux_amd64,omitempty"`
	Imports    []Import  `json:"imports"`
	Decls      []Decl    `json:"decls"`
	Calls      []Call    `json:"calls"`
	FuncLits   int       `json:"funclits"`
	Inits      int       `json:"inits"`
	Blanks     int       `json:"blank_funcs"`
	VarLits    int       `json:"pkgvar_funclits"`
	LocalTypes int       `json:"local_types"`
}

var fset = token.NewFileSet()

func pos(p token.Pos) (int, int) {
	if !p.IsValid() {
		return 0, -1
	}
	position := fset.Position(p)
	return position.Line, position.Column - 1
}

func span(n ast.Node) Pos {
	l, c := pos(n.Pos())
	el, ec := pos(n.End())
	return Pos{l, c, el, ec}
}

func text(src []byte, n ast.Node) string {
	f := fset.File(n.Pos())
	if f == nil {
		return ""
	}
	s, e := f.Offset(n.Pos()), f.Offset(n.End())
	if s < 0 || e > len(src) || s > e {
		return ""
	}
	return string(src[s:e])
}

func unparen(e ast.Expr) ast.Expr {
	for {
		p, ok := e.(*ast.ParenExpr)
		if !ok {
			return e
		}
		e = p.X
	}
}

// recvBase returns the receiver base type name, pointer flag and generic flag.
func recvBase(e ast.Expr) (string, bool, bool) {
	ptr := false
	e = unparen(e)
	if s, ok := e.(*ast.StarExpr); ok {
		ptr = true
		e = unparen(s.X)
	}
	generic := false
	switch x := e.(type) {
	case *ast.IndexExpr:
		generic = true
		e = x.X
	case *ast.IndexListExpr:
		generic = true
		e = x.X
	}
	if id, ok := e.(*ast.Ident); ok {
		return id.Name, ptr, generic
	}
	return "?", ptr, generic
}

// typeName returns the last identifier of an embedded type expression and its node.
func typeName(e ast.Expr) (string, ast.Node) {
	e = unparen(e)
	if s, ok := e.(*ast.StarExpr); ok {
		e = unparen(s.X)
	}
	switch x := e.(type) {
	case *ast.IndexExpr:
		e = x.X
	case *ast.IndexListExpr:
		e = x.X
	}
	switch x := e.(type) {
	case *ast.Ident:
		return x.Name, x
	case *ast.SelectorExpr:
		return x.Sel.Name, x.Sel
	}
	return "", nil
}

type walker struct {
	src    []byte
	out    *FileOut
	inits  int
	blanks int
	stack  []string // qualname of the code that runs at this point
	lits   []int    // closure counters per stack level
}

func (w *walker) push(q string) {
	w.stack = append(w.stack, q)
	w.lits = append(w.lits, 0)
}

func (w *walker) pop() {
	w.stack = w.stack[:len(w.stack)-1]
	w.lits = w.lits[:len(w.lits)-1]
}

func (w *walker) caller() string {
	if len(w.stack) == 0 {
		return "<pkg>"
	}
	return w.stack[len(w.stack)-1]
}

// calleeToken returns the token a definition request would be asked at.
func calleeToken(fun ast.Expr) (string, ast.Node, string, bool) {
	e := unparen(fun)
	inst := false
	switch x := e.(type) {
	case *ast.IndexExpr:
		inst = true
		e = unparen(x.X)
	case *ast.IndexListExpr:
		inst = true
		e = unparen(x.X)
	}
	switch x := e.(type) {
	case *ast.Ident:
		return x.Name, x, "ident", inst
	case *ast.SelectorExpr:
		return x.Sel.Name, x.Sel, "selector", inst
	case *ast.FuncLit:
		return "", nil, "funclit", inst
	case *ast.CallExpr:
		return "", nil, "call", inst
	case *ast.ArrayType, *ast.MapType, *ast.ChanType, *ast.FuncType, *ast.InterfaceType, *ast.StructType:
		return "", nil, "typelit", inst
	case *ast.StarExpr:
		return "", nil, "star", inst
	}
	return "", nil, "other", inst
}

func (w *walker) expr(n ast.Node) {
	ast.Inspect(n, func(node ast.Node) bool {
		switch x := node.(type) {
		case *ast.FuncLit:
			// runtime-style closure names: F.func1, nested F.func1.1
			w.out.FuncLits++
			i := len(w.lits) - 1
			w.lits[i]++
			parent := w.stack[i]
			name := fmt.Sprintf("%s.func%d", parent, w.lits[i])
			w.push(name)
			w.expr(x.Body)
			w.pop()
			return false
		case *ast.DeclStmt:
			if g, ok := x.Decl.(*ast.GenDecl); ok && g.Tok == token.TYPE {
				w.out.LocalTypes += len(g.Specs)
			}
		case *ast.CallExpr:
			name, tok, shape, inst := calleeToken(x.Fun)
			c := Call{Name: name, Shape: shape, Inst: inst, Caller: w.caller()}
			if shape == "selector" {
				e := unparen(x.Fun)
				switch ie := e.(type) {
				case *ast.IndexExpr:
					e = unparen(ie.X)
				case *ast.IndexListExpr:
					e = unparen(ie.X)
				}
				if sel, ok := e.(*ast.SelectorExpr); ok {
					if id, ok := unparen(sel.X).(*ast.Ident); ok {
						c.Recv = id.Name
					}
				}
			}
			if tok != nil {
				c.Line, c.Col = pos(tok.Pos())
				_, c.EndCol = pos(tok.End())
			} else {
				c.Line, c.Col = pos(x.Lparen)
				c.EndCol = c.Col
			}
			w.out.Calls = append(w.out.Calls, c)
		}
		return true
	})
}

func (w *walker) fields(parent string, kind string, list *ast.FieldList) {
	if list == nil {
		return
	}
	for _, f := range list.List {
		if len(f.Names) == 0 {
			name, node := typeName(f.Type)
			k := "embed"
			if kind == "interface" {
				k = "iembed"
			}
			d := Decl{Kind: k, Name: name, Parent: parent}
			if node != nil {
				d.Pos = span(node)
			} else {
				d.Pos = span(f.Type)
			}
			w.out.Decls = append(w.out.Decls, d)
			continue
		}
		for _, id := range f.Names {
			k := "field"
			if kind == "interface" {
				k = "imethod"
			}
			d := Decl{Kind: k, Name: id.Name, Parent: parent, Pos: span(id)}
			if k == "imethod" {
				d.Qual = parent + "." + id.Name
			}
			w.out.Decls = append(w.out.Decls, d)
		}
	}
}

func (w *walker) tparams(parent string, list *ast.FieldList) bool {
	if list == nil || len(list.List) == 0 {
		return false
	}
	for _, f := range list.List {
		for _, id := range f.Names {
			w.out.Decls = append(w.out.Decls, Decl{Kind: "tparam", Name: id.Name, Parent: parent, Pos: span(id)})
		}
	}
	return true
}

func (w *walker) typeBody(name string, t ast.Expr) {
	switch x := t.(type) {
	case *ast.StructType:
		w.fields(name, "struct", x.Fields)
	case *ast.InterfaceType:
		w.fields(name, "interface", x.Methods)
	}
	// anonymous struct/interface types nested inside (fields of struct type) are not descended
}

func (w *walker) file(f *ast.File) {
	for _, decl := range f.Decls {
		switch d := decl.(type) {
		case *ast.FuncDecl:
			q := d.Name.Name
			dd := Decl{Kind: "func", Name: d.Name.Name, Pos: span(d.Name)}
			if d.Recv != nil && len(d.Recv.List) > 0 {
				base, ptr, generic := recvBase(d.Recv.List[0].Type)
				dd.Kind = "method"
				dd.Recv = text(w.src, d.Recv.List[0].Type)
				dd.RecvBase, dd.Ptr, dd.Generic = base, ptr, generic
				q = base + "." + d.Name.Name
				if d.Name.Name == "_" {
					w.blanks++
					q = fmt.Sprintf("%s._#%d", base, w.blanks)
				}
			} else if d.Name.Name == "init" {
				w.inits++
				q = fmt.Sprintf("init#%d", w.inits)
				w.out.Inits++
			} else if d.Name.Name == "_" {
				w.blanks++
				q = fmt.Sprintf("_#%d", w.blanks)
				w.out.Blanks++
			}
			if w.tparams(q, d.Type.TypeParams) {
				dd.Generic = true
			}
			dd.Qual = q
			w.out.Decls = append(w.out.Decls, dd)
			// parameters/results may hold func literals? (default values do not exist in Go)
			if d.Body != nil {
				w.push(q)
				w.expr(d.Body)
				w.pop()
			}
		case *ast.GenDecl:
			for _, spec := range d.Specs {
				switch s := spec.(type) {
				case *ast.TypeSpec:
					k := "type"
					if s.Assign.IsValid() {
						k = "alias"
					}
					dd := Decl{Kind: k, Name: s.Name.Name, Pos: span(s.Name), Qual: s.Name.Name}
					if w.tparams(s.Name.Name, s.TypeParams) {
						dd.Generic = true
					}
					w.out.Decls = append(w.out.Decls, dd)
					w.typeBody(s.Name.Name, s.Type)
				case *ast.ValueSpec:
					k := "var"
					if d.Tok == token.CONST {
						k = "const"
					}
					for _, id := range s.Names {
						w.out.Decls = append(w.out.Decls, Decl{Kind: k, Name: id.Name, Pos: span(id)})
					}
					before := w.out.FuncLits
					w.push("<init>")
					for _, v := range s.Values {
						w.expr(v)
					}
					w.pop()
					w.out.VarLits += w.out.FuncLits - before
				}
			}
		}
	}
}

func constraints(f *ast.File) (string, []string) {
	goBuild := ""
	var plus []string
	for _, cg := range f.Comments {
		if cg.Pos() > f.Package {
			break
		}
		for _, c := range cg.List {
			if constraint.IsGoBuild(c.Text) {
				goBuild = strings.TrimSpace(strings.TrimPrefix(c.Text, "//go:build"))
			} else if constraint.IsPlusBuild(c.Text) {
				plus = append(plus, strings.TrimSpace(strings.TrimPrefix(c.Text, "// +build")))
			}
		}
	}
	return goBuild, plus
}

func main() {
	repeat := flag.Int("repeat", 1, "parse every file this many times (timing only on the last pass)")
	parseOnly := flag.Bool("parse-only", false, "only parse; print the summary")
	flag.Parse()
	ctx := build.Default
	ctx.GOOS, ctx.GOARCH, ctx.CgoEnabled = "linux", "amd64", true

	var paths []string
	sc := bufio.NewScanner(os.Stdin)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	for sc.Scan() {
		if p := strings.TrimSpace(sc.Text()); p != "" {
			paths = append(paths, p)
		}
	}
	srcs := make([][]byte, len(paths))
	var total int
	for i, p := range paths {
		b, err := os.ReadFile(p)
		if err != nil {
			fmt.Fprintln(os.Stderr, "read:", err)
			os.Exit(1)
		}
		srcs[i] = b
		total += len(b)
	}
	enc := json.NewEncoder(bufio.NewWriterSize(os.Stdout, 1<<20))
	out := bufio.NewWriterSize(os.Stdout, 1<<20)
	enc = json.NewEncoder(out)
	var parseTotal, extractTotal time.Duration
	errFiles := 0
	for r := 0; r < *repeat; r++ {
		parseTotal, extractTotal, errFiles = 0, 0, 0
		fset = token.NewFileSet()
		for i, p := range paths {
			t0 := time.Now()
			f, err := parser.ParseFile(fset, p, srcs[i], parser.ParseComments|parser.SkipObjectResolution|parser.AllErrors)
			dt := time.Since(t0)
			parseTotal += dt
			if err != nil {
				errFiles++
			}
			if *parseOnly || r < *repeat-1 {
				continue
			}
			t1 := time.Now()
			fo := &FileOut{Path: p, Bytes: len(srcs[i]), ParseUS: dt.Microseconds(), Imports: []Import{}, Decls: []Decl{}, Calls: []Call{}}
			if el, ok := err.(scanner.ErrorList); ok {
				fo.NErrors = len(el)
				for j, e := range el {
					if j >= 5 {
						break
					}
					fo.Errors = append(fo.Errors, ErrItem{e.Pos.Line, e.Pos.Column - 1, e.Msg})
				}
			} else if err != nil {
				fo.NErrors = 1
				fo.Errors = append(fo.Errors, ErrItem{0, 0, err.Error()})
			}
			if f != nil {
				fo.Package = f.Name.Name
				fo.PkgLine, fo.PkgCol = pos(f.Name.Pos())
				fo.Generated = ast.IsGenerated(f)
				fo.GoBuild, fo.PlusBuild = constraints(f)
				for _, im := range f.Imports {
					it := Import{Path: strings.Trim(im.Path.Value, "\"`"), Pos: span(im.Path)}
					if im.Name != nil {
						it.Name = im.Name.Name
					}
					fo.Imports = append(fo.Imports, it)
				}
				w := &walker{src: srcs[i], out: fo}
				w.file(f)
			}
			if ok, merr := ctx.MatchFile(filepath.Dir(p), filepath.Base(p)); merr == nil {
				fo.MatchLinux = &ok
			}
			extractTotal += time.Since(t1)
			fo.ExtractUS = time.Since(t1).Microseconds()
			if err := enc.Encode(fo); err != nil {
				fmt.Fprintln(os.Stderr, "encode:", err)
				os.Exit(1)
			}
		}
	}
	out.Flush()
	fmt.Fprintf(os.Stderr, "files=%d bytes=%d parse_ms=%.1f extract_ms=%.1f error_files=%d\n",
		len(paths), total, float64(parseTotal.Microseconds())/1000, float64(extractTotal.Microseconds())/1000, errFiles)
}
