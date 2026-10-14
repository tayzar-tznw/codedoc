"""Units, workspaces and groups of a Go repo, found from files only (spec §3, Appendix B).

A unit is a module (a directory with go.mod). Nothing here runs a go command
against the repo: go.mod, go.work, vendor/modules.txt and build constraints are
parsed in Python, ported from the go 1.27.2 sources (cmd/vendor/golang.org/x/
mod/modfile, cmd/go/internal/modload, go/build). The ports agree with the go
command on 2,693 go.mod files and 167,226 recorded build-constraint decisions
(checked 2026-10-14 against the go-modules research corpora).

Field rules of units.json (the lead's rulings of 2026-10-14):
- A module's group is the go.work found walking up from its root when that
  go.work lists it, else a single-module group (R4).
- in_repo_deps: sorted in-repo module keys M whose packages the unit's files
  import (files the primary build compiles, tests included; M = the longest
  module-path prefix among the repo's modules), when the unit's group builds the
  import from M's in-repo source: M is a workspace module of the group, or a
  local replace points at M's root. Otherwise it is a published version.
- missing_modules (offline, loadable non-vendored groups): "path@version" for
  every requirement listed in the group's go.mod files (after replaces; a
  workspace module required at a version without a replace counts, because the
  go command fetches that go.mod) whose go.mod is in neither the isolated nor the
  user's cache, and for the highest listed version of a module whose packages
  the repo's compiled files import, also when its zip is missing. Substituted
  peers' requirements count by go.mod (R28). On etcd and Prometheus this gave
  0/0 with a complete cache and 303/393 with an empty one (loads: 0 errors;
  108/222 external errors), where checking every zip reported 202/76.
- module_source: "vendor" when every group is vendored, else offline
  "file-proxy" (a user cache exists) or "none", and "proxy" in download mode.

Beyond C4: scan_go_files(exclude=...) prunes extra directories (OUTPUT_DIR
always); parse_go_mod(strict=False) is the lax dependency parse;
builds_from_config() reads GO_BUILD_* and GO_EXTRA_BUILDS; discover_peer()
discovers a peer repo; peer_replaces() and group_modules() report what a
group's control files hold; stops() lists the discovery problems that stop a
run (R15); vendor_errors() and file_imports() serve doctor.
"""
from __future__ import annotations

import functools
import json
import os
import posixpath
import re
import shutil
import unicodedata

from . import go_modcache
from .go_model import (BuildConfig, Discovery, GoUnit, Group, Peer, Replace, Toolchain,
                       UNITS_VERSION)
from .go_toolchain import PINNED_VERSION as PINNED
from .go_toolchain import compare_go_versions, flag, from_toolchain_name, setting


class ModError(ValueError):
    """A go.mod or go.work syntax error; `line` is 1-based (0 when unknown)."""

    def __init__(self, msg: str, line: int = 0):
        super().__init__(msg)
        self.line = line


# ---------------------------------------------------------------- versions (x/mod/semver, module)

_NUM = re.compile(r"0|[1-9][0-9]*")
_IDENT = re.compile(r"[0-9A-Za-z-]+")


def _semver_parse(v: str) -> tuple[str, str, str, str, str] | None:
    """(major, minor, patch, prerelease, build) of a semantic version, or None."""
    if not v.startswith("v"):
        return None
    rest = v[1:]
    parts: list[str] = []
    for i in range(3):
        m = _NUM.match(rest)
        if not m:
            return None
        parts.append(m.group(0))
        rest = rest[m.end():]
        if rest == "" or i == 2:
            break
        if rest[0] != ".":
            return None
        rest = rest[1:]
    if len(parts) < 3:
        if rest:
            return None                        # shorthand v1 / v1.2 takes no suffix
        parts += ["0"] * (3 - len(parts))
    pre = build = ""
    if rest.startswith("-"):
        end = rest.find("+") if "+" in rest else len(rest)
        pre, rest = rest[:end], rest[end:]
        for ident in pre[1:].split("."):
            if not _IDENT.fullmatch(ident) or (ident.isdigit() and ident != "0"
                                                and ident.startswith("0")):
                return None
    if rest.startswith("+"):
        build, rest = rest, ""
        for ident in build[1:].split("."):
            if not _IDENT.fullmatch(ident):
                return None
    if rest:
        return None
    return parts[0], parts[1], parts[2], pre, build


def canonical_version(v: str) -> str:
    """module.CanonicalVersion: semver.Canonical, keeping "+incompatible"; "" if invalid."""
    p = _semver_parse(v)
    if p is None:
        return ""
    cv = f"v{p[0]}.{p[1]}.{p[2]}{p[3]}"
    return cv + "+incompatible" if p[4] == "+incompatible" else cv


def _semver_major(v: str) -> str:
    p = _semver_parse(v)
    return f"v{p[0]}" if p else ""


def _cmp_int(a: str, b: str) -> int:
    if len(a) != len(b):
        return -1 if len(a) < len(b) else 1
    return (a > b) - (a < b)


def semver_compare(a: str, b: str) -> int:
    """semver.Compare: -1, 0 or 1; invalid versions sort before valid ones."""
    pa, pb = _semver_parse(a), _semver_parse(b)
    if pa is None or pb is None:
        return 0 if pa is pb else (-1 if pa is None else 1)
    for x, y in zip(pa[:3], pb[:3]):
        c = _cmp_int(x, y)
        if c:
            return c
    xa, xb = pa[3], pb[3]
    if xa == xb:
        return 0
    if not xa or not xb:
        return 1 if not xa else -1
    ia, ib = xa[1:].split("."), xb[1:].split(".")
    for x, y in zip(ia, ib):
        if x == y:
            continue
        nx, ny = x.isdigit(), y.isdigit()
        if nx and ny:
            return _cmp_int(x, y)
        if nx != ny:
            return -1 if nx else 1
        return -1 if x < y else 1
    return (len(ia) > len(ib)) - (len(ia) < len(ib))


def _split_path_version(path: str) -> tuple[str, str, bool]:
    """module.SplitPathVersion: (prefix, pathMajor, ok)."""
    if path.startswith("gopkg.in/"):
        i = len(path)
        if path.endswith("-unstable"):
            i -= len("-unstable")
        while i > 0 and path[i - 1].isdigit() and path[i - 1].isascii():
            i -= 1
        if i <= 1 or path[i - 1] != "v" or path[i - 2] != ".":
            return path, "", False
        major = path[i - 2:]
        if len(major) <= 2 or (major[2] == "0" and major != ".v0"):
            return path, "", False
        return path[:i - 2], major, True
    i = len(path)
    dot = False
    while i > 0 and (path[i - 1] in "0123456789."):
        dot = dot or path[i - 1] == "."
        i -= 1
    if i <= 1 or i == len(path) or path[i - 1] != "v" or path[i - 2] != "/":
        return path, "", True
    major = path[i - 2:]
    if dot or len(major) <= 2 or major[2] == "0" or major == "/v1":
        return path, "", False
    return path[:i - 2], major, True


def _check_path_major(v: str, major: str) -> str | None:
    """module.CheckPathMajor: an error message, or None."""
    if major.startswith(".v") and major.endswith("-unstable"):
        major = major[:-len("-unstable")]
    if v.startswith("v0.0.0-") and major == ".v1":
        return None
    m = _semver_major(v)
    if major == "":
        if m in ("v0", "v1") or v.endswith("+incompatible"):
            return None
        want = "v0 or v1"
    else:
        if m == major[1:]:
            return None
        want = major[1:]
    return f'version "{v}" invalid: should be {want}, not {m}'


# ---------------------------------------------------------------- go.mod / go.work lexer

GO_VERSION_RE = re.compile(r"^([1-9][0-9]*)\.(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*))?([a-z]+[0-9]+)?$")
_LAX_GO_VERSION_RE = re.compile(r"^v?(([1-9][0-9]*)\.(0|[1-9][0-9]*))([^0-9].*)$")
TOOLCHAIN_RE = re.compile(r"^default$|^go1($|\.)")
_PUNCT = set("\n()[]{},")


def _is_ident(c: str) -> bool:
    if c in " ()[]{},":
        return False
    return not c.isspace() and c.isprintable()


def _lex(text: str) -> list[tuple[str, str, int]]:
    """Tokens (kind, value, line) like modfile/read.go. Kinds: NL, ( ) [ ] { } ,,
    STR, ID, COMMENT (whole-line), EOLCOMMENT (suffix), EOF."""
    toks: list[tuple[str, str, int]] = []
    i, n, line = 0, len(text), 1
    while True:
        while i < n:
            c = text[i]
            if c in " \t\r":
                i += 1
                continue
            if text.startswith("//", i):
                j = text.find("\n", i)
                j = n if j < 0 else j + 1
                line_start = text.rfind("\n", 0, i) + 1
                suffix = text[line_start:i].strip() != ""
                toks.append(("EOLCOMMENT" if suffix else "COMMENT", text[i:j].rstrip("\n"), line))
                toks.append(("NL", "\n", line))         # a comment consumes its newline
                line += 1
                i = j
                continue
            if text.startswith("/*", i):
                raise ModError("mod files must use // comments (not /* */ comments)", line)
            break
        if i >= n:
            break
        c = text[i]
        if c in _PUNCT:
            toks.append(("NL" if c == "\n" else c, c, line))
            if c == "\n":
                line += 1
            i += 1
            continue
        if c == '"':
            j = i + 1
            while True:
                if j >= n:
                    raise ModError("unexpected EOF in string", line)
                if text[j] == "\n":
                    raise ModError("unexpected newline in string", line)
                ch = text[j]
                j += 1
                if ch == '"':
                    break
                if ch == "\\":
                    if j >= n:
                        raise ModError("unexpected EOF in string", line)
                    j += 1
            toks.append(("STR", text[i:j], line))
            i = j
            continue
        if not _is_ident(c):
            raise ModError(f"unexpected input character {c!r}", line)
        j = i
        while j < n and _is_ident(text[j]):
            if text.startswith("//", j):
                break
            if text.startswith("/*", j):
                raise ModError("mod files must use // comments (not /* */ comments)", line)
            j += 1
        toks.append(("ID", text[i:j], line))
        i = j
    toks.append(("EOF", "", line))
    return toks


def _statements(text: str) -> list[tuple]:
    """('line', tokens, eol_comment) and ('block', verb_tokens, [(tokens, eol_comment)])."""
    if text.startswith("\ufeff"):
        raise ModError("unexpected input character '\\ufeff'", 1)
    toks = _lex(text)
    pos = 0
    stmts: list[tuple] = []
    while True:
        k = toks[pos][0]
        if k == "EOF":
            break
        if k in ("NL", "COMMENT"):
            pos += 1
            continue
        line: list[tuple[str, str, int]] = []
        while True:
            k, v, ln = toks[pos]
            if k in ("NL", "EOF", "EOLCOMMENT"):
                comment = None
                if k == "EOLCOMMENT":
                    comment = v
                    pos += 1
                    k = toks[pos][0]
                if k == "NL":
                    pos += 1
                stmts.append(("line", line, comment))
                break
            if k == "(":
                nk = toks[pos + 1][0]
                if nk in ("NL", "EOLCOMMENT", "EOF"):
                    pos += 1
                    blines: list[tuple[list, str | None]] = []
                    while True:
                        k2, _v2, ln2 = toks[pos]
                        if k2 in ("NL", "COMMENT", "EOLCOMMENT"):
                            pos += 1
                            continue
                        if k2 == "EOF":
                            raise ModError("syntax error (unterminated block)", ln2)
                        if k2 == ")":
                            pos += 1
                            k3 = toks[pos][0]
                            if k3 not in ("NL", "EOF", "EOLCOMMENT"):
                                raise ModError(
                                    "syntax error (expected newline after closing paren)", ln2)
                            if k3 == "EOLCOMMENT":
                                pos += 1
                            if toks[pos][0] == "NL":
                                pos += 1
                            break
                        bl: list[tuple[str, str, int]] = []
                        bcomment = None
                        while True:
                            k4, v4, _ln4 = toks[pos]
                            if k4 in ("NL", "EOF", "EOLCOMMENT"):
                                if k4 == "EOLCOMMENT":
                                    bcomment = v4
                                    pos += 1
                                if toks[pos][0] == "NL":
                                    pos += 1
                                break
                            bl.append(toks[pos])
                            pos += 1
                        blines.append((bl, bcomment))
                    stmts.append(("block", line, blines))
                    break
                if nk == ")" and toks[pos + 2][0] in ("NL", "EOF", "EOLCOMMENT"):
                    pos += 2
                    if toks[pos][0] == "EOLCOMMENT":
                        pos += 1
                    if toks[pos][0] == "NL":
                        pos += 1
                    stmts.append(("block", line, []))
                    break
            line.append(toks[pos])
            pos += 1
    return stmts


