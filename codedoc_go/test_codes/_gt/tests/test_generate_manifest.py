import json
import os

import generate_manifest as gm
from conftest import write


def manifest_of(root):
    with open(os.path.join(root, "ground_truth", "manifest.json"), encoding="utf-8") as handle:
        return json.load(handle)


def test_writes_manifest_with_lines_byte_columns_and_offsets(tiny, capsys):
    assert gm.main(["generate_manifest.py", tiny]) == 0
    assert "8 cases, 3 pins" in capsys.readouterr().out
    m = manifest_of(tiny)
    assert (m["version"], m["fixture"], m["go"]) == (1, "tiny", "go1.27.2")
    assert m["build"] == {"goos": "linux", "goarch": "amd64", "cgo": False, "tags": []}
    assert m["repos"] == [{"name": "tiny", "dir": "."}]
    cases = {c["id"]: c for c in m["cases"]}
    assert (cases["CALL-001"]["line"], cases["CALL-001"]["col"], cases["CALL-001"]["offset"]) == (12, 16, 163)
    assert (cases["EXT-002"]["line"], cases["EXT-002"]["col"]) == (14, 44)
    assert (cases["FV-001"]["line"], cases["FV-001"]["col"]) == (34, 9)
    with open(os.path.join(tiny, "app", "cmd", "tinyapp", "main.go"), "rb") as handle:
        data = handle.read()
    call = cases["CALL-001"]
    assert data[call["offset"]:call["offset"] + len(b"Greet")] == b"Greet"
    assert cases["DEF-001"]["site_kind"] == "defer" and cases["DEF-001"]["entry"] == "tinyapp"


def test_byte_column_after_multibyte_text(tmp_path):
    root = tmp_path / "mb"
    write(str(root), {"go.mod": "module example.com/mb\n\ngo 1.26\n",
                      "a.go": 'package a\n\nfunc F(s string) string { return s }\n\nvar X = "日本語" + F("x")\n'})
    line, col, offset = gm.locate({"id": "MB-001", "file": "a.go", "snippet": 'F("x")', "token": "F"}, str(root))
    assert (line, col) == (5, 23)  # 日本語 is 9 bytes, 3 characters
    assert open(root / "a.go", "rb").read()[offset:offset + 1] == b"F"


def test_targets_and_pins_get_anchors(tiny):
    gm.main(["generate_manifest.py", tiny])
    m = manifest_of(tiny)
    cases = {c["id"]: c for c in m["cases"]}
    greet = cases["CALL-001"]["expect"]["targets"][0]
    assert (greet["qualname"], greet["line"], greet["col"]) == ("Greeter.Greet", 9, 19)
    speak = cases["IFC-001"]["expect"]
    assert [(t["qualname"], t["line"]) for t in speak["targets"]] == [("Speaker.Speak", 14)]
    assert [(t["qualname"], t["line"], t["col"]) for t in speak["runtime_targets"]] == \
        [("Dog.Speak", 19, 12), ("Cat.Speak", 23, 15)]
    assert speak["distractors"][0]["qualname"] == "Greeter.Greet" and speak["open_world"] is False
    closure = cases["FV-001"]["expect"]["runtime_targets"][0]
    assert (closure["qualname"], closure["line"], closure["col"]) == ("main.func1", 13, 8)
    pins = {p["qualname"]: p for p in m["decl_pins"]}
    assert (pins["Greeter"]["line"], pins["<package>"]["line"]) == (5, 1)
    assert cases["EXT-002"]["expect"]["external"] == {"module": "example.com/thirdparty/uuidlite"}


def test_token_occurrence_and_identifier_boundaries():
    assert gm.token_offset("log.Log(log)", "log", 1) == 0
    assert gm.token_offset("log.Log(log)", "log", 2) == 8
    assert gm.token_offset("logger.flush()", "log", 1) is None
    assert gm.token_offset("x.Log(m)", "Log", 1) == 2
    assert gm.token_offset("日本(x)", "日本", 1) == 0


def replace_in_cases(root, old, new):
    path = os.path.join(root, "ground_truth", "cases.py")
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    assert old in text
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text.replace(old, new))


def test_non_unique_snippet_names_the_case(tiny, capsys):
    replace_in_cases(tiny, 'case("EXT-001", MAIN, "fmt.Println(g.Greet"', 'case("EXT-001", MAIN, "fmt.Println("')
    assert gm.main(["generate_manifest.py", tiny]) == 1
    out = capsys.readouterr().out
    assert "ERROR: EXT-001: snippet occurs 3 times in app/cmd/tinyapp/main.go: 'fmt.Println('" in out
    assert not os.path.exists(os.path.join(tiny, "ground_truth", "manifest.json"))


def test_missing_token_undeclared_target_and_kind_mismatch(tiny, capsys):
    replace_in_cases(tiny, 'core.Chorus(", "Chorus"', 'core.Chorus(", "Choir"')
    replace_in_cases(tiny, 'GREET = target("Greeter.Greet"', 'GREET = target("Greeter.Hello"')
    replace_in_cases(tiny, '{"qualname": "Greeter", "module": LIB, "kind": "class"',
                     '{"qualname": "Greeter", "module": LIB, "kind": "method"')
    assert gm.main(["generate_manifest.py", tiny]) == 1
    out = capsys.readouterr().out
    assert "CALL-002: token 'Choir' occurrence 1 not in snippet" in out
    assert "CALL-001 targets: Greeter.Hello is not declared in lib/core/core.go" in out
    assert "pin Greeter: Greeter is a class (struct), target says method" in out


def test_duplicate_ids_are_rejected(tiny, capsys):
    replace_in_cases(tiny, 'case("CALL-002"', 'case("CALL-001"')
    assert gm.main(["generate_manifest.py", tiny]) == 1
    assert "duplicate case ids: ['CALL-001']" in capsys.readouterr().out
