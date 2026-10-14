"""A synthetic Go repo for the graph builder tests: entity records (C3) and
resolution records (C5) of one module with a package split over several files,
an external test package, a command and a file without a record.

make_data() returns PipelineData as Phases 1, 1.5 and 1.6 leave it; the ID
helpers name the nodes the builders must produce.
"""
from __future__ import annotations

from graph_generator.pipeline import PipelineData, _make_id

REPO = "iryo"
MOD = "example.com/iryo"
BILLING = f"{MOD}/libs/billing"
FEE = "libs/billing/fee.go"
EXTRA = "libs/billing/fee_extra.go"
BASE = "libs/billing/base.go"
FEE_TEST = "libs/billing/fee_test.go"
XTEST = "libs/billing/billing_test.go"
MAIN = "cmd/reception/main.go"
KDOC = "libs/kernel/doc.go"
BAD = "tools/bad.go"   # scanned, but extraction gave no record
NODE_TABLES = ("Files", "Packages", "Classes", "Methods", "Directories")
EDGE_TABLES = ("PackageImports", "FileDependsOn", "ClassEmbeds", "ClassImplements", "MethodCalls",
               "PossiblyCalls", "FileDefinesClass", "ClassDefinesMethod", "DirContainsFile",
               "PackageContainsFile")


# ── C3 builders ─────────────────────────────────────────────────────

def pkg_type(doc="", end=40):
    return {"name": "<package>", "qualname": "<package>", "kind": "package", "type_params": "",
            "embeds": [], "methods": [], "name_range": [1, 9, 16], "start_line": 1,
            "end_line": end, "doc": doc}


def typ(q, kind="struct", line=5, end=None, embeds=(), methods=(), doc="", type_params=""):
    return {"name": q, "qualname": q, "kind": kind, "type_params": type_params,
            "embeds": list(embeds), "methods": list(methods),
            "name_range": [line, 6, 6 + len(q)], "start_line": line, "end_line": end or line + 2,
            "doc": doc}


def iface_method(iface, name, line, doc="", signature=""):
    return {"name": name, "qualname": f"{iface}.{name}", "name_range": [line, 2, 2 + len(name)],
            "signature": signature or f"{name}() int", "doc": doc, "start_line": line,
            "end_line": line}


def recv(base, owner, pointer=True, unresolved=False):
    return {"text": ("*" if pointer else "") + base, "base": base, "pointer": pointer,
            "owner_relpath": owner, "receiver_unresolved": unresolved}


def fn(q, kind="func", line=10, receiver=None, parent=None, doc="", signature=None,
       has_body=True, type_params=""):
    name = q.rsplit(".", 1)[-1].split("#")[0]
    return {"name": name, "qualname": q, "member_kind": kind, "receiver": receiver,
            "type_params": type_params,
            "signature": signature if signature is not None else f"func {name}()",
            "has_body": has_body, "linkname": None, "name_range": [line, 6, 6 + len(name)],
            "start_line": line, "end_line": line + 3, "doc": doc, "parent": parent, "sites": []}


def record(package=BILLING, name="billing", origin="package", types=(), funcs=(), imports=(),
           generated=False, build_included=True, build_constraint="", doc=""):
    return {"sha256": None, "module": MOD, "package": package, "package_name": name,
            "origin": origin, "generated": generated, "build_constraint": build_constraint,
            "build_included": build_included, "syntax_errors": [], "imports": list(imports),
            "types": [pkg_type(doc), *types], "funcs": list(funcs)}


def imp(path, line, col=2, alias="", kind="normal"):
    return {"path": path, "alias": alias, "kind": kind, "line": line, "col": col, "offset": 0}


# ── C5 builders ─────────────────────────────────────────────────────

def tgt(relpath, qualname, kind="method", repo=None):
    t = {"unit": MOD, "package": BILLING, "relpath": relpath, "qualname": qualname, "line": 1,
         "kind": kind}
    if repo:
        t["repo"] = repo
    return t


def cand(relpath, qualname, test_only=False):
    return {**tgt(relpath, qualname), "test_only": test_only, "via_embedded_iface": False}