_ESC = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
        "\\": "\\", '"': '"'}
_HEX = set("0123456789abcdefABCDEF")


def _go_unquote(s: str) -> str:
    """strconv.Unquote for a double-quoted Go string."""
    if len(s) < 2 or s[0] != '"' or s[-1] != '"':
        raise ValueError("invalid syntax")
    out: list[str] = []
    body, i = s[1:-1], 0
    while i < len(body):
        c = body[i]
        if c == '"':
            raise ValueError("invalid syntax")
        if c != "\\":
            out.append(c)
            i += 1
            continue
        e = body[i + 1] if i + 1 < len(body) else ""
        if e in _ESC:
            out.append(_ESC[e])
            i += 2
        elif e in ("x", "u", "U"):
            width = {"x": 2, "u": 4, "U": 8}[e]
            digits = body[i + 2:i + 2 + width]
            if len(digits) != width or not set(digits) <= _HEX:
                raise ValueError("invalid syntax")
            code = int(digits, 16)
            if e != "x" and (code > 0x10FFFF or 0xD800 <= code <= 0xDFFF):
                raise ValueError("invalid syntax")
            out.append(chr(code))
            i += 2 + width
        elif e in "01234567" and e:
            digits = body[i + 1:i + 4]
            if len(digits) != 3 or not all(d in "01234567" for d in digits) or int(digits, 8) > 255:
                raise ValueError("invalid syntax")
            out.append(chr(int(digits, 8)))
            i += 4
        else:
            raise ValueError("invalid syntax")
    return "".join(out)


def _parse_string(tok: tuple[str, str, int]) -> str:
    v = tok[1]
    if v.startswith('"'):
        try:
            return _go_unquote(v)
        except ValueError as exc:
            raise ModError(f"invalid quoted string: {exc}", tok[2]) from None
    if any(q in v for q in "\"'`"):
        raise ModError("invalid quoted string: unquoted string cannot contain quote", tok[2])
    return v


def _parse_version(verb: str, path: str, tok: tuple[str, str, int]) -> str:
    try:
        t = _parse_string(tok)
    except ModError as exc:
        raise ModError(f'{verb} {path}: version "{tok[1]}" invalid: {exc}', tok[2]) from None
    cv = canonical_version(t)
    if not cv:
        raise ModError(f'{verb} {path}: version "{t}" invalid: must be of the form v1.2.3', tok[2])
    return cv


def _path_major(verb: str, path: str, ln: int) -> str:
    _prefix, major, ok = _split_path_version(path)
    if not ok:
        raise ModError(f"{verb} {path}: invalid module path", ln)
    return major


def is_directory_path(ns: str) -> bool:
    """modfile.IsDirectoryPath: a relative ./ ../ path, or a rooted one."""
    return (ns in (".", "..") or ns.startswith(("./", ".\\", "../", "..\\", "/", "\\"))
            or (len(ns) >= 2 and ns[0].isascii() and ns[0].isalpha() and ns[1] == ":"))


def _is_indirect(comment: str | None) -> bool:
    if not comment:
        return False
    f = comment[2:].split() if comment.startswith("//") else comment.split()
    return (len(f) == 1 and f[0] == "indirect") or (len(f) > 1 and f[0] == "indirect;")


def _replace(verb: str, args: list[tuple[str, str, int]], ln: int) -> dict:
    vals = [v for _, v, _ in args]
    arrow = 1 if len(vals) >= 2 and vals[1] == "=>" else 2
    if len(vals) < arrow + 2 or len(vals) > arrow + 3 or vals[arrow] != "=>":
        raise ModError(f"usage: {verb} module/path [v1.2.3] => other/module v1.4\n"
                       f"\t or {verb} module/path [v1.2.3] => ../local/directory", ln)
    old_path = _parse_string(args[0])
    major = _path_major(verb, old_path, ln)
    old: dict = {"Path": old_path}
    if arrow == 2:
        old["Version"] = _parse_version(verb, old_path, args[1])
        err = _check_path_major(old["Version"], major)
        if err:
            raise ModError(f"{verb} {old_path}: {err}", ln)
    ns = _parse_string(args[arrow + 1])
    new: dict = {"Path": ns}
    if len(vals) == arrow + 2:
        if not is_directory_path(ns):
            if "@" in ns:
                raise ModError("replacement module must match format 'path version', "
                               "not 'path@version'", ln)
            raise ModError("replacement module without version must be directory path "
                           "(rooted or starting with . or ..)", ln)
        if "\\" in ns:
            raise ModError("replacement directory appears to be Windows path "
                           "(on a non-windows system)", ln)
    else:
        nv = _parse_version(verb, ns, args[arrow + 2])
        if is_directory_path(ns):
            raise ModError(f'replacement module directory path "{ns}" cannot have version', ln)
        new["Version"] = nv
    return {"Old": old, "New": new}


def _version_interval(verb: str, args: list[tuple[str, str, int]], ln: int
                      ) -> tuple[str, str, list]:
    toks = list(args)
    if not toks or toks[0][1] == "(":
        raise ModError("expected '[' or version", ln)
    if toks[0][1] != "[":
        v = _parse_version(verb, "", toks[0])
        return v, v, toks[1:]
    toks = toks[1:]
    if not toks:
        raise ModError("expected version after '['", ln)
    low = _parse_version(verb, "", toks[0])
    toks = toks[1:]
    if not toks or toks[0][1] != ",":
        raise ModError("expected ',' after version", ln)
    toks = toks[1:]
    if not toks:
        raise ModError("expected version after ','", ln)
    high = _parse_version(verb, "", toks[0])
    toks = toks[1:]
    if not toks or toks[0][1] != "]":
        raise ModError("expected ']' after version", ln)
    return low, high, toks[1:]


_LAX_VERBS = {"go", "module", "retract", "require", "ignore"}
_MOD_BLOCK_VERBS = {"module", "godebug", "require", "exclude", "replace", "retract", "tool", "ignore"}


def _items(st: tuple, block_verbs: set[str], strict: bool) -> list[tuple[str, list, str | None, int]]:
    """(verb, args, eol_comment, line) for one statement; unknown blocks raise when strict."""
    if st[0] == "line":
        if not st[1]:
            return []
        return [(st[1][0][1], st[1][1:], st[2], st[1][0][2])]
    verbtoks = st[1]
    ln = verbtoks[0][2] if verbtoks else 0
    if len(verbtoks) != 1 or verbtoks[0][1] not in block_verbs:
        if strict:
            raise ModError("unknown block type: " + " ".join(v for _, v, _ in verbtoks), ln)
        return []
    verb = verbtoks[0][1]
    return [(verb, bl, c, bl[0][2] if bl else ln) for bl, c in st[2]]


def _godebug(args: list, ln: int) -> dict:
    if len(args) != 1 or any(c in args[0][1] for c in "\"`',"):
        raise ModError("usage: godebug key=value", ln)
    key, eq, value = args[0][1].partition("=")
    if not eq:
        raise ModError("usage: godebug key=value", ln)
    return {"Key": key, "Value": value}


def _go_line(verb: str, args: list, ln: int, strict: bool) -> str:
    if len(args) != 1:
        raise ModError("go directive expects exactly one argument", ln)
    v = args[0][1]
    if not GO_VERSION_RE.match(v):
        m = None if strict else _LAX_GO_VERSION_RE.match(v)
        if not m:
            raise ModError(f"invalid go version '{v}': must match format 1.23.0", ln)
        v = m.group(1)
    return v


def _toolchain_line(args: list, ln: int) -> str:
    if len(args) != 1:
        raise ModError("toolchain directive expects exactly one argument", ln)
    v = args[0][1]
    if not TOOLCHAIN_RE.match(v):
        raise ModError(f"invalid toolchain version '{v}': must match format go1.23.0 or default", ln)
    return v


def parse_go_mod(text: str, strict: bool = True) -> dict:
    """Parse go.mod text into the shape of `go mod edit -json` (without the
    comment-derived Module.Deprecated and Retract.Rationale). strict=False is
    the lax parse the go command applies to dependencies' go.mod files: only
    go, module, retract, require and ignore are kept. Raises ModError."""
    out: dict = {"Module": {"Path": ""}}
    seen_module = False
    for st in _statements(text):
        for verb, args, comment, ln in _items(st, _MOD_BLOCK_VERBS, strict):
            if not strict and verb not in _LAX_VERBS:
                continue
            if verb == "module":
                if seen_module:
                    raise ModError("repeated module statement", ln)
                seen_module = True
                if len(args) != 1:
                    raise ModError("usage: module module/path", ln)
                out["Module"]["Path"] = _parse_string(args[0])
            elif verb == "go":
                if "Go" in out:
                    raise ModError("repeated go statement", ln)
                out["Go"] = _go_line(verb, args, ln, strict)
            elif verb == "toolchain":
                if "Toolchain" in out:
                    raise ModError("repeated toolchain statement", ln)
                out["Toolchain"] = _toolchain_line(args, ln)
            elif verb == "godebug":
                out.setdefault("GoDebug", []).append(_godebug(args, ln))
            elif verb in ("require", "exclude"):
                if len(args) != 2:
                    raise ModError(f"usage: {verb} module/path v1.2.3", ln)
                path = _parse_string(args[0])
                version = _parse_version(verb, path, args[1])
                err = _check_path_major(version, _path_major(verb, path, ln))
                if err:
                    raise ModError(f"{verb} {path}: {err}", ln)
                if verb == "require":
                    req: dict = {"Path": path, "Version": version}
                    if _is_indirect(comment):
                        req["Indirect"] = True
                    out.setdefault("Require", []).append(req)
                else:
                    out.setdefault("Exclude", []).append({"Path": path, "Version": version})
            elif verb == "replace":
                out.setdefault("Replace", []).append(_replace(verb, args, ln))
            elif verb == "retract":
                try:
                    low, high, rest = _version_interval(verb, args, ln)
                except ModError:
                    if strict:
                        raise
                    continue
                if rest and strict:
                    raise ModError(f'unexpected token after version: "{rest[0][1]}"', ln)
                out.setdefault("Retract", []).append({"Low": low, "High": high})
            elif verb == "tool":
                if len(args) != 1:
                    raise ModError("tool directive expects exactly one argument", ln)
                out.setdefault("Tool", []).append({"Path": _parse_string(args[0])})
            elif verb == "ignore":
                if len(args) != 1:
                    raise ModError("ignore directive expects exactly one argument", ln)
                out.setdefault("Ignore", []).append({"Path": _parse_string(args[0])})
            else:
                raise ModError(f"unknown directive: {verb}", ln)
    return out


