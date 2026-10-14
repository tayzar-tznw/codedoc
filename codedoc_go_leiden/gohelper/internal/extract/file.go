package extract

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"go/ast"
	"go/parser"
	"go/scanner"
	"go/token"
	"path"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"
)

// module is the go.mod that owns a file: the deepest one above it.
type module struct {
	root string // repo-relative POSIX directory of go.mod ("" = repo root)
	path string // module path; "" when go.mod has no module line
}

// fileResult is one file's extraction plus what owner resolution needs.
type fileResult struct {
	relpath string
	record  *FileRecord // nil when err is set
	err     string
	dir     string          // POSIX directory of relpath ("" = repo root)
	types   map[string]bool // package-level type names in the record
	recvs   []*ReceiverRecord
}

// receiverTextMax caps receiver texts: a chained call's receiver can hold
// whole closures.
const receiverTextMax = 200

// extractSource builds the FileRecord of one file. owner_relpath is filled
// later by resolveOwners, across the files of the same directory and package
// clause.
func extractSource(relpath string, src []byte, bc *buildConfig, mod *module) *fileResult {
	res := &fileResult{relpath: relpath, dir: path.Dir(relpath), types: map[string]bool{}}
	if res.dir == "." {
		res.dir = ""
	}
	fset := token.NewFileSet()
	f, perr := parser.ParseFile(fset, relpath, src, parseMode)
	var tf *token.File
	fset.Iterate(func(x *token.File) bool { tf = x; return false })
	tf.SetLinesForContent(src)
	errLines, firstMsg := errorLines(tf, perr)
	if !f.Package.IsValid() {
		res.err = "cannot parse the package clause"
		if firstMsg != "" {
			res.err = firstMsg
		}
		return res
	}
	sum := sha256.Sum256(src)
	rec := &FileRecord{
		SHA256:          hex.EncodeToString(sum[:]),
		PackageName:     f.Name.Name,
		Origin:          origin(relpath, f.Name.Name),
		Generated:       ast.IsGenerated(f),
		BuildConstraint: constraintText(src),
		BuildIncluded:   bc.included(relpath, src, f),
		SyntaxErrors:    errLines,
		Imports:         []ImportRecord{},
		Types:           []TypeRecord{},
		Funcs:           []*FuncRecord{},
		Conversions:     []int{},
	}
	if mod != nil && mod.path != "" {
		rec.Module = mod.path
		rec.Package = importPath(mod, res.dir, rec.Origin == "xtest")
	}
	x := &extractor{
		tf: tf, src: src, rec: rec, res: res,
		typeQ: map[string]bool{}, methodQ: map[string]bool{}, blanks: map[string]int{},
		linknames: linknames(f), initFrame: &frame{},
	}
	if len(errLines) > 0 {
		x.firstErr = errLines[0]
	}
	x.walker = walker{site: x.onSite, enter: x.enterLit, leave: x.leaveLit}
	x.file(f)
	res.record = rec
	return res
}

// errorLines returns the sorted, unique lines of the parse errors and the
// message of the earliest one. Both come from byte offsets: go/parser sorts
// its errors by //line-adjusted positions.
func errorLines(tf *token.File, perr error) ([]int, string) {
	lines := []int{}
	el, ok := perr.(scanner.ErrorList)
	if !ok || len(el) == 0 {
		return lines, ""
	}
	seen := map[int]bool{}
	first := el[0]
	for _, e := range el {
		if e.Pos.Offset < first.Pos.Offset {
			first = e
		}
		if l := tf.PositionFor(tf.Pos(min(max(e.Pos.Offset, 0), tf.Size())), false).Line; !seen[l] {
			seen[l] = true
			lines = append(lines, l)
		}
	}
	sort.Ints(lines)
	p := tf.PositionFor(tf.Pos(min(max(first.Pos.Offset, 0), tf.Size())), false)
	return lines, fmt.Sprintf("%s:%d:%d: %s", tf.Name(), p.Line, p.Column, first.Msg)
}

func origin(relpath, clause string) string {
	test := strings.HasSuffix(path.Base(relpath), "_test.go")
	switch {
	case test && strings.HasSuffix(clause, "_test"):
		return "xtest"
	case test:
		return "test"
	case clause == "main":
		return "command"
	}
	return "package"
}

