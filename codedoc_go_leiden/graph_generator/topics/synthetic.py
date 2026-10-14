"""Synthetic C6 rows for the topic tests and the Phase 4a benchmark.

`RowBuilder` writes rows in the column shapes of parent spec §6.1–6.2 with
readable IDs (`file:<path>`, `class:<path>:<qualname>`,
`method:<path>:<qualname>`); the topics package never parses IDs, so real
`_make_id` IDs behave the same. `synthetic_repo(n_atoms, seed)` builds a
repo-sized graph: feature packages whose types call each other densely and
other features sparsely, a utility package everyone calls, interfaces with
implementers, embedding, tests and generated code.
"""

from __future__ import annotations

import posixpath
import random


class RowBuilder:
    def __init__(self, repo: str = "iryo", module: str = "example.com/iryo"):
        self.repo = repo
        self.module = module
        self.nodes: dict[str, list[dict]] = {"Files": [], "Packages": [], "Classes": [], "Methods": []}
        self.edges: dict[str, list[dict]] = {"MethodCalls": [], "ClassEmbeds": [], "ClassImplements": [],
                                             "PossiblyCalls": []}
        self._files: dict[str, dict] = {}
        self._classes: dict[str, dict] = {}
        self._methods: dict[str, dict] = {}
        self._line = 0

    def file(self, path: str, origin: str = "package", generated: bool = False, summary: str = "",
             build_included: bool = True) -> str:
        """Files row plus the file's `<package>` pseudo-type; returns the file_id."""
        if path in self._files:
            return self._files[path]["file_id"]
        directory = posixpath.dirname(path)
        package = f"{self.module}/{directory}" if directory else self.module
        row = {"file_id": f"file:{path}", "file_name": posixpath.basename(path), "extension": ".go",
               "directory": directory, "path": path, "origin": origin, "generated": generated,
               "package": package, "package_name": posixpath.basename(directory) or "main",
               "module": self.module, "build_constraint": "", "build_included": build_included,
               "summary": summary, "repo": self.repo}
        self._files[path] = row
        self.nodes["Files"].append(row)
        self.type(path, "<package>", kind="package")
        return row["file_id"]

    def type(self, path: str, name: str, kind: str = "struct", summary: str = "") -> str:
        """Classes row in `path` (created on first use); returns the class_id."""
        self.file(path)
        f = self._files[path]
        cid = f"class:{path}:{name}"
        row = {"class_id": cid, "name": name, "qualname": name, "package": f["package"],
               "fqcn": f"{f['package']}.{name}", "file_id": f["file_id"], "kind": kind, "type_params": "",
               "embeds": "", "start_line": 1, "end_line": 2, "origin": f["origin"],
               "module": self.module, "summary": summary, "repo": self.repo}
        self._classes[cid] = row
        self.nodes["Classes"].append(row)
        return cid

    def package_atom(self, path: str) -> str:
        self.file(path)
        return f"class:{path}:<package>"

    def method(self, owner: str, name: str, member_kind: str = "method", path: str | None = None,
               class_id: str | None = "<owner>", summary: str = "") -> str:
        """Methods row owned by the atom `owner` (declared in `path`, default
        the owner's file); `class_id=None` writes a row without an owner."""
        c = self._classes[owner]
        if path:
            self.file(path)
        file_row = self._files[path] if path else next(f for f in self._files.values()
                                                      if f["file_id"] == c["file_id"])
        qual = name if c["kind"] == "package" else f"{c['name']}.{name}"
        mid = f"method:{file_row['path']}:{qual}"
        self._line += 1
        row = {"method_id": mid, "name": name.rsplit(".", 1)[-1],
               "class_id": owner if class_id == "<owner>" else class_id,
               "file_id": file_row["file_id"], "qualname": qual, "fqmn": f"{c['package']}.{qual}",
               "signature": f"func {name}()", "member_kind": member_kind, "receiver": "",
               "type_params": "", "has_body": member_kind != "interface_method",
               "start_line": self._line, "end_line": self._line, "origin": file_row["origin"],
               "module": self.module, "entry_point": "", "summary": summary, "repo": self.repo}
        self._methods[mid] = row
        self.nodes["Methods"].append(row)
        return mid

    def func(self, path: str, name: str, summary: str = "") -> str:
        return self.method(self.package_atom(path), name, member_kind="func", summary=summary)

    def closure(self, parent: str, ordinal: int = 1, class_id: str | None = "<owner>") -> str:
        p = self._methods[parent]
        owner = p["class_id"]
        path = next(f["path"] for f in self._files.values() if f["file_id"] == p["file_id"])
        qual = f"{p['qualname']}.func{ordinal}"
        mid = f"method:{path}:{qual}"
        row = dict(p, method_id=mid, name=f"func{ordinal}", qualname=qual, member_kind="closure",
                   class_id=owner if class_id == "<owner>" else class_id)
        self._methods[mid] = row
        self.nodes["Methods"].append(row)
        return mid

    def call(self, caller: str, callee: str, resolution: str = "types", site_kind: str = "call") -> None:
        self.edges["MethodCalls"].append({
            "edge_id": f"MethodCalls:{caller}->{callee}", "caller_method": caller, "callee_method": callee,
            "callee_name": self._methods[callee]["name"], "resolution": resolution,
            "site_kind": site_kind, "call_line": self._methods[caller]["start_line"]})

    def possibly(self, caller: str, callee: str) -> None:
        self.edges["PossiblyCalls"].append({
            "edge_id": f"PossiblyCalls:{caller}->{callee}", "caller_method": caller, "callee_method": callee,
            "callee_name": self._methods[callee]["name"], "reason": "implementation",
            "candidate_count": 1, "open_world": False})

    def embed(self, child: str, embedded: str, pointer: bool = False) -> None:
        self.edges["ClassEmbeds"].append({"edge_id": f"ClassEmbeds:{child}->{embedded}", "child_class": child,
                                          "embedded_class": embedded, "pointer": pointer, "resolution": "types"})

    def implements(self, cls: str, iface: str, pointer_receiver: bool = False) -> None:
        self.edges["ClassImplements"].append({"edge_id": f"ClassImplements:{cls}->{iface}", "class_id": cls,
                                              "interface_id": iface, "pointer_receiver": pointer_receiver,
                                              "resolution": "types"})

    def rows(self) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
        return self.nodes, self.edges