def parse_go_work(text: str) -> dict:
    """Parse go.work text into the shape of `go work edit -json`. Raises ModError."""
    out: dict = {}
    for st in _statements(text):
        for verb, args, _comment, ln in _items(st, {"godebug", "use", "replace"}, True):
            if verb == "go":
                if "Go" in out:
                    raise ModError("repeated go statement", ln)
                out["Go"] = _go_line(verb, args, ln, True)
            elif verb == "toolchain":
                if "Toolchain" in out:
                    raise ModError("repeated toolchain statement", ln)
                out["Toolchain"] = _toolchain_line(args, ln)
            elif verb == "godebug":
                out.setdefault("Godebug", []).append(_godebug(args, ln))
            elif verb == "use":
                if len(args) != 1:
                    raise ModError("usage: use local/dir", ln)
                out.setdefault("Use", []).append({"DiskPath": _parse_string(args[0])})
            elif verb == "replace":
                out.setdefault("Replace", []).append(_replace(verb, args, ln))
            else:
                raise ModError(f"unknown directive: {verb}", ln)
    return out


_DIRECTIVE = re.compile(r"^[ \t]*([A-Za-z]+)[ \t]")


def directive_line(text: str, verb: str) -> int | None:
    """1-based line of the first single-line `verb` directive in go.mod / go.work text."""
    for i, line in enumerate(text.split("\n"), 1):
        m = _DIRECTIVE.match(line)
        if m and m.group(1) == verb:
            return i
    return None


# ---------------------------------------------------------------- vendor/modules.txt (modload/vendor.go)

def parse_modules_txt(text: str) -> dict:
    """Parse vendor/modules.txt like modload.readVendorList.

    Returns {"workspace": bool, "modules": [{"path", "version", "replace",
    "explicit", "go", "packages"}]} with one entry per distinct (path, version)
    header in order of appearance; version "" marks a wildcard replacement
    record (`# path => dir`). "replace" is {"path", "version"} or None."""
    lines = text.split("\n")
    workspace = bool(lines) and lines[0].strip() == "## workspace"
    mods: dict[tuple[str, str], dict] = {}
    cur: tuple[str, str] | None = None

    def entry(key: tuple[str, str]) -> dict:
        if key not in mods:
            mods[key] = {"path": key[0], "version": key[1], "replace": None,
                         "explicit": False, "go": None, "packages": []}
        return mods[key]

    for line in lines:
        if line.startswith("# "):
            f = line.split()
            if len(f) < 3:
                continue
            if _semver_parse(f[2]) is not None:
                cur = (f[1], f[2])
                f = f[3:]
            elif f[2] == "=>":
                cur = (f[1], "")
                f = f[2:]
            else:
                cur = None
                continue
            e = entry(cur)
            if len(f) >= 2 and f[0] == "=>":
                if len(f) == 2:
                    e["replace"] = {"path": f[1], "version": ""}
                elif len(f) == 3 and _semver_parse(f[2]) is not None:
                    e["replace"] = {"path": f[1], "version": f[2]}
            continue
        if cur is None:
            continue
        if line.startswith("## "):
            e = entry(cur)
            for item in line[3:].split(";"):
                item = item.strip()
                if item == "explicit":
                    e["explicit"] = True
                elif item.startswith("go "):
                    e["go"] = item[3:]
            continue
        f = line.split()
        if len(f) == 1 and _plausible_import_path(f[0]):
            entry(cur)["packages"].append(f[0])
    return {"workspace": workspace, "modules": list(mods.values())}


def _plausible_import_path(p: str) -> bool:
    """A cheap stand-in for module.CheckImportPath on modules.txt package lines."""
    return (not p.startswith(("#", "/", ".")) and not p.endswith("/") and "//" not in p
            and all(c.isalnum() or c in "-._~/+" for c in p))


def _replace_map(gomod: dict) -> dict[tuple[str, str], dict]:
    return {(r["Old"]["Path"], r["Old"].get("Version", "")): r["New"]
            for r in gomod.get("Replace") or []}


def _lookup_replace(rmap: dict[tuple[str, str], dict], path: str, version: str) -> dict | None:
    return rmap.get((path, version)) or rmap.get((path, ""))


def vendor_errors(gomods: list[dict], modtxt: dict, *, workspace: bool = False,
                  gowork: dict | None = None, roots: list[str] | None = None,
                  work_dir: str | None = None) -> list[str]:
    """Port of modload.checkVendorConsistency: messages for every way the
    modules.txt contradicts the main modules' go.mod files (and, in workspace
    mode, the go.work replaces). An empty list means the go command would use
    the vendor directory. *roots* and *work_dir* canonicalize relative go.mod
    replacements in workspace mode, as the go command does."""
    meta = {(m["path"], m["version"]): m for m in modtxt["modules"]}
    vendor_version: dict[str, str] = {}
    vendor_list: list[tuple[str, str]] = []
    for m in modtxt["modules"]:
        if not m["packages"]:
            continue
        seen_v = vendor_version.get(m["path"])
        if seen_v is None or semver_compare(seen_v, m["version"]) < 0:
            vendor_version[m["path"]] = m["version"]
            vendor_list.append((m["path"], m["version"]))
    errors: list[str] = []

    def err(path: str, version: str, detail: str) -> None:
        errors.append(f"{path}@{version}: {detail}" if version else f"{path}: {detail}")

    pre114 = (not workspace and len(gomods) == 1
              and compare_go_versions(gomods[0].get("Go") or "1.16", "1.14") < 0)
    for gm in gomods:
        for r in gm.get("Require") or []:
            p, v = r["Path"], r["Version"]
            if not meta.get((p, v), {}).get("explicit"):
                if pre114:
                    vv = vendor_version.get(p)
                    if vv is not None and vv != v:
                        err(p, v, f"is explicitly required in go.mod, but vendor/modules.txt "
                                  f"indicates {p}@{vv}")
                else:
                    err(p, v, "is explicitly required in go.mod, but not marked as explicit "
                              "in vendor/modules.txt")

    mains = {gm["Module"]["Path"] for gm in gomods}
    work_map = _replace_map(gowork) if gowork else {}
    maps = [(_replace_map(gm), (roots[i] if roots else None)) for i, gm in enumerate(gomods)]

    def effective(path: str, version: str) -> dict | None:
        if path in mains and version == "":
            return None
        r = _lookup_replace(work_map, path, version)
        if r is not None:
            return dict(r)
        for rmap, root in maps:
            r = _lookup_replace(rmap, path, version)
            if r is not None:
                r = dict(r)
                if (workspace and work_dir and root and not r.get("Version")
                        and not os.path.isabs(r["Path"])):
                    rel = os.path.relpath(os.path.normpath(os.path.join(root, r["Path"])), work_dir)
                    r["Path"] = rel if is_directory_path(rel) else "./" + rel
                return r
        return None

    def describe(m: dict) -> str:
        return f"{m['path']}@{m['version']}" if m.get("version") else m["path"]

    seen: set[tuple[str, str]] = set()
    sources = [gm.get("Replace") or [] for gm in gomods]
    if gowork:
        sources.append(gowork.get("Replace") or [])
    for replaces in sources:
        for r in replaces:
            old = (r["Old"]["Path"], r["Old"].get("Version", ""))
            if old in seen:
                continue
            seen.add(old)
            new = effective(*old)
            vr = meta.get(old, {}).get("replace")
            if vr is None:
                if new is None:
                    continue
                if pre114 and (old[1] == "" or vendor_version.get(old[0]) != old[1]):
                    continue
                err(old[0], old[1], "is replaced in go.mod, but not marked as replaced in "
                                    "vendor/modules.txt")
            elif new is None or (vr["path"], vr["version"]) != (new["Path"], new.get("Version", "")):
                shown = describe({"path": new["Path"], "version": new.get("Version", "")}) if new else ""
                err(old[0], old[1], f"is replaced by {shown} in go.mod, but marked as replaced by "
                                    f"{describe(vr)} in vendor/modules.txt")

    required = {(r["Path"], r["Version"]) for gm in gomods for r in gm.get("Require") or []}
    article = "a " if workspace else ""
    for p, v in vendor_list:
        if meta[(p, v)]["explicit"] and (p, v) not in required:
            err(p, v, f"is marked as explicit in vendor/modules.txt, but not explicitly "
                      f"required in {article}go.mod")
    where = "the workspace" if workspace else "go.mod"
    for m in modtxt["modules"]:
        if m["replace"] is not None and effective(m["path"], m["version"]) is None:
            err(m["path"], m["version"], f"is marked as replaced in vendor/modules.txt, but not "
                                         f"replaced in {where}")
    return errors


# ---------------------------------------------------------------- build constraints (go/build)

KNOWN_OS = frozenset({
    "aix", "android", "darwin", "dragonfly", "freebsd", "hurd", "illumos", "ios", "js", "linux",
    "nacl", "netbsd", "openbsd", "plan9", "solaris", "wasip1", "windows", "zos"})
UNIX_OS = frozenset({
    "aix", "android", "darwin", "dragonfly", "freebsd", "hurd", "illumos", "ios", "linux",
    "netbsd", "openbsd", "solaris"})
KNOWN_ARCH = frozenset({
    "386", "amd64", "amd64p32", "arm", "armbe", "arm64", "arm64be", "loong64", "mips", "mipsle",
    "mips64", "mips64le", "mips64p32", "mips64p32le", "ppc", "ppc64", "ppc64le", "riscv",
    "riscv64", "s390", "s390x", "sparc", "sparc64", "wasm"})
RELEASE_TAGS_MAX = 27                          # go1.1 ... go1.27 are true under go1.27.2

_REGABI_ARCH = frozenset({"amd64", "arm64", "loong64", "ppc64", "ppc64le", "riscv64", "s390x"})
_NO_DWARF5_OS = frozenset({"aix", "darwin", "ios"})
_ARCH_TAGS = {"386": ("386.sse2",), "amd64": ("amd64.v1",), "arm": ("arm.5", "arm.6", "arm.7"),
              "arm64": ("arm64.v8.0",), "mips": ("mips.hardfloat",),
              "mipsle": ("mipsle.hardfloat",), "mips64": ("mips64.hardfloat",),
              "mips64le": ("mips64le.hardfloat",), "ppc64": ("ppc64.power8",),
              "ppc64le": ("ppc64le.power8",), "riscv64": ("riscv64.rva20u64",),
              "wasm": ("wasm.satconv", "wasm.signext")}


def tool_tags(goos: str, goarch: str) -> tuple[str, ...]:
    """context.ToolTags of go1.27.2 for GOOS/GOARCH with default GOEXPERIMENT and
    GOAMD64/GOARM/... levels (recorded for every port on 2026-10-13)."""
    tags: list[str] = []
    if goarch in _REGABI_ARCH:
        tags += ["goexperiment.regabiwrappers", "goexperiment.regabiargs"]
    if goos not in _NO_DWARF5_OS:
        tags.append("goexperiment.dwarf5")
    tags += ["goexperiment.jsonv2", "goexperiment.greenteagc",
             "goexperiment.randomizedheapbase64", "goexperiment.sizespecializedmalloc"]
    return tuple(tags) + _ARCH_TAGS.get(goarch, ())