def call(caller, name, line, status, target=None, via="types", candidates=(), open_world=False,
         reason=None, kind="call", col=2, external=None):
    return {"site": {"caller": caller, "kind": kind, "name": name, "line": line, "col": col},
            "status": status, "via": via, "target": target, "static_target": None,
            "external": external, "candidates": list(candidates), "open_world": open_world,
            "build_variants": [], "reason": reason, "build": "linux-amd64"}


def import_rec(line, path, status="resolved", dir=None, clause=None, repo=None, col=2):
    target = None
    if dir is not None:
        target = {"unit": MOD, "package": path, "dir": dir, "clause": clause}
        if repo:
            target["repo"] = repo
    return {"line": line, "col": col, "import_path": path, "status": status, "target": target,
            "external": None if status == "resolved" else {"module": "std", "version": "",
                                                           "package": path, "qualname": "",
                                                           "peer": bool(repo)},
            "reason": None}


def embed_rec(child_rel, child, embedded, status="resolved", pointer=False):
    return {"type": {"relpath": child_rel, "qualname": child}, "embedded": embedded,
            "status": status, "external": None, "pointer": pointer}


def implements_rec(type_rel, type_q, iface, status="resolved", pointer_receiver=False):
    return {"type": {"relpath": type_rel, "qualname": type_q}, "iface": iface, "status": status,
            "external": None, "pointer_receiver": pointer_receiver}


# ── the world ───────────────────────────────────────────────────────

def entities() -> dict[str, dict]:
    return {
        FEE: record(doc="Package billing computes fees.\n", types=[
            typ("FeeCalculator", line=5, doc="FeeCalculator computes fees.\n", embeds=[
                {"text": "Base", "line": 6, "col": 2, "pointer": False},
                {"text": "sync.Mutex", "line": 7, "col": 3, "pointer": True}]),
            typ("Totaler", kind="interface", line=10,
                methods=[iface_method("Totaler", "Total", 11, doc="Total sums.\n")]),
            typ("Pair", line=13, type_params="[K comparable, V any]"),
        ], funcs=[
            fn("FeeCalculator.Calculate", "method", 14, recv("FeeCalculator", FEE),
               doc="Calculate returns the fee.\n",
               signature="func (c *FeeCalculator) Calculate(p int) int"),
            fn("FeeCalculator.Total", "method", 22, recv("FeeCalculator", FEE, pointer=False)),
            fn("New", line=26, doc="New makes one.\n"),
            fn("New.func1", "closure", 27, parent="New", signature="func()"),
            fn("New.func1.func1", "closure", 28, parent="New.func1", signature="func()"),
            fn("init#1", "init", 33, signature="func init()"),
            fn("<init>", "var_init", 36, signature=""),
            fn("<init>.func1", "closure", 37, parent="<init>", signature="func() int"),
        ]),
        EXTRA: record(funcs=[
            fn("FeeCalculator.Round", "method", 5, recv("FeeCalculator", FEE)),
            fn("FeeCalculator.Round.func1", "closure", 7, parent="FeeCalculator.Round"),
            fn("Ghost.Do", "method", 20, recv("Ghost", EXTRA, unresolved=True)),
        ]),
        BASE: record(types=[typ("Base", line=3)], funcs=[
            fn("Base.ID", "method", 6, recv("Base", BASE, pointer=False)),
            fn("Base.Write", "method", 9, recv("Base", BASE)),
        ]),
        FEE_TEST: record(origin="test", funcs=[fn("TestCalculate", line=7)]),
        XTEST: record(package=f"{BILLING}_test", name="billing_test", origin="xtest",
                      imports=[imp(BILLING, 3, col=8)], funcs=[fn("TestTotal", line=8)]),
        MAIN: record(package=f"{MOD}/cmd/reception", name="main", origin="command",
                     imports=[imp(BILLING, 5, alias="b"), imp("fmt", 6)],
                     funcs=[fn("main", line=9, signature="func main()")]),
        KDOC: record(package=f"{MOD}/libs/kernel", name="kernel",
                     doc="Package kernel holds shared types.\n"),
    }


