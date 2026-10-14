"""Shared helpers for the Go ground-truth tools (stdlib only).

Two parts:

1. Case helpers for ground_truth/cases.py (master plan C10). A cases.py starts
   with
       import os, sys
       sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "_gt"))
       from gt_lib import target, resolved, ambiguous, external, peer, plain, case

2. A small Go reader: a tokenizer with automatic semicolons and a structural
   parser that finds declarations under the C3 qualified-name grammar and the
   call sites of spec §4.3 (positions are 1-based lines and 1-based byte
   columns, offsets 0-based bytes). It is not a full Go parser: it parses the
   statement and type structure it needs and skips the rest. Plus readers for
   go.mod and go.work, module ownership and the _proxy layout.

generate_manifest.py derives case positions from snippets on its own; the
checker re-derives them here independently. goracle (go/parser) computes the
same qualified names, and run_oracle.py fails a case whose target it cannot
map, so a disagreement between this reader and Go's parser surfaces there.
"""
from __future__ import annotations

import os
import re

# ═══════════════════════════════════════════════════════════════ case helpers

STATUSES = ("resolved", "external", "builtin", "ambiguous", "dynamic", "unresolved", "conversion")
SITE_KINDS = ("call", "go", "defer", "range")
TARGET_KINDS = ("method", "class")


def target(qualname, module, kind, file, repo=None):
    """A declaration: qualname per C3, module path, "method" or "class", fixture-relative file.

    repo names the repo of a peer target (F3); leave it out elsewhere.
    """
    out = {"qualname": qualname, "module": module, "kind": kind, "file": file}
    if repo is not None:
        out["repo"] = repo
    return out


def _expect(status, targets=(), candidates=(), static=None, runtime=(), open_world=None,
            external_module=None, reason=None):
    return {"status": status, "targets": list(targets), "candidates": list(candidates),
            "static_target": static, "runtime_targets": list(runtime), "open_world": open_world,
            "external": {"module": external_module} if external_module is not None else None,
            "reason": reason}


def resolved(*targets, static=None, runtime=(), candidates=(), open_world=None):
    """One in-repo target. Interface calls: the interface method as target, the
    implementers the program runs as runtime, the in-repo implementers as candidates."""
    return _expect("resolved", targets, candidates, static, runtime, open_world)


def ambiguous(*candidates, runtime=()):
    return _expect("ambiguous", (), candidates, None, runtime)


def external(module):
    """A target outside the repo: a module path, "std" or "C"."""
    return _expect("external", external_module=module)


def peer(*targets, module, runtime=(), candidates=(), open_world=None):
    """An external target in a peer repo (F3); every target carries repo=... A call on a
    peer interface method lists the in-repo implementers as candidates (ruling Q2)."""
    return _expect("external", targets, candidates, None, runtime, open_world, external_module=module)


def plain(status, reason=None, runtime=()):
    """dynamic, unresolved, builtin or conversion; runtime lists the in-repo functions the
    program runs there. conversion is correct when no record exists at the site (master C10)."""
    return _expect(status, (), (), None, runtime, reason=reason)


def case(cid, file, snippet, token, kind, expect, why, entry="reception-desk", occurrence=1, distractors=(),
         offline=None):
    """One call site. kind is the site kind (call, go, defer, range); entry names the
    entries.json entry that executes the site, or None when no entry does. expect is the
    answer of evaluate's env (download) mode; offline is the offline mode's answer where
    it really differs (master C10), else None. Distractors apply to both."""
    expect = dict(expect)
    expect["distractors"] = list(distractors)
    if offline is not None:
        offline = dict(offline)
        offline["distractors"] = list(distractors)
    return {"id": cid, "category": cid.split("-")[0], "file": file, "snippet": snippet, "token": token,
            "occurrence": occurrence, "site_kind": kind, "expect": expect, "expect_offline": offline,
            "why": why, "entry": entry}


# ═══════════════════════════════════════════════════════════════ files

def read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def split_lines(data: bytes) -> list[bytes]:
    """Lines as go/token counts them: only \\n ends a line."""
    return data.split(b"\n")


# ═══════════════════════════════════════════════════════════════ tokenizer

KEYWORDS = frozenset("break case chan const continue default defer else fallthrough for func go goto if "
                     "import interface map package range return select struct switch type var".split())
_OPS = sorted("<<= >>= &^= ... && || <- ++ -- == != <= >= := += -= *= /= %= &= |= ^= << >> &^ ~ "
              "+ - * / % & | ^ < > = ! ( ) [ ] { } , ; . :".split(), key=len, reverse=True)
