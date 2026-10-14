#!/usr/bin/env python3
"""Build the synthetic module proxy in test_codes/_proxy (stdlib only).

Usage: python3 build_proxy.py [--check]

Module sources live in src/<module path>/<version>/ (a go.mod plus .go files).
The script writes the GOPROXY protocol files next to src/, so the directory
works as GOPROXY=file://<abs path of _proxy> with GOSUMDB=off:

  <module>/@v/list            known versions, one per line
  <module>/@v/<version>.info  {"Version": ..., "Time": ...}
  <module>/@v/<version>.mod   the go.mod of that version, unmodified
  <module>/@v/<version>.zip   module zip, every file under <module>@<version>/

<module> and <version> are case-encoded ("!" + lower-case letter for each
upper-case letter). sums.txt gets the go.sum lines of every version (the h1:
hash of the zip's files and of the go.mod, as golang.org/x/mod/sumdb/dirhash
computes them), so fixtures can commit go.sum entries without running the go
command with -mod=mod. The output is byte-for-byte reproducible: fixed zip
timestamps, stored (uncompressed) sorted entries, fixed .info times.

--check builds in memory and exits 1 when a committed file differs, is
missing, or is left over in an @v directory.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
SUMS = "sums.txt"
VERSION_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?(\+incompatible)?$")
# Commit times for the .info files; a version missing here gets DEFAULT_TIME.
TIMES = {"v1.0.0": "2026-01-15T00:00:00Z", "v1.1.0": "2026-03-02T00:00:00Z"}
DEFAULT_TIME = "2026-01-01T00:00:00Z"
ZIP_DATE = (2026, 1, 1, 0, 0, 0)
NAME_RE = re.compile(r"^[\w !#$%&()+,\-.=@\[\]^{}~]+$")


class ProxyError(Exception):
    pass


def escape(path: str) -> str:
    """Case-encode a module path or version for the proxy URL space."""
    return re.sub(r"[A-Z]", lambda m: "!" + m.group(0).lower(), path)


def hash1(files: dict[str, bytes]) -> str:
    """dirhash.Hash1: SHA-256 over "<sha256 hex>  <name>\\n" lines in name order."""
    summary = hashlib.sha256()
    for name in sorted(files):
        if "\n" in name:
            raise ProxyError(f"file name with a newline: {name!r}")
        summary.update(f"{hashlib.sha256(files[name]).hexdigest()}  {name}\n".encode("utf-8"))
    return "h1:" + base64.b64encode(summary.digest()).decode("ascii")


def module_line(gomod: bytes) -> str | None:
    for raw in gomod.decode("utf-8").splitlines():
        line = raw.split("//", 1)[0].strip()
        if line.startswith("module"):
            rest = line[len("module"):].strip()
            if rest and rest[0] in "\"`":
                rest = rest[1:-1]
            return rest
    return None


def collect_files(root: str) -> dict[str, bytes]:
    """Files of one module version as the go command would zip them."""
    out: dict[str, bytes] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        keep = []
        for d in sorted(dirnames):
            full = os.path.join(dirpath, d)
            if d == "vendor" or os.path.islink(full) or os.path.isfile(os.path.join(full, "go.mod")):
                continue
            keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            rel = name if rel_dir == "." else os.path.join(rel_dir, name).replace(os.sep, "/")
            for part in rel.split("/"):
                if not NAME_RE.match(part) or part in (".", ".."):
                    raise ProxyError(f"{root}: file name not allowed in a module zip: {rel!r}")
            with open(full, "rb") as handle:
                out[rel] = handle.read()
    folded = {}
    for rel in out:
        key = rel.casefold()
        if key in folded:
            raise ProxyError(f"{root}: {rel!r} and {folded[key]!r} collide under case folding")
        folded[key] = rel
    if "go.mod" not in out:
        raise ProxyError(f"{root}: no go.mod")
    return out


def build_zip(prefix: str, files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    # Stored, not deflated: zlib versions differ in their output bytes.
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for rel in sorted(files):
            info = zipfile.ZipInfo(prefix + rel, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o644 << 16
            info.create_system = 3
            zf.writestr(info, files[rel])
    return buf.getvalue()


def version_key(version: str) -> tuple:
    m = VERSION_RE.match(version)
    major, minor, patch = (int(m.group(i)) for i in (1, 2, 3))
    pre = m.group(4)
    return (major, minor, patch, 0 if pre else 1, pre or "")


def discover(src: str = SRC) -> dict[str, list[str]]:
    """{module path: [versions]} from src/<module>/<version>/go.mod."""
    found: dict[str, list[str]] = {}
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames.sort()
        base = os.path.basename(dirpath)
        if VERSION_RE.match(base) and "go.mod" in filenames:
            module = os.path.relpath(os.path.dirname(dirpath), src).replace(os.sep, "/")
            found.setdefault(module, []).append(base)
            dirnames[:] = []
    for versions in found.values():
        versions.sort(key=version_key)
    return found


def build(src: str = SRC) -> dict[str, bytes]:
    """Every output file (path relative to _proxy) mapped to its bytes."""
    outputs: dict[str, bytes] = {}
    sums: list[str] = []
    modules = discover(src)
    if not modules:
        raise ProxyError(f"no module versions under {src}")
    for module in sorted(modules):
        versions = modules[module]
        vdir = escape(module) + "/@v/"
        for version in versions:
            files = collect_files(os.path.join(src, module, version))
            declared = module_line(files["go.mod"])
            if declared != module:
                raise ProxyError(f"src/{module}/{version}/go.mod declares module {declared!r}")
            major = int(VERSION_RE.match(version).group(1))
            suffix = re.search(r"/v(\d+)$", module)
            if major >= 2 and (not suffix or int(suffix.group(1)) != major):
                raise ProxyError(f"{module}@{version}: major version {major} needs the /v{major} suffix")
            prefix = f"{module}@{version}/"
            outputs[vdir + escape(version) + ".mod"] = files["go.mod"]
            outputs[vdir + escape(version) + ".zip"] = build_zip(prefix, files)
            info = {"Version": version, "Time": TIMES.get(version, DEFAULT_TIME)}
            outputs[vdir + escape(version) + ".info"] = (json.dumps(info) + "\n").encode("utf-8")
            sums.append(f"{module} {version} {hash1({prefix + k: v for k, v in files.items()})}")
            sums.append(f"{module} {version}/go.mod {hash1({'go.mod': files['go.mod']})}")
        outputs[vdir + "list"] = "".join(v + "\n" for v in versions).encode("utf-8")
    outputs[SUMS] = ("\n".join(sums) + "\n").encode("utf-8")
    return outputs


def stale_files(root: str, outputs: dict[str, bytes]) -> list[str]:
    """Files inside @v directories (and sums.txt) that the build does not produce."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        if rel_dir == "src" or rel_dir.startswith("src/"):
            dirnames[:] = []
            continue
        if os.path.basename(dirpath) != "@v":
            continue
        for name in filenames:
            rel = f"{rel_dir}/{name}"
            if rel not in outputs:
                out.append(rel)
    return sorted(out)


