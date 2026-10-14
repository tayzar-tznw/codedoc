"""Pure-Python model of the go command's file selection (go/build semantics, Go 1.27.2).

Reads files only. Mirrors:
  - go/build.Context.matchFile      (name prefix, extension, goodOSArchFile, shouldBuild)
  - go/build.parseFileHeader        (where //go:build and // +build lines count)
  - go/build/constraint             (//go:build grammar, // +build grammar)
  - go/build.Context.matchTag       (GOOS/GOARCH/compiler/cgo/unix/android/illumos/ios, tags)
  - go/build Import loop            (package clause, _test/x_test split, import "C", "documentation")
Sources: $GOROOT/src/go/build/build.go, go/build/constraint/expr.go, internal/syslist/syslist.go.
"""
import os
import re
import unicodedata

KNOWN_OS = {"aix", "android", "darwin", "dragonfly", "freebsd", "hurd", "illumos", "ios", "js",
            "linux", "nacl", "netbsd", "openbsd", "plan9", "solaris", "wasip1", "windows", "zos"}
UNIX_OS = {"aix", "android", "darwin", "dragonfly", "freebsd", "hurd", "illumos", "ios", "linux",
           "netbsd", "openbsd", "solaris"}
KNOWN_ARCH = {"386", "amd64", "amd64p32", "arm", "armbe", "arm64", "arm64be", "loong64", "mips",
              "mipsle", "mips64", "mips64le", "mips64p32", "mips64p32le", "ppc", "ppc64", "ppc64le",
              "riscv", "riscv64", "s390", "s390x", "sparc", "sparc64", "wasm"}
# Extensions go/build files into a package (fileListForExt); others are not package files.
OTHER_EXTS = {".c", ".cc", ".cxx", ".cpp", ".m", ".h", ".hh", ".hpp", ".hxx", ".f", ".F", ".for",
              ".f90", ".s", ".S", ".sx", ".swig", ".swigcxx", ".syso"}


class Ctx:
    def __init__(self, goos, goarch, cgo, tool_tags=(), release=27, build_tags=(), compiler="gc"):
        self.goos, self.goarch, self.cgo, self.compiler = goos, goarch, cgo, compiler
        self.tags = set(build_tags) | set(tool_tags) | {f"go1.{i}" for i in range(1, release + 1)}

    def match_tag(self, name):
        if self.cgo and name == "cgo":
            return True
        if name in (self.goos, self.goarch, self.compiler):
            return True
        if self.goos == "android" and name == "linux":
            return True
        if self.goos == "illumos" and name == "solaris":
            return True
        if self.goos == "ios" and name == "darwin":
            return True
        if name == "unix" and self.goos in UNIX_OS:
            return True
        if name == "boringcrypto":
            name = "goexperiment.boringcrypto"
        return name in self.tags


# ---------------------------------------------------------------- file names
def good_os_arch_file(name, ctx):
    name = name.split(".", 1)[0]
    i = name.find("_")
    if i < 0:
        return True
    parts = name[i:].split("_")
    if parts and parts[-1] == "test":
        parts = parts[:-1]
    n = len(parts)
    if n >= 2 and parts[n - 2] in KNOWN_OS and parts[n - 1] in KNOWN_ARCH:
        return ctx.match_tag(parts[n - 1]) and ctx.match_tag(parts[n - 2])
    if n >= 1 and (parts[n - 1] in KNOWN_OS or parts[n - 1] in KNOWN_ARCH):
        return ctx.match_tag(parts[n - 1])
    return True


# ---------------------------------------------------------------- constraint grammar
class ConstraintError(Exception):
    pass


def _is_tag_char(c):
    return c == "_" or c == "." or unicodedata.category(c)[0] in ("L", "N") and (c.isalpha() or c.isdigit() or unicodedata.category(c) == "Nd")


def _is_letter_or_digit(c):
    # unicode.IsLetter || unicode.IsDigit (Nd only)
    cat = unicodedata.category(c)
    return cat.startswith("L") or cat == "Nd"