// importPath is the module path plus the directory below the module root;
// an external test package gets the suffix "_test".
func importPath(mod *module, dir string, xtest bool) string {
	p := mod.path
	switch {
	case dir == mod.root:
	case mod.root == "":
		p += "/" + dir
	default:
		p += "/" + strings.TrimPrefix(dir, mod.root+"/")
	}
	if xtest {
		p += "_test"
	}
	return p
}

// linknames maps local names to targets of two-argument //go:linkname
// directives (the one-argument form names no target).
func linknames(f *ast.File) map[string]string {
	m := map[string]string{}
	for _, cg := range f.Comments {
		for _, c := range cg.List {
			if rest, ok := strings.CutPrefix(c.Text, "//go:linkname "); ok {
				if fields := strings.Fields(rest); len(fields) == 2 {
					if _, dup := m[fields[0]]; !dup {
						m[fields[0]] = fields[1]
					}
				}
			}
		}
	}
	return m
}

// frame is a function whose body is being walked.
type frame struct {
	fn   *FuncRecord // nil for <init> until it gets code
	lits int         // func literals directly nested so far
}

type extractor struct {
	tf        *token.File
	src       []byte
	rec       *FileRecord
	res       *fileResult
	firstErr  int             // first syntax-error line; 0 = none
	typeQ     map[string]bool // claimed type qualnames
	methodQ   map[string]bool // claimed function, closure and interface-method qualnames
	inits     int
	blanks    map[string]int // blank functions per receiver base ("" = none)
	linknames map[string]string
	walker    walker
	frames    []*frame
	initFrame *frame         // <init>, shared by all package-level var specs
	initSpec  *ast.ValueSpec // the package-level var spec being walked
	initUsed  bool           // initSpec contributed a site or closure to <init>
}

func (x *extractor) file(f *ast.File) {
	x.rec.Types = append(x.rec.Types, TypeRecord{
		Name: "<package>", Qualname: x.claim(x.typeQ, "<package>"), Kind: "package",
		Embeds: []EmbedRecord{}, Methods: []MethodRecord{},
		NameRange: x.nameRange(f.Name.Pos(), f.Name.End()), StartLine: 1, EndLine: x.tf.LineCount(),
		Doc: f.Doc.Text(),
	})
	x.imports(f)
	for _, decl := range f.Decls {
		switch d := decl.(type) {
		case *ast.FuncDecl:
			if x.omitted(d.Pos()) {
				continue
			}
			fn := x.funcDecl(d)
			if d.Body != nil {
				x.frames = append(x.frames, &frame{fn: fn})
				x.walker.walk(d.Body)
				x.frames = x.frames[:len(x.frames)-1]
			}
		case *ast.GenDecl:
			for _, spec := range d.Specs {
				switch s := spec.(type) {
				case *ast.TypeSpec:
					if !x.omitted(s.Pos()) {
						x.typeSpec(d, s)
					}
				case *ast.ValueSpec:
					if d.Tok == token.VAR && len(s.Values) > 0 && !x.omitted(s.Pos()) {
						x.varSpec(s)
					}
				}
			}
		}
	}
	for _, fn := range x.rec.Funcs {
		sort.SliceStable(fn.Sites, func(i, j int) bool { return fn.Sites[i].Offset < fn.Sites[j].Offset })
	}
	sort.Ints(x.rec.Conversions)
}

func (x *extractor) imports(f *ast.File) {
	for _, im := range f.Imports {
		if x.omitted(im.Pos()) {
			continue
		}
		p, err := strconv.Unquote(im.Path.Value)
		if err != nil {
			p = strings.Trim(im.Path.Value, "\"`")
		}
		r := ImportRecord{Path: p, Kind: "normal"}
		if im.Name != nil {
			r.Alias = im.Name.Name
			switch r.Alias {
			case ".":
				r.Kind = "dot"
			case "_":
				r.Kind = "blank"
			}
		}
		pos := x.tf.PositionFor(im.Path.Pos(), false)
		r.Line, r.Col, r.Offset = pos.Line, pos.Column, pos.Offset
		x.rec.Imports = append(x.rec.Imports, r)
	}
}

