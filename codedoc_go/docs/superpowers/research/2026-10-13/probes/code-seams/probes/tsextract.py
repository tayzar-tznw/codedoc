"""tsextract: probe for the codedoc_go extractor decision (option a).

tree-sitter 0.25.2 + tree-sitter-go 0.25.0 through the Python binding. Reads
file paths (one per line) on stdin, writes one JSON object per file on
stdout with the same keys as goextract (main.go): ERROR/MISSING counts,
package clause, build constraints, generated header, imports, declarations
with name ranges, call sites with callee-token ranges, receivers, embedded
fields, interface methods and type parameters.

Positions are 1-based lines and 0-based BYTE columns (tree-sitter points).
Usage: venv/bin/python -I tsextract.py [--parse-only] [--repeat N] < files
"""
from __future__ import annotations

import json
import re
import sys
import time

import tree_sitter as ts
import tree_sitter_go as tsgo

PARSER = ts.Parser(ts.Language(tsgo.language()))
GENERATED = re.compile(r"^// Code generated .* DO NOT EDIT\.$")
TYPELIT = {"slice_type", "array_type", "map_type", "channel_type", "function_type",
           "interface_type", "struct_type", "implicit_length_array_type"}


def pos(node: ts.Node) -> dict:
    return {"line": node.start_point[0] + 1, "col": node.start_point[1],
            "end_line": node.end_point[0] + 1, "end_col": node.end_point[1]}


def unparen(node: ts.Node | None) -> ts.Node | None:
    while node is not None and node.type == "parenthesized_expression":
        inner = [c for c in node.named_children if c.type != "comment"]
        if len(inner) != 1:
            break
        node = inner[0]
    return node