def parse_go_build_expr(s):
    """Return a function tag->bool evaluator; raises ConstraintError like constraint.parseExpr."""
    toks = []
    i = 0
    while True:
        while i < len(s) and s[i] in " \t":
            i += 1
        if i >= len(s):
            break
        c = s[i]
        if c in "()!":
            toks.append(c); i += 1; continue
        if c in "&|":
            if i + 1 >= len(s) or s[i + 1] != c:
                raise ConstraintError("invalid syntax at " + c)
            toks.append(c * 2); i += 2; continue
        j = i
        while j < len(s) and (_is_letter_or_digit(s[j]) or s[j] in "_."):
            j += 1
        if j == i:
            raise ConstraintError("invalid syntax at " + c)
        toks.append(("tag", s[i:j])); i = j
    pos = [0]
    size = [0]

    def peek():
        return toks[pos[0]] if pos[0] < len(toks) else None

    def nxt():
        t = peek(); pos[0] += 1; return t

    def p_or():
        x = p_and()
        while peek() == "||":
            nxt(); y = p_and(); x = ("or", x, y)
        return x

    def p_and():
        x = p_not()
        while peek() == "&&":
            nxt(); y = p_not(); x = ("and", x, y)
        return x

    def p_not():
        size[0] += 1
        if size[0] > 1000:
            raise ConstraintError("build expression too large")
        if peek() == "!":
            nxt()
            if peek() == "!":
                raise ConstraintError("double negation not allowed")
            return ("not", p_atom())
        return p_atom()

    def p_atom():
        t = nxt()
        if t == "(":
            x = p_or()
            if nxt() != ")":
                raise ConstraintError("missing close paren")
            return x
        if isinstance(t, tuple):
            return t
        if t is None:
            raise ConstraintError("unexpected end of expression")
        raise ConstraintError("unexpected token " + t)

    x = p_or()
    if peek() is not None:
        raise ConstraintError("unexpected token")
    return x


def eval_expr(x, ctx):
    k = x[0]
    if k == "tag":
        return ctx.match_tag(x[1])
    if k == "not":
        return not eval_expr(x[1], ctx)
    if k == "and":
        a = eval_expr(x[1], ctx); b = eval_expr(x[2], ctx)  # both sides evaluated, like Go
        return a and b
    a = eval_expr(x[1], ctx); b = eval_expr(x[2], ctx)
    return a or b


def _valid_tag(w):
    return w != "" and all(_is_letter_or_digit(c) or c in "_." for c in w)


def parse_plus_build(text):
    """// +build: space = OR, comma = AND, ! = NOT; invalid literal -> tag 'ignore'."""
    x = None
    for clause in text.split():
        y = None
        for lit in clause.split(","):
            if lit.startswith("!!") or lit == "!":
                z = ("tag", "ignore")
            else:
                neg = lit.startswith("!")
                if neg:
                    lit = lit[1:]
                z = ("tag", lit) if _valid_tag(lit) else ("tag", "ignore")
                if neg:
                    z = ("not", z)
            y = z if y is None else ("and", y, z)
        x = y if x is None else ("or", x, y)
    return x if x is not None else ("tag", "ignore")


def split_go_build(line):
    if not line.startswith("//go:build"):
        return None
    rest = line.strip()[len("//go:build"):]
    trim = rest.strip()
    if len(rest) == len(trim) and rest != "":
        return None
    return trim


def split_plus_build(line):
    if not line.startswith("//"):
        return None
    line = line[2:].strip()
    if not line.startswith("+build"):
        return None
    rest = line[len("+build"):]
    trim = rest.strip()
    if len(rest) == len(trim) and rest != "":
        return None
    return trim


def parse_file_header(content):
    """Port of go/build.parseFileHeader. Returns (plus_build_region_lines, go_build_line) or raises."""
    end = 0
    ended = False
    in_slash_star = False
    go_build = None
    offset = 0
    lines = content.split("\n")
    consumed = 0
    region_end_line = 0
    for idx, raw in enumerate(lines):
        line = raw.strip(" \t\r\n\v\f")
        if len(line) == 0 and not ended:
            region_end_line = idx + 1
            continue
        if not line.startswith("//"):
            ended = True
        if not in_slash_star and line.startswith("//go:build"):
            rest = line[len("//go:build"):]
            if len(rest) == 0 or len(rest.strip()) < len(rest):
                if go_build is not None:
                    raise ConstraintError("multiple //go:build comments")
                go_build = line
        stop = False
        while len(line) > 0:
            if in_slash_star:
                k = line.find("*/")
                if k >= 0:
                    in_slash_star = False
                    line = line[k + 2:].strip()
                    continue
                break
            if line.startswith("//"):
                break
            if line.startswith("/*"):
                in_slash_star = True
                line = line[2:].strip()
                continue
            stop = True
            break
        if stop:
            break
    return lines[:region_end_line], go_build