class _Tags:
    """go/build Context.matchTag for one build configuration."""

    def __init__(self, build: BuildConfig):
        self.goos, self.goarch, self.cgo = build.goos, build.goarch, build.cgo
        self.tags = (set(build.tags) | set(tool_tags(build.goos, build.goarch))
                     | {f"go1.{i}" for i in range(1, RELEASE_TAGS_MAX + 1)})

    def match(self, name: str) -> bool:
        if self.cgo and name == "cgo":
            return True
        if name in (self.goos, self.goarch, "gc"):
            return True
        if (self.goos, name) in (("android", "linux"), ("illumos", "solaris"), ("ios", "darwin")):
            return True
        if name == "unix" and self.goos in UNIX_OS:
            return True
        if name == "boringcrypto":
            name = "goexperiment.boringcrypto"
        return name in self.tags


def _good_os_arch_file(name: str, tags: _Tags) -> bool:
    name = name.split(".", 1)[0]
    i = name.find("_")
    if i < 0:
        return True
    parts = name[i:].split("_")
    if parts[-1] == "test":
        parts = parts[:-1]
    n = len(parts)
    if n >= 2 and parts[-2] in KNOWN_OS and parts[-1] in KNOWN_ARCH:
        return tags.match(parts[-1]) and tags.match(parts[-2])
    if n >= 1 and (parts[-1] in KNOWN_OS or parts[-1] in KNOWN_ARCH):
        return tags.match(parts[-1])
    return True


class ConstraintError(ValueError):
    pass


def _is_tag_rune(c: str) -> bool:
    """unicode.IsLetter || unicode.IsDigit || '_' || '.'."""
    if c in "_.":
        return True
    cat = unicodedata.category(c)
    return cat.startswith("L") or cat == "Nd"


def _parse_go_build_expr(s: str) -> tuple:
    """go/build/constraint.parseExpr for a //go:build expression; raises ConstraintError."""
    toks: list = []
    i = 0
    while True:
        while i < len(s) and s[i] in " \t":
            i += 1
        if i >= len(s):
            break
        c = s[i]
        if c in "()!":
            toks.append(c)
            i += 1
            continue
        if c in "&|":
            if i + 1 >= len(s) or s[i + 1] != c:
                raise ConstraintError("invalid syntax at " + c)
            toks.append(c * 2)
            i += 2
            continue
        j = i
        while j < len(s) and _is_tag_rune(s[j]):
            j += 1
        if j == i:
            raise ConstraintError("invalid syntax at " + c)
        toks.append(("tag", s[i:j]))
        i = j
    pos, size = [0], [0]

    def peek():
        return toks[pos[0]] if pos[0] < len(toks) else None

    def advance():
        t = peek()
        pos[0] += 1
        return t

    def p_or():
        x = p_and()
        while peek() == "||":
            advance()
            x = ("or", x, p_and())
        return x

    def p_and():
        x = p_not()
        while peek() == "&&":
            advance()
            x = ("and", x, p_not())
        return x

    def p_not():
        size[0] += 1
        if size[0] > 1000:
            raise ConstraintError("build expression too large")
        if peek() == "!":
            advance()
            if peek() == "!":
                raise ConstraintError("double negation not allowed")
            return ("not", p_atom())
        return p_atom()

    def p_atom():
        t = advance()
        if t == "(":
            x = p_or()
            if advance() != ")":
                raise ConstraintError("missing close paren")
            return x
        if isinstance(t, tuple):
            return t
        raise ConstraintError("unexpected end of expression" if t is None else "unexpected token " + t)

    x = p_or()
    if peek() is not None:
        raise ConstraintError("unexpected token")
    return x


def _eval(x: tuple, tags: _Tags) -> bool:
    if x[0] == "tag":
        return tags.match(x[1])
    if x[0] == "not":
        return not _eval(x[1], tags)
    a, b = _eval(x[1], tags), _eval(x[2], tags)
    return (a and b) if x[0] == "and" else (a or b)


def _parse_plus_build(text: str) -> tuple | None:
    """// +build: space = OR, comma = AND, ! = NOT; an invalid literal is the tag
    "ignore"; more than 100 operators makes the line count for nothing (None)."""
    x, size = None, 0
    for clause in text.split():
        y = None
        for lit in clause.split(","):
            if lit.startswith("!!") or lit == "!":
                z: tuple = ("tag", "ignore")
            else:
                neg = lit.startswith("!")
                word = lit[1:] if neg else lit
                z = ("tag", word) if word and all(_is_tag_rune(c) for c in word) else ("tag", "ignore")
                if neg:
                    z = ("not", z)
            if y is None:
                y = z
            else:
                size += 1
                if size > 100:
                    return None
                y = ("and", y, z)
        if x is None:
            x = y
        else:
            size += 1
            if size > 100:
                return None
            x = ("or", x, y)
    return x if x is not None else ("tag", "ignore")


# unicode.IsSpace, which bytes.TrimSpace uses.
_GO_SPACE = ("\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
             "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000")


def _split_marker(line: str, marker: str) -> str | None:
    """The expression after `marker` (`//go:build` or `+build`) when the line is
    exactly that marker followed by space or nothing; else None."""
    if not line.startswith(marker):
        return None
    rest = line[len(marker):]
    trim = rest.strip(_GO_SPACE)
    if len(rest) == len(trim) and rest != "":
        return None
    return trim


def _file_header(text: str) -> tuple[list[str], str | None]:
    """go/build.parseFileHeader: (the lines before the last blank line of the
    leading comment block, the //go:build line or None). Raises ConstraintError
    for a second //go:build line."""
    lines = text.split("\n")
    end = 0
    ended = in_star = False
    go_build = None
    for idx, raw in enumerate(lines):
        line = raw.strip(_GO_SPACE)
        if not line and not ended:
            end = idx + 1
            continue
        if not line.startswith("//"):
            ended = True
        if not in_star and _split_marker(line, "//go:build") is not None:
            if go_build is not None:
                raise ConstraintError("multiple //go:build comments")
            go_build = line
        stop = False
        while line:
            if in_star:
                k = line.find("*/")
                if k < 0:
                    break
                in_star = False
                line = line[k + 2:].strip(_GO_SPACE)
                continue
            if line.startswith("//"):
                break
            if line.startswith("/*"):
                in_star = True
                line = line[2:].strip(_GO_SPACE)
                continue
            stop = True
            break
        if stop:
            break
    return lines[:end], go_build


def _should_build(header: tuple[list[str], str | None], tags: _Tags) -> bool:
    region, go_build = header
    if go_build is not None:
        return _eval(_parse_go_build_expr(_split_marker(go_build, "//go:build") or ""), tags)
    ok = True
    for raw in region:
        line = raw.strip(_GO_SPACE)
        if not line.startswith("//") or "+build" not in line:
            continue
        expr = _split_marker(line[2:].strip(_GO_SPACE), "+build")
        if expr is None:
            continue
        x = _parse_plus_build(expr)
        if x is not None and not _eval(x, tags):
            ok = False
    return ok


_TOKEN = re.compile(r'\s+|//[^\n]*|/\*.*?\*/|"(?:[^"\\\n]|\\.)*"|`[^`]*`'
                    r"|[A-Za-z_\u0080-\U0010ffff][\w\u0080-\U0010ffff]*|.", re.S)


def _tokens(text: str):
    """Go tokens with a ";" for each newline that ends a line with tokens."""
    last = None
    for m in _TOKEN.finditer(text):
        t = m.group(0)
        if t.isspace() or t.startswith("//") or t.startswith("/*"):
            if "\n" in t and last is not None and last != ";":
                last = ";"
                yield ";"
            continue
        last = t
        yield t


def _prologue(text: str) -> tuple[str | None, list[str]]:
    """(package clause name, import paths); reads no further than the imports."""
    it = _tokens(text)
    tok = next(it, None)
    while tok == ";":
        tok = next(it, None)
    if tok != "package":
        return None, []
    pkg = next(it, None)
    imports: list[str] = []
    tok = next(it, None)
    while tok is not None:
        if tok == ";":
            tok = next(it, None)
            continue
        if tok != "import":
            break
        tok = next(it, None)
        closer = ")" if tok == "(" else ";"
        while tok is not None and tok != closer:
            if tok[:1] in "\"`" and len(tok) >= 2:
                imports.append(tok[1:-1])
            tok = next(it, None)
        tok = next(it, None)
    return pkg, imports


class _Source:
    """One .go file, with its header and prologue parsed once for every build."""

    def __init__(self, relpath: str, data: bytes):
        self.name = posixpath.basename(relpath.replace("\\", "/"))
        text = data.decode("utf-8", errors="replace")
        self.text = text[1:] if text.startswith("\ufeff") else text
        self._header: tuple | ConstraintError | None = None
        self._prologue: tuple[str | None, list[str]] | None = None

    @property
    def prologue(self) -> tuple[str | None, list[str]]:
        if self._prologue is None:
            self._prologue = _prologue(self.text)
        return self._prologue

    def built_by(self, build: BuildConfig) -> bool:
        if self.name.startswith(("_", ".")) or not self.name.endswith(".go"):
            return False
        tags = _Tags(build)
        if not _good_os_arch_file(self.name, tags):
            return False
        if self._header is None:
            try:
                self._header = _file_header(self.text)
            except ConstraintError as exc:
                self._header = exc
        if isinstance(self._header, ConstraintError):
            return False
        try:
            if not _should_build(self._header, tags):
                return False
        except ConstraintError:
            return False
        pkg, imports = self.prologue
        if pkg == "documentation":
            return False
        if "C" in imports:
            return build.cgo and not self.name.endswith("_test.go")
        return True


def _file_facts(path: str, relpath: str, builds: list[BuildConfig]) -> tuple[list[str], frozenset[str]]:
    """(import paths, ids of the builds that compile the file) for one .go file."""
    try:
        with open(path, "rb") as f:
            src = _Source(relpath, f.read())
    except OSError:
        return [], frozenset()
    built = frozenset(b.id for b in builds if src.built_by(b))
    return (src.prologue[1] if built else []), built


def file_imports(path: str) -> list[str]:
    """Import paths of a .go file (every file, whatever its build constraints)."""
    try:
        with open(path, "rb") as f:
            return _Source(path, f.read()).prologue[1]
    except OSError:
        return []


def match_file(relpath: str, content_head: bytes, build: BuildConfig) -> bool:
    """True when the go command compiles this .go file into its package under
    *build*: go/build's MatchFile (name prefix, GOOS/GOARCH file name suffixes,
    //go:build or // +build) plus its package rules (`import "C"` needs cgo
    and is invalid in a _test.go file; `package documentation` is ignored).
    *content_head* is the file's bytes, at least through the import block."""
    return _Source(relpath, content_head).built_by(build)


# ---------------------------------------------------------------- scanning (spec §3.1)

class _Ignore:
    """go.mod `ignore` patterns of one module (cmd/go/internal/search.IgnorePatterns)."""

    def __init__(self, paths: list[str]):
        self.relative = [_slashes(p[2:]) for p in paths if p.startswith("./")]
        self.anywhere = [_slashes(p) for p in paths if not p.startswith("./")]

    def match(self, rel_to_module: str) -> bool:
        d = _slashes(rel_to_module)
        return any(d.startswith(p) for p in self.relative) or any(p in d for p in self.anywhere)


def _slashes(p: str) -> str:
    p = p.replace("\\", "/")
    return ("" if p.startswith("/") else "/") + p + ("" if p.endswith("/") else "/")


def _module_ignore(gomod_path: str) -> _Ignore | None:
    try:
        with open(gomod_path, encoding="utf-8", errors="surrogateescape") as f:
            paths = [i["Path"] for i in parse_go_mod(f.read(), strict=False).get("Ignore") or []]
    except (OSError, ModError):
        return None
    return _Ignore(paths) if paths else None