func (x *extractor) funcDecl(d *ast.FuncDecl) *FuncRecord {
	name := d.Name.Name
	fn := &FuncRecord{
		Name: name, MemberKind: "func", TypeParams: x.fieldsText(d.Type.TypeParams),
		Signature: x.oneLine(d.Pos(), d.Type.End()), HasBody: d.Body != nil,
		NameRange: x.nameRange(d.Name.Pos(), d.Name.End()), StartLine: x.line(d.Pos()), EndLine: x.line(d.End()),
		Doc: d.Doc.Text(), Sites: []SiteRecord{},
	}
	var q string
	switch {
	case d.Recv != nil && len(d.Recv.List) > 0:
		fn.MemberKind = "method"
		fn.Receiver = x.receiver(d.Recv.List[0].Type)
		if name == "_" {
			x.blanks[fn.Receiver.Base]++
			q = fmt.Sprintf("%s._#%d", fn.Receiver.Base, x.blanks[fn.Receiver.Base])
		} else {
			q = fn.Receiver.Base + "." + name
		}
	case name == "init":
		fn.MemberKind = "init"
		x.inits++
		q = "init#" + strconv.Itoa(x.inits)
	case name == "_":
		x.blanks[""]++
		q = "_#" + strconv.Itoa(x.blanks[""])
	default:
		q = name
		if target, ok := x.linknames[name]; ok {
			fn.Linkname = &target
		}
	}
	fn.Qualname = x.claim(x.methodQ, q)
	x.rec.Funcs = append(x.rec.Funcs, fn)
	return fn
}

// receiver: base is the type name without pointer, parentheses and type
// parameters.
func (x *extractor) receiver(t ast.Expr) *ReceiverRecord {
	r := &ReceiverRecord{Text: x.oneLine(t.Pos(), t.End())}
	e := ast.Unparen(t)
	if s, ok := e.(*ast.StarExpr); ok {
		r.Pointer = true
		e = ast.Unparen(s.X)
	}
	switch ix := e.(type) {
	case *ast.IndexExpr:
		e = ix.X
	case *ast.IndexListExpr:
		e = ix.X
	}
	if id, ok := ast.Unparen(e).(*ast.Ident); ok {
		r.Base = id.Name
	} else {
		r.Base = x.oneLine(e.Pos(), e.End())
	}
	x.res.recvs = append(x.res.recvs, r)
	return r
}

func (x *extractor) typeSpec(g *ast.GenDecl, s *ast.TypeSpec) {
	kind := "named"
	switch ast.Unparen(s.Type).(type) {
	case *ast.StructType:
		kind = "struct"
	case *ast.InterfaceType:
		kind = "interface"
	}
	if s.Assign.IsValid() {
		kind = "alias"
	}
	start := s.Pos()
	if !g.Lparen.IsValid() {
		start = g.Pos() // the type keyword
	}
	doc := s.Doc
	if doc == nil && len(g.Specs) == 1 {
		doc = g.Doc
	}
	ty := TypeRecord{
		Name: s.Name.Name, Qualname: x.claim(x.typeQ, s.Name.Name), Kind: kind,
		TypeParams: x.fieldsText(s.TypeParams), Embeds: []EmbedRecord{}, Methods: []MethodRecord{},
		NameRange: x.nameRange(s.Name.Pos(), s.Name.End()), StartLine: x.line(start), EndLine: x.line(s.End()),
		Doc: doc.Text(),
	}
	switch t := ast.Unparen(s.Type).(type) {
	case *ast.StructType:
		for _, fld := range fieldList(t.Fields) {
			if len(fld.Names) == 0 && !x.omitted(fld.Pos()) {
				if e, ok := x.embed(fld.Type); ok {
					ty.Embeds = append(ty.Embeds, e)
				}
			}
		}
	case *ast.InterfaceType:
		for _, fld := range fieldList(t.Methods) {
			if x.omitted(fld.Pos()) {
				continue
			}
			ft, isMethod := fld.Type.(*ast.FuncType)
			if len(fld.Names) == 0 {
				if e, ok := x.embed(fld.Type); ok {
					ty.Embeds = append(ty.Embeds, e)
				}
				continue
			}
			if !isMethod {
				continue
			}
			doc := fld.Doc
			if doc == nil {
				doc = fld.Comment
			}
			for _, n := range fld.Names {
				ty.Methods = append(ty.Methods, MethodRecord{
					Name: n.Name, Qualname: x.claim(x.methodQ, ty.Qualname+"."+n.Name),
					NameRange: x.nameRange(n.Pos(), n.End()), Signature: x.oneLine(n.Pos(), ft.End()),
					Doc: doc.Text(), StartLine: x.line(fld.Pos()), EndLine: x.line(fld.End()),
				})
			}
		}
	}
	x.res.types[s.Name.Name] = true
	x.rec.Types = append(x.rec.Types, ty)
}

func fieldList(fl *ast.FieldList) []*ast.Field {
	if fl == nil {
		return nil
	}
	return fl.List
}

