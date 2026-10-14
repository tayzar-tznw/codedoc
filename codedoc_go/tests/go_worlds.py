"""Builders for WP-D tests: repo layouts, file:// module proxies, git repos.

Everything writes under a caller-supplied directory (pytest tmp_path). The
module proxy follows the GOPROXY protocol (`<escaped path>/@v/{list,V.info,
V.mod,V.zip}`) so it serves as a user module cache's `cache/download` tree.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import zipfile

GO = os.environ.get("CODEDOC_TEST_GO") or os.environ.get("GO_TOOLCHAIN") or (
    "/home/admin/scratch/codedoc-go-research/toolchain/go/bin/go")


def write_tree(root, files: dict[str, str | bytes]) -> str:
    """Write {relpath: content} under *root*; returns str(root)."""
    root = str(root)
    for rel, content in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(path, mode) as f:
            f.write(content)
    return root


def esc(path: str) -> str:
    return "".join("!" + c.lower() if c.isupper() else c for c in path)


def _dirhash(prefix: str, files: dict[str, str]) -> str:
    """golang.org/x/mod/sumdb/dirhash Hash1 over the module zip's files."""
    lines = []
    for rel in sorted(files):
        data = files[rel].encode()
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {prefix}{rel}\n")
    summary = hashlib.sha256("".join(lines).encode()).digest()
    return "h1:" + base64.b64encode(summary).decode()


def make_proxy(download_dir, spec: dict[str, dict[str, dict[str, str]]]) -> dict[str, str]:
    """Write a GOPROXY tree for {module: {version: {relpath: content}}} under
    *download_dir* (a `cache/download` directory). Each version must have a
    "go.mod". Returns go.sum lines {"<mod> <ver>": "h1:...", "<mod> <ver>/go.mod": "h1:..."}."""
    sums: dict[str, str] = {}
    for mod, versions in spec.items():
        d = os.path.join(str(download_dir), esc(mod), "@v")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "list"), "w") as f:
            f.write("\n".join(sorted(versions)) + "\n")
        for ver, files in versions.items():
            with open(os.path.join(d, ver + ".info"), "w") as f:
                json.dump({"Version": ver, "Time": "2026-01-01T00:00:00Z"}, f)
            with open(os.path.join(d, ver + ".mod"), "w") as f:
                f.write(files["go.mod"])
            buf = io.BytesIO()
            prefix = f"{mod}@{ver}/"
            with zipfile.ZipFile(buf, "w") as z:
                for rel, content in sorted(files.items()):
                    z.writestr(prefix + rel, content)
            with open(os.path.join(d, ver + ".zip"), "wb") as f:
                f.write(buf.getvalue())
            sums[f"{mod} {ver}"] = _dirhash(prefix, files)
            sums[f"{mod} {ver}/go.mod"] = _dirhash("", {"go.mod": files["go.mod"]})
    return sums


def go_sum(sums: dict[str, str], *keys: str) -> str:
    """go.sum text for the given "<mod> <ver>" keys (zip and go.mod lines)."""
    out = []
    for key in keys:
        out.append(f"{key} {sums[key]}")
        out.append(f"{key}/go.mod {sums[key + '/go.mod']}")
    return "\n".join(out) + "\n"


def git(repo, *args: str) -> str:
    """Run git in *repo* without reading or writing any git config."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(repo), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
           "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "init.defaultBranch=main", *args],
        cwd=str(repo), env=env, capture_output=True, text=True, check=True)
    return proc.stdout


def snapshot(root) -> dict[str, tuple[str, int]]:
    """{relpath: (sha256, mtime_ns)} for every file under *root*, .git included."""
    out = {}
    for dirpath, _dirs, files in os.walk(str(root)):
        for name in files:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                out[os.path.relpath(path, str(root))] = ("link:" + os.readlink(path), 0)
                continue
            with open(path, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            out[os.path.relpath(path, str(root))] = (digest, os.stat(path).st_mtime_ns)
    return out


def have_go() -> bool:
    return os.path.isfile(GO) and shutil.which("git") is not None