class _Layout:
    """What one walk of a repo found."""

    def __init__(self):
        self.files: list[str] = []             # scanned .go files, sorted relpaths
        self.skipped: dict[str, int] = {}
        self.owner: dict[str, str] = {}        # file -> root of the module that owns it
        self.module_roots: list[str] = []      # dirs with go.mod, sorted
        self.workfiles: list[str] = []         # go.work files, sorted


def _rel(root: str, path: str) -> str:
    rel = os.path.relpath(path, root)
    return "" if rel == "." else rel.replace(os.sep, "/")


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _excluded_dirs(root: str, exclude) -> set[str]:
    out = set()
    for p in list(exclude) + [str(setting("OUTPUT_DIR", "output_docs_pipeline") or "")]:
        if p:
            real = os.path.realpath(os.path.abspath(p))
            if real != root and _inside(real, root):
                out.add(real)
    return out


def _work_use_dirs(work_path: str) -> list[str]:
    """Absolute real paths of the `use` directories of a go.work ([] if unparseable)."""
    try:
        with open(work_path, encoding="utf-8", errors="surrogateescape") as f:
            work = parse_go_work(f.read())
    except (OSError, ModError):
        return []
    base = os.path.dirname(work_path)
    return [os.path.realpath(os.path.join(base, u["DiskPath"])) for u in work.get("Use") or []]


def _walk(root: str, exclude) -> _Layout:
    """Walk *root* (a real path) like the go command's `./...`: directories named
    testdata or starting with "." or "_", everything below a directory named
    vendor and a module's go.mod `ignore` directories are pruned; nested modules
    are walked as their own modules; symlinked directories are not followed;
    `.go` files starting with "_" or "." are skipped. A go.work `use` directory
    is walked even inside a pruned directory, because the go command loads it.
    Skips are counted per reason in .go files (dot directories are not entered)."""
    lay = _Layout()
    excluded = _excluded_dirs(root, exclude)
    skipped: dict[str, int] = {}
    pruned: list[tuple[str, str]] = []         # (abs dir, reason)
    links: list[tuple[str, str]] = []          # (relpath, real target)
    walked: set[str] = set()
    exempt: set[str] = set()
    modules: set[str] = set()
    found: set[str] = set()

    def walk_from(start: str, mod_root: str | None, ignore: _Ignore | None) -> None:
        stack = [(start, mod_root, ignore, False)]
        while stack:
            d, mroot, ign, is_vendor = stack.pop()
            if d in walked:
                continue
            walked.add(d)
            rel_d = _rel(root, d)
            if os.path.isfile(os.path.join(d, "go.mod")):
                mroot, ign = rel_d, _module_ignore(os.path.join(d, "go.mod"))
                modules.add(rel_d)
            if os.path.isfile(os.path.join(d, "go.work")):
                lay.workfiles.append(posixpath.join(rel_d, "go.work") if rel_d else "go.work")
                exempt.update(u for u in _work_use_dirs(os.path.join(d, "go.work"))
                              if _inside(u, root))
            try:
                entries = sorted(os.scandir(d), key=lambda e: e.name)
            except OSError:
                continue
            subdirs = []
            for e in entries:
                full = os.path.join(d, e.name)
                if e.is_symlink():
                    if os.path.isdir(full):
                        skipped["symlink-dir"] = skipped.get("symlink-dir", 0) + 1
                    elif e.name.endswith(".go"):
                        links.append((_rel(root, full), os.path.realpath(full)))
                    continue
                if e.is_dir(follow_symlinks=False):
                    subdirs.append(e)
                    continue
                if not e.name.endswith(".go") or not e.is_file(follow_symlinks=False):
                    continue
                rel = _rel(root, full)
                if e.name.startswith("_"):
                    reason = "underscore"
                elif e.name.startswith("."):
                    reason = "dot"
                elif not _utf8(rel):
                    reason = "non-utf8"
                elif not os.access(full, os.R_OK):
                    reason = "unreadable"
                else:
                    found.add(full)
                    lay.files.append(rel)
                    if mroot is not None:
                        lay.owner[rel] = mroot
                    continue
                skipped[reason] = skipped.get(reason, 0) + 1
            for e in reversed(subdirs):
                full = os.path.join(d, e.name)
                reason = _prune_reason(e.name, full, is_vendor, excluded)
                if reason is None and ign is not None and mroot is not None:
                    rel_mod = os.path.relpath(full, os.path.join(root, mroot) if mroot else root)
                    if ign.match(rel_mod):
                        reason = "ignored"
                if reason is not None:
                    pruned.append((full, reason))
                    continue
                stack.append((full, mroot, ign, e.name == "vendor"))

    walk_from(root, None, None)
    while True:                                # go.work use dirs inside pruned dirs
        todo = sorted(u for u in exempt if u not in walked
                      and os.path.isfile(os.path.join(u, "go.mod"))
                      and not any(_inside(u, x) for x in excluded)
                      and any(_inside(u, p) for p, _ in pruned))
        if not todo:
            break
        for u in todo:
            walk_from(u, None, None)
    for d, reason in pruned:
        if reason in ("dot", "excluded") or d in walked:
            continue
        n = _count_go_files(d, walked)
        if n:
            skipped[reason] = skipped.get(reason, 0) + n
    for rel, real in links:
        key = "symlink" if real in found else "symlink-unscanned"
        skipped[key] = skipped.get(key, 0) + 1
    lay.files.sort()
    lay.module_roots = sorted(modules)
    lay.workfiles.sort()
    lay.skipped = skipped
    return lay


def _prune_reason(name: str, full: str, parent_is_vendor: bool, excluded: set[str]) -> str | None:
    if full in excluded:
        return "excluded"
    if name.startswith("."):
        return "dot"
    if name.startswith("_"):
        return "underscore"
    if name == "testdata":
        return "testdata"
    if parent_is_vendor:
        return "vendor"
    return None


def _count_go_files(d: str, walked: set[str]) -> int:
    n = 0
    stack = [d]
    while stack:
        cur = stack.pop()
        if cur in walked:
            continue
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                stack.append(e.path)
            elif e.name.endswith(".go") and e.is_file(follow_symlinks=False):
                n += 1
    return n


def _utf8(rel: str) -> bool:
    try:
        rel.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def scan_go_files(repo_root: str, exclude=()) -> tuple[list[str], dict[str, int]]:
    """Sorted repo-relative POSIX paths of the .go files the repo's modules can
    compile (see _walk for the rules), and skip counts by reason. OUTPUT_DIR
    and the *exclude* directories are pruned when they lie inside the repo.

    Reasons count .go files: "testdata", "underscore" and "dot" (directory or
    file names; dot directories are not entered, so only dot files count),
    "vendor" (below a vendor directory), "ignored" (go.mod ignore),
    "non-utf8" and "unreadable". A symlinked .go file counts as "symlink"
    when its real path is scanned (the real file is kept) and as
    "symlink-unscanned" otherwise; "symlink-dir" counts symlinked directories."""
    lay = _walk(os.path.realpath(repo_root), exclude)
    return lay.files, lay.skipped


# ---------------------------------------------------------------- modules, workspaces, groups (spec §3.1, §3.2)

# Spec §3.3 also lists toolchain lines. Under GOTOOLCHAIN=local the go command
# ignores them (go1.27.2 loads `go 1.26` + `toolchain go1.28.1`; probed
# 2026-10-13, test_go_command_agrees_with_the_toolchain_gate), so by default a
# newer toolchain line only warns. True makes it fail the group closed too.
GATE_TOOLCHAIN_LINES = False


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as f:
            return f.read()
    except OSError:
        return None


class _Mod:
    """A parsed go.mod: one of this repo's modules, or an out-of-repo workspace module."""

    def __init__(self, abs_dir: str, label: str, gomod: dict, text: str):
        self.dir = abs_dir                     # absolute real path
        self.label = label                     # how messages name the go.mod
        self.gomod = gomod
        self.text = text
        self.path: str = gomod["Module"]["Path"]

    @property
    def go(self) -> str | None:
        return self.gomod.get("Go")


def _load_mod(abs_dir: str, label: str) -> tuple[_Mod | None, str]:
    text = _read(os.path.join(abs_dir, "go.mod"))
    if text is None:
        return None, f"{label}: no go.mod"
    try:
        gomod = parse_go_mod(text)
    except ModError as exc:
        return None, f"{label}:{exc.line}: {exc}" if exc.line else f"{label}: {exc}"
    if not gomod["Module"]["Path"]:
        return None, f"{label}: no module directive"
    return _Mod(abs_dir, label, gomod, text), ""


def _join(root: str, rel: str) -> str:
    """Absolute path of repo-relative *rel* ("" = *root*), without a trailing slash."""
    return os.path.join(root, rel) if rel else root


def _gomod_label(rel: str) -> str:
    return posixpath.join(rel, "go.mod") if rel else "go.mod"


def _modtxt_go_lines(label: str, text: str) -> list[tuple[str, int, str, str]]:
    out = []
    for i, line in enumerate(text.split("\n"), 1):
        if line.startswith("## "):
            for item in line[3:].split(";"):
                item = item.strip()
                if item.startswith("go "):
                    out.append((label, i, "go", item[3:]))
    return out


def _vendoring_error(module_dir: str, errs: list[str], fix: str) -> str:
    """The go command's refusal, as one warning line (R35)."""
    return (f"go: inconsistent vendoring in {module_dir}: {'; '.join(errs)}. The go command "
            f"refuses to build this group, so its sites are unresolved (load-error). Fix: {fix}")


def _module_vendor(m: _Mod, label_dir: str) -> tuple[bool, str | None, str | None, list]:
    """(vendor mode, error, note, modules.txt go lines) for a module in
    single-module mode. An inconsistent vendor/ is an error (R35)."""
    vdir = os.path.join(m.dir, "vendor")
    if not os.path.isdir(vdir) or compare_go_versions(m.go or "1.16", "1.14") < 0:
        return False, None, None, []
    label = posixpath.join(label_dir, "vendor/modules.txt") if label_dir else "vendor/modules.txt"
    text = _read(os.path.join(vdir, "modules.txt")) or ""
    modtxt = parse_modules_txt(text)
    if modtxt["workspace"]:
        return False, None, (f"{label} starts with \"## workspace\", so in single-module mode "
                             f"the go command does not use this vendor directory"), []
    errs = vendor_errors([m.gomod], modtxt)
    if errs:
        return False, _vendoring_error(m.dir, errs, "go mod vendor"), None, []
    return True, None, None, _modtxt_go_lines(label, text)


