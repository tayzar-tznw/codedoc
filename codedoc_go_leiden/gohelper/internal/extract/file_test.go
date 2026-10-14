package extract

import (
	"crypto/sha256"
	"encoding/hex"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"testing"
	"unicode/utf8"
)

var (
	testBuild  = newBuildConfig("linux", "amd64", nil, false)
	testModule = &module{root: "", path: "example.com/m"}
)

func extract1(t *testing.T, relpath, src string) *FileRecord {
	t.Helper()
	res := extractSource(relpath, []byte(src), testBuild, testModule)
	if res.err != "" {
		t.Fatalf("extract: %s", res.err)
	}
	resolveOwners([]*fileResult{res})
	return res.record
}

func typeQuals(rec *FileRecord) []string {
	var out []string
	for _, ty := range rec.Types {
		out = append(out, ty.Qualname)
	}
	return out
}

func funcQuals(rec *FileRecord) []string {
	var out []string
	for _, fn := range rec.Funcs {
		out = append(out, fn.Qualname)
	}
	return out
}

func typeOf(t *testing.T, rec *FileRecord, q string) TypeRecord {
	t.Helper()
	for _, ty := range rec.Types {
		if ty.Qualname == q {
			return ty
		}
	}
	t.Fatalf("no type %q in %v", q, typeQuals(rec))
	return TypeRecord{}
}

func funcOf(t *testing.T, rec *FileRecord, q string) *FuncRecord {
	t.Helper()
	for _, fn := range rec.Funcs {
		if fn.Qualname == q {
			return fn
		}
	}
	t.Fatalf("no func %q in %v", q, funcQuals(rec))
	return nil
}

// siteNames lists "kind:shape:name@line<caller" for each site of fn.
func strp(s string) *string { return &s }

func siteNames(fn *FuncRecord) []string {
	out := []string{}
	for _, s := range fn.Sites {
		out = append(out, s.Kind+":"+s.Shape+":"+s.Name+"@"+strconv.Itoa(s.Line)+"<"+s.Caller)
	}
	return out
}

func TestTypesKindsEmbedsAndInterfaceMethods(t *testing.T) {
	src := `package billing

import "fmt"

// Base is embedded.
type Base struct{}

type FeeCalculator struct {
	Base
	*Logger
	pkg.Mixin
	*pkg.PtrMixin
	List[int]
	rate int
}

type List[T any] struct{ items []T }

// Totaler totals.
type Totaler interface {
	// Total sums the fees.
	Total() Yen
	fmt.Stringer
	Close() error // Close releases.
	~int | ~string
}

type Alias = FeeCalculator

type (
	// Yen is money.
	Yen int
	Points float64
)

type GAlias[T any] = List[T]
`
	rec := extract1(t, "billing/fee.go", src)
	if got, want := typeQuals(rec), []string{"<package>", "Base", "FeeCalculator", "List", "Totaler", "Alias", "Yen", "Points", "GAlias"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("types: got %v, want %v", got, want)
	}
	pkg := typeOf(t, rec, "<package>")
	if pkg.Name != "<package>" || pkg.Kind != "package" || pkg.NameRange != [3]int{1, 9, 16} || pkg.StartLine != 1 || pkg.EndLine != 36 {
		t.Errorf("<package>: %+v", pkg)
	}
	base := typeOf(t, rec, "Base")
	if base.Kind != "struct" || base.NameRange != [3]int{6, 6, 10} || base.StartLine != 6 || base.EndLine != 6 || base.Doc != "Base is embedded.\n" {
		t.Errorf("Base: %+v", base)
	}
	fee := typeOf(t, rec, "FeeCalculator")
	wantEmbeds := []EmbedRecord{
		{"Base", 9, 2, false},
		{"Logger", 10, 3, true},
		{"pkg.Mixin", 11, 6, false},
		{"pkg.PtrMixin", 12, 7, true},
		{"List[int]", 13, 2, false},
	}
	if fee.Kind != "struct" || fee.NameRange != [3]int{8, 6, 19} || fee.StartLine != 8 || fee.EndLine != 15 || !reflect.DeepEqual(fee.Embeds, wantEmbeds) {
		t.Errorf("FeeCalculator: %+v", fee)
	}
	if l := typeOf(t, rec, "List"); l.Kind != "struct" || l.TypeParams != "[T any]" {
		t.Errorf("List: %+v", l)
	}
	tot := typeOf(t, rec, "Totaler")
	wantMethods := []MethodRecord{
		{Name: "Total", Qualname: "Totaler.Total", NameRange: [3]int{22, 2, 7}, Signature: "Total() Yen", Doc: "Total sums the fees.\n", StartLine: 22, EndLine: 22},
		{Name: "Close", Qualname: "Totaler.Close", NameRange: [3]int{24, 2, 7}, Signature: "Close() error", Doc: "Close releases.\n", StartLine: 24, EndLine: 24},
	}
	if tot.Kind != "interface" || tot.Doc != "Totaler totals.\n" || !reflect.DeepEqual(tot.Methods, wantMethods) {
		t.Errorf("Totaler: %+v", tot)
	}
	if want := []EmbedRecord{{"fmt.Stringer", 23, 6, false}}; !reflect.DeepEqual(tot.Embeds, want) {
		t.Errorf("Totaler embeds: got %+v, want %+v", tot.Embeds, want)
	}
	if a := typeOf(t, rec, "Alias"); a.Kind != "alias" || a.NameRange != [3]int{28, 6, 11} {
		t.Errorf("Alias: %+v", a)
	}
	yen := typeOf(t, rec, "Yen")
	if yen.Kind != "named" || yen.NameRange != [3]int{32, 2, 5} || yen.StartLine != 32 || yen.EndLine != 32 || yen.Doc != "Yen is money.\n" {
		t.Errorf("Yen: %+v", yen)
	}
	if p := typeOf(t, rec, "Points"); p.Kind != "named" || p.Doc != "" {
		t.Errorf("Points: %+v", p)
	}
	if g := typeOf(t, rec, "GAlias"); g.Kind != "alias" || g.TypeParams != "[T any]" {
		t.Errorf("GAlias: %+v", g)
	}
	for _, ty := range rec.Types {
		if ty.Embeds == nil || ty.Methods == nil {
			t.Errorf("%s: embeds and methods must be [] not null", ty.Qualname)
		}
	}
}

