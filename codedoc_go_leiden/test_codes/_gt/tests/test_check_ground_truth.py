import json
import os

import check_ground_truth as cg
import generate_manifest as gm
from conftest import write
from test_generate_manifest import replace_in_cases


def check(root, capsys):
    rc = cg.main(["check_ground_truth.py", root])
    return rc, capsys.readouterr().out


def generated(tiny):
    assert gm.main(["generate_manifest.py", tiny]) == 0
    return tiny


def edit_manifest(root, fn):
    path = os.path.join(root, "ground_truth", "manifest.json")
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    fn(data)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


def case_of(data, cid):
    return next(c for c in data["cases"] if c["id"] == cid)


def test_tiny_fixture_passes(tiny, capsys):
    rc, out = check(generated(tiny), capsys)
    assert rc == 0, out
    assert "ground truth OK: tiny: 8 cases, 3 pins, 2 modules, 1 go.work" in out


def test_id_bijection_and_stale_manifest(tiny, capsys):
    generated(tiny)
    edit_manifest(tiny, lambda d: d["cases"].pop())
    replace_in_cases(tiny, '"pointer receiver method"', '"pointer receiver method (edited)"')
    rc, out = check(tiny, capsys)
    assert rc == 1
    assert "cases.py case IFC-001 is not in manifest.json (regenerate it)" in out
    assert "CALL-001: manifest.json differs from cases.py (regenerate it)" in out


def test_positions_are_rederived(tiny, capsys):
    generated(tiny)

    def tamper(d):
        case_of(d, "CALL-001")["col"] += 1
        case_of(d, "CALL-002")["offset"] += 3
        case_of(d, "EXT-001")["line"] += 1
    edit_manifest(tiny, tamper)
    rc, out = check(tiny, capsys)
    assert rc == 1
    assert "CALL-001: column 17 but the token is at byte column 16" in out
    assert "CALL-002: offset 183 but line and column give 180" in out
    assert "EXT-001: snippet not on line 13" in out


def test_site_kind_and_duplicate_site(tiny, capsys):
    replace_in_cases(tiny, 'case("BLT-001", MAIN, \'len("end")\', "len", "call"',
                     'case("BLT-001", MAIN, \'len("end")\', "len", "go"')
    replace_in_cases(tiny, 'case("DEF-001", MAIN, "defer fmt.Println", "Println", "defer"',
                     'case("DEF-001", MAIN, "fmt.Println(g.Greet", "Println", "call"')
    replace_in_cases(tiny, 'case("EXT-001", MAIN, "fmt.Println(g.Greet", "Println"',
                     'case("EXT-001", MAIN, "Println(g.Greet", "Println"')
    rc, out = check(generated(tiny), capsys)
    assert rc == 1
    assert "BLT-001: the site at app/cmd/tinyapp/main.go:15:20 is a call site, case says go" in out
    assert "same site as" in out and "(app/cmd/tinyapp/main.go:12:6 call)" in out


def test_anchor_must_match_the_declaration(tiny, capsys):
    generated(tiny)

    def tamper(d):
        case_of(d, "CALL-001")["expect"]["targets"][0]["line"] = 10
        d["decl_pins"][0]["col"] = 1
    edit_manifest(tiny, tamper)
    rc, out = check(tiny, capsys)
    assert rc == 1
    assert "CALL-001 targets: anchor lib/core/core.go:10:19 but Greeter.Greet is declared at 9:19" in out
    assert "pin Greeter: anchor lib/core/core.go:5:1 but Greeter is declared at 5:6" in out