_TOKEN_RE = re.compile(rb"""
    (?P<nl>\n)
  | (?P<ws>[ \t\r\f\v]+)
  | (?P<lc>//[^\n]*)
  | (?P<gc>/\*.*?\*/)
  | (?P<raw>`[^`]*`)
  | (?P<str>"(?:[^"\\\n]|\\.)*")
  | (?P<char>'(?:[^'\\\n]|\\.)*')
  | (?P<num>0[xX][0-9a-fA-F_]*(?:\.[0-9a-fA-F_]*)?(?:[pP][+-]?[0-9_]+)?i?
           |0[bBoO][0-9_]+i?
           |(?:[0-9][0-9_]*(?:\.[0-9_]*)?|\.[0-9][0-9_]*)(?:[eE][+-]?[0-9_]+)?i?)
  | (?P<ident>[A-Za-z_\x80-\xff][A-Za-z0-9_\x80-\xff]*)
  | (?P<op>""" + b"|".join(re.escape(op.encode()) for op in _OPS) + rb""")
""", re.S | re.X)
_SEMI_AFTER_OPS = frozenset(("++", "--", ")", "]", "}"))
_SEMI_AFTER_KW = frozenset(("break", "continue", "fallthrough", "return"))


class Tok:
    __slots__ = ("kind", "text", "line", "col", "offset", "end", "auto")

    def __init__(self, kind, text, line, col, offset, end, auto=False):
        self.kind, self.text, self.line, self.col, self.offset, self.end, self.auto = \
            kind, text, line, col, offset, end, auto

    def __repr__(self) -> str:
        return f"Tok({self.kind},{self.text!r},{self.line}:{self.col})"


def tokenize(data: bytes) -> list[Tok]:
    """Go tokens with automatic semicolons (kind "semi"); bytes >= 0x80 outside
    literals and comments belong to identifiers, as in valid Go."""
    out: list[Tok] = []
    line, line_start, pos, n = 1, 0, 0, len(data)

    def needs_semi() -> bool:
        if not out:
            return False
        last = out[-1]
        return (last.kind in ("ident", "num", "str", "char") or
                (last.kind == "keyword" and last.text in _SEMI_AFTER_KW) or
                (last.kind == "op" and last.text in _SEMI_AFTER_OPS))

    while pos < n:
        m = _TOKEN_RE.match(data, pos)
        if m is None:
            pos += 1  # stray byte (syntax error): skip it
            continue
        kind = m.lastgroup
        text = m.group(kind)
        if kind == "nl" or (kind == "gc" and b"\n" in text):
            if needs_semi():
                out.append(Tok("semi", "\n", line, pos - line_start + 1, pos, pos, auto=True))
        elif kind not in ("ws", "lc", "gc"):
            tkind = {"raw": "str", "str": "str"}.get(kind, kind)
            word = text.decode("utf-8", errors="replace")
            if kind == "ident" and word in KEYWORDS:
                tkind = "keyword"
            elif kind == "op" and word == ";":
                tkind = "semi"
            out.append(Tok(tkind, word, line, pos - line_start + 1, pos, m.end()))
        newlines = text.count(b"\n")
        if newlines:
            line += newlines
            line_start = pos + text.rfind(b"\n") + 1
        pos = m.end()
    if needs_semi():
        out.append(Tok("semi", "", line, pos - line_start + 1, pos, pos, auto=True))
    out.append(Tok("eof", "", line, pos - line_start + 1, pos, pos))
    return out


# ═══════════════════════════════════════════════════════════════ parser

_OPENERS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = {")": "(", "]": "[", "}": "{"}
_TYPE_START_KW = frozenset(("map", "chan", "func", "struct", "interface"))


class Decl:
    """A declaration under the C3 grammar. kind is "method" (funcs, methods,
    closures, interface methods, init#k, <init>) or "class" (types, <package>)."""

    __slots__ = ("qualname", "kind", "member", "name", "line", "col", "offset", "decl_line", "parent",
                 "receiver", "pointer")

    def __init__(self, qualname, kind, member, name, tok, decl_line, parent=None, receiver=None, pointer=False):
        self.qualname, self.kind, self.member, self.name = qualname, kind, member, name
        self.line, self.col, self.offset = (tok.line, tok.col, tok.offset) if tok else (decl_line, 0, -1)
        self.decl_line, self.parent, self.receiver, self.pointer = decl_line, parent, receiver, pointer

    def __repr__(self) -> str:
        return f"Decl({self.qualname},{self.member},{self.line}:{self.col})"


class Site:
    """A call site: kind call/go/defer/range, the callee token, shape, caller qualname."""

    __slots__ = ("kind", "name", "line", "col", "offset", "shape", "caller", "lparen")

    def __init__(self, kind, name, tok, shape, caller, lparen):
        self.kind, self.name, self.shape, self.caller, self.lparen = kind, name, shape, caller, lparen
        self.line, self.col, self.offset = (tok.line, tok.col, tok.offset) if tok else (0, 0, -1)

    def __repr__(self) -> str:
        return f"Site({self.kind},{self.name},{self.line}:{self.col},{self.shape},{self.caller})"