func TestFuncQualnamesReceiversAndKinds(t *testing.T) {
	src := `package billing

// Calculate computes the fee.
func (c *FeeCalculator) Calculate(p Points) Yen { return 0 }

func (c FeeCalculator) Value() int { return 1 }

func (l *List[T]) Push(v T) {}

func (List[_]) Len() int { return 0 }

func (r *Rand) N[T Int](n T) T { return n }

func Map[T, U any](xs []T, f func(T) U) []U { return nil }

func init() {}

func (FeeCalculator) init() {}

func init() {}

func _() {}

func _() {}

func (FeeCalculator) _() {}

func Plain(
	a int,
	b string,
) (int, error) {
	return 0, nil
}
`
	rec := extract1(t, "billing/fee.go", src)
	want := []string{"FeeCalculator.Calculate", "FeeCalculator.Value", "List.Push", "List.Len", "Rand.N", "Map",
		"init#1", "FeeCalculator.init", "init#2", "_#1", "_#2", "FeeCalculator._#1", "Plain"}
	if got := funcQuals(rec); !reflect.DeepEqual(got, want) {
		t.Fatalf("funcs: got %v, want %v", got, want)
	}
	calc := funcOf(t, rec, "FeeCalculator.Calculate")
	// No listed file declares FeeCalculator: the method's own <package> owns it (R18).
	wantCalc := &FuncRecord{Name: "Calculate", Qualname: "FeeCalculator.Calculate", MemberKind: "method",
		Receiver: &ReceiverRecord{Text: "*FeeCalculator", Base: "FeeCalculator", Pointer: true,
			OwnerRelpath: strp("billing/fee.go"), Unresolved: true},
		Signature: "func (c *FeeCalculator) Calculate(p Points) Yen", HasBody: true,
		NameRange: [3]int{4, 25, 34}, StartLine: 4, EndLine: 4, Doc: "Calculate computes the fee.\n", Sites: []SiteRecord{}}
	if !reflect.DeepEqual(calc, wantCalc) {
		t.Errorf("Calculate:\n got %+v %+v\nwant %+v %+v", calc, calc.Receiver, wantCalc, wantCalc.Receiver)
	}
	for q, r := range map[string]ReceiverRecord{
		"FeeCalculator.Value": {Text: "FeeCalculator", Base: "FeeCalculator", Pointer: false},
		"List.Push":           {Text: "*List[T]", Base: "List", Pointer: true},
		"List.Len":            {Text: "List[_]", Base: "List", Pointer: false},
		"Rand.N":              {Text: "*Rand", Base: "Rand", Pointer: true},
	} {
		fn := funcOf(t, rec, q)
		got := ReceiverRecord{Text: fn.Receiver.Text, Base: fn.Receiver.Base, Pointer: fn.Receiver.Pointer}
		if fn.MemberKind != "method" || got != r {
			t.Errorf("%s: kind %s receiver %+v, want %+v", q, fn.MemberKind, fn.Receiver, r)
		}
	}
	if n := funcOf(t, rec, "Rand.N"); n.TypeParams != "[T Int]" {
		t.Errorf("Rand.N type params %q", n.TypeParams)
	}
	m := funcOf(t, rec, "Map")
	if m.MemberKind != "func" || m.Receiver != nil || m.TypeParams != "[T, U any]" || m.Signature != "func Map[T, U any](xs []T, f func(T) U) []U" {
		t.Errorf("Map: %+v", m)
	}
	i1 := funcOf(t, rec, "init#1")
	if i1.Name != "init" || i1.MemberKind != "init" || i1.NameRange != [3]int{16, 6, 10} {
		t.Errorf("init#1: %+v", i1)
	}
	if fn := funcOf(t, rec, "FeeCalculator.init"); fn.MemberKind != "method" {
		t.Errorf("a method named init is a method: %+v", fn)
	}
	if fn := funcOf(t, rec, "_#2"); fn.Name != "_" || fn.MemberKind != "func" || fn.NameRange != [3]int{24, 6, 7} {
		t.Errorf("_#2: %+v", fn)
	}
	p := funcOf(t, rec, "Plain")
	if p.Signature != "func Plain( a int, b string, ) (int, error)" || p.StartLine != 28 || p.EndLine != 33 {
		t.Errorf("Plain: signature %q lines %d-%d", p.Signature, p.StartLine, p.EndLine)
	}
}