// embed describes an embedded field or interface element that names one
// type (T, *T, pkg.T, T[A]); union and ~ terms are skipped.
func (x *extractor) embed(t ast.Expr) (EmbedRecord, bool) {
	ptr := false
	if s, ok := t.(*ast.StarExpr); ok {
		ptr, t = true, s.X
	}
	var name *ast.Ident
	switch e := t.(type) {
	case *ast.Ident:
		name = e
	case *ast.SelectorExpr:
		name = e.Sel
	case *ast.IndexExpr:
		name = baseIdent(e.X)
	case *ast.IndexListExpr:
		name = baseIdent(e.X)
	}
	if name == nil {
		return EmbedRecord{}, false
	}
	p := x.tf.PositionFor(name.Pos(), false)
	return EmbedRecord{Text: x.oneLine(t.Pos(), t.End()), Line: p.Line, Col: p.Column, Pointer: ptr}, true
}

func baseIdent(e ast.Expr) *ast.Ident {
	switch b := e.(type) {
	case *ast.Ident:
		return b
	case *ast.SelectorExpr:
		return b.Sel
	}
	return nil
}

func (x *extractor) varSpec(s *ast.ValueSpec) {
	x.initSpec, x.initUsed = s, false
	x.frames = append(x.frames, x.initFrame)
	for _, v := range s.Values {
		x.walker.walk(v)
	}
	x.frames = x.frames[:len(x.frames)-1]
	if x.initUsed {
		x.initFrame.fn.EndLine = max(x.initFrame.fn.EndLine, x.line(s.End()))
	}
	x.initSpec = nil
}

// ensure returns the frame's function, creating <init> on its first site or
// closure. <init>'s name range is the first var name of that spec.
func (x *extractor) ensure(fr *frame) *FuncRecord {
	if fr != x.initFrame {
		return fr.fn
	}
	x.initUsed = true
	if fr.fn == nil {
		s := x.initSpec
		nr := x.nameRange(s.Pos(), s.Pos())
		if len(s.Names) > 0 {
			nr = x.nameRange(s.Names[0].Pos(), s.Names[0].End())
		}
		fr.fn = &FuncRecord{
			Name: "<init>", Qualname: x.claim(x.methodQ, "<init>"), MemberKind: "var_init", HasBody: true,
			NameRange: nr, StartLine: x.line(s.Pos()), EndLine: x.line(s.End()), Sites: []SiteRecord{},
		}
		x.rec.Funcs = append(x.rec.Funcs, fr.fn)
	}
	return fr.fn
}

func (x *extractor) enterLit(l *ast.FuncLit) bool {
	fr := x.frames[len(x.frames)-1]
	fr.lits++
	if x.omitted(l.Pos()) {
		return false // dropped with its sites and closures
	}
	parent := x.ensure(fr).Qualname
	n := strconv.Itoa(fr.lits)
	fn := &FuncRecord{
		Name: "func" + n, Qualname: x.claim(x.methodQ, parent+".func"+n), MemberKind: "closure",
		Signature: x.oneLine(l.Type.Pos(), l.Type.End()), HasBody: true,
		NameRange: x.nameRange(l.Type.Func, l.Type.Func+token.Pos(len("func"))),
		StartLine: x.line(l.Pos()), EndLine: x.line(l.End()), Parent: &parent, Sites: []SiteRecord{},
	}
	x.rec.Funcs = append(x.rec.Funcs, fn)
	x.frames = append(x.frames, &frame{fn: fn})
	return true
}

func (x *extractor) leaveLit(*ast.FuncLit) {
	x.frames = x.frames[:len(x.frames)-1]
}

func (x *extractor) onSite(s Site) {
	if s.Call != nil && typeLiteral(s.Call.Fun) { // a conversion by syntax (R53); resolve agrees
		x.rec.Conversions = append(x.rec.Conversions, x.tf.Offset(s.Pos))
		return
	}
	fn := x.ensure(x.frames[len(x.frames)-1])
	p := x.tf.PositionFor(s.Pos, false)
	r := SiteRecord{
		Kind: s.Kind, Name: s.Name, Line: p.Line, Col: p.Column, EndCol: p.Column + int(s.End-s.Pos),
		Offset: p.Offset, Shape: s.Shape, Caller: fn.Qualname,
	}
	if s.Recv != nil {
		text := x.receiverText(s.Recv)
		r.Receiver = &text
	}
	fn.Sites = append(fn.Sites, r)
}