class _Ctx:
    __slots__ = ("qualname", "lits")

    def __init__(self, qualname):
        self.qualname, self.lits = qualname, 0


class GoFile:
    """Declarations, imports and call sites of one Go source file."""

    def __init__(self, data: bytes, name: str = "<source>") -> None:
        self.name = name
        self.data = data
        self.toks = tokenize(data)
        self.match = self._match_brackets()
        self.package: str | None = None
        self.package_line = 0
        self.imports: list[dict] = []
        self.decls: list[Decl] = []
        self.sites: list[Site] = []
        self.calls: dict[int, Site] = {}        # lparen token index -> site (only real calls)
        self.lit_end: dict[int, int] = {}       # closing brace index of a func literal -> func token index
        self.type_ends: set[int] = set()        # token indexes directly after a skipped type literal
        self._init_counter = 0
        self._blank_counter = 0
        self._init_ctx = _Ctx("<init>")
        self._init_used = False
        self._init_line = 0
        self._parse()

    # ------------------------------------------------------------ brackets
    def _match_brackets(self) -> dict[int, int]:
        match: dict[int, int] = {}
        stack: list[int] = []
        for i, t in enumerate(self.toks):
            if t.kind != "op":
                continue
            if t.text in _OPENERS:
                stack.append(i)
            elif t.text in _CLOSERS:
                # tolerate syntax errors: pop to the nearest matching opener
                for depth in range(len(stack) - 1, -1, -1):
                    if self.toks[stack[depth]].text == _CLOSERS[t.text]:
                        j = stack[depth]
                        del stack[depth:]
                        match[i], match[j] = j, i
                        break
        return match

    def _tok(self, i: int) -> Tok:
        return self.toks[i] if 0 <= i < len(self.toks) else self.toks[-1]

    def _is(self, i: int, text: str) -> bool:
        t = self._tok(i)
        return t.text == text and t.kind in ("op", "keyword", "semi")

    def _skip_brackets(self, i: int) -> int:
        """Index after the bracket group opened at i."""
        return self.match.get(i, len(self.toks) - 2) + 1

    def _stmt_end(self, i: int, stop: int) -> int:
        """Index of the semicolon or closer that ends the statement starting at i."""
        while i < stop:
            t = self.toks[i]
            if t.kind == "op" and t.text in _OPENERS:
                i = self._skip_brackets(i)
                continue
            if t.kind in ("semi", "eof") or (t.kind == "op" and t.text in _CLOSERS):
                return i
            i += 1
        return stop

    # ------------------------------------------------------------ top level
    def _parse(self) -> None:
        toks, i, n = self.toks, 0, len(self.toks) - 1
        while i < n and toks[i].kind == "semi":
            i += 1
        if self._is(i, "package") and toks[i + 1].kind == "ident":
            self.package = toks[i + 1].text
            self.package_line = toks[i].line
            self.decls.append(Decl("<package>", "class", "package", "<package>", toks[i + 1], toks[i].line))
            i += 2
        while i < n:
            t = toks[i]
            if t.kind == "keyword" and t.text == "import":
                i = self._import_decl(i)
            elif t.kind == "keyword" and t.text == "func":
                i = self._func_decl(i)
            elif t.kind == "keyword" and t.text == "type":
                i = self._type_decl(i, top=True)
            elif t.kind == "keyword" and t.text in ("var", "const"):
                i = self._value_decl(i)
            elif t.kind == "op" and t.text in _OPENERS:
                i = self._skip_brackets(i)
            else:
                i += 1
        if self._init_used:
            self.decls.append(Decl("<init>", "method", "var_init", "<init>", None, self._init_line))

    def _import_decl(self, i: int) -> int:
        j = i + 1
        if self._is(j, "("):
            end = self.match.get(j, len(self.toks) - 1)
            k = j + 1
            while k < end:
                k = self._import_spec(k, end)
            return end + 1
        return self._import_spec(j, len(self.toks) - 1)

    def _import_spec(self, k: int, stop: int) -> int:
        toks = self.toks
        while k < stop and toks[k].kind == "semi":
            k += 1
        if k >= stop:
            return stop
        alias, kind = "", "normal"
        if toks[k].kind == "ident" or self._is(k, "."):
            alias = toks[k].text
            kind = "dot" if alias == "." else ("blank" if alias == "_" else "normal")
            k += 1
        if toks[k].kind == "str":
            t = toks[k]
            self.imports.append({"path": t.text.strip('"`'), "alias": alias, "kind": kind,
                                 "line": t.line, "col": t.col, "offset": t.offset})
            k += 1
        while k < stop and toks[k].kind != "semi":
            k += 1
        return k + 1

    def _func_decl(self, i: int) -> int:
        toks = self.toks
        j = i + 1
        receiver, pointer = None, False
        if self._is(j, "("):
            inner = [t for t in toks[j + 1:self.match.get(j, j)]]
            cut = next((x for x, t in enumerate(inner) if t.text == "[" and t.kind == "op"), len(inner))
            names = [t for t in inner[:cut] if t.kind == "ident"]
            pointer = any(t.text == "*" and t.kind == "op" for t in inner[:cut])
            receiver = names[-1].text if names else None
            j = self._skip_brackets(j)
        if toks[j].kind != "ident":
            return j
        name_tok = toks[j]
        name = name_tok.text
        j += 1
        if self._is(j, "["):
            j = self._skip_brackets(j)
        if self._is(j, "("):
            j = self._skip_brackets(j)
        j = self._skip_result(j)
        if receiver is not None:
            qual, member = f"{receiver}.{name}", "method"
        elif name == "init":
            self._init_counter += 1
            qual, member = f"init#{self._init_counter}", "init"
        elif name == "_":
            self._blank_counter += 1
            qual, member = f"_#{self._blank_counter}", "func"
        else:
            qual, member = name, "func"
        self.decls.append(Decl(qual, "method", member, name, name_tok, toks[i].line, receiver=receiver,
                               pointer=pointer))
        if self._is(j, "{"):
            end = self.match.get(j, len(toks) - 1)
            self._scan(j + 1, end, _Ctx(qual))
            return end + 1
        return j

    def _type_decl(self, i: int, top: bool) -> int:
        j = i + 1
        if self._is(j, "("):
            end = self.match.get(j, len(self.toks) - 1)
            k = j + 1
            while k < end:
                if self.toks[k].kind == "semi":
                    k += 1
                    continue
                spec_end = self._stmt_end(k, end)
                if top:
                    self._type_spec(k, spec_end)
                k = spec_end + 1
            return end + 1
        spec_end = self._stmt_end(j, len(self.toks) - 1)
        if top:
            self._type_spec(j, spec_end)
        return spec_end + 1

    def _type_params_follow(self, j: int) -> bool:
        """At "[" after a type name: a type parameter list (not an array length)?"""
        a, b = self._tok(j + 1), self._tok(j + 2)
        if a.kind != "ident":
            return False
        return b.kind == "ident" or (b.kind in ("op", "keyword") and b.text in
                                     ("[", "(", "~", ",", "interface", "func", "map", "chan", "struct"))

    def _type_spec(self, k: int, end: int) -> None:
        toks = self.toks
        if toks[k].kind != "ident":
            return
        name_tok = toks[k]
        j = k + 1
        if self._is(j, "[") and self._type_params_follow(j):
            j = self._skip_brackets(j)
        if self._is(j, "="):
            kind = "alias"
        elif self._is(j, "struct"):
            kind = "struct"
        elif self._is(j, "interface"):
            kind = "interface"
        else:
            kind = "named"
        self.decls.append(Decl(name_tok.text, "class", kind, name_tok.text, name_tok, name_tok.line))
        if kind == "interface" and self._is(j + 1, "{"):
            close = self.match.get(j + 1, end)
            m = j + 2
            while m < close:
                if toks[m].kind == "semi":
                    m += 1
                    continue
                elem_end = self._stmt_end(m, close)
                if toks[m].kind == "ident" and self._is(m + 1, "("):
                    mt = toks[m]
                    self.decls.append(Decl(f"{name_tok.text}.{mt.text}", "method", "interface_method", mt.text,
                                           mt, mt.line, receiver=name_tok.text))
                m = elem_end + 1

    def _value_decl(self, i: int) -> int:
        is_var = self.toks[i].text == "var"
        j = i + 1
        if self._is(j, "("):
            end = self.match.get(j, len(self.toks) - 1)
            k = j + 1
            while k < end:
                if self.toks[k].kind == "semi":
                    k += 1
                    continue
                spec_end = self._stmt_end(k, end)
                if is_var:
                    self._var_spec(k, spec_end)
                k = spec_end + 1
            return end + 1
        spec_end = self._stmt_end(j, len(self.toks) - 1)
        if is_var:
            self._var_spec(j, spec_end)
        return spec_end + 1

    def _var_spec(self, k: int, end: int) -> None:
        eq = k
        while eq < end and not self._is(eq, "="):
            t = self.toks[eq]
            eq = self._skip_brackets(eq) if (t.kind == "op" and t.text in _OPENERS) else eq + 1
        if eq >= end:
            return
        before = (len(self.sites), self._init_ctx.lits)
        self._scan(eq + 1, end, self._init_ctx)
        if (len(self.sites), self._init_ctx.lits) != before:
            if not self._init_used:
                self._init_line = self.toks[k].line
            self._init_used = True

    # ------------------------------------------------------------ types
    def _skip_result(self, j: int) -> int:
        """After a parameter list: skip an optional result (a parenthesized list or one type)."""
        t = self._tok(j)
        if self._is(j, "("):
            return self._skip_brackets(j)
        if t.kind == "ident" or (t.kind in ("op", "keyword") and t.text in ("*", "[", "<-") + tuple(_TYPE_START_KW)):
            return self._skip_type(j)
        return j

    def _skip_type(self, j: int) -> int:
        """Index after the type that starts at j (j itself when no type starts there)."""
        t = self._tok(j)
        if t.kind == "ident":
            j += 1
            if self._is(j, ".") and self._tok(j + 1).kind == "ident":
                j += 2
            if self._is(j, "["):
                j = self._skip_brackets(j)
            return j
        if t.kind not in ("op", "keyword"):
            return j
        if t.text == "(":
            return self._skip_brackets(j)
        if t.text == "*":
            return self._skip_type(j + 1)
        if t.text == "[":
            return self._skip_type(self._skip_brackets(j))
        if t.text == "map":
            return self._skip_type(self._skip_brackets(j + 1)) if self._is(j + 1, "[") else j + 1
        if t.text == "chan":
            j += 1
            if self._is(j, "<-"):
                j += 1
            return self._skip_type(j)
        if t.text == "<-" and self._is(j + 1, "chan"):
            return self._skip_type(j + 2)
        if t.text in ("struct", "interface"):
            return self._skip_brackets(j + 1) if self._is(j + 1, "{") else j + 1
        if t.text == "func":
            j += 1
            if self._is(j, "("):
                j = self._skip_brackets(j)
            return self._skip_result(j)
        return j

    # ------------------------------------------------------------ bodies
    def _operand_end(self, i: int) -> bool:
        t = self._tok(i)
        return t.kind in ("ident", "num", "str", "char") or (t.kind == "op" and t.text in (")", "]", "}"))

    def _scan(self, start: int, end: int, ctx: _Ctx) -> None:
        """Find func literals, call sites, go/defer statements and range sites in [start, end)."""
        toks = self.toks
        stmts: list[tuple[str, int]] = []
        i = start
        while i < end:
            t = toks[i]
            if t.kind == "keyword":
                if t.text == "func":
                    i = self._func_lit_or_type(i, end, ctx)
                    continue
                if t.text in ("map", "chan", "struct", "interface"):
                    j = self._skip_type(i)
                    self.type_ends.add(j)
                    i = max(j, i + 1)
                    continue
                if t.text == "type":
                    i = self._type_decl(i, top=False)
                    continue
                if t.text in ("go", "defer"):
                    stmts.append((t.text, i))
                elif t.text == "range":
                    self._range_site(i, end, ctx)
            elif t.kind == "op":
                if t.text == "<-" and self._is(i + 1, "chan") and not self._operand_end(i - 1):
                    j = self._skip_type(i)
                    self.type_ends.add(j)
                    i = max(j, i + 1)
                    continue
                if t.text == "[" and not self._operand_end(i - 1):
                    j = self._skip_type(i)
                    self.type_ends.add(j)
                    i = max(j, i + 1)
                    continue
                if t.text == "(" and self._operand_end(i - 1):
                    self._call_site(i, ctx)
            i += 1
        for kind, k in stmts:
            last = self._stmt_end(k + 1, end) - 1
            if self._is(last, ")"):
                site = self.calls.get(self.match.get(last, -1))
                if site is not None:
                    site.kind = kind

    def _func_lit_or_type(self, i: int, end: int, ctx: _Ctx) -> int:
        j = i + 1
        if self._is(j, "("):
            j = self._skip_brackets(j)
        j = self._skip_result(j)
        if self._is(j, "{") and j < end:
            ctx.lits += 1
            qual = f"{ctx.qualname}.func{ctx.lits}"
            self.decls.append(Decl(qual, "method", "closure", "func", self.toks[i], self.toks[i].line,
                                   parent=ctx.qualname))
            close = self.match.get(j, end)
            self.lit_end[close] = i
            self._scan(j + 1, close, _Ctx(qual))
            return close + 1
        self.type_ends.add(j)
        return max(j, i + 1)

    def _call_site(self, lp: int, ctx: _Ctx) -> None:
        """The site of the call whose ( is toks[lp], with the callee token extract.Sites gives it
        (spec §4.3, master plan C2): an identifier or selector, an index expression whose base is
        one, or a func literal, through parentheses; for any other callee, conversions to type
        literals included, the ( itself and no name. The outer call of f()(x) is no site."""
        toks = self.toks
        prev = lp - 1
        p = toks[prev]
        other = Site("call", "", toks[lp], "other", ctx.qualname, lp)
        if lp in self.type_ends:
            self._add_site(other, lp)  # conversion to a type literal
            return
        if p.kind == "ident":
            shape = "selector" if self._is(prev - 1, ".") else "ident"
            self._add_site(Site("call", p.text, p, shape, ctx.qualname, lp), lp)
        elif p.text == "]":
            base = self._index_base(prev)
            if base is None:
                self._add_site(other, lp)
            else:
                self._add_site(Site("call", base.text, base, "index", ctx.qualname, lp), lp)
        elif p.text == "}":
            fn = self.lit_end.get(prev)
            if fn is not None:
                self._add_site(Site("call", "func", toks[fn], "funclit", ctx.qualname, lp), lp)
            else:
                self._add_site(other, lp)
        elif p.text == ")":
            op = self.match.get(prev, -1)
            if op in self.calls:
                return  # f()(x): the inner call is the site (spec §4.3)
            if op < 0 or self._is(op - 1, "."):
                self._add_site(other, lp)  # x.(T)(y)
                return
            callee = self._callee_at(op, prev)
            if callee == "call":
                return  # (f())(x)
            if callee is None:
                self._add_site(Site("call", "", toks[lp], "paren", ctx.qualname, lp), lp)
            else:
                self._add_site(Site("call", callee[0], callee[1], callee[2], ctx.qualname, lp), lp)

    def _index_base(self, close: int) -> Tok | None:
        """The base identifier of the index expression whose ] is toks[close], when the base
        is an identifier or a selector (through parentheses), else None."""
        ob = self.match.get(close, -1)
        if ob <= 0 or self._slice(ob, close):
            return None
        b = self.toks[ob - 1]
        if b.kind == "ident":
            return b
        if b.text == ")" and b.kind == "op":
            op = self.match.get(ob - 1, -1)
            if op >= 0 and not self._operand_end(op - 1) and not self._is(op - 1, "."):
                callee = self._callee_at(op, ob - 1)
                if isinstance(callee, tuple) and callee[2] in ("ident", "selector"):
                    return callee[1]
        return None

    def _callee_at(self, lo: int, hi: int):
        """extract.callee on the expression toks[lo..hi]: (name, token, shape) for an identifier,
        a selector, an index expression whose base is one, or a func literal, through
        parentheses; "call" for a call; None for any other expression."""
        while self._is(lo, "(") and self.match.get(lo) == hi:
            lo, hi = lo + 1, hi - 1
        if hi < lo or not self._primary(lo, hi):
            return None
        t = self.toks[hi]
        if t.kind == "ident":
            if hi == lo:
                return t.text, t, "ident"
            return (t.text, t, "selector") if self._is(hi - 1, ".") else None
        if t.kind != "op":
            return None
        if t.text == "}":
            if self.lit_end.get(hi) == lo or (self._is(lo, "func") and self._func_lit_close(lo) == hi):
                return "func", self.toks[lo], "funclit"
            return None
        if t.text == ")":
            op = self.match.get(hi, -1)
            return "call" if op > lo and self._operand_end(op - 1) else None
        if t.text == "]":
            base = self._index_base(hi)
            return (base.text, base, "index") if base is not None and self.match.get(hi, -1) > lo else None
        return None

    def _slice(self, ob: int, close: int) -> bool:
        """A colon outside nested brackets between toks[ob] = [ and toks[close] = ]: a slice
        expression, never a callee."""
        j = ob + 1
        while j < close:
            t = self.toks[j]
            if t.kind == "op" and t.text in _OPENERS:
                j = self._skip_brackets(j)
                continue
            if t.kind == "op" and t.text == ":":
                return True
            j += 1
        return False

    def _primary(self, lo: int, hi: int) -> bool:
        """No operator but "." outside brackets in toks[lo..hi]."""
        j = lo
        while j <= hi:
            t = self.toks[j]
            if t.kind == "op":
                if t.text in _OPENERS:
                    j = self._skip_brackets(j)
                    continue
                if t.text != ".":
                    return False
            j += 1
        return True

    def _func_lit_close(self, i: int) -> int:
        """Index of the } that closes the func literal whose func keyword is toks[i], else -1."""
        j = i + 1
        if self._is(j, "("):
            j = self._skip_brackets(j)
        j = self._skip_result(j)
        return self.match.get(j, -1) if self._is(j, "{") else -1

    def _add_site(self, site: Site, lp: int) -> None:
        self.sites.append(site)
        self.calls[lp] = site

    def _range_body(self, i: int, end: int) -> tuple[int, bool]:
        """Index of the { that opens the body of the range clause starting after
        i, and whether the range expression is a primary expression (no
        operator but "." outside brackets)."""
        j = i + 1
        primary = True
        while j < end:
            t = self.toks[j]
            if t.kind == "keyword" and t.text in ("map", "chan", "struct", "interface"):
                j = self._skip_type(j)
                if self._is(j, "{"):
                    j = self._skip_brackets(j)  # composite literal of a type literal
                continue
            if t.kind == "keyword" and t.text == "func":
                k = j + 1
                if self._is(k, "("):
                    k = self._skip_brackets(k)
                k = self._skip_result(k)
                j = self._skip_brackets(k) if self._is(k, "{") else k
                continue
            if t.kind == "op" and t.text == "[" and not self._operand_end(j - 1):
                j = self._skip_type(j)
                if self._is(j, "{"):
                    j = self._skip_brackets(j)
                continue
            if t.kind == "op" and t.text in ("(", "["):
                j = self._skip_brackets(j)
                continue
            if t.kind == "op" and t.text == "{":
                return j, primary  # after an identifier this is the body (spec: TypeName literals need parentheses)
            if t.kind in ("semi", "eof") or (t.kind == "op" and t.text in _CLOSERS):
                return -1, False
            if t.kind == "op" and t.text != ".":
                primary = False
            j += 1
        return -1, False

    def _range_site(self, i: int, end: int, ctx: _Ctx) -> None:
        """for ... range X { where X has a callee shape (spec §4.3, C2): an identifier or
        selector, an index expression whose base is one, or a func literal, through
        parentheses; the token follows the call rule."""
        body, primary = self._range_body(i, end)
        if body <= i + 1 or not primary:
            return
        callee = self._callee_at(i + 1, body - 1)
        if isinstance(callee, tuple):
            self.sites.append(Site("range", callee[0], callee[1], callee[2], ctx.qualname, -1))

    # ------------------------------------------------------------ queries
    def decl(self, qualname: str) -> Decl | None:
        for d in self.decls:
            if d.qualname == qualname:
                return d
        return None

    def site_at(self, line: int, col: int, kind: str | None = None) -> Site | None:
        for s in self.sites:
            if s.line == line and s.col == col and (kind is None or s.kind == kind):
                return s
        return None

    def enclosing(self, line: int, col: int) -> str | None:
        """Caller qualname of a site at (line, col)."""
        site = self.site_at(line, col)
        return site.caller if site else None