func TestBlankMethodsAreNumberedPerFileAndReceiverType(t *testing.T) {
	src := `package p

type T struct{}

type U struct{}

func (T) _() {}

func (U) _() {}

func (T) _() {}

func _() {}

func (*T) _() {}

func _() {}
`
	rec := extract1(t, "p.go", src)
	want := []string{"T._#1", "U._#1", "T._#2", "_#1", "T._#3", "_#2"}
	if got := funcQuals(rec); !reflect.DeepEqual(got, want) {
		t.Fatalf("funcs: got %v, want %v (R17)", got, want)
	}
	for _, q := range []string{"T._#1", "T._#2", "T._#3", "U._#1"} {
		fn := funcOf(t, rec, q)
		if fn.Name != "_" || fn.MemberKind != "method" || fn.Receiver.Unresolved || *fn.Receiver.OwnerRelpath != "p.go" {
			t.Errorf("%s: %+v receiver %+v", q, fn, fn.Receiver)
		}
	}
}

func TestReceiverOwnerRule(t *testing.T) {
	type file struct{ relpath, src string }
	files := []file{
		{"b/own.go", "package b\n\ntype Twin struct{}\n\nfunc (Twin) Own() {}\n"},
		{"b/twin_linux.go", "package b\n\ntype Twin struct{}\n"},
		{"b/a_windows.go", "package b\n\ntype Twin struct{}\n\ntype Ghost struct{}\n"},
		{"b/use.go", "package b\n\nfunc (Twin) Use() {}\n\nfunc (*Ghost) Boo() {}\n\nfunc (Missing) M() {}\n"},
		{"b/z_darwin.go", "package b\n\ntype Ghost struct{}\n"},
		{"b/x_test.go", "package b_test\n\nfunc (Twin) T() {}\n"},
		{"c/use.go", "package b\n\nfunc (Twin) C() {}\n"},
	}
	var results []*fileResult
	for _, f := range files {
		results = append(results, extractSource(f.relpath, []byte(f.src), testBuild, testModule))
	}
	resolveOwners(results)
	owners := map[string]string{}
	for _, r := range results {
		for _, fn := range r.record.Funcs {
			o := *fn.Receiver.OwnerRelpath
			if fn.Receiver.Unresolved {
				o += " unresolved"
			}
			owners[r.relpath+" "+fn.Qualname] = o
		}
	}
	want := map[string]string{
		"b/own.go Twin.Own":  "b/own.go",               // same-file twin first
		"b/use.go Twin.Use":  "b/own.go",               // a compiled twin; smallest relpath among compiled ones
		"b/use.go Ghost.Boo": "b/a_windows.go",         // no twin compiled: smallest relpath
		"b/use.go Missing.M": "b/use.go unresolved",    // not declared: the file's <package>
		"b/x_test.go Twin.T": "b/x_test.go unresolved", // another package clause
		"c/use.go Twin.C":    "c/use.go unresolved",    // another directory
	}
	if !reflect.DeepEqual(owners, want) {
		t.Errorf("owners:\n got %v\nwant %v", owners, want)
	}
}

