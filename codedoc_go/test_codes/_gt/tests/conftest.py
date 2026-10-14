import os
import shutil
import sys

import pytest

GT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROXY = os.path.normpath(os.path.join(GT, "..", "_proxy"))
sys.path.insert(0, GT)
sys.dont_write_bytecode = True

GO_WORK = """\
go 1.26

use (
	./app
	./lib
)
"""

LIB_MOD = """\
module example.com/tiny/lib

go 1.26
"""

APP_MOD = """\
module example.com/tiny/app

go 1.26

require example.com/thirdparty/uuidlite v1.1.0
"""

CORE = """\
package core

import "strings"

type Greeter struct {
	Name string
}

func (g *Greeter) Greet(who string) string {
	return "hi " + strings.ToUpper(who)
}

type Speaker interface {
	Speak() string
}

type Dog struct{}

func (Dog) Speak() string { return "わん" }

type Cat struct{}

func (c *Cat) Speak() string { return "にゃー" }

func Chorus(ss ...Speaker) string {
	out := ""
	for _, s := range ss {
		out += s.Speak()
	}
	return out
}

func Apply(f func(string) string, v string) string {
	return f(v)
}
"""

MAIN = """\
package main

import (
	"fmt"

	"example.com/thirdparty/uuidlite"
	"example.com/tiny/lib/core"
)

func main() {
	g := &core.Greeter{Name: "受付"}
	fmt.Println(g.Greet("x"), core.Chorus(core.Dog{}, &core.Cat{}))
	up := func(s string) string { return s + "!" }
	fmt.Println(core.Apply(up, "y"), uuidlite.New().Short())
	defer fmt.Println(len("end"))
}
"""

CASES = """\
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "_gt"))
from gt_lib import ambiguous, case, external, plain, resolved, target  # noqa: E402

CORE = "lib/core/core.go"
MAIN = "app/cmd/tinyapp/main.go"
LIB = "example.com/tiny/lib"
APP = "example.com/tiny/app"
RUN = "tinyapp"

GREET = target("Greeter.Greet", LIB, "method", CORE)
SPEAK = target("Speaker.Speak", LIB, "method", CORE)
DOG = target("Dog.Speak", LIB, "method", CORE)
CAT = target("Cat.Speak", LIB, "method", CORE)

CASES = [
    case("CALL-001", MAIN, 'g.Greet("x")', "Greet", "call", resolved(GREET), "pointer receiver method", entry=RUN),
    case("IFC-001", CORE, "out += s.Speak()", "Speak", "call",
         resolved(SPEAK, runtime=[DOG, CAT], candidates=[DOG, CAT], open_world=False),
         "interface method; Dog and *Cat run", entry=RUN, distractors=[GREET]),
    case("FV-001", CORE, "return f(v)", "f", "call", plain("dynamic", runtime=[target("main.func1", APP, "method", MAIN)]),
         "func-typed parameter", entry=RUN),
    case("EXT-001", MAIN, "fmt.Println(g.Greet", "Println", "call", external("std"), "stdlib", entry=RUN),
    case("EXT-002", MAIN, "uuidlite.New()", "New", "call", external("example.com/thirdparty/uuidlite"),
         "third-party module from _proxy", entry=RUN),
    case("BLT-001", MAIN, 'len("end")', "len", "call", plain("builtin"), "builtin", entry=RUN),
    case("DEF-001", MAIN, "defer fmt.Println", "Println", "defer", external("std"), "deferred stdlib call", entry=RUN),
    case("CALL-002", MAIN, "core.Chorus(", "Chorus", "call", resolved(target("Chorus", LIB, "method", CORE)),
         "package function across modules of the workspace", entry=RUN),
]
DECL_PINS = [
    {"qualname": "Greeter", "module": LIB, "kind": "class", "file": CORE},
    {"qualname": "main.func1", "module": APP, "kind": "method", "file": MAIN},
    {"qualname": "<package>", "module": LIB, "kind": "class", "file": CORE},
]
XPR_PAIRS = []
"""

ENTRIES = """\
{"tinyapp": {"dir": "app", "cmd": ["go", "run", "./cmd/tinyapp"]}}
"""


def write(root, files):
    for rel, text in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)


def proxy_sums(*keys):
    """go.sum lines (zip and go.mod) of "<module> <version>" keys from _proxy/sums.txt."""
    with open(os.path.join(PROXY, "sums.txt"), encoding="utf-8") as handle:
        lines = handle.readlines()
    return "".join(line for line in lines
                   if " ".join(line.split()[:2]).removesuffix("/go.mod") in keys or " ".join(line.split()[:2]) in keys)


def tiny_files():
    return {
        "go.work": GO_WORK,
        "lib/go.mod": LIB_MOD,
        "lib/core/core.go": CORE,
        "app/go.mod": APP_MOD,
        "app/go.sum": proxy_sums("example.com/thirdparty/uuidlite v1.1.0"),
        "app/cmd/tinyapp/main.go": MAIN,
        "entries.json": ENTRIES,
        "README.md": "tiny fixture\n",
        "ground_truth/cases.py": CASES,
    }


@pytest.fixture
def tiny(tmp_path):
    root = tmp_path / "tiny"
    write(str(root), tiny_files())
    gt_link = tmp_path / "_gt"
    shutil.copytree(GT, gt_link, ignore=shutil.ignore_patterns("tests", "oracle", "__pycache__"))
    return str(root)


# The team's pinned toolchain (master plan, global constraints) and the place
# `python -m graph_generator setup go` installs one; tried after the env and PATH.
TOOLCHAIN_FALLBACKS = (
    os.path.join(os.path.dirname(GT), "..", ".tools", "go", "bin", "go"),
    "/home/admin/scratch/codedoc-go-research/toolchain/go/bin/go",
)


def find_go_or_none():
    import run_oracle
    try:
        go = run_oracle.find_go()
        if run_oracle.go_version(go).startswith("go1.27"):
            return go
    except run_oracle.OracleError:
        pass
    for cand in TOOLCHAIN_FALLBACKS:
        cand = os.path.normpath(cand)
        if os.path.isfile(cand) and run_oracle.go_version(cand).startswith("go1.27"):
            return cand
    return None


@pytest.fixture(scope="session")
def go_bin():
    go = find_go_or_none()
    if go is None:
        pytest.skip("no go1.27 toolchain (set GORACLE_GO)")
    return go