def should_build(content, ctx):
    region, go_build = parse_file_header(content)
    if go_build is not None:
        x = parse_go_build_expr(split_go_build(go_build))
        return eval_expr(x, ctx)
    ok = True
    for raw in region:
        line = raw.strip()
        if not line.startswith("//") or "+build" not in line:
            continue
        text = split_plus_build(line)
        if text is None:
            continue
        x = parse_plus_build(text)
        if not eval_expr(x, ctx):
            ok = False
    return ok


# ---------------------------------------------------------------- package clause and imports
_TOKEN = re.compile(r'\s+|//[^\n]*|/\*.*?\*/|"(?:[^"\\\n]|\\.)*"|`[^`]*`|[A-Za-z_\u0080-￿][\w\u0080-￿]*|.', re.S)


def package_and_imports(content):
    """Return (package_name, has_import_C). Minimal Go lexer over the file prologue."""
    toks = []
    for m in _TOKEN.finditer(content):
        t = m.group(0)
        if t.isspace() or t.startswith("//") or t.startswith("/*"):
            if "\n" in t and toks and toks[-1] != ";":
                toks.append(";")
            continue
        toks.append(t)
        if len(toks) > 4000:
            break
    i = 0
    while i < len(toks) and toks[i] == ";":
        i += 1
    if i >= len(toks) or toks[i] != "package":
        return None, False
    pkg = toks[i + 1] if i + 1 < len(toks) else None
    i += 2
    cgo = False
    while i < len(toks):
        if toks[i] == ";":
            i += 1; continue
        if toks[i] != "import":
            break
        i += 1
        if i < len(toks) and toks[i] == "(":
            i += 1
            while i < len(toks) and toks[i] != ")":
                t = toks[i]
                if t[:1] in "\"`" and t[1:-1] == "C":
                    cgo = True
                i += 1
            i += 1
        else:
            while i < len(toks) and toks[i] != ";":
                t = toks[i]
                if t[:1] in "\"`" and t[1:-1] == "C":
                    cgo = True
                i += 1
    return pkg, cgo


def classify_dir(dirpath, names, ctx):
    """Return dict: file -> one of GoFiles/CgoFiles/TestGoFiles/XTestGoFiles/IgnoredGoFiles/InvalidGoFiles/skip."""
    out = {}
    pkg_name = None
    entries = []
    for name in sorted(names):
        if not name.endswith(".go"):
            continue
        if name.startswith("_") or name.startswith("."):
            out[name] = "skip"
            continue
        if not good_os_arch_file(name, ctx):
            out[name] = "IgnoredGoFiles"
            continue
        with open(os.path.join(dirpath, name), "rb") as f:
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        if text.startswith("﻿"):
            text = text[1:]
        try:
            ok = should_build(text, ctx)
        except ConstraintError:
            out[name] = "InvalidGoFiles"
            continue
        if not ok:
            out[name] = "IgnoredGoFiles"
            continue
        pkg, cgo = package_and_imports(text)
        if pkg == "documentation":
            out[name] = "IgnoredGoFiles"
            continue
        entries.append((name, pkg, cgo))
    for name, pkg, cgo in entries:
        is_test = name.endswith("_test.go")
        is_xtest = False
        if is_test and pkg and pkg.endswith("_test") and pkg_name != pkg:
            is_xtest = True
            pkg = pkg[: -len("_test")]
        if pkg_name is None:
            pkg_name = pkg
        if cgo and not is_test:
            out[name] = "CgoFiles" if ctx.cgo else "IgnoredGoFiles"
        elif is_xtest:
            out[name] = "XTestGoFiles"
        elif is_test:
            out[name] = "TestGoFiles"
        else:
            out[name] = "GoFiles"
    return out