func TestClosuresCallersAndInit(t *testing.T) {
	src := `package p

var handler = func() {
	helper()
	inner := func() { deep() }
	inner()
}

var plain = compute()

var noCall = 1

func F() {
	a()
	f1 := func() {
		b()
		f2 := func() { c() }
		f2()
	}
	go func() { d() }()
	defer func() { e() }()
	f1()
}

func init() {
	setup()
	func() { boot() }()
}

func (s *S) M() {
	func() {}()
}
`
	rec := extract1(t, "p.go", src)
	want := []string{"<init>", "<init>.func1", "<init>.func1.func1", "F", "F.func1", "F.func1.func1", "F.func2", "F.func3",
		"init#1", "init#1.func1", "S.M", "S.M.func1"}
	if got := funcQuals(rec); !reflect.DeepEqual(got, want) {
		t.Fatalf("funcs: got %v, want %v", got, want)
	}
	init := funcOf(t, rec, "<init>")
	if init.Name != "<init>" || init.MemberKind != "var_init" || init.Parent != nil || init.Receiver != nil ||
		init.NameRange != [3]int{3, 5, 12} || init.StartLine != 3 || init.EndLine != 9 || !init.HasBody {
		t.Errorf("<init>: %+v", init)
	}
	checks := map[string][]string{
		"<init>":             {"call:ident:compute@9<<init>"},
		"<init>.func1":       {"call:ident:helper@4<<init>.func1", "call:ident:inner@6<<init>.func1"},
		"<init>.func1.func1": {"call:ident:deep@5<<init>.func1.func1"},
		"F":                  {"call:ident:a@14<F", "go:funclit:func@20<F", "defer:funclit:func@21<F", "call:ident:f1@22<F"},
		"F.func1":            {"call:ident:b@16<F.func1", "call:ident:f2@18<F.func1"},
		"F.func1.func1":      {"call:ident:c@17<F.func1.func1"},
		"F.func2":            {"call:ident:d@20<F.func2"},
		"F.func3":            {"call:ident:e@21<F.func3"},
		"init#1":             {"call:ident:setup@26<init#1", "call:funclit:func@27<init#1"},
		"init#1.func1":       {"call:ident:boot@27<init#1.func1"},
		"S.M":                {"call:funclit:func@31<S.M"},
		"S.M.func1":          {},
	}
	for q, want := range checks {
		if got := siteNames(funcOf(t, rec, q)); !reflect.DeepEqual(got, want) {
			t.Errorf("%s sites: got %v, want %v", q, got, want)
		}
	}
	for q, w := range map[string]struct {
		parent string
		nr     [3]int
		lines  [2]int
	}{
		"<init>.func1":       {"<init>", [3]int{3, 15, 19}, [2]int{3, 7}},
		"<init>.func1.func1": {"<init>.func1", [3]int{5, 11, 15}, [2]int{5, 5}},
		"F.func1":            {"F", [3]int{15, 8, 12}, [2]int{15, 19}},
		"F.func1.func1":      {"F.func1", [3]int{17, 9, 13}, [2]int{17, 17}},
		"F.func2":            {"F", [3]int{20, 5, 9}, [2]int{20, 20}},
		"F.func3":            {"F", [3]int{21, 8, 12}, [2]int{21, 21}},
		"S.M.func1":          {"S.M", [3]int{31, 2, 6}, [2]int{31, 31}},
	} {
		fn := funcOf(t, rec, q)
		if fn.MemberKind != "closure" || fn.Parent == nil || *fn.Parent != w.parent || fn.NameRange != w.nr ||
			fn.StartLine != w.lines[0] || fn.EndLine != w.lines[1] || fn.Signature != "func()" || !fn.HasBody ||
			fn.Name != q[strings.LastIndex(q, ".")+1:] {
			t.Errorf("%s: %+v (parent %v)", q, fn, fn.Parent)
		}
	}
	// A funclit site's token is the closure's func keyword.
	goSite := funcOf(t, rec, "F").Sites[1]
	if nr := funcOf(t, rec, "F.func2").NameRange; goSite.Line != nr[0] || goSite.Col != nr[1] || goSite.EndCol != nr[2] {
		t.Errorf("go funclit site %+v does not match F.func2 name range %v", goSite, nr)
	}
}