// receiverText is the selector operand's source, cut to receiverTextMax
// bytes. The raw span is cut first: in a long call chain every receiver
// spans the chain before it.
func (x *extractor) receiverText(e ast.Expr) string {
	from, to := x.tf.Offset(e.Pos()), x.tf.Offset(e.End())
	cut := to-from > 4*receiverTextMax
	if cut {
		to = runeStart(x.src, from+4*receiverTextMax)
	}
	text := collapse(x.src[from:to])
	if len(text) > receiverTextMax {
		text, cut = text[:runeStart([]byte(text), receiverTextMax)], true
	}
	if cut {
		text += "…"
	}
	return text
}

// runeStart moves i back to the start of the UTF-8 sequence holding it.
func runeStart(b []byte, i int) int {
	for i > 0 && !utf8.RuneStart(b[i]) {
		i--
	}
	return i
}

// claim reserves a qualname in a namespace. A name already taken (only in
// code that does not compile, e.g. two func F in a //go:build ignore file)
// gets "#2", "#3", ..., so (relpath, qualname) stays unique.
func (x *extractor) claim(ns map[string]bool, q string) string {
	if !ns[q] {
		ns[q] = true
		return q
	}
	for k := 2; ; k++ {
		if c := q + "#" + strconv.Itoa(k); !ns[c] {
			ns[c] = true
			return c
		}
	}
}

func (x *extractor) line(p token.Pos) int {
	return x.tf.PositionFor(p, false).Line
}

// omitted: declarations at or after the first syntax-error line are left out.
func (x *extractor) omitted(p token.Pos) bool {
	return x.firstErr > 0 && x.line(p) >= x.firstErr
}

func (x *extractor) nameRange(start, end token.Pos) [3]int {
	p := x.tf.PositionFor(start, false)
	return [3]int{p.Line, p.Column, p.Column + int(end-start)}
}

func (x *extractor) fieldsText(fl *ast.FieldList) string {
	if fl == nil || len(fl.List) == 0 {
		return ""
	}
	return x.oneLine(fl.Pos(), fl.End())
}

// oneLine returns the source between two positions, with each whitespace run
// that spans lines collapsed to one space.
func (x *extractor) oneLine(from, to token.Pos) string {
	return collapse(x.src[x.tf.Offset(from):x.tf.Offset(to)])
}

func collapse(b []byte) string {
	if bytes.IndexByte(b, '\n') < 0 {
		return string(b)
	}
	var sb strings.Builder
	for i := 0; i < len(b); {
		j := i
		for j < len(b) && (b[j] == ' ' || b[j] == '\t' || b[j] == '\r' || b[j] == '\n') {
			j++
		}
		switch {
		case j == i:
			sb.WriteByte(b[i])
			j++
		case bytes.IndexByte(b[i:j], '\n') >= 0:
			sb.WriteByte(' ')
		default:
			sb.Write(b[i:j])
		}
		i = j
	}
	return sb.String()
}

// resolveOwners fills receiver.owner_relpath (spec R18): the file declaring
// the receiver's base type among the files of the same directory and package
// clause, preferring the method's own file, then the twin the primary build
// compiles, then the smallest relpath. A base type declared nowhere leaves
// the method to its own file's <package>, flagged receiver_unresolved.
func resolveOwners(results []*fileResult) {
	type key struct{ dir, clause string }
	groups := map[key][]*fileResult{}
	for _, r := range results {
		if r.record != nil {
			k := key{r.dir, r.record.PackageName}
			groups[k] = append(groups[k], r)
		}
	}
	for _, g := range groups {
		declared := map[string][]*fileResult{}
		for _, r := range g {
			for name := range r.types {
				declared[name] = append(declared[name], r)
			}
		}
		for _, r := range g {
			for _, recv := range r.recvs {
				best := owner(r, declared[recv.Base])
				recv.Unresolved = best == nil
				if best == nil {
					best = r
				}
				rel := best.relpath
				recv.OwnerRelpath = &rel
			}
		}
	}
}

// owner picks among the files declaring a type; nil when there are none.
func owner(self *fileResult, twins []*fileResult) *fileResult {
	var best *fileResult
	for _, t := range twins {
		if t == self {
			return t
		}
		if best == nil || (t.record.BuildIncluded && !best.record.BuildIncluded) ||
			(t.record.BuildIncluded == best.record.BuildIncluded && t.relpath < best.relpath) {
			best = t
		}
	}
	return best
}