class _Facts:
    """What a group's go.work / go.mod files say, read from disk."""

    def __init__(self, group: Group):
        self.work: dict | None = None
        self.work_dir: str | None = None
        self.work_text = ""
        self.mains: list[_Mod] = []
        if group.kind == "workspace" and group.source:
            self.work_dir = os.path.dirname(group.source)
            self.work_text = _read(group.source) or ""
            try:
                self.work = parse_go_work(self.work_text)
            except ModError:
                self.work = {}
            for d in group.uses:
                m, _err = _load_mod(d, os.path.join(d, "go.mod"))
                if m is not None:
                    self.mains.append(m)
        elif group.source:
            m, _err = _load_mod(os.path.dirname(group.source), group.source)
            if m is not None:
                self.mains.append(m)
        self.main_dirs = {m.path: m.dir for m in self.mains}
        self.maps: list[tuple[dict, str]] = []
        if self.work:
            self.maps.append((_replace_map(self.work), self.work_dir or ""))
        self.maps += [(_replace_map(m.gomod), m.dir) for m in self.mains]
        self.replaced = {old for rmap, _base in self.maps for old, _v in rmap}
        self.requires: dict[str, set[str]] = {}
        for m in self.mains:
            for r in m.gomod.get("Require") or []:
                self.requires.setdefault(r["Path"], set()).add(r["Version"])

    def source(self, path: str, version: str) -> tuple[str, object]:
        """("main", dir) | ("dir", abs dir) | ("version", (path, version)) the group builds."""
        if path in self.main_dirs:
            return "main", self.main_dirs[path]
        for rmap, base in self.maps:
            r = _lookup_replace(rmap, path, version)
            if r is not None:
                if r.get("Version"):
                    return "version", (r["Path"], r["Version"])
                return "dir", os.path.realpath(os.path.join(base, r["Path"]))
        return "version", (path, version)

    def needs(self, path: str, version: str) -> tuple[str, str, bool] | None:
        """(path, version, go.mod only) the module graph fetches for a requirement,
        or None when a directory replacement provides it. A requirement on a
        workspace module still needs that version's go.mod unless it is replaced."""
        for rmap, _base in self.maps:
            r = _lookup_replace(rmap, path, version)
            if r is not None:
                return (r["Path"], r["Version"], False) if r.get("Version") else None
        if path in self.main_dirs:
            return path, version, True
        return path, version, False

    def work_go_line(self) -> tuple[str | None, _Mod | None]:
        """The generated go.work's go line: the repo's, raised to the highest
        module go line when the go command would refuse the workspace."""
        declared = (self.work or {}).get("Go")
        top, culprit = declared or "1.18", None
        for m in self.mains:
            if compare_go_versions(m.go or "1.16", top) > 0:
                top, culprit = m.go, m
        return (top if culprit else declared), culprit


def _imported_modules(facts: _Facts, import_lists) -> set[str]:
    """Required module paths that provide a package imported by the files."""
    paths = sorted(facts.requires, key=len, reverse=True)
    out: set[str] = set()
    seen: set[str] = set()
    for imports in import_lists:
        for imp in imports:
            if imp in seen or "." not in imp.split("/", 1)[0]:
                continue                       # std packages have no dot in the first element
            seen.add(imp)
            for p in paths:
                if imp == p or imp.startswith(p + "/"):
                    out.add(p)
                    break
    return out


def _too_new(where: str, verb: str, version: str, tc: str) -> str:
    need = from_toolchain_name(version) if verb == "toolchain" else version
    return (f'{where}: "{verb} {version}" is newer than the toolchain {tc} (GOTOOLCHAIN=local '
            f"never downloads another), so the group's sites are unresolved (toolchain). "
            f"Fix: {_fix_for(need)}")


def _gate(lines: list[tuple[str, int | None, str, str]], tc: str) -> tuple[bool, list[str]]:
    """(fails closed, warnings) for the go / toolchain lines of a group (spec §3.3)."""
    failed, out = False, []
    if not tc:
        return False, out
    for label, ln, verb, version in lines:
        where = f"{label}:{ln}" if ln else label
        if verb == "go" and compare_go_versions(version, tc) > 0:
            failed = True
            out.append(_too_new(where, verb, version, tc))
        elif verb == "toolchain":
            need = from_toolchain_name(version)
            if need and compare_go_versions(need, tc) > 0:
                if GATE_TOOLCHAIN_LINES:
                    failed = True
                    out.append(_too_new(where, verb, version, tc))
                else:
                    out.append(f'{where}: "toolchain {version}" is newer than the toolchain {tc}; '
                               f"GOTOOLCHAIN=local ignores the line, so the group still loads, but "
                               f"standard-library targets may differ from what {version} compiles "
                               f"(set GO_TOOLCHAIN to a go >= {need} binary to match it)")
    return failed, out


def _mod_lines(m: _Mod, label: str) -> list[tuple[str, int | None, str, str]]:
    out = []
    for verb, key in (("go", "Go"), ("toolchain", "Toolchain")):
        if m.gomod.get(key):
            out.append((label, directive_line(m.text, verb), verb, m.gomod[key]))
    return out


def _check_repo_name(repo: str) -> None:
    if not repo or repo in (".", "..") or "/" in repo or os.sep in repo or "\0" in repo:
        raise ValueError(f"invalid repo name {repo!r}: it must be a single path element")


def _control_base(output_dir: str, repo: str) -> str:
    return os.path.join(os.path.realpath(os.path.abspath(output_dir)), "repos", repo, "go")


def _replace_obj(r: dict, base: str) -> Replace:
    new = r["New"]
    local = not new.get("Version")
    return Replace(old=r["Old"]["Path"], old_version=r["Old"].get("Version", ""),
                   new=os.path.realpath(os.path.join(base, new["Path"])) if local else new["Path"],
                   new_version=new.get("Version", ""), local=local)


def _unit_of(m: _Mod, rel: str, vendor: bool, deps: tuple[str, ...] = ()) -> GoUnit:
    return GoUnit(
        key=m.path, root=rel, go=m.go, toolchain=m.gomod.get("Toolchain"),
        requires=tuple((r["Path"], r["Version"], bool(r.get("Indirect")))
                       for r in m.gomod.get("Require") or []),
        replaces=tuple(_replace_obj(r, m.dir) for r in m.gomod.get("Replace") or []),
        vendor=vendor, in_repo_deps=deps)


def _nearest_work(module_dir: str, root: str, work_dirs: dict[str, str]) -> str | None:
    """The go.work the go command finds walking up from *module_dir* inside the repo."""
    d = module_dir
    while True:
        if d in work_dirs:
            return work_dirs[d]
        if d == root or not _inside(d, root):
            return None
        d = os.path.dirname(d)


def _in_repo_deps(m: _Mod, imports: list[str], facts: _Facts, mods: dict[str, _Mod]) -> set[str]:
    """In-repo module paths M such that the unit's files (primary build, tests
    included) import a package under M (longest module-path prefix among the
    repo's modules), and the group builds that import from M's in-repo source: M
    is a workspace module of the group, or a local replace points at M's root.
    Anything else resolves to a published version and does not count."""
    by_len = sorted(mods.values(), key=lambda x: len(x.path), reverse=True)
    out: set[str] = set()
    for imp in set(imports):
        dep = next((x for x in by_len if imp == x.path or imp.startswith(x.path + "/")), None)
        if dep is None or dep is m or dep.path in out:
            continue
        versions = sorted(facts.requires.get(dep.path, ()), key=functools.cmp_to_key(semver_compare))
        kind, where = facts.source(dep.path, versions[-1] if versions else "")
        if kind in ("main", "dir") and where == dep.dir:
            out.add(dep.path)
    return out


def _fix_for(need: str) -> str:
    fix = f"set GO_TOOLCHAIN to a go >= {need} binary"
    if compare_go_versions(need, PINNED) <= 0:
        return f"run `python -m graph_generator setup go` (installs {PINNED}) or {fix}"
    return fix + f" (`python -m graph_generator setup go` installs {PINNED}, which is older)"


def _dependency_go(text: str | None) -> str | None:
    if text is None:
        return None
    try:
        return parse_go_mod(text, strict=False).get("Go")
    except ModError:
        return None