func TestInitClosuresAreNumberedAcrossTheFilesVarSpecs(t *testing.T) {
	src := `package p

var (
	first  = func() { a() }
	second = func() { b() }
)

func F() {}

var third = wrap(func() {})
`
	rec := extract1(t, "p.go", src)
	want := []string{"<init>", "<init>.func1", "<init>.func2", "F", "<init>.func3"}
	if got := funcQuals(rec); !reflect.DeepEqual(got, want) {
		t.Fatalf("funcs: got %v, want %v", got, want)
	}
	init := funcOf(t, rec, "<init>")
	if init.NameRange != [3]int{4, 2, 7} || init.StartLine != 4 || init.EndLine != 10 {
		t.Errorf("<init> spans its contributing specs: %+v", init)
	}
	if got := siteNames(init); !reflect.DeepEqual(got, []string{"call:ident:wrap@10<<init>"}) {
		t.Errorf("<init> sites %v", got)
	}
	if got := siteNames(funcOf(t, rec, "<init>.func2")); !reflect.DeepEqual(got, []string{"call:ident:b@5<<init>.func2"}) {
		t.Errorf("<init>.func2 sites %v", got)
	}
}

func TestDeclarationOnTheFirstErrorLineIsOmitted(t *testing.T) {
	rec := extract1(t, "p.go", "package p\n\nfunc Good() {}\n\nfunc Bad( {}\n\nfunc After() {}\n")
	if len(rec.SyntaxErrors) == 0 || rec.SyntaxErrors[0] != 5 {
		t.Fatalf("syntax errors %v, want line 5 first", rec.SyntaxErrors)
	}
	if got := funcQuals(rec); !reflect.DeepEqual(got, []string{"Good"}) {
		t.Errorf("funcs: got %v, want [Good]", got)
	}
}

func TestInitExistsForClosuresWithoutSites(t *testing.T) {
	rec := extract1(t, "p.go", "package p\n\nvar x = 1\n\nvar h = func() {}\n")
	if got, want := funcQuals(rec), []string{"<init>", "<init>.func1"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("funcs: got %v, want %v", got, want)
	}
	if init := funcOf(t, rec, "<init>"); len(init.Sites) != 0 || init.NameRange != [3]int{5, 5, 6} {
		t.Errorf("<init>: %+v", init)
	}
}

func TestNoInitWithoutInitializerCode(t *testing.T) {
	rec := extract1(t, "p.go", "package p\n\nvar x = 1\nvar y int\nvar z = T{A: 1}\n")
	if len(rec.Funcs) != 0 {
		t.Fatalf("funcs: %v", funcQuals(rec))
	}
}

func TestPositionsAreByteColumnsAndIgnoreLineDirectives(t *testing.T) {
	src := "package p\n\nfunc 計算() {\n\ts := \"日本語\"; f(s)\n}\n\n//line gram.y:100\nfunc G() {\n\tg()\n}\n"
	tf, f := parseSrc(t, src)
	if p := tf.Position(f.Decls[1].Pos()); p.Filename != "gram.y" || p.Line != 100 {
		t.Fatalf("the //line directive is not active: %v", p)
	}
	rec := extract1(t, "p.go", src)
	calc := funcOf(t, rec, "計算")
	if calc.NameRange != [3]int{3, 6, 12} {
		t.Errorf("計算 name range %v, want [3 6 12] (byte columns)", calc.NameRange)
	}
	wantOff := strings.Index(src, "f(s)")
	if s := calc.Sites[0]; s.Name != "f" || s.Line != 4 || s.Col != 20 || s.EndCol != 21 || s.Offset != wantOff {
		t.Errorf("site after multibyte text: %+v, want line 4 col 20 end 21 offset %d", s, wantOff)
	}
	g := funcOf(t, rec, "G")
	if g.NameRange != [3]int{8, 6, 7} || g.StartLine != 8 || g.EndLine != 10 {
		t.Errorf("G: name range %v lines %d-%d, want [8 6 7] 8-10", g.NameRange, g.StartLine, g.EndLine)
	}
	if s := g.Sites[0]; s.Line != 9 || s.Col != 2 || s.Offset != strings.Index(src, "g()") {
		t.Errorf("site under //line: %+v", s)
	}
}

