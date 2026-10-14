"""Pure-Python go.mod / go.work parser modelled on golang.org/x/mod/modfile (Go 1.27.2 vendored copy).

parse_mod(text, strict=True) -> dict shaped like `go mod edit -json` (minus comment-derived
Module.Deprecated and Retract.Rationale). parse_work(text) -> dict shaped like `go work edit -json`.
Raises ModError on the same classes of error the Go parser reports.
Reads text only; never touches the filesystem beyond what the caller passes in.
"""
import re

GO_VERSION_RE = re.compile(r"^([1-9][0-9]*)\.(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*))?([a-z]+[0-9]+)?$")
TOOLCHAIN_RE = re.compile(r"^default$|^go1($|\.)")
PUNCT = set("\n()[]{},")


class ModError(Exception):
    pass


def _is_ident(c):
    if c in " ()[]{},":
        return False
    return not c.isspace() and c.isprintable()


def lex(text):
    """Yield (kind, value, is_line_comment_only). kinds: NL, '(', ')', '[', ']', '{', '}', ',', STR, ID, COMMENT, EOLCOMMENT."""
    toks = []
    i, n = 0, len(text)
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
                body = text[i:j].rstrip("\n")
                # comment consumes its newline, like read.go
                if suffix:
                    toks.append(("EOLCOMMENT", body))
                    toks.append(("NL", "\n"))  # EOL comment ends the line
                else:
                    toks.append(("COMMENT", body))
                    toks.append(("NL", "\n"))
                i = j
                continue
            if text.startswith("/*", i):
                raise ModError("mod files must use // comments (not /* */ comments)")
            break
        if i >= n:
            break
        c = text[i]
        if c in PUNCT:
            toks.append(("NL" if c == "\n" else c, c))
            i += 1
            continue
        if c in "\"`":
            q = c
            j = i + 1
            while True:
                if j >= n:
                    raise ModError("unexpected EOF in string")
                if text[j] == "\n":
                    raise ModError("unexpected newline in string")
                ch = text[j]
                j += 1
                if ch == q:
                    break
                if ch == "\\" and q != "`":
                    if j >= n:
                        raise ModError("unexpected EOF in string")
                    j += 1
            toks.append(("STR", text[i:j]))
            i = j
            continue
        if not _is_ident(c):
            raise ModError("unexpected input character %r" % c)
        j = i
        while j < n and _is_ident(text[j]):
            if text.startswith("//", j):
                break
            if text.startswith("/*", j):
                raise ModError("mod files must use // comments (not /* */ comments)")
            j += 1
        toks.append(("ID", text[i:j]))
        i = j
    toks.append(("EOF", ""))
    return toks


def parse_stmts(text):
    """Return list of ('line', tokens, comments) / ('block', verbtokens, [lines]) like read.go."""
    if text.startswith("﻿"):
        raise ModError("unexpected input character '\\ufeff'")
    toks = lex(text)
    pos = 0
    stmts = []

    def peek():
        return toks[pos]

    while True:
        k, v = toks[pos]
        if k == "EOF":
            break
        if k in ("NL", "COMMENT"):
            pos += 1
            continue
        # statement
        line = []
        eol_comment = None
        while True:
            k, v = toks[pos]
            if k in ("NL", "EOF", "EOLCOMMENT"):
                if k == "EOLCOMMENT":
                    eol_comment = v
                    pos += 1  # the NL after it is consumed below
                    k, v = toks[pos]
                if k == "NL":
                    pos += 1
                stmts.append(("line", line, eol_comment))
                break
            if k == "(":
                nk = toks[pos + 1][0]
                if nk in ("NL", "EOLCOMMENT", "EOF"):
                    pos += 1
                    # block
                    blines = []
                    while True:
                        k2, v2 = toks[pos]
                        if k2 in ("NL", "COMMENT"):
                            pos += 1
                            continue
                        if k2 == "EOLCOMMENT":
                            pos += 1
                            continue
                        if k2 == "EOF":
                            raise ModError("syntax error (unterminated block)")
                        if k2 == ")":
                            pos += 1
                            k3 = toks[pos][0]
                            if k3 not in ("NL", "EOF", "EOLCOMMENT"):
                                raise ModError("syntax error (expected newline after closing paren)")
                            if k3 == "EOLCOMMENT":
                                pos += 1
                            if toks[pos][0] == "NL":
                                pos += 1
                            break
                        bl = []
                        bcomment = None
                        while True:
                            k4, v4 = toks[pos]
                            if k4 in ("NL", "EOF", "EOLCOMMENT"):
                                if k4 == "EOLCOMMENT":
                                    bcomment = v4
                                    pos += 1
                                if toks[pos][0] == "NL":
                                    pos += 1
                                break
                            bl.append((k4, v4))
                            pos += 1
                        blines.append((bl, bcomment))
                    stmts.append(("block", line, blines))
                    break
                elif nk == ")" and toks[pos + 2][0] in ("NL", "EOF", "EOLCOMMENT"):
                    pos += 2
                    if toks[pos][0] == "EOLCOMMENT":
                        pos += 1
                    if toks[pos][0] == "NL":
                        pos += 1
                    stmts.append(("block", line, []))
                    break
                else:
                    line.append((k, v))
                    pos += 1
                    continue
            line.append((k, v))
            pos += 1
    return stmts


