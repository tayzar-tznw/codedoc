import os

import gt_lib


def kinds(toks):
    return [(t.kind, t.text) for t in toks]


def test_tokenizer_semicolons_literals_and_byte_columns():
    src = 'package p\n\nvar s = "日本{" + `raw\nfunc(` /* c\n */\nvar r = \'}\'\n'.encode()
    toks = gt_lib.tokenize(src)
    texts = [t.text for t in toks if t.kind != "semi"]
    assert texts[:7] == ["package", "p", "var", "s", "=", '"日本{"', "+"]
    raw = next(t for t in toks if t.text.startswith("`"))
    assert (raw.line, raw.col) == (3, 21)  # byte column: 日本 is 6 bytes
    # the general comment holding a newline ends the statement
    semis = [t for t in toks if t.kind == "semi"]
    assert [t.line for t in semis] == [1, 4, 6]
    rune = next(t for t in toks if t.kind == "char")
    assert rune.text == "'}'" and (rune.line, rune.col) == (6, 9)


def test_tokenizer_keywords_and_auto_semicolon_after_return():
    toks = gt_lib.tokenize(b"func f() {\n\treturn\n}\n")
    assert kinds(toks)[:4] == [("keyword", "func"), ("ident", "f"), ("op", "("), ("op", ")")]
    ret = next(i for i, t in enumerate(toks) if t.text == "return")
    assert toks[ret + 1].kind == "semi" and toks[ret + 1].auto


SRC = """\
package p

import (
	"fmt"
	str "strings"
)

var Hook = func() int { return one() }

var table = map[string]func() int{"one": one, "two": func() int { return 2 }}

type (
	Pair[K comparable, V any] struct{ k K; v V }
	Arr [4]int
	Alias = Pair[string, int]
	Reader interface {
		Read(p []byte) (int, error)
		fmt.Stringer
	}
)

func one() int { return 1 }

func init() { _ = Hook() }

func init() { _ = table["two"]() }

func _() {}

func (p *Pair[K, V]) Swap() *Pair[K, V] { return p }

func Run(xs []int, it func(func(int) bool), c *Counter) {
	defer fmt.Println(str.ToUpper("x"))
	go func() { _ = one() }()
	go one()
	for v := range it {
		_ = v
	}
	for _, x := range xs {
		_ = x
	}
	for v := range c.All {
		_ = v
	}
	for v := range *c.p {
		_ = v
	}
	f := func() func() int {
		return func() int { return one() }
	}
	_ = f()()
	_ = []byte("x")
	_ = (one)()
}
"""


def test_declarations_follow_the_c3_grammar():
    gof = gt_lib.GoFile(SRC.encode())
    decls = {d.qualname: d for d in gof.decls}
    assert gof.package == "p"
    assert [i["path"] for i in gof.imports] == ["fmt", "strings"] and gof.imports[1]["alias"] == "str"
    for qual, member, kind in [("<package>", "package", "class"), ("<init>.func1", "closure", "method"),
                               ("<init>.func2", "closure", "method"), ("<init>", "var_init", "method"),
                               ("Pair", "struct", "class"), ("Arr", "named", "class"), ("Alias", "alias", "class"),
                               ("Reader", "interface", "class"), ("Reader.Read", "interface_method", "method"),
                               ("one", "func", "method"), ("init#1", "init", "method"), ("init#2", "init", "method"),
                               ("_#1", "func", "method"), ("Pair.Swap", "method", "method"),
                               ("Run.func1", "closure", "method"), ("Run.func2", "closure", "method"),
                               ("Run.func2.func1", "closure", "method")]:
        assert qual in decls, qual
        assert (decls[qual].member, decls[qual].kind) == (member, kind), qual
    assert "Reader.Stringer" not in decls and "Reader.fmt" not in decls
    assert (decls["Pair.Swap"].line, decls["Pair.Swap"].col) == (30, 22)
    assert decls["Pair.Swap"].receiver == "Pair" and decls["Pair.Swap"].pointer
    # the map type's func is a type, not a literal: one closure on that line
    assert decls["<init>.func2"].line == 10 and decls["<init>.func2"].col == 54
    assert decls["Run.func1"].line == 34 and decls["Run.func1"].col == 5


def test_sites_kinds_shapes_and_callers():
    gof = gt_lib.GoFile(SRC.encode())
    sites = {(s.line, s.col): s for s in gof.sites}
    assert (sites[33, 12].kind, sites[33, 12].name, sites[33, 12].caller) == ("defer", "Println", "Run")
    assert (sites[33, 24].kind, sites[33, 24].name) == ("call", "ToUpper")
    assert (sites[34, 5].kind, sites[34, 5].shape) == ("go", "funclit")
    assert sites[34, 18].caller == "Run.func1"
    assert sites[35, 5].kind == "go"
    assert (sites[36, 17].kind, sites[36, 17].name) == ("range", "it")
    assert (sites[39, 20].kind, sites[39, 20].name) == ("range", "xs")
    assert (sites[42, 19].kind, sites[42, 19].shape) == ("range", "selector")
    assert not any(s.kind == "range" and s.line == 45 for s in gof.sites)  # range *c.p is not a selector
    assert sites[51, 6].kind == "call" and sites[51, 6].name == "f"  # f()(): the inner call only
    assert not any(s.line == 51 and s.col != 6 for s in gof.sites)
    assert sites[49, 30].caller == "Run.func2.func1"
    assert (sites[52, 12].shape, sites[52, 12].name) == ("other", "")  # []byte("x"): the ( (master plan C2)
    assert (sites[53, 7].shape, sites[53, 7].name) == ("ident", "one")  # (one)(): parentheses are transparent
    assert sites[8, 32].caller == "<init>.func1"
    assert sites[26, 19].shape == "index" and sites[26, 19].caller == "init#2"