def test_module_truthfulness(tiny, capsys):
    replace_in_cases(tiny, 'GREET = target("Greeter.Greet", LIB', 'GREET = target("Greeter.Greet", APP')
    write(tiny, {"app/go.mod": "module example.com/tiny/app\n\ngo 1.26\n\nrequire (\n"
                               "\texample.com/thirdparty/uuidlite v1.1.0\n\texample.com/thirdparty/textfmt v9.9.9\n)\n",
                 "lib/extra/extra.go": 'package extra\n\nimport "example.com/thirdparty/yamlish"\n\nvar _ = yamlish.Write\n',
                 "lib/core/more.go": 'package core\n\nimport "example.com/tiny/lib/nope"\n\nvar _ = nope.X\n'})
    rc, out = check(generated(tiny), capsys)
    assert rc == 1
    assert "CALL-001 targets: lib/core/core.go belongs to module example.com/tiny/lib, target says " \
           "example.com/tiny/app" in out
    assert "app/go.mod: require example.com/thirdparty/textfmt v9.9.9 is neither a workspace module, " \
           "a local replace nor a version in _proxy" in out
    assert "lib/extra/extra.go: imports example.com/thirdparty/yamlish, which no module reachable from " \
           "example.com/tiny/lib provides" in out
    assert "lib/core/more.go: imports example.com/tiny/lib/nope, but lib/nope holds no Go package" in out


def test_workspace_problems_and_layout_exceptions(tiny, capsys):
    write(tiny, {"go.work": "go 1.26\n\nuse (\n\t./app\n\t./lib\n\t./gone\n)\n"})
    rc, out = check(generated(tiny), capsys)
    assert rc == 1 and "MISMATCH: ./go.work: use ./gone has no go.mod" in out
    with open(os.path.join(tiny, "ground_truth", "cases.py"), "a", encoding="utf-8") as handle:
        handle.write('\nLAYOUT_EXCEPTIONS = ["use ./gone has no go.mod", "never reported"]\n')
    rc, out = check(generated(tiny), capsys)
    assert rc == 1
    assert "use ./gone" not in out.replace("LAYOUT_EXCEPTIONS entry", "")
    assert "LAYOUT_EXCEPTIONS entry matches no layout problem: 'never reported'" in out


def test_local_replace_must_point_at_the_module(tiny, capsys):
    write(tiny, {"tool/go.mod": "module example.com/tiny/tool\n\ngo 1.26\n\nrequire example.com/tiny/lib v0.0.0\n\n"
                                "replace example.com/tiny/lib => ../app\n",
                 "tool/main.go": 'package main\n\nimport "example.com/tiny/lib/core"\n\nfunc main() { core.Chorus() }\n'})
    rc, out = check(generated(tiny), capsys)
    assert rc == 1
    assert "tool/go.mod: replace example.com/tiny/lib => ../app declares module example.com/tiny/app" in out


def test_expectation_schema(tiny, capsys):
    replace_in_cases(tiny, 'distractors=[GREET]', 'distractors=[DOG]')
    replace_in_cases(tiny, 'external("example.com/thirdparty/uuidlite")', 'external("uuidlite")')
    replace_in_cases(tiny, 'plain("builtin")', 'plain("builtin", reason="missing-module")')
    rc, out = check(generated(tiny), capsys)
    assert rc == 1
    assert "IFC-001: distractor Dog.Speak is also an expected target" in out
    assert "EXT-002: external.module must be 'std', 'C' or a module path, not 'uuidlite'" in out
    assert "BLT-001: reason 'missing-module' needs status unresolved" in out


def test_xpr_pairs(tiny, capsys):
    with open(os.path.join(tiny, "ground_truth", "cases.py"), "a", encoding="utf-8") as handle:
        handle.write('\nXPR_PAIRS = [{"kind": "line", "cases": ["CALL-001", "CALL-002"]},\n'
                     '             {"kind": "file", "files": ["lib/core/core.go", "app/cmd/tinyapp/main.go"]}]\n')
    rc, out = check(generated(tiny), capsys)
    assert rc == 1
    assert "XPR pair ['CALL-001', 'CALL-002']: expectations do not diverge" not in out
    assert "XPR file pair ['lib/core/core.go', 'app/cmd/tinyapp/main.go']: files are not byte-identical" in out