func TestSyntaxErrorOmitsDeclarationsFromTheFirstErrorLine(t *testing.T) {
	src := `package p

import "fmt"

func Good() {
	fmt.Println()
}

func Broken() {
	b(
}

func After() {
	c()
}

type Late struct{}
`
	rec := extract1(t, "p.go", src)
	if len(rec.SyntaxErrors) == 0 || rec.SyntaxErrors[0] != 11 || !sort.IntsAreSorted(rec.SyntaxErrors) {
		t.Fatalf("syntax errors %v, want sorted lines starting at 11", rec.SyntaxErrors)
	}
	if got, want := funcQuals(rec), []string{"Good", "Broken"}; !reflect.DeepEqual(got, want) {
		t.Errorf("funcs: got %v, want %v", got, want)
	}
	if got, want := typeQuals(rec), []string{"<package>"}; !reflect.DeepEqual(got, want) {
		t.Errorf("types: got %v, want %v", got, want)
	}
	if got := siteNames(funcOf(t, rec, "Good")); !reflect.DeepEqual(got, []string{"call:selector:Println@6<Good"}) {
		t.Errorf("Good sites %v", got)
	}
	if got := siteNames(funcOf(t, rec, "Broken")); len(got) == 0 || got[0] != "call:ident:b@10<Broken" {
		t.Errorf("Broken sites %v", got)
	}
	if len(rec.Imports) != 1 {
		t.Errorf("imports %v", rec.Imports)
	}
}

func TestSyntaxErrorLinesIgnoreLineDirectives(t *testing.T) {
	src := "package p\n\n//line gram.y:100\nfunc F() {\n\tx := \n}\n"
	rec := extract1(t, "p.go", src)
	if len(rec.SyntaxErrors) == 0 || rec.SyntaxErrors[0] != 6 {
		t.Errorf("syntax errors %v, want the unadjusted line 6 first", rec.SyntaxErrors)
	}
}

func TestImports(t *testing.T) {
	src := "package p\n\nimport \"fmt\"\n\nimport (\n\tf \"fmt\"\n\t. \"strings\"\n\t_ \"embed\"\n\traw `net/http`\n)\n"
	rec := extract1(t, "p.go", src)
	want := []ImportRecord{
		{"fmt", "", "normal", 3, 8, strings.Index(src, `"fmt"`)},
		{"fmt", "f", "normal", 6, 4, strings.LastIndex(src, `"fmt"`)},
		{"strings", ".", "dot", 7, 4, strings.Index(src, `"strings"`)},
		{"embed", "_", "blank", 8, 4, strings.Index(src, `"embed"`)},
		{"net/http", "raw", "normal", 9, 6, strings.Index(src, "`net/http`")},
	}
	if !reflect.DeepEqual(rec.Imports, want) {
		t.Errorf("imports:\n got %+v\nwant %+v", rec.Imports, want)
	}
}

func TestLinknameAndBodylessDeclarations(t *testing.T) {
	src := `package p

import _ "unsafe"

//go:linkname nanotime runtime.nanotime
func nanotime() int64

//go:linkname exported
func exported() {}

func asm(x int) int
`
	rec := extract1(t, "p.go", src)
	nt := funcOf(t, rec, "nanotime")
	if nt.Linkname == nil || *nt.Linkname != "runtime.nanotime" || nt.HasBody {
		t.Errorf("nanotime: linkname %v has_body %v", nt.Linkname, nt.HasBody)
	}
	if ex := funcOf(t, rec, "exported"); ex.Linkname != nil || !ex.HasBody {
		t.Errorf("exported: linkname %v has_body %v", ex.Linkname, ex.HasBody)
	}
	if a := funcOf(t, rec, "asm"); a.Linkname != nil || a.HasBody || a.Signature != "func asm(x int) int" {
		t.Errorf("asm: %+v", a)
	}
}

func TestPackageDocAndGenerated(t *testing.T) {
	src := "// Code generated by mockgen. DO NOT EDIT.\n\n// Package p does things.\npackage p\n"
	rec := extract1(t, "p.go", src)
	if !rec.Generated {
		t.Error("generated header not detected")
	}
	pkg := typeOf(t, rec, "<package>")
	if pkg.Doc != "Package p does things.\n" || pkg.NameRange != [3]int{4, 9, 10} || pkg.EndLine != 4 {
		t.Errorf("<package>: %+v", pkg)
	}
	if rec := extract1(t, "q.go", "package p\n\n// Code generated by hand. DO NOT EDIT.\nfunc F() {}\n"); rec.Generated {
		t.Error("a generated comment after the package clause does not count")
	}
}