def resolutions() -> dict[str, dict]:
    return {
        EXTRA: {"calls": [
            call("FeeCalculator.Round", "Calculate", 12, "resolved",
                 tgt(FEE, "FeeCalculator.Calculate")),
            call("FeeCalculator.Round", "Println", 13, "external",
                 external={"module": "std", "version": "", "package": "fmt",
                           "qualname": "Println", "peer": False}, via="none"),
            call("FeeCalculator.Round", "len", 14, "builtin", via="none"),
            call("FeeCalculator.Round", "fn", 15, "dynamic", via="none"),
            call("FeeCalculator.Round", "Missing", 16, "unresolved", via="none",
                 reason="missing-module"),
            call("FeeCalculator.Round", "func", 17, "resolved",
                 tgt(EXTRA, "FeeCalculator.Round.func1"), via="convention:funclit", kind="go"),
            call("FeeCalculator.Round", "Calculate", 11, "resolved",
                 tgt(FEE, "FeeCalculator.Calculate"), kind="defer"),
            call("FeeCalculator.Round", "mk", 10, "resolved", tgt(FEE, "New"),
                 via="convention:func-alias"),
        ]},
        MAIN: {"calls": [
            call("main", "Total", 9, "resolved", tgt(FEE, "Totaler.Total"),
                 candidates=[cand(FEE, "FeeCalculator.Total")], open_world=True),
            call("main", "Write", 10, "external", via="none",
                 external={"module": "std", "version": "", "package": "io",
                           "qualname": "Writer.Write", "peer": False},
                 candidates=[cand(BASE, "Base.Write")], open_world=True),
            call("main", "Describe", 11, "ambiguous", reason="implementation",
                 candidates=[cand(FEE, "FeeCalculator.Total"), cand(BASE, "Base.ID")]),
            call("main", "Calculate", 12, "ambiguous", reason="variant",
                 candidates=[cand(FEE, "FeeCalculator.Calculate"),
                             cand(EXTRA, "FeeCalculator.Round")]),
            call("main", "New", 13, "resolved", tgt(FEE, "New")),
            call("main", "Shared", 14, "external", tgt("lib/x.go", "Shared", repo="shared"),
                 via="none", external={"module": "example.com/shared", "version": "",
                                       "package": "example.com/shared", "qualname": "Shared",
                                       "peer": True}),
            call("main", "Gone", 15, "unresolved", via="none", reason="incomplete-type",
                 candidates=[cand(BASE, "Base.ID")]),
        ], "imports": [
            import_rec(5, BILLING, dir="libs/billing", clause="billing"),
            import_rec(6, "fmt", status="external"),
        ]},
        XTEST: {"calls": [], "imports": [
            import_rec(3, BILLING, dir="libs/billing", clause="billing", col=8)]},
        FEE: {"calls": [], "embeds": [
            embed_rec(FEE, "FeeCalculator", tgt(BASE, "Base", kind="class")),
            embed_rec(FEE, "FeeCalculator", None, status="external", pointer=True),
        ], "implements": [
            implements_rec(FEE, "FeeCalculator", tgt(FEE, "Totaler", kind="class")),
        ]},
        BASE: {"calls": [], "implements": [
            implements_rec(BASE, "Base", None, status="external", pointer_receiver=True)]},
    }


def make_data(ents=None, res=None, extra_files=(BAD,)) -> PipelineData:
    ents = entities() if ents is None else ents
    res = resolutions() if res is None else res
    hashes = {rel: f"sha-{rel}" for rel in [*ents, *extra_files]}
    for rel, ent in ents.items():
        ent["sha256"] = hashes[rel]
    files = {rel: {"sha256": hashes.get(rel), "calls": [], "imports": [], "embeds": [],
                   "implements": [], **frec} for rel, frec in res.items()}
    return PipelineData(target_dir="/repos/iryo", repo=REPO, file_hashes=hashes,
                        extracted_entities=ents,
                        resolutions={"version": 1, "repo": REPO, "files": files, "stats": {}})


def fid(rel):
    return _make_id("file", REPO, rel, "")


def cid(rel, q):
    return _make_id("class", REPO, rel, q)


def mid(rel, q):
    return _make_id("method", REPO, rel, q)


def pid(d, clause):
    return _make_id("package", REPO, d, clause)


def did(d):
    return _make_id("dir", REPO, d, "")