def write(root: str, outputs: dict[str, bytes]) -> int:
    changed = 0
    for rel, data in sorted(outputs.items()):
        path = os.path.join(root, rel)
        try:
            with open(path, "rb") as handle:
                if handle.read() == data:
                    continue
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        changed += 1
    for rel in stale_files(root, outputs):
        os.remove(os.path.join(root, rel))
        changed += 1
    return changed


def check(root: str, outputs: dict[str, bytes]) -> list[str]:
    problems = []
    for rel, data in sorted(outputs.items()):
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            problems.append(f"missing: {rel}")
            continue
        with open(path, "rb") as handle:
            if handle.read() != data:
                problems.append(f"differs: {rel}")
    problems += [f"left over: {rel}" for rel in stale_files(root, outputs)]
    return problems


def main(argv: list[str]) -> int:
    if len(argv) > 2 or (len(argv) == 2 and argv[1] != "--check"):
        print("usage: build_proxy.py [--check]", file=sys.stderr)
        return 2
    try:
        outputs = build()
    except ProxyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    modules = discover()
    count = sum(len(v) for v in modules.values())
    if len(argv) == 2:
        problems = check(HERE, outputs)
        for text in problems:
            print("MISMATCH:", text)
        if problems:
            print(f"{len(problems)} problem(s); run build_proxy.py to rebuild")
            return 1
        print(f"proxy OK: {len(modules)} modules, {count} versions")
        return 0
    changed = write(HERE, outputs)
    print(f"built {len(modules)} modules, {count} versions; {changed} file(s) written or removed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