_PARSED: dict[str, GoFile] = {}


def parse_file(path: str) -> GoFile:
    real = os.path.realpath(path)
    if real not in _PARSED:
        _PARSED[real] = GoFile(read_bytes(real), path)
    return _PARSED[real]


# ═══════════════════════════════════════════════════════════════ go.mod / go.work

_WORD_RE = re.compile(r'"(?:[^"\\]|\\.)*"|`[^`]*`|\S+')


def _words(line: str) -> list[str]:
    out = []
    for w in _WORD_RE.findall(line):
        if w.startswith('"'):
            w = bytes(w[1:-1], "utf-8").decode("unicode_escape")
        elif w.startswith("`"):
            w = w[1:-1]
        out.append(w)
    return out


def _directives(text: str):
    """(verb, args, comment) for every directive, blocks expanded."""
    block = None
    for raw in text.splitlines():
        comment = ""
        code = raw
        cut = _comment_start(raw)
        if cut >= 0:
            code, comment = raw[:cut], raw[cut + 2:].strip()
        words = _words(code.strip())
        if not words:
            continue
        if block is not None:
            if words == [")"]:
                block = None
                continue
            yield block, words, comment
            continue
        if words[-1] == "(" and len(words) == 2:
            block = words[0]
            continue
        yield words[0], words[1:], comment