SHAPES = """\
package p

type T struct{ f func() }

func g() func() { return nil }

func Gen[A any](yield func(A) bool) {}

func Shapes(m map[string]func(), fs [][]func(), x any, fp *func(), ch chan func(), t T) {
	m["k"]()
	fs[0][1]()
	x.(func())()
	(*fp)()
	(<-ch)()
	g()()
	(g())()
	(t.f)()
	func() {}()
	(func() {})()
	_ = []byte("s")
	_ = (*[4]byte)(nil)
	_ = map[string]int(nil)
	Gen[int](func(int) bool { return true })
	for range Gen[int] {
	}
	for range (Gen[string]) {
	}
	for range func(yield func(int) bool) {} {
	}
}
"""


def test_site_tokens_follow_extract():
    """Every callee shape takes extract.Sites' token (master plan C2, helper-extract ruling): the
    identifier through parentheses, the index base, the func keyword, else the call's ( with no
    name; no site for the outer call of f()(x). goracle's TestSiteTokensFollowExtract has the
    same source."""
    got = [f"{s.line}:{s.col} {s.name or '-'} {s.shape} {s.kind}"
           for s in sorted(gt_lib.GoFile(SHAPES.encode()).sites, key=lambda s: (s.line, s.col))]
    assert got == [
        "10:2 m index call", "11:10 - other call", "12:12 - other call", "13:7 - paren call", "14:8 - paren call",
        "15:2 g ident call", "16:3 g ident call", "17:5 f selector call", "18:2 func funclit call",
        "19:3 func funclit call", "20:12 - other call", "21:16 - paren call", "22:20 - other call",
        "23:2 Gen index call", "24:12 Gen index range", "26:13 Gen index range", "28:12 func funclit range"]


def test_case_helpers_build_the_c10_shapes():
    t = gt_lib.target("FeeCalculator.Calculate", "example.com/iryo/billing", "method", "libs/billing/fee.go")
    assert t == {"qualname": "FeeCalculator.Calculate", "module": "example.com/iryo/billing", "kind": "method",
                 "file": "libs/billing/fee.go"}
    assert gt_lib.target("F", "m", "method", "a.go", repo="shared")["repo"] == "shared"
    r = gt_lib.resolved(t, runtime=[t], candidates=[t], open_world=True)
    assert (r["status"], r["targets"], r["runtime_targets"], r["candidates"], r["open_world"]) == \
        ("resolved", [t], [t], [t], True)
    assert gt_lib.external("std")["external"] == {"module": "std"}
    assert gt_lib.peer(t, module="example.com/iryo/shared")["targets"] == [t]
    assert gt_lib.plain("unresolved", reason="missing-module")["reason"] == "missing-module"
    c = gt_lib.case("TWIN-001", "a.go", "x.Log(m)", "Log", "call", gt_lib.plain("dynamic"), "why", distractors=[t])
    assert c["category"] == "TWIN" and c["entry"] == "reception-desk" and c["occurrence"] == 1
    assert c["expect"]["distractors"] == [t] and c["site_kind"] == "call"


def test_gomod_and_gowork_readers():
    mod = gt_lib.parse_gomod("""\
module "example.com/a" // the module
go 1.26
toolchain go1.26.8
require (
	example.com/b v1.2.0
	example.com/c v0.1.0 // indirect
)
require example.com/d v1.0.0
replace (
	example.com/b => ../b
	example.com/c v0.1.0 => example.com/c2 v0.2.0
)
exclude example.com/e v1.0.0
""")
    assert mod["module"] == "example.com/a" and mod["go"] == "1.26" and mod["toolchain"] == "go1.26.8"
    assert mod["require"] == [("example.com/b", "v1.2.0", False), ("example.com/c", "v0.1.0", True),
                              ("example.com/d", "v1.0.0", False)]
    assert mod["replace"][0] == {"old": "example.com/b", "old_version": "", "new": "../b", "new_version": "",
                                 "local": True}
    assert mod["replace"][1]["local"] is False and mod["replace"][1]["new_version"] == "v0.2.0"
    work = gt_lib.parse_gowork("go 1.26\n\nuse (\n\t./a\n\t\"./b c\"\n)\nuse ./d\nreplace x => ./y\n")
    assert work["use"] == ["./a", "./b c", "./d"] and work["replace"][0]["local"]


def test_layout_ownership_packages_and_proxy(tmp_path):
    root = tmp_path / "fx"
    for rel, text in {"go.mod": "module example.com/root\n", "a/b.go": "package b\n",
                      "nested/go.mod": "module example.com/nested\n", "nested/x/x_test.go": "package x_test\n",
                      "nested/testdata/t.go": "package t\n", "_skip/s.go": "package s\n"}.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    lay = gt_lib.Layout(str(root))
    assert set(lay.modules) == {"", "nested"}
    assert lay.module_of("a/b.go") == "example.com/root"
    assert lay.module_of("nested/x/x_test.go") == "example.com/nested"
    assert lay.package_of("nested/x/x_test.go", "x_test") == "example.com/nested/x_test"
    assert lay.package_of("a/b.go", "b") == "example.com/root/a"
    assert "nested/testdata/t.go" not in lay.go_files and "_skip/s.go" not in lay.go_files
    proxy = os.path.join(os.path.dirname(gt_lib.__file__), "..", "_proxy")
    assert gt_lib.proxy_has(proxy, "example.com/thirdparty/uuidlite", "v1.1.0")
    assert not gt_lib.proxy_has(proxy, "example.com/thirdparty/uuidlite", "v9.9.9")
    assert gt_lib.escape_path("example.com/AB") == "example.com/!a!b"