def discover(repo_root: str, repo: str, *, builds: list[BuildConfig], peers: list[Peer],
             mode: str, output_dir: str, toolchain: Toolchain | None) -> Discovery:
    """Phase 1 discovery (spec §3 with Appendix B) from files only: units,
    groups, the toolchain gate, vendor and module sources, peer substitutions.
    *output_dir* is the base OUTPUT_DIR; group control directories
    (OUTPUT_DIR/repos/<repo>/go/<group>/) are named here and written by
    prepare_control_dirs."""
    if mode not in ("offline", "download"):
        raise ValueError(f"mode must be 'offline' or 'download', not {mode!r}")
    _check_repo_name(repo)
    root = os.path.realpath(repo_root)
    lay = _walk(root, (output_dir,))
    tc = toolchain.version if toolchain is not None else ""
    warnings: list[str] = []

    mods: dict[str, _Mod] = {}
    for rel in lay.module_roots:
        m, err = _load_mod(_join(root, rel), _gomod_label(rel))
        if m is None:
            warnings.append(f"{err}; the files under {rel or 'the repo root'} are not resolved "
                            f"(no-module)")
        else:
            mods[rel] = m

    works: dict[str, tuple[dict, list[tuple[str, str]]]] = {}
    for rel in lay.workfiles:
        text = _read(os.path.join(root, rel)) or ""
        try:
            parsed = parse_go_work(text)
        except ModError as exc:
            warnings.append(f"{rel}:{exc.line}: {exc}; this go.work is ignored, so the modules "
                            f"below it load as single modules (GOWORK=off)")
            continue
        base = os.path.dirname(os.path.join(root, rel))
        works[rel] = (parsed, [(u["DiskPath"], os.path.realpath(os.path.join(base, u["DiskPath"])))
                               for u in parsed.get("Use") or []])
    work_dirs = {os.path.dirname(os.path.join(root, rel)): rel for rel in lay.workfiles}
    listed: dict[str, list[str]] = {}
    for rel, (_parsed, uses) in works.items():
        for _disk, real in uses:
            listed.setdefault(real, []).append(rel)

    dropped_dup: dict[str, str] = {}
    by_path: dict[str, list[str]] = {}
    for rel, m in mods.items():
        by_path.setdefault(m.path, []).append(rel)
    for path, rels in by_path.items():
        if len(rels) < 2:
            continue
        rels.sort(key=lambda r: (mods[r].dir not in listed, len(r.split("/")) if r else 0, r))
        for rel in rels[1:]:
            warnings.append(f"{_gomod_label(rel)} declares module {path}, like "
                            f"{_gomod_label(rels[0])}; only the latter is a unit, so the files "
                            f"under {rel} are not resolved (no-module)")
            dropped_dup[rel] = _gomod_label(rels[0])
            del mods[rel]

    # R4: a module's group is the go.work found walking up from its root, if
    # that go.work lists it; otherwise it is a single-module group.
    home: dict[str, str | None] = {}
    for rel, m in sorted(mods.items()):
        nearest = _nearest_work(m.dir, root, work_dirs)
        listing = listed.get(m.dir, [])
        home[rel] = nearest if nearest in works and nearest in listing else None
        others = sorted(w for w in listing if w != home[rel])
        if others:
            outcome = (f"it belongs to the workspace of {nearest}" if home[rel] else
                       "it loads as a single module (GOWORK=off)")
            warnings.append(f"{', '.join(others)} lists {rel or '.'}, but the go command only "
                            f"uses the go.work found walking up from a module "
                            f"({nearest or 'none here'}), so {outcome}")

    base = _control_base(output_dir, repo)
    groups: list[Group] = []
    gate_lines: dict[str, list] = {}
    for wrel, (parsed, uses) in sorted(works.items()):
        members = sorted(mods[r].path for r, w in home.items() if w == wrel)
        if not members:
            continue
        gid = f"ws{sum(1 for g in groups if g.kind == 'workspace')}"
        g = Group(id=gid, kind="workspace", dir=os.path.join(base, gid), modules=members,
                  gowork=os.path.join(base, gid, "go.work"), modfile=None, vendor=False,
                  degraded=False, status="ok", pattern="work", source=os.path.join(root, wrel))
        g.replaces = [_replace_obj(r, os.path.dirname(g.source)) for r in parsed.get("Replace") or []]
        work_text = _read(g.source) or ""
        seen: dict[str, str] = {}
        lines: list = []
        if parsed.get("Go"):
            lines.append((wrel, directive_line(work_text, "go"), "go", parsed["Go"]))
        if parsed.get("Toolchain"):
            lines.append((wrel, directive_line(work_text, "toolchain"), "toolchain",
                          parsed["Toolchain"]))
        for disk, real in uses:
            why, m, label = None, None, ""
            if not os.path.isdir(real):
                why = "the directory does not exist"
            elif _inside(real, root):
                rel = _rel(root, real)
                m = mods.get(rel)
                if m is None:
                    why = (f"the module there duplicates {dropped_dup[rel]}" if rel in dropped_dup
                           else "there is no valid go.mod there")
                label = _gomod_label(rel)
            else:
                label = os.path.join(real, "go.mod")
                m, err = _load_mod(real, label)
                if m is None:
                    why = err
            if why is None and m.path in seen:
                why = f"module {m.path} is already used from {seen[m.path]}"
            if why is not None:
                g.warnings.append(f"{wrel}: use {disk}: {why}; dropped, because the go command "
                                  f"would fail on it, so the group is degraded")
                g.degraded = True
                continue
            seen[m.path] = disk
            g.uses.append(real)
            lines += _mod_lines(m, label)
        facts = _Facts(g)
        go_line, culprit = facts.work_go_line()
        if culprit is not None:
            declared = parsed.get("Go")
            where = (f'{wrel}:{directive_line(work_text, "go")}: "go {declared}"' if declared
                     else f"{wrel} has no go line (that means go 1.18), which")
            g.warnings.append(f"{where} is older than \"go {culprit.go}\" of {culprit.label}; the go "
                              f"command refuses such a workspace, so the generated go.work says "
                              f"\"go {go_line}\" and the group is degraded")
            g.degraded = True
        vdir = os.path.join(os.path.dirname(g.source), "vendor")
        if os.path.isdir(vdir):
            vtext = _read(os.path.join(vdir, "modules.txt")) or ""
            modtxt = parse_modules_txt(vtext)
            if modtxt["workspace"]:
                errs = vendor_errors([m.gomod for m in facts.mains], modtxt, workspace=True,
                                     gowork=facts.work, roots=[m.dir for m in facts.mains],
                                     work_dir=facts.work_dir)
                if errs:
                    g.status = "error"
                    g.warnings.append(_vendoring_error(os.path.dirname(g.source), errs,
                                                       "go work vendor"))
                else:
                    g.vendor = True
                    vlabel = posixpath.join(posixpath.dirname(wrel), "vendor/modules.txt").lstrip("/")
                    lines += _modtxt_go_lines(vlabel, vtext)
        gate_lines[gid] = lines
        groups.append(g)

    vendor_of: dict[str, bool] = {}
    for rel, m in sorted(mods.items()):
        vendored, error, note, vlines = _module_vendor(m, rel)
        vendor_of[rel] = vendored
        if home[rel] is not None:
            continue
        gid = f"m{sum(1 for g in groups if g.kind == 'module')}"
        g = Group(id=gid, kind="module", dir=m.dir, modules=[m.path], gowork=None,
                  modfile=os.path.join(base, gid, "go.mod"), vendor=vendored, degraded=False,
                  status="error" if error else "ok", pattern="./...",
                  source=os.path.join(m.dir, "go.mod"))
        g.warnings += [w for w in (error, note) if w]
        gate_lines[gid] = _mod_lines(m, _gomod_label(rel)) + vlines
        groups.append(g)

    disc = Discovery(repo=repo, repo_root=root, units=[], unit_of_file={}, groups=groups,
                     builds=list(builds), toolchain=tc, mode=mode, module_source="none",
                     peers=list(peers), skipped=dict(lay.skipped), warnings=warnings)
    user = go_modcache.user_modcache(toolchain, output_dir)
    mod_dirs = go_modcache.mod_sources(user, output_dir, mode)
    caches = [os.path.join(os.path.realpath(os.path.abspath(output_dir)), "go", "modcache")] + (
        [user] if user else [])
    primary = next((b.id for b in builds if b.primary), builds[0].id if builds else None)
    file_facts: dict[str, tuple[list[str], frozenset[str]]] = {}

    def facts_of(f: str) -> tuple[list[str], frozenset[str]]:
        if f not in file_facts:
            file_facts[f] = _file_facts(os.path.join(root, f), f, builds)
        return file_facts[f]

    deps: dict[str, set[str]] = {rel: set() for rel in mods}
    disc.units = [_unit_of(mods[r], r, vendor_of[r]) for r in sorted(mods)]   # for the peer checks
    peer_unit = {u.key: u for peer in peers for u in peer.units}
    for g in groups:
        failed, gate_warnings = _gate(gate_lines[g.id], tc)
        g.warnings += gate_warnings
        if failed and g.status == "ok":
            g.status = "toolchain"
        facts = _Facts(g)
        for rel, m in mods.items():
            if m.path in g.modules:
                unit_imports = [imp for f, o in lay.owner.items() if o == rel
                                for imp in facts_of(f)[0] if primary in facts_of(f)[1]]
                deps[rel] = _in_repo_deps(m, unit_imports, facts, mods)
        plan, peer_warnings = _peer_plan(disc, g, facts)
        g.warnings += peer_warnings
        if g.vendor or g.status != "ok":
            continue
        subst = {path for path, _d in plan}
        # (path, version, required by) for the group's modules, then substituted peers (R28).
        wanted = [(path, version, None) for path, versions in sorted(facts.requires.items())
                  for version in sorted(versions)]
        wanted += [(p, v, peer_path) for peer_path, _d in plan
                   for p, v, _ind in peer_unit[peer_path].requires]
        group_files = [f for f, o in lay.owner.items() if o in mods and mods[o].path in g.modules]
        imported: set[str] = set()
        if mode == "offline":
            imported = _imported_modules(facts, (facts_of(f)[0] for f in group_files))
        missing: set[str] = set()
        peer_missing: dict[str, set[str]] = {}
        newer: dict[str, str] = {}
        for path, version, via_peer in wanted:
            need = None if path in subst else facts.needs(path, version)
            if need is None:
                continue
            # R5: a dependency whose go.mod has a newer go line fails the whole load.
            dep_go = _dependency_go(go_modcache.read_mod(need[0], need[1], mod_dirs))
            if tc and dep_go and compare_go_versions(dep_go, tc) > 0:
                newer[f"{need[0]}@{need[1]}"] = dep_go
            if mode != "offline":
                continue
            # The module graph needs the go.mod of every listed version; MVS selects
            # the highest, whose zip is needed when the repo's compiled files import
            # one of the module's packages (peers' imports are not read: go.mod only).
            versions = facts.requires.get(path)
            zip_needed = (via_peer is None and path in imported and versions is not None
                          and version == max(versions, key=functools.cmp_to_key(semver_compare)))
            if not go_modcache.module_available(need[0], need[1], caches,
                                                mod_only=need[2] or not zip_needed):
                missing.add(f"{need[0]}@{need[1]}")
                if via_peer is not None:
                    peer_missing.setdefault(via_peer, set()).add(f"{need[0]}@{need[1]}")
        for dep, dep_go in sorted(newer.items()):
            g.warnings.append(f"dependency {dep} says \"go {dep_go}\" in its go.mod, newer than "
                              f"the toolchain {tc}: the go command will refuse to load this group, "
                              f"so its sites will be unresolved (toolchain). Fix: {_fix_for(dep_go)}")
        g.missing_modules = sorted(missing)
        if missing:
            first = ", ".join(g.missing_modules[:3])
            note = (" A workspace group needs every .mod file of its module graph: one "
                    "missing .mod leaves every external package unresolved, while in-repo "
                    "packages still load." if g.kind == "workspace" else "")
            g.warnings.append(f"{len(missing)} required modules are missing offline (first: "
                              f"{first}); calls into them and on values of their types will "
                              f"be unresolved (missing-module). Fix: add --download-modules."
                              + note)
        for peer_path, gone in sorted(peer_missing.items()):
            g.warnings.append(f"the substituted peer module {peer_path} requires {len(gone)} "
                              f"modules that are missing offline (first: "
                              f"{', '.join(sorted(gone)[:3])}), so external resolution in this "
                              f"group degrades. Fix: add --download-modules")
    disc.units = [_unit_of(mods[r], r, vendor_of[r], tuple(sorted(deps[r]))) for r in sorted(mods)]
    disc.unit_of_file = {f: mods[o].path for f, o in sorted(lay.owner.items()) if o in mods}
    nonvendor = [g for g in groups if not g.vendor]
    if groups and not nonvendor:
        disc.module_source = "vendor"
    elif mode == "download":
        disc.module_source = "proxy" if groups else "none"
    else:
        disc.module_source = "file-proxy" if user else "none"
    for g in groups:
        disc.warnings += [f"group {g.id}: {w}" for w in g.warnings]
    return disc


def stops(discovery: Discovery) -> list[str]:
    """Discovery problems that stop a run (R15): every group needs a newer Go
    than the toolchain. Everything else in discovery.warnings only warns."""
    if discovery.groups and all(g.status == "toolchain" for g in discovery.groups):
        return [f"every group needs a newer Go than the toolchain {discovery.toolchain}, so "
                f"nothing can be resolved (see the group warnings for the lines). Fix: set "
                f"GO_TOOLCHAIN to a newer go binary"]
    return []


# ---------------------------------------------------------------- peers (spec §8.2)

def discover_peer(name: str, root: str) -> Peer:
    """A peer repo discovered like a local one, from files only."""
    real = os.path.realpath(root)
    units: list[GoUnit] = []
    seen: set[str] = set()
    for rel in _walk(real, ()).module_roots:
        m, _err = _load_mod(_join(real, rel), _gomod_label(rel))
        if m is None or m.path in seen:
            continue
        seen.add(m.path)
        units.append(_unit_of(m, rel, _module_vendor(m, rel)[0]))
    return Peer(name=name, root=real, units=tuple(units))


def _peer_plan(disc: Discovery, group: Group, facts: _Facts) -> tuple[list[tuple[str, str]], list[str]]:
    providers: dict[str, list[tuple[Peer, GoUnit]]] = {}
    for peer in disc.peers:
        for u in peer.units:
            providers.setdefault(u.key, []).append((peer, u))
    wanted = sorted(p for p in facts.requires if p in providers and p not in facts.main_dirs)
    if not wanted:
        return [], []
    if group.vendor:
        names = ", ".join(sorted({peer.name for p in wanted for peer, _u in providers[p]}))
        return [], [f"peer modules ({', '.join(wanted)} from {names}) are not substituted: the "
                    f"group is vendored, so its vendored copies win"]
    own = {u.key for u in disc.units}
    plan, out = [], []
    for path in wanted:
        provs = providers[path]
        if len(provs) > 1:
            out.append(f"{path} is provided by several peers ({', '.join(p.name for p, _u in provs)}); "
                       f"it is not substituted and resolves as without peers")
            continue
        peer, unit = provs[0]
        if path in own:
            out.append(f"{path} is a module of this repo and of peer {peer.name}; it is not "
                       f"substituted")
            continue
        if path in facts.replaced:
            out.append(f"{path} is not substituted by peer {peer.name}: the repo already replaces it")
            continue
        if disc.toolchain and unit.go and compare_go_versions(unit.go, disc.toolchain) > 0:
            out.append(f"{path} is not substituted by peer {peer.name}: its go.mod says "
                       f"\"go {unit.go}\", newer than the toolchain {disc.toolchain}")
            continue
        plan.append((path, os.path.realpath(os.path.join(peer.root, unit.root))))
    return plan, out