def _comment_start(line: str) -> int:
    quote = None
    for k, ch in enumerate(line):
        if quote:
            if ch == quote and (quote == "`" or line[k - 1] != "\\"):
                quote = None
        elif ch in "\"`":
            quote = ch
        elif line.startswith("//", k):
            return k
    return -1


def _replace(args: list[str]) -> dict | None:
    if "=>" not in args:
        return None
    at = args.index("=>")
    old, new = args[:at], args[at + 1:]
    if not old or not new:
        return None
    local = new[0].startswith(("./", "../", "/")) or new[0] in (".", "..")
    return {"old": old[0], "old_version": old[1] if len(old) > 1 else "",
            "new": new[0], "new_version": new[1] if len(new) > 1 else "", "local": local}


def parse_gomod(text: str) -> dict:
    out = {"module": None, "go": None, "toolchain": None, "require": [], "replace": [], "exclude": []}
    for verb, args, comment in _directives(text):
        if verb == "module" and args:
            out["module"] = args[0]
        elif verb == "go" and args:
            out["go"] = args[0]
        elif verb == "toolchain" and args:
            out["toolchain"] = args[0]
        elif verb == "require" and len(args) >= 2:
            out["require"].append((args[0], args[1], comment.startswith("indirect")))
        elif verb == "replace":
            rep = _replace(args)
            if rep:
                out["replace"].append(rep)
        elif verb == "exclude" and len(args) >= 2:
            out["exclude"].append((args[0], args[1]))
    return out