def test_xpr_line_pair_identical_lines_with_diverging_targets(tmp_path, capsys):
    root = tmp_path / "xpr"
    body = "package {p}\n\nfunc Log() string {{ return \"{p}\" }}\n\nfunc Run() string {{ return Log() }}\n"
    write(str(root), {"go.mod": "module example.com/xpr\n\ngo 1.26\n", "a/a.go": body.format(p="a"),
                      "b/b.go": body.format(p="b"), "README.md": "x\n",
                      "entries.json": "{}\n",
                      "ground_truth/cases.py":
                          "from gt_lib import case, resolved, target\nM = 'example.com/xpr'\n"
                          "CASES = [case('XPR-001', 'a/a.go', 'return Log()', 'Log', 'call', "
                          "resolved(target('Log', M, 'method', 'a/a.go')), 'a', entry=None),\n"
                          "         case('XPR-002', 'b/b.go', 'return Log()', 'Log', 'call', "
                          "resolved(target('Log', M, 'method', 'b/b.go')), 'b', entry=None)]\n"
                          "XPR_PAIRS = [{'kind': 'line', 'cases': ['XPR-001', 'XPR-002']}]\n"})
    assert gm.main(["generate_manifest.py", str(root)]) == 0
    rc, out = check(str(root), capsys)
    assert rc == 0, out


def test_entries_schema(tiny, capsys):
    write(tiny, {"entries.json": json.dumps({
        "tinyapp": {"dir": "app", "cmd": ["go", "run", "-mod=mod", "./cmd/tinyapp"]},
        "bad": {"dir": "nowhere", "test": [], "env": {"GOFLAGS": "-x", "GOWORK": "off"}, "extra": 1},
        "both": {"dir": "app", "cmd": ["go", "build", "."], "test": ["./..."]}})})
    replace_in_cases(tiny, 'case("BLT-001", MAIN, \'len("end")\', "len", "call", plain("builtin"), "builtin", entry=RUN)',
                     'case("BLT-001", MAIN, \'len("end")\', "len", "call", plain("builtin"), "builtin", entry="ghost")')
    rc, out = check(generated(tiny), capsys)
    assert rc == 1
    for text in ["entry tinyapp: -mod flags are not allowed", "entry bad: unknown keys ['extra']",
                 "entry bad: dir missing: nowhere", "entry bad: test must be a non-empty list of package patterns",
                 "entry bad: env may not set GOFLAGS", "entry both: needs exactly one of cmd and test",
                 "entry both: cmd must be [\"go\", \"run\", <package>, args...]", "BLT-001: unknown entry 'ghost'"]:
        assert text in out, text


def test_readme_qa_features_and_cross_repo_edges(tiny, capsys):
    os.remove(os.path.join(tiny, "README.md"))
    write(tiny, {"ground_truth/qa_questions.json": json.dumps({
        "version": 1, "fixture": "tiny", "language": "ja", "questions": [
            {"id": "QA-01", "question": "Greeter.Greet を呼ぶのは誰ですか", "check": {"type": "callers_of",
             "subject": "example.com/tiny/lib/core.Greeter.Greet"}, "expected": ["example.com/tiny/app/cmd/tinyapp.main"],
             "match": "exact_set"},
            {"id": "QA-02", "question": "English only", "check": {"type": "class_file",
             "subject": "example.com/tiny/lib/core.Greeter"}, "expected": ["lib/core/other.go"], "match": "equals"},
            {"id": "QA-03", "question": "存在しますか", "check": {"type": "node_exists",
             "subject": "example.com/tiny/lib/core.Nope"}, "expected": ["exists"], "match": "equals"}]}),
        "ground_truth/features.json": json.dumps({"features": [{"name": "受付", "files": ["app/cmd/tinyapp/main.go"]},
                                                               {"name": "受付", "files": ["missing.go"]}]})})
    with open(os.path.join(tiny, "ground_truth", "cases.py"), "a", encoding="utf-8") as handle:
        handle.write('\nCROSS_REPO_EDGES = [{"table": "CrossRepoRef", "kind": "calls", "source": "tiny:lib/core/core.go:Dog",'
                     ' "target": "tiny:lib/core/core.go:Speaker"}]\n')
    rc, out = check(generated(tiny), capsys)
    assert rc == 1
    for text in ["README.md is missing", "QA-02: question is not Japanese",
                 "QA-02: example.com/tiny/lib/core.Greeter is declared in lib/core/core.go, expected ['lib/core/other.go']",
                 "QA-03: subject example.com/tiny/lib/core.Nope resolves to 0 declarations",
                 "features.json: missing or duplicate name '受付'", "features.json: 受付: file missing: missing.go",
                 "CrossRepoRef with bad kind"]:
        assert text in out, text
    assert "QA-01" not in out