func TestSiteRecordsAreTheSitesOfTheFile(t *testing.T) {
	src := `package p

var v = mk(func() int { return one() })

func F(x T) {
	defer x.Close()
	go func() { x.Run(g()) }()
	for range x.All {
		_ = (*T)(nil)
	}
	f()(x)
}
`
	rec := extract1(t, "p.go", src)
	var got []string
	for _, fn := range rec.Funcs {
		for _, s := range fn.Sites {
			if src[s.Offset:s.Offset+len(s.Name)] != s.Name {
				t.Errorf("site %+v: offset does not point at its name", s)
			}
			got = append(got, s.Kind+":"+s.Shape+":"+s.Name+":"+strconv.Itoa(s.Offset))
		}
	}
	tf, f := parseSrc(t, src)
	var want []string
	for _, s := range Sites(f) {
		want = append(want, s.Kind+":"+s.Shape+":"+s.Name+":"+strconv.Itoa(tf.Offset(s.Pos)))
	}
	sort.Strings(got)
	sort.Strings(want)
	if !reflect.DeepEqual(got, want) {
		t.Errorf("record sites %v\nSites() %v", got, want)
	}
	run := funcOf(t, rec, "F.func1").Sites
	if len(run) != 2 || run[0].Name != "Run" || run[0].Receiver == nil || *run[0].Receiver != "x" || run[1].Receiver != nil {
		t.Errorf("receivers: %+v", run)
	}
}

func TestTypeLiteralCallsAreConversionsNotSites(t *testing.T) {
	// Spec R53: a call of a type literal, through parentheses and pointer
	// stars, is a conversion by syntax in every file, counted and not a site.
	// A named type is undecidable by syntax: (*T)(p), T(x) and any(x) stay.
	src := `package p

var b = []byte("x")

func F(s string, p unsafe.Pointer, m M, c C, f func(), x any) {
	_ = []byte(s)
	_ = [4]byte(s)
	_ = map[string]int(m)
	_ = (chan int)(c)
	_ = (<-chan int)(c)
	_ = (func())(f)
	_ = interface{}(x)
	_ = struct{}(x)
	_ = (*[8]byte)(p)
	_ = (**[]int)(p)
	_ = (*T)(p)
	_ = T(x)
	_ = any(x)
	go []byte(s)
	g()
}
`
	rec := extract1(t, "p.go", src)
	want := []string{"call:paren:@16<F", "call:ident:T@17<F", "call:ident:any@18<F", "call:ident:g@20<F"}
	if got := siteNames(funcOf(t, rec, "F")); !reflect.DeepEqual(got, want) {
		t.Errorf("sites %v, want %v", got, want)
	}
	if len(rec.Conversions) != 12 { // 10 above plus `go []byte(s)` in F, and b's initializer
		t.Errorf("conversions %v, want 12", rec.Conversions)
	}
	for i, off := range rec.Conversions { // each at its call's Lparen, in order
		if src[off] != '(' || (i > 0 && off <= rec.Conversions[i-1]) {
			t.Errorf("conversion %d at offset %d (%q)", i, off, src[off:off+8])
		}
	}
	if q := funcQuals(rec); len(q) != 1 || q[0] != "F" {
		t.Errorf("funcs %v: a conversion alone makes no <init>", q)
	}
	if _, f := parseSrc(t, src); len(Sites(f)) != 4+12 {
		t.Errorf("Sites() has %d sites; resolve still classifies the conversions", len(Sites(f)))
	}
}

func TestDuplicateQualnamesInInvalidCodeGetSuffixes(t *testing.T) {
	src := `//go:build ignore

package main

func main() {}

func main() { helper() }

type T struct{}

type T int

func (T) M() {}

func (T) M() { func() {}() }
`
	rec := extract1(t, "gen.go", src)
	if got, want := funcQuals(rec), []string{"main", "main#2", "T.M", "T.M#2", "T.M#2.func1"}; !reflect.DeepEqual(got, want) {
		t.Errorf("funcs: got %v, want %v", got, want)
	}
	if got, want := typeQuals(rec), []string{"<package>", "T", "T#2"}; !reflect.DeepEqual(got, want) {
		t.Errorf("types: got %v, want %v", got, want)
	}
	if s := funcOf(t, rec, "main#2").Sites; len(s) != 1 || s[0].Caller != "main#2" {
		t.Errorf("main#2 sites %+v", s)
	}
}