def synthetic_repo(n_atoms: int = 5000, seed: int = 0, feature_size: int = 40,
                   cross_rate: float = 0.03) -> tuple[dict, dict, dict[str, list[str]]]:
    """(nodes, edges, features): about `n_atoms` eligible atoms in features of
    about `feature_size` atoms; `features` maps a feature to its file paths."""
    rng = random.Random(seed)
    b = RowBuilder()
    features: dict[str, list[str]] = {}
    feature_methods: list[list[str]] = []
    n_features = max(2, n_atoms // feature_size)
    util_methods = [b.func("pkg/util/strings.go", f"Util{i}") for i in range(6)]
    b.file("pkg/gen/api.pb.go", generated=True)
    gen_types = [b.type("pkg/gen/api.pb.go", f"Msg{i}") for i in range(4)]
    gen_methods = [b.method(t, "Marshal") for t in gen_types]
    atoms = 2 + len(gen_types)
    for fi in range(n_features):
        if atoms >= n_atoms:
            break
        base = f"pkg/feat{fi:03d}"
        files = [f"{base}/f{k}.go" for k in range(max(1, feature_size // 5))]
        features[f"feat{fi:03d}"] = list(files)
        methods: list[str] = []
        types: list[str] = []
        iface = b.type(files[0], f"Store{fi}", kind="interface")
        iface_methods = [b.method(iface, "Get", member_kind="interface_method"),
                         b.method(iface, "Put", member_kind="interface_method")]
        atoms += 1
        for path in files:
            atoms += 1  # the <package> pseudo-type
            methods += [b.func(path, f"Helper{rng.randrange(10**6)}") for _ in range(rng.randint(1, 3))]
            for _ in range(rng.randint(2, 4)):
                t = b.type(path, f"T{fi}x{rng.randrange(10**6)}")
                types.append(t)
                atoms += 1
                methods += [b.method(t, f"M{j}") for j in range(rng.randint(2, 5))]
        for t in rng.sample(types, k=max(1, len(types) // 4)):
            b.implements(t, iface)
        for _ in range(max(1, len(types) // 6)):
            child, parent = rng.sample(types, 2)
            b.embed(child, parent)
        for m in methods:
            for callee in rng.sample(methods, k=min(len(methods), rng.randint(1, 3))):
                if callee != m:
                    b.call(m, callee)
            if rng.random() < 0.3:
                b.call(m, rng.choice(iface_methods))
            if rng.random() < 0.2:
                b.call(m, rng.choice(util_methods))
            if rng.random() < 0.02:
                b.call(m, rng.choice(gen_methods))
        test_path = f"{base}/f0_test.go"
        b.file(test_path, origin="test")
        tt = b.type(test_path, f"fixture{fi}")
        for _ in range(3):
            b.call(b.method(tt, f"Check{rng.randrange(10**6)}"), rng.choice(methods))
        feature_methods.append(methods)
    for _ in range(int(cross_rate * sum(len(m) for m in feature_methods))):
        a, c = rng.sample(range(len(feature_methods)), 2)
        b.call(rng.choice(feature_methods[a]), rng.choice(feature_methods[c]))
    nodes, edges = b.rows()
    return nodes, edges, features