class Extractor:
    def __init__(self, src: bytes, tree: ts.Tree) -> None:
        self.src = src
        self.tree = tree
        self.out = {"imports": [], "decls": [], "calls": [], "funclits": 0, "inits": 0,
                    "blank_funcs": 0, "pkgvar_funclits": 0}
        self.stack: list[str] = []
        self.lits: list[int] = []
        self.inits = 0
        self.blanks = 0

    def text(self, node: ts.Node) -> str:
        return self.src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    # -- declarations ---------------------------------------------------
    def recv(self, plist: ts.Node) -> tuple[str, str, bool, bool]:
        params = [c for c in plist.named_children if c.type == "parameter_declaration"]
        if not params:
            return "", "?", False, False
        t = params[0].child_by_field_name("type")
        text = self.text(t) if t else ""
        ptr = generic = False
        t = unparen(t)
        if t is not None and t.type == "pointer_type":
            ptr = True
            t = unparen(t.named_children[0] if t.named_children else None)
        if t is not None and t.type == "generic_type":
            generic = True
            t = t.child_by_field_name("type")
        base = self.text(t) if t is not None and t.type == "type_identifier" else "?"
        return text, base, ptr, generic

    def tparams(self, parent: str, node: ts.Node | None) -> bool:
        if node is None:
            return False
        found = False
        for decl in node.named_children:
            if decl.type != "type_parameter_declaration":
                continue
            for i, child in enumerate(decl.children):
                if decl.field_name_for_child(i) == "name":
                    found = True
                    self.out["decls"].append({"k": "tparam", "name": self.text(child),
                                              "parent": parent, **pos(child)})
        return found

    def embedded_name(self, t: ts.Node | None) -> ts.Node | None:
        t = unparen(t)
        if t is not None and t.type == "pointer_type":
            t = unparen(t.named_children[0] if t.named_children else None)
        if t is not None and t.type == "generic_type":
            t = t.child_by_field_name("type")
        if t is None:
            return None
        if t.type == "type_identifier":
            return t
        if t.type == "qualified_type":
            return t.child_by_field_name("name")
        return None

    def type_body(self, name: str, t: ts.Node | None) -> None:
        if t is None:
            return
        if t.type == "struct_type":
            lst = next((c for c in t.named_children if c.type == "field_declaration_list"), None)
            for fd in (lst.named_children if lst else []):
                if fd.type != "field_declaration":
                    continue
                names = [c for i, c in enumerate(fd.children) if fd.field_name_for_child(i) == "name"]
                if names:
                    for n in names:
                        self.out["decls"].append({"k": "field", "name": self.text(n), "parent": name, **pos(n)})
                else:
                    tok = self.embedded_name(fd.child_by_field_name("type"))
                    node = tok if tok is not None else fd
                    self.out["decls"].append({"k": "embed", "name": self.text(tok) if tok else "",
                                              "parent": name, **pos(node)})
        elif t.type == "interface_type":
            for el in t.named_children:
                if el.type == "method_elem":
                    n = el.child_by_field_name("name")
                    self.out["decls"].append({"k": "imethod", "name": self.text(n), "parent": name,
                                              "q": f"{name}.{self.text(n)}", **pos(n)})
                elif el.type == "type_elem":
                    kids = [c for c in el.named_children if c.type != "comment"]
                    tok = self.embedded_name(kids[0]) if len(kids) == 1 else None
                    node = tok if tok is not None else el
                    self.out["decls"].append({"k": "iembed", "name": self.text(tok) if tok else "",
                                              "parent": name, **pos(node)})

    def top(self, node: ts.Node) -> None:
        for child in node.named_children:
            t = child.type
            if t == "function_declaration":
                n = child.child_by_field_name("name")
                name = self.text(n)
                q = name
                if name == "init":
                    self.inits += 1
                    q = f"init#{self.inits}"
                    self.out["inits"] += 1
                elif name == "_":
                    self.blanks += 1
                    q = f"_#{self.blanks}"
                    self.out["blank_funcs"] += 1
                d = {"k": "func", "name": name, **pos(n)}
                if self.tparams(q, child.child_by_field_name("type_parameters")):
                    d["generic"] = True
                d["q"] = q
                self.out["decls"].append(d)
                self.body(q, child.child_by_field_name("body"))
            elif t == "method_declaration":
                n = child.child_by_field_name("name")
                name = self.text(n)
                text, base, ptr, generic = self.recv(child.child_by_field_name("receiver"))
                q = f"{base}.{name}"
                if name == "_":
                    self.blanks += 1
                    q = f"{base}._#{self.blanks}"
                d = {"k": "method", "name": name, "recv": text, "recv_base": base, **pos(n), "q": q}
                if ptr:
                    d["ptr"] = True
                if generic:
                    d["generic"] = True
                self.out["decls"].append(d)
                self.body(q, child.child_by_field_name("body"))
            elif t == "type_declaration":
                for spec in child.named_children:
                    if spec.type not in ("type_spec", "type_alias"):
                        continue
                    n = spec.child_by_field_name("name")
                    name = self.text(n)
                    d = {"k": "type" if spec.type == "type_spec" else "alias", "name": name,
                         **pos(n), "q": name}
                    if self.tparams(name, spec.child_by_field_name("type_parameters")):
                        d["generic"] = True
                    self.out["decls"].append(d)
                    self.type_body(name, spec.child_by_field_name("type"))
            elif t in ("var_declaration", "const_declaration"):
                kind = "var" if t == "var_declaration" else "const"
                specs = []
                for c in child.named_children:
                    if c.type in ("var_spec", "const_spec"):
                        specs.append(c)
                    elif c.type == "var_spec_list":
                        specs += [s for s in c.named_children if s.type == "var_spec"]
                for spec in specs:
                    for i, c in enumerate(spec.children):
                        if spec.field_name_for_child(i) == "name":
                            self.out["decls"].append({"k": kind, "name": self.text(c), **pos(c)})
                    value = spec.child_by_field_name("value")
                    if value is not None:
                        before = self.out["funclits"]
                        self.body("<init>", value)
                        self.out["pkgvar_funclits"] += self.out["funclits"] - before

    # -- bodies -----------------------------------------------------------
    def body(self, q: str, node: ts.Node | None) -> None:
        if node is None:
            return
        self.stack.append(q)
        self.lits.append(0)
        self.walk(node)
        self.stack.pop()
        self.lits.pop()

    def callee(self, call: ts.Node) -> tuple[str, ts.Node | None, str, bool]:
        fn = unparen(call.child_by_field_name("function"))
        inst = call.child_by_field_name("type_arguments") is not None
        if fn is not None and fn.type == "index_expression":
            inst = True
            fn = unparen(fn.child_by_field_name("operand"))
        elif fn is not None and fn.type == "generic_type":
            inst = True
            fn = fn.child_by_field_name("type")
        if fn is None:
            return "", None, "other", inst
        if fn.type in ("identifier", "type_identifier"):
            return self.text(fn), fn, "ident", inst
        if fn.type == "selector_expression":
            f = fn.child_by_field_name("field")
            return self.text(f), f, "selector", inst
        if fn.type == "qualified_type":
            f = fn.child_by_field_name("name")
            return self.text(f), f, "selector", inst
        if fn.type == "func_literal":
            return "", None, "funclit", inst
        if fn.type == "call_expression":
            return "", None, "call", inst
        if fn.type in TYPELIT:
            return "", None, "typelit", inst
        if fn.type in ("unary_expression", "pointer_type"):
            return "", None, "star", inst
        return "", None, "other", inst

    def walk(self, node: ts.Node) -> None:
        stack = [node]
        while stack:
            n = stack.pop()
            t = n.type
            if t == "func_literal":
                self.out["funclits"] += 1
                i = len(self.lits) - 1
                self.lits[i] += 1
                parent = self.stack[i]
                name = f"{parent}.func{self.lits[i]}"
                self.body(name, n.child_by_field_name("body"))
                continue
            if t == "call_expression":
                name, tok, shape, inst = self.callee(n)
                c = {"name": name, "shape": shape, "caller": self.stack[-1]}
                if inst:
                    c["inst"] = True
                if tok is not None:
                    c["line"], c["col"], c["end_col"] = tok.start_point[0] + 1, tok.start_point[1], tok.end_point[1]
                else:
                    args = n.child_by_field_name("arguments")
                    a = args if args is not None else n
                    c["line"], c["col"], c["end_col"] = a.start_point[0] + 1, a.start_point[1], a.start_point[1]
                self.out["calls"].append(c)
            if t == "type_conversion_expression":
                self.out.setdefault("conversions", 0)
                self.out["conversions"] += 1
            stack.extend(reversed(n.children))

    # -- file-level facts ----------------------------------------------
    def run(self) -> dict:
        root = self.tree.root_node
        pkg = next((c for c in root.named_children if c.type == "package_clause"), None)
        out = self.out
        if pkg is not None:
            ident = next((c for c in pkg.named_children if c.type == "package_identifier"), None)
            if ident is not None:
                out["pkg"] = self.text(ident)
                out["pkg_line"], out["pkg_col"] = ident.start_point[0] + 1, ident.start_point[1]
        generated = False
        plus = []
        go_build = None
        for c in root.children:
            if pkg is not None and c.start_byte >= pkg.start_byte:
                break
            if c.type == "comment":
                line = self.text(c)
                if GENERATED.match(line):
                    generated = True
                if line.startswith("//go:build"):
                    go_build = line[len("//go:build"):].strip()
                elif line.startswith("// +build"):
                    plus.append(line[len("// +build"):].strip())
        out["generated"] = generated
        if go_build is not None:
            out["go_build"] = go_build
        if plus:
            out["plus_build"] = plus
        for imp in _iter(root, "import_spec"):
            path = imp.child_by_field_name("path")
            name = imp.child_by_field_name("name")
            item = {"path": self.text(path).strip('"`'), **pos(path)}
            if name is not None:
                item["name"] = self.text(name)
            out["imports"].append(item)
        self.top(root)
        return out