def peer_replaces(discovery: Discovery, group: Group) -> list[tuple[str, str]]:
    """(module path, absolute peer module dir) substituted into *group* (spec §8.2)."""
    return _peer_plan(discovery, group, _Facts(group))[0]


# ---------------------------------------------------------------- control directories (spec §3.5)

def _must_quote(s: str) -> bool:
    if s == "" or "//" in s or "/*" in s:
        return True
    for c in s:
        if c in " \"'`":
            return True
        if c in "()[]{}," and len(s) > 1:
            return True
        if not c.isprintable():
            return True
    return False


def _go_quote(s: str) -> str:
    if not _must_quote(s):
        return s
    out = ['"']
    for c in s:
        if c in '"\\':
            out.append("\\" + c)
        elif c.isprintable():
            out.append(c)
        elif ord(c) < 0x10000:
            out.append(f"\\u{ord(c):04x}" if ord(c) > 0xFF else f"\\x{ord(c):02x}")
        else:
            out.append(f"\\U{ord(c):08x}")
    return "".join(out) + '"'


def _render_go_work(group: Group, facts: _Facts, plan: list[tuple[str, str]]) -> str:
    work = facts.work or {}
    lines = [f"// Generated by codedoc_go from {group.source}; do not edit."]
    go_line, _culprit = facts.work_go_line()
    if go_line:
        lines.append(f"go {go_line}")
    if work.get("Toolchain"):
        lines.append(f"toolchain {work['Toolchain']}")
    lines += [f"godebug {d['Key']}={d['Value']}" for d in work.get("Godebug") or []]
    lines += ["use ("] + [f"\t{_go_quote(u)}" for u in group.uses] + [")"]
    for r in work.get("Replace") or []:
        old, new = r["Old"], r["New"]
        lhs = _go_quote(old["Path"]) + (f" {old['Version']}" if old.get("Version") else "")
        rhs = (f"{_go_quote(new['Path'])} {new['Version']}" if new.get("Version") else
               _go_quote(os.path.realpath(os.path.join(facts.work_dir or "", new["Path"]))))
        lines.append(f"replace {lhs} => {rhs}")
    lines += [f"replace {_go_quote(p)} => {_go_quote(d)}" for p, d in plan]
    return "\n".join(lines) + "\n"


def _vendor_view_text(text: str, facts: _Facts, ctl: str) -> str:
    """modules.txt with directory replacements spelled as the go command
    canonicalizes them for a go.work in *ctl* (modload.canonicalizeReplacePath):
    go.work replaces as the absolute paths the generated go.work holds, go.mod
    replaces relative to *ctl*."""
    work_map = _replace_map(facts.work or {})
    out = []
    for line in text.split("\n"):
        f = line.split()
        if line.startswith("# ") and len(f) >= 4 and "=>" in f:
            i = f.index("=>")
            path = f[1]
            version = f[2] if i == 3 else ""
            target = f[i + 1:]
            if len(target) == 1 and is_directory_path(target[0]):
                new = None
                r = _lookup_replace(work_map, path, version)
                if r is not None and not r.get("Version"):
                    new = os.path.realpath(os.path.join(facts.work_dir or "", r["Path"]))
                elif r is None:
                    for m in facts.mains:
                        r = _lookup_replace(_replace_map(m.gomod), path, version)
                        if r is not None:
                            if not r.get("Version"):
                                if os.path.isabs(r["Path"]):
                                    new = r["Path"]
                                else:
                                    rel = os.path.relpath(os.path.normpath(
                                        os.path.join(m.dir, r["Path"])), ctl)
                                    new = rel if is_directory_path(rel) else "./" + rel
                            break
                if new is not None and new != target[0]:
                    line = "# " + " ".join(f[1:i + 1] + [new])
        out.append(line)
    return "\n".join(out)


def _place_workspace_vendor(ctl: str, group: Group, facts: _Facts) -> None:
    src = os.path.realpath(os.path.join(os.path.dirname(group.source or ""), "vendor"))
    text = _read(os.path.join(src, "modules.txt")) or ""
    view = _vendor_view_text(text, facts, ctl)
    dest = os.path.join(ctl, "vendor")
    if view == text:
        os.symlink(src, dest)
        return
    os.mkdir(dest)
    for entry in sorted(os.listdir(src)):
        if entry != "modules.txt":
            os.symlink(os.path.join(src, entry), os.path.join(dest, entry))
    with open(os.path.join(dest, "modules.txt"), "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(view)


def _copy_if_exists(src: str, dest: str) -> None:
    if os.path.isfile(src):
        shutil.copyfile(src, dest)


def prepare_control_dirs(discovery: Discovery, output_dir: str) -> None:
    """Write every group's control directory OUTPUT_DIR/repos/<repo>/go/<group>/
    (spec §3.5), replacing what an earlier run left there. Workspace: a go.work
    with absolute use and replace paths, peer replaces, the repo's go.work.sum
    and its workspace vendor directory. Module: a copy of go.mod (plus peer
    replaces unless vendored) and of go.sum, for -modfile; download mode fills
    the go.sum (go_modcache.download puts the go.mod back). Nothing is written into the repo; the group's
    dir/gowork/modfile are set to the written paths."""
    _check_repo_name(discovery.repo)
    base = _control_base(output_dir, discovery.repo)
    if os.path.lexists(base):
        shutil.rmtree(base)
    os.makedirs(base)
    for g in discovery.groups:
        ctl = os.path.join(base, g.id)
        os.makedirs(ctl)
        facts = _Facts(g)
        plan, _warnings = _peer_plan(discovery, g, facts)
        if g.kind == "workspace":
            g.dir, g.gowork = ctl, os.path.join(ctl, "go.work")
            with open(g.gowork, "w", encoding="utf-8", errors="surrogateescape") as f:
                f.write(_render_go_work(g, facts, plan))
            _copy_if_exists(os.path.join(os.path.dirname(g.source or ""), "go.work.sum"),
                            os.path.join(ctl, "go.work.sum"))
            if g.vendor:
                _place_workspace_vendor(ctl, g, facts)
        else:
            g.modfile = os.path.join(ctl, "go.mod")
            with open(g.source or "", "rb") as f:
                data = f.read()
            if plan and not g.vendor:
                if not data.endswith(b"\n"):
                    data += b"\n"
                data += b"\n// Peer substitutions added by codedoc_go (spec section 8.2).\n"
                data += "".join(f"replace {_go_quote(p)} => {_go_quote(d)}\n"
                                for p, d in plan).encode("utf-8", errors="surrogateescape")
            with open(g.modfile, "wb") as f:
                f.write(data)
            _copy_if_exists(os.path.join(os.path.dirname(g.source or ""), "go.sum"),
                            os.path.join(ctl, "go.sum"))


# ---------------------------------------------------------------- units.json (spec §3.8)

def write_units_json(discovery: Discovery, path: str) -> None:
    """Write units.json (spec §3.8 shape) atomically."""
    group_of = {key: g.id for g in discovery.groups for key in g.modules}
    doc = {
        "version": UNITS_VERSION, "repo": discovery.repo, "repo_root": discovery.repo_root,
        "toolchain": discovery.toolchain,
        "builds": [{"id": b.id, "goos": b.goos, "goarch": b.goarch, "tags": list(b.tags),
                    "cgo": b.cgo, "primary": b.primary} for b in discovery.builds],
        "mode": discovery.mode, "module_source": discovery.module_source,
        "groups": [{"id": g.id, "kind": g.kind, "dir": g.dir, "gowork": g.gowork,
                    "modfile": g.modfile, "modules": list(g.modules), "vendor": g.vendor,
                    "degraded": g.degraded, "status": g.status, "pattern": g.pattern,
                    "missing_modules": list(g.missing_modules), "warnings": list(g.warnings),
                    "source": g.source, "uses": list(g.uses),
                    "replaces": [{"old": r.old, "old_version": r.old_version, "new": r.new,
                                  "new_version": r.new_version, "local": r.local} for r in g.replaces]}
                   for g in discovery.groups],
        "units": [{"key": u.key, "root": u.root, "go": u.go, "toolchain": u.toolchain,
                   "group": group_of.get(u.key),
                   "requires": [{"path": p, "version": v, "indirect": i} for p, v, i in u.requires],
                   "replaces": [{"old": r.old, "old_version": r.old_version, "new": r.new,
                                 "new_version": r.new_version, "local": r.local} for r in u.replaces],
                   "in_repo_deps": list(u.in_repo_deps), "vendor": u.vendor}
                  for u in discovery.units],
        "peers": [{"name": p.name, "root": p.root,
                   "units": [{"key": u.key, "root": u.root} for u in p.units]}
                  for p in discovery.peers],
        "skipped": dict(sorted(discovery.skipped.items())),
        "warnings": list(discovery.warnings),
    }
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=True, indent=1)
    os.replace(tmp, path)


def group_modules(group: Group) -> tuple[dict[str, str], dict[str, set[str]]]:
    """(main module path -> absolute dir, required module path -> versions) of
    *group*, read from its go.work / go.mod files."""
    facts = _Facts(group)
    return dict(facts.main_dirs), {p: set(v) for p, v in facts.requires.items()}


# ---------------------------------------------------------------- build configurations (spec §3.4)

_GOOS_WORD = re.compile(r"[a-z0-9]+")


def _build(goos: str, goarch: str, tags: list[str], cgo: bool, primary: bool,
           source: str) -> BuildConfig:
    if not (_GOOS_WORD.fullmatch(goos) and _GOOS_WORD.fullmatch(goarch)):
        raise ValueError(f"{source}: {goos!r}/{goarch!r} is not a GOOS/GOARCH pair such as "
                         f"linux/amd64")
    for tag in tags:
        if not all(_is_tag_rune(c) for c in tag):
            raise ValueError(f"{source}: build tag {tag!r} may hold only letters, digits, "
                             f"'_' and '.'")
    return BuildConfig(id=f"{goos}-{goarch}" + "".join(f"+{t}" for t in tags), goos=goos,
                       goarch=goarch, tags=tuple(tags), cgo=cgo, primary=primary)


def builds_from_config() -> list[BuildConfig]:
    """The primary build from GO_BUILD_GOOS/GOARCH/TAGS/CGO, then GO_EXTRA_BUILDS
    ("goos/goarch[+tag+tag]" entries separated by spaces; cgo stays off for
    them). Raises ValueError naming a malformed or repeated entry."""
    tags = [t.strip() for t in str(setting("GO_BUILD_TAGS", "") or "").split(",") if t.strip()]
    builds = [_build(str(setting("GO_BUILD_GOOS", "linux") or "linux"),
                     str(setting("GO_BUILD_GOARCH", "amd64") or "amd64"), tags,
                     flag(setting("GO_BUILD_CGO", "0")), True,
                     "GO_BUILD_GOOS/GO_BUILD_GOARCH/GO_BUILD_TAGS")]
    for entry in str(setting("GO_EXTRA_BUILDS", "") or "").split():
        target, *extra = entry.split("+")
        goos, sep, goarch = target.partition("/")
        if not sep or not all(extra):
            raise ValueError(f"GO_EXTRA_BUILDS entry {entry!r} is not goos/goarch[+tag+tag]")
        builds.append(_build(goos, goarch, extra, False, False, f"GO_EXTRA_BUILDS entry {entry!r}"))
    seen: set[str] = set()
    for b in builds:
        if b.id in seen:
            raise ValueError(f"build configuration {b.id} is listed twice "
                             f"(GO_BUILD_* and GO_EXTRA_BUILDS)")
        seen.add(b.id)
    return builds