def parse_gowork(text: str) -> dict:
    out = {"go": None, "toolchain": None, "use": [], "replace": []}
    for verb, args, _comment in _directives(text):
        if verb == "go" and args:
            out["go"] = args[0]
        elif verb == "toolchain" and args:
            out["toolchain"] = args[0]
        elif verb == "use" and args:
            out["use"].append(args[0])
        elif verb == "replace":
            rep = _replace(args)
            if rep:
                out["replace"].append(rep)
    return out


SKIP_DIRS = {"testdata", "vendor", "ground_truth"}


def skip_dir(name: str) -> bool:
    """Directories the go command skips for ./... (plus ground_truth)."""
    return name in SKIP_DIRS or name.startswith(("_", "."))


class Layout:
    """Modules (go.mod) and workspaces (go.work) of a fixture directory.

    Directories the go command skips (testdata, vendor, names starting with _
    or .) are not scanned, with one exception: a module there that a local
    replace or a go.work use names (such as go_layouts/_outside/, the target
    of out-of-repo replaces) is recorded too, so that its files have an owner.
    """

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        self.modules: dict[str, dict] = {}     # fixture-relative module dir ("" = root) -> parsed go.mod
        self.workspaces: dict[str, dict] = {}  # fixture-relative dir of a go.work -> parsed go.work
        self.go_files: list[str] = []
        self.errors: list[str] = []
        self._scan("", nested_modules=True)
        self._add_referenced()

    def _rel(self, dirpath: str) -> str:
        rel = os.path.relpath(dirpath, self.root).replace(os.sep, "/")
        return "" if rel == "." else rel

    def _scan(self, top: str, nested_modules: bool) -> None:
        """Walk top with the go command's skip rules; below top, a directory with its own
        go.mod is scanned only when nested_modules is set."""
        for dirpath, dirnames, filenames in os.walk(os.path.join(self.root, top)):
            rel = self._rel(dirpath)
            keep = []
            for d in sorted(dirnames):
                if skip_dir(d):
                    continue
                if not nested_modules and os.path.isfile(os.path.join(dirpath, d, "go.mod")):
                    continue
                keep.append(d)
            dirnames[:] = keep
            if "go.mod" in filenames:
                with open(os.path.join(dirpath, "go.mod"), encoding="utf-8") as handle:
                    mod = parse_gomod(handle.read())
                if not mod["module"]:
                    self.errors.append(f"{rel or '.'}/go.mod has no module line")
                self.modules[rel] = mod
            if "go.work" in filenames:
                with open(os.path.join(dirpath, "go.work"), encoding="utf-8") as handle:
                    self.workspaces[rel] = parse_gowork(handle.read())
            for name in sorted(filenames):
                if name.endswith(".go"):
                    self.go_files.append(f"{rel}/{name}" if rel else name)

    def _add_referenced(self) -> None:
        """Record the modules in skipped directories that local replaces and go.work uses name."""
        pending = [(d, r["new"]) for d, m in self.modules.items() for r in m["replace"] if r["local"]]
        for w, work in self.workspaces.items():
            pending += [(w, r["new"]) for r in work["replace"] if r["local"]]
            pending += [(w, u) for u in work["use"]]
        while pending:
            base, new = pending.pop()
            target = os.path.normpath(os.path.join(base, new)).replace(os.sep, "/")
            target = "" if target == "." else target
            if target.startswith("../") or target == ".." or target in self.modules:
                continue
            if not os.path.isfile(os.path.join(self.root, target, "go.mod")):
                continue
            self._scan(target, nested_modules=False)
            pending += [(target, r["new"]) for r in self.modules[target]["replace"] if r["local"]]

    def module_dir_of(self, rel: str) -> str | None:
        """Deepest module directory above a fixture-relative path."""
        parts = rel.split("/")[:-1]
        for k in range(len(parts), -1, -1):
            cand = "/".join(parts[:k])
            if cand in self.modules:
                return cand
        return None

    def module_of(self, rel: str) -> str | None:
        d = self.module_dir_of(rel)
        return self.modules[d]["module"] if d is not None else None

    def package_of(self, rel: str, package_clause: str | None = None) -> str | None:
        """Import path of the package of a .go file (with _test for external tests)."""
        d = self.module_dir_of(rel)
        if d is None:
            return None
        sub = os.path.dirname(rel)[len(d):].lstrip("/") if d else os.path.dirname(rel)
        path = self.modules[d]["module"] + ("/" + sub if sub else "")
        if package_clause and package_clause.endswith("_test") and rel.endswith("_test.go"):
            path += "_test"
        return path

    def dirs_of_module(self, module_path: str) -> list[str]:
        return [d for d, m in self.modules.items() if m["module"] == module_path]


def escape_path(path: str) -> str:
    return re.sub(r"[A-Z]", lambda m: "!" + m.group(0).lower(), path)


def proxy_has(proxy_dir: str, module: str, version: str) -> bool:
    base = os.path.join(proxy_dir, escape_path(module), "@v", escape_path(version))
    return all(os.path.isfile(base + ext) for ext in (".info", ".mod", ".zip"))