def _iter(node: ts.Node, kind: str):
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == kind:
            yield n
        else:
            stack.extend(reversed(n.children))


def errors(tree: ts.Tree) -> list[tuple[int, int, str]]:
    found = []
    if not tree.root_node.has_error:
        return found
    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        if n.is_missing:
            found.append((n.start_point[0] + 1, n.start_point[1], "MISSING " + n.type))
        elif n.type == "ERROR":
            found.append((n.start_point[0] + 1, n.start_point[1], "ERROR"))
        if n.has_error:
            stack.extend(n.children)
    return sorted(found)


def main() -> None:
    args = sys.argv[1:]
    parse_only = "--parse-only" in args
    repeat = int(args[args.index("--repeat") + 1]) if "--repeat" in args else 1
    paths = [line.strip() for line in sys.stdin if line.strip()]
    srcs = []
    for p in paths:
        with open(p, "rb") as f:
            srcs.append(f.read())
    w = sys.stdout
    parse_total = extract_total = 0.0
    err_files = 0
    for r in range(repeat):
        parse_total = extract_total = 0.0
        err_files = 0
        last = r == repeat - 1
        for p, src in zip(paths, srcs):
            t0 = time.perf_counter()
            tree = PARSER.parse(src)
            dt = time.perf_counter() - t0
            parse_total += dt
            errs = errors(tree)
            if errs:
                err_files += 1
            if parse_only or not last:
                continue
            t1 = time.perf_counter()
            rec = {"path": p, "bytes": len(src), "parse_us": int(dt * 1e6),
                   "nerrors": len(errs),
                   "errors": [{"line": l, "col": c, "msg": m} for l, c, m in errs[:5]]}
            rec.update(Extractor(src, tree).run())
            et = time.perf_counter() - t1
            extract_total += et
            rec["extract_us"] = int(et * 1e6)
            w.write(json.dumps(rec, ensure_ascii=True) + "\n")
    w.flush()
    sys.stderr.write(f"files={len(paths)} bytes={sum(map(len, srcs))} parse_ms={parse_total*1000:.1f} "
                     f"extract_ms={extract_total*1000:.1f} error_files={err_files}\n")


if __name__ == "__main__":
    main()