func TestOriginAndImportPath(t *testing.T) {
	sub := &module{root: "sub", path: "example.com/m/sub"}
	for _, c := range []struct {
		relpath, src string
		mod          *module
		origin, pkg  string
	}{
		{"a.go", "package m\n", testModule, "package", "example.com/m"},
		{"billing/fee.go", "package billing\n", testModule, "package", "example.com/m/billing"},
		{"billing/fee_test.go", "package billing\n", testModule, "test", "example.com/m/billing"},
		{"billing/x_test.go", "package billing_test\n", testModule, "xtest", "example.com/m/billing_test"},
		{"cmd/tool/main.go", "package main\n", testModule, "command", "example.com/m/cmd/tool"},
		{"cmd/tool/main_test.go", "package main\n", testModule, "test", "example.com/m/cmd/tool"},
		{"sub/inner/x.go", "package inner\n", sub, "package", "example.com/m/sub/inner"},
		{"sub/x.go", "package sub\n", sub, "package", "example.com/m/sub"},
		{"loose/x.go", "package loose\n", nil, "package", ""},
	} {
		res := extractSource(c.relpath, []byte(c.src), testBuild, c.mod)
		if res.err != "" {
			t.Fatalf("%s: %s", c.relpath, res.err)
		}
		r := res.record
		wantMod := ""
		if c.mod != nil {
			wantMod = c.mod.path
		}
		if r.Origin != c.origin || r.Package != c.pkg || r.Module != wantMod || r.PackageName != strings.Fields(c.src)[1] {
			t.Errorf("%s: origin %q package %q module %q name %q", c.relpath, r.Origin, r.Package, r.Module, r.PackageName)
		}
	}
}

func TestUnparseableFileIsAnErrorRecord(t *testing.T) {
	for _, src := range []string{"", "func F() {}\n", "package\n", "\x00garbage"} {
		res := extractSource("bad.go", []byte(src), testBuild, testModule)
		if res.err == "" || res.record != nil {
			t.Errorf("%q: want an error, got %+v", src, res)
		}
	}
}

func TestEmptyListsAreNotNull(t *testing.T) {
	rec := extract1(t, "p.go", "package p\n")
	if rec.SyntaxErrors == nil || rec.Imports == nil || rec.Funcs == nil || rec.Types == nil {
		t.Errorf("record lists must be [] not null: %+v", rec)
	}
	sum := sha256.Sum256([]byte("package p\n"))
	if rec.SHA256 != hex.EncodeToString(sum[:]) {
		t.Errorf("sha256 %s", rec.SHA256)
	}
}

func TestBOMAndCRLFKeepByteColumns(t *testing.T) {
	bom := "\ufeffpackage p\n\nfunc F() { g() }\n"
	rec := extract1(t, "bom.go", bom)
	if nr := typeOf(t, rec, "<package>").NameRange; nr != [3]int{1, 12, 13} {
		t.Errorf("<package> after a BOM: %v, want [1 12 13] (the BOM's 3 bytes count)", nr)
	}
	if s := funcOf(t, rec, "F").Sites[0]; s.Line != 3 || s.Col != 12 || s.Offset != strings.Index(bom, "g()") {
		t.Errorf("site after a BOM: %+v", s)
	}
	crlf := "package p\r\n\r\n// F does it.\r\nfunc F(\r\n\ta int,\r\n) {\r\n\tg()\r\n}\r\n"
	rec = extract1(t, "crlf.go", crlf)
	f := funcOf(t, rec, "F")
	if f.NameRange != [3]int{4, 6, 7} || f.Doc != "F does it.\n" || f.Signature != "func F( a int, )" || f.EndLine != 8 {
		t.Errorf("CRLF func: %+v", f)
	}
	if s := f.Sites[0]; s.Line != 7 || s.Col != 2 || s.Offset != strings.Index(crlf, "g()") {
		t.Errorf("CRLF site: %+v", s)
	}
}

func TestLongReceiverTextIsCutOnARuneBoundary(t *testing.T) {
	src := "package p\n\nfunc F() {\n\tfn(\"" + strings.Repeat("あ", 70) + "\").M()\n}\n"
	rec := extract1(t, "p.go", src)
	var m SiteRecord
	for _, s := range funcOf(t, rec, "F").Sites {
		if s.Name == "M" {
			m = s
		}
	}
	if m.Receiver == nil || !utf8.ValidString(*m.Receiver) || !strings.HasSuffix(*m.Receiver, "…") ||
		len(*m.Receiver) > receiverTextMax+len("…") || !strings.HasPrefix(*m.Receiver, `fn("あ`) {
		t.Errorf("receiver %v", m.Receiver)
	}
	// A raw span over 4x the cap is cut before whitespace collapses.
	src = "package p\n\nfunc F() {\n\tf(" + strings.Repeat("\n", 900) + "x).M()\n}\n"
	rec = extract1(t, "q.go", src)
	if r := funcOf(t, rec, "F").Sites[1].Receiver; r == nil || *r != "f( …" {
		t.Errorf("receiver of a long span: %v", r)
	}
}