_ESC = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "\"": "\"", "'": "'"}


def _go_unquote(s):
    if not (len(s) >= 2 and s[0] == '"' and s[-1] == '"'):
        raise ModError("invalid quoted string")
    out, i, body = [], 0, s[1:-1]
    while i < len(body):
        c = body[i]
        if c != "\\":
            out.append(c); i += 1; continue
        i += 1
        e = body[i] if i < len(body) else ""
        if e in _ESC:
            out.append(_ESC[e]); i += 1
        elif e == "x":
            out.append(chr(int(body[i + 1:i + 3], 16))); i += 3
        elif e == "u":
            out.append(chr(int(body[i + 1:i + 5], 16))); i += 5
        elif e == "U":
            out.append(chr(int(body[i + 1:i + 9], 16))); i += 9
        elif e in "01234567":
            out.append(chr(int(body[i:i + 3], 8))); i += 3
        else:
            raise ModError("invalid quoted string")
    return "".join(out)


def _str(tok):
    k, v = tok
    if v.startswith('"'):
        return _go_unquote(v)
    if any(q in v for q in "\"'`"):
        raise ModError("invalid quoted string: unquoted string cannot contain quote")
    return v


def is_directory_path(ns):
    return (ns == "." or ns.startswith("./") or ns.startswith(".\\") or ns == ".." or ns.startswith("../")
            or ns.startswith("..\\") or ns.startswith("/") or ns.startswith("\\")
            or (len(ns) >= 2 and ns[0].isascii() and ns[0].isalpha() and ns[1] == ":"))


SEMVER_RE = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
                       r"(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$")


def _version(v):
    if not SEMVER_RE.match(v):
        raise ModError(f'version "{v}" invalid: must be of the form v1.2.3')
    return v


def _indirect(comment):
    if not comment:
        return False
    t = comment[2:].strip()
    return t == "indirect" or t.startswith("indirect;")


MAIN_ONLY_OK_LAX = {"go", "module", "retract", "require", "ignore"}
MOD_BLOCK_VERBS = {"module", "godebug", "require", "exclude", "replace", "retract", "tool", "ignore"}


def _replace(args):
    vals = [v for _, v in args]
    arrow = 1 if len(vals) >= 2 and vals[1] == "=>" else 2
    if len(vals) < arrow + 2 or len(vals) > arrow + 3 or vals[arrow] != "=>":
        raise ModError("usage: replace module/path [v1.2.3] => other/module v1.4")
    old = {"Path": _str(args[0])}
    if arrow == 2:
        old["Version"] = _version(_str(args[1]))
    ns = _str(args[arrow + 1])
    new = {"Path": ns}
    if len(vals) == arrow + 2:
        if not is_directory_path(ns):
            if "@" in ns:
                raise ModError("replacement module must match format 'path version', not 'path@version'")
            raise ModError("replacement module without version must be directory path (rooted or starting with . or ..)")
        if "\\" in ns:
            raise ModError("replacement directory appears to be Windows path (on a non-windows system)")
    else:
        if is_directory_path(ns):
            raise ModError(f'replacement module directory path "{ns}" cannot have version')
        new["Version"] = _version(_str(args[arrow + 2]))
    return {"Old": old, "New": new}


def parse_mod(text, strict=True):
    out = {"Module": {"Path": ""}}
    seen_module = seen_go = False
    for st in parse_stmts(text):
        if st[0] == "line":
            items = [(st[1][0][1], st[1][1:], st[2])] if st[1] else []
        else:
            verbtoks = st[1]
            if len(verbtoks) > 1:
                if strict:
                    raise ModError("unknown block type: " + " ".join(v for _, v in verbtoks))
                continue
            verb = verbtoks[0][1]
            if verb not in MOD_BLOCK_VERBS:
                if strict:
                    raise ModError("unknown block type: " + verb)
                continue
            items = [(verb, bl, c) for bl, c in st[2]]
        for verb, args, comment in items:
            if not strict and verb not in MAIN_ONLY_OK_LAX:
                continue
            if verb == "module":
                if seen_module:
                    raise ModError("repeated module statement")
                if len(args) != 1:
                    raise ModError("usage: module module/path")
                seen_module = True
                out["Module"]["Path"] = _str(args[0])
            elif verb == "go":
                if seen_go:
                    raise ModError("repeated go statement")
                if len(args) != 1:
                    raise ModError("go directive expects exactly one argument")
                v = _str(args[0])
                if not GO_VERSION_RE.match(v):
                    raise ModError(f"invalid go version '{v}': must match format 1.23.0")
                seen_go = True
                out["Go"] = v
            elif verb == "toolchain":
                if "Toolchain" in out:
                    raise ModError("repeated toolchain statement")
                if len(args) != 1:
                    raise ModError("toolchain directive expects exactly one argument")
                v = _str(args[0])
                if not TOOLCHAIN_RE.match(v):
                    raise ModError(f"invalid toolchain version '{v}': must match format go1.23.0 or default")
                out["Toolchain"] = v
            elif verb == "godebug":
                if len(args) != 1 or "=" not in args[0][1]:
                    raise ModError("usage: godebug key=value")
                k, _, v = args[0][1].partition("=")
                out.setdefault("GoDebug", []).append({"Key": k, "Value": v})
            elif verb in ("require", "exclude"):
                if len(args) != 2:
                    raise ModError(f"usage: {verb} module/path v1.2.3")
                p = _str(args[0]); v = _version(_str(args[1]))
                if verb == "require":
                    r = {"Path": p, "Version": v}
                    if _indirect(comment):
                        r["Indirect"] = True
                    out.setdefault("Require", []).append(r)
                else:
                    out.setdefault("Exclude", []).append({"Path": p, "Version": v})
            elif verb == "replace":
                out.setdefault("Replace", []).append(_replace(args))
            elif verb == "retract":
                vals = [v for _, v in args]
                if len(vals) == 1:
                    out.setdefault("Retract", []).append({"Low": _version(_str(args[0])), "High": _version(_str(args[0]))})
                elif len(vals) == 5 and vals[0] == "[" and vals[2] == "," and vals[4] == "]":
                    out.setdefault("Retract", []).append({"Low": _version(vals[1]), "High": _version(vals[3])})
                else:
                    raise ModError("usage: retract version | retract [low, high]")
            elif verb == "tool":
                if len(args) != 1:
                    raise ModError("tool directive expects exactly one argument")
                out.setdefault("Tool", []).append({"Path": _str(args[0])})
            elif verb == "ignore":
                if len(args) != 1:
                    raise ModError("ignore directive expects exactly one argument")
                out.setdefault("Ignore", []).append({"Path": _str(args[0])})
            else:
                raise ModError("unknown directive: " + verb)
    return out


def parse_work(text):
    out = {}
    for st in parse_stmts(text):
        if st[0] == "line":
            items = [(st[1][0][1], st[1][1:], st[2])] if st[1] else []
        else:
            if len(st[1]) > 1 or st[1][0][1] not in ("godebug", "use", "replace"):
                raise ModError("unknown block type: " + " ".join(v for _, v in st[1]))
            items = [(st[1][0][1], bl, c) for bl, c in st[2]]
        for verb, args, comment in items:
            if verb == "go":
                v = _str(args[0])
                if not GO_VERSION_RE.match(v):
                    raise ModError(f"invalid go version '{v}': must match format 1.23.0")
                out["Go"] = v
            elif verb == "toolchain":
                out["Toolchain"] = _str(args[0])
            elif verb == "godebug":
                k, _, v = args[0][1].partition("=")
                out.setdefault("Godebug", []).append({"Key": k, "Value": v})
            elif verb == "use":
                if len(args) != 1:
                    raise ModError("usage: use local/dir")
                out.setdefault("Use", []).append({"DiskPath": _str(args[0])})
            elif verb == "replace":
                out.setdefault("Replace", []).append(_replace(args))
            else:
                raise ModError("unknown directive: " + verb)
    return out
