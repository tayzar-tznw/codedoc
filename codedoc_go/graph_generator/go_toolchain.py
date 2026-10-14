"""The Go toolchain: discovery, version order, the go environment, the helper (spec §3.3, §3.6).

Every go command and every helper run gets an environment built from scratch:
nothing is inherited except HOME and USER, so no user GOFLAGS, GOPROXY,
GOPRIVATE, GOTOOLCHAIN or go env file can run programs, download a toolchain or
write into the repo (go-modules §7.4).

Beyond C4: env_for() takes an optional toolchain (default: find_toolchain());
helper_source_hash() is the helper part of the entity reuse stamp;
compare_go_versions() orders Go versions like the go command; probe_toolchain(),
minimum_go(), base_env() and ensure_go_dirs() serve doctor and go_modcache.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from datetime import date

from . import config
from .go_model import BuildConfig, Group, Toolchain

PINNED_VERSION = "go1.27.2"
PINNED_URL = "https://go.dev/dl/go1.27.2.linux-amd64.tar.gz"
PINNED_SHA256 = "ecbadb99091a3f46e31f5f934b068b1864eafa7995211b39eaddf76996045fe5"
MIN_GO = "1.25"                                # the `work` package pattern (spec §3.3)

CODEDOC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(CODEDOC_ROOT, ".tools")
HELPER_DIR = os.path.join(CODEDOC_ROOT, "gohelper")
DEFAULT_GO_PROXY = "https://proxy.golang.org"


class ToolchainError(RuntimeError):
    """No usable toolchain; the message names the fix."""


class HelperBuildError(RuntimeError):
    """`go build` of gohelper failed; the message carries the compiler output."""


def setting(name: str, default=None):
    """A C8 setting from config, or from the environment while config lacks it."""
    if hasattr(config, name):
        return getattr(config, name)
    return os.environ.get(name, default)


def flag(value) -> bool:
    return value if isinstance(value, bool) else str(value or "").strip() == "1"


# ---------------------------------------------------------------- version order (internal/gover)

def _cut_int(x: str) -> tuple[str, str] | None:
    i = 0
    while i < len(x) and x[i].isdigit() and x[i].isascii():
        i += 1
    if i == 0 or (x[0] == "0" and i != 1):
        return None
    return x[:i], x[i:]


def _gover_parse(x: str) -> tuple[str, str, str, str, str] | None:
    """(major, minor, patch, kind, pre) of a Go version like 1.21, 1.21.3 or 1.21rc1."""
    cut = _cut_int(x)
    if cut is None:
        return None
    major, x = cut
    if x == "":
        return major, "0", "0", "", ""
    if x[0] != ".":
        return None
    cut = _cut_int(x[1:])
    if cut is None:
        return None
    minor, x = cut
    if x == "":
        # Before 1.21 a missing patch means .0; from 1.21 on "1.21" < "1.21.0".
        return major, minor, ("0" if _cmp_int(minor, "21") < 0 else ""), "", ""
    if x[0] == ".":
        cut = _cut_int(x[1:])
        if cut is None or cut[1] != "":
            return None
        return major, minor, cut[0], "", ""
    i = 0
    while i < len(x) and not x[i].isdigit():
        if not ("a" <= x[i] <= "z"):
            return None
        i += 1
    if i == 0:
        return None
    kind, x = x[:i], x[i:]
    if x == "":
        return major, minor, "", kind, ""
    cut = _cut_int(x)
    if cut is None or cut[1] != "":
        return None
    return major, minor, "", kind, cut[0]


def _cmp_int(a: str, b: str) -> int:
    if a == b:
        return 0
    if len(a) != len(b):
        return -1 if len(a) < len(b) else 1
    return -1 if a < b else 1


def _strip_go(v: str) -> str:
    return v[2:] if v.startswith("go") else v


def compare_go_versions(a: str, b: str) -> int:
    """gover.Compare on Go versions, with or without the "go" prefix:
    1.21 < 1.21rc1 < 1.21rc2 < 1.21.0 < 1.21.1. Malformed versions sort first."""
    va, vb = _gover_parse(_strip_go(a)), _gover_parse(_strip_go(b))
    if va is None or vb is None:
        return 0 if va is vb else (-1 if va is None else 1)
    for x, y in zip(va[:3], vb[:3]):
        c = _cmp_int(x, y)
        if c:
            return c
    if va[3] != vb[3]:                         # "" < alpha < beta < rc
        return -1 if va[3] < vb[3] else 1
    return _cmp_int(va[4], vb[4])


def from_toolchain_name(name: str) -> str:
    """gover.FromToolchain: "go1.2.3-bigcorp" -> "1.2.3"; "" for "default" or junk."""
    if "/" in name or "\\" in name or not name.startswith("go"):
        return ""
    v = re.split(r"[ \t-]", name[2:], maxsplit=1)[0]
    return v if _gover_parse(v) is not None else ""


def version_tuple(v: str) -> tuple[int, int, int]:
    """(major, minor, patch) of "go1.26.8" / "1.26"; a missing patch or a
    prerelease reads as 0. Use compare_go_versions for exact ordering."""
    p = _gover_parse(from_toolchain_name(v) if v.startswith("go") else v)
    if p is None:
        raise ValueError(f"not a Go version: {v!r}")
    return int(p[0]), int(p[1]), int(p[2] or 0)


# ---------------------------------------------------------------- finding the toolchain

_probe_cache: dict[str, Toolchain] = {}


def go_dir(output_dir: str) -> str:
    """OUTPUT_DIR/go, the state shared by every repo (spec §2.3)."""
    return os.path.join(os.path.realpath(os.path.abspath(output_dir)), "go")


def _default_output_dir() -> str:
    return str(setting("OUTPUT_DIR", "output_docs_pipeline") or "output_docs_pipeline")


def ensure_go_dirs(output_dir: str) -> str:
    """Create OUTPUT_DIR/go/{modcache,gocache,gopath,tmp,xdg,bin} and switch
    telemetry off in the xdg dir before any go command runs there. Returns OUTPUT_DIR/go."""
    root = go_dir(output_dir)
    for sub in ("modcache", "gocache", "gopath", "tmp", "bin",
                os.path.join("xdg", "go", "telemetry")):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    mode = os.path.join(root, "xdg", "go", "telemetry", "mode")
    if not os.path.isfile(mode):
        with open(mode, "w") as f:
            f.write(f"off {date.today().isoformat()}")
    return root


def _read_version_file(goroot: str) -> str | None:
    try:
        with open(os.path.join(goroot, "VERSION"), encoding="utf-8") as f:
            first = f.readline().strip()
    except OSError:
        return None
    return first if from_toolchain_name(first) else None


def probe_toolchain(go: str) -> Toolchain:
    """Toolchain of the go binary *go*: GOROOT and version from $GOROOT/VERSION,
    else from `go env GOROOT GOVERSION` run with a clean environment."""
    real = os.path.realpath(os.path.expanduser(go))
    if real in _probe_cache:
        return _probe_cache[real]
    if not (os.path.isfile(real) and os.access(real, os.X_OK)):
        raise ToolchainError(f"{go} is not an executable go binary")
    goroot = os.path.dirname(os.path.dirname(real))
    version = _read_version_file(goroot)
    if version is None:
        out_root = ensure_go_dirs(_default_output_dir())
        env = {"PATH": os.pathsep.join([os.path.dirname(real), "/usr/bin", "/bin"]),
               "GOTOOLCHAIN": "local", "GOENV": "off",
               "XDG_CONFIG_HOME": os.path.join(out_root, "xdg")}
        for key in ("HOME", "USER"):
            if key in os.environ:
                env[key] = os.environ[key]
        try:
            proc = subprocess.run([real, "env", "GOROOT", "GOVERSION"], env=env,
                                  cwd=os.path.join(out_root, "tmp"), capture_output=True,
                                  text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToolchainError(f"cannot run {go}: {exc}") from None
        lines = proc.stdout.split("\n")
        if proc.returncode != 0 or len(lines) < 2 or not from_toolchain_name(lines[1].strip()):
            raise ToolchainError(f"cannot tell the Go version of {go}: "
                                 f"{(proc.stderr or proc.stdout).strip()[:300]}")
        goroot, version = os.path.realpath(lines[0].strip()), lines[1].strip()
    bin_go = os.path.join(goroot, "bin", "go")
    tc = Toolchain(go=bin_go if os.path.isfile(bin_go) else real, version=version, goroot=goroot)
    _probe_cache[real] = tc
    return tc


def minimum_go() -> str:
    """The higher of the helper's go.mod go line and 1.25 (spec §3.3)."""
    want = MIN_GO
    try:
        with open(os.path.join(HELPER_DIR, "go.mod"), encoding="utf-8") as f:
            m = re.search(r"(?m)^go[ \t]+(\S+)", f.read())
        if m and compare_go_versions(m.group(1), want) > 0:
            want = m.group(1)
    except OSError:
        pass
    return want


_SETUP = (f"run `python -m graph_generator setup go` (installs {PINNED_VERSION} into "
          f"codedoc_go/.tools/go)")


def find_toolchain() -> Toolchain:
    """GO_TOOLCHAIN if set, else codedoc_go/.tools/go/bin/go when installed,
    else `go` on PATH (spec §2.5). Raises ToolchainError naming the fix."""
    configured = setting("GO_TOOLCHAIN")
    if configured:
        path = os.path.expanduser(str(configured))
        if os.path.isdir(path):
            path = os.path.join(path, "bin", "go")
        if not os.path.isfile(path):
            raise ToolchainError(f"GO_TOOLCHAIN={configured} is not a go binary: point it at "
                                 f"<GOROOT>/bin/go, or unset it and {_SETUP}")
        candidate = path
    elif os.path.isfile(os.path.join(TOOLS_DIR, "go", "bin", "go")):
        candidate = os.path.join(TOOLS_DIR, "go", "bin", "go")
    else:
        found = shutil.which("go")
        if not found:
            raise ToolchainError(f"no Go toolchain found: {_SETUP}, or set GO_TOOLCHAIN to a go "
                                 f"binary")
        candidate = found
    tc = probe_toolchain(candidate)
    need = minimum_go()
    if compare_go_versions(from_toolchain_name(tc.version), need) < 0:
        raise ToolchainError(f"{tc.go} is {tc.version}, older than go{need} (the minimum for "
                             f"the helper and the `work` pattern): {_SETUP}, or set GO_TOOLCHAIN "
                             f"to a go >= {need} binary")
    return tc


# ---------------------------------------------------------------- the environment (spec §3.6)

def download_proxy() -> str:
    """GO_PROXY for download mode, with `direct` and `off` entries dropped (no VCS fetches)."""
    raw = str(setting("GO_PROXY", DEFAULT_GO_PROXY) or DEFAULT_GO_PROXY)
    parts = [p.strip() for p in re.split(r"[,|]", raw) if p.strip() not in ("", "direct", "off")]
    return ",".join(parts) or "off"


def file_proxy_url(user_modcache: str) -> str:
    """GOPROXY URL that reads a module cache's cache/download tree (go-modules R8)."""
    from pathlib import Path
    return Path(os.path.join(user_modcache, "cache", "download")).as_uri()


def base_env(toolchain: Toolchain, output_dir: str) -> dict[str, str]:
    """The parts of the §3.6 table that do not depend on the group or build."""
    root = ensure_go_dirs(output_dir)
    env: dict[str, str] = {}
    for key in ("HOME", "USER"):
        if key in os.environ:
            env[key] = os.environ[key]
    env.update({
        "PATH": os.pathsep.join([os.path.join(toolchain.goroot, "bin"), "/usr/bin", "/bin"]),
        "GOROOT": toolchain.goroot,
        "GOTOOLCHAIN": "local",
        "GOENV": "off",
        "GOVCS": "*:off",
        "GOAUTH": "off",
        "GOMODCACHE": os.path.join(root, "modcache"),
        "GOCACHE": os.path.join(root, "gocache"),
        "GOPATH": os.path.join(root, "gopath"),
        "TMPDIR": os.path.join(root, "tmp"),
        "GOTMPDIR": os.path.join(root, "tmp"),
        "XDG_CONFIG_HOME": os.path.join(root, "xdg"),
    })
    return env


def env_for(group: Group, build: BuildConfig, *, mode: str, output_dir: str,
            user_modcache: str | None, toolchain: Toolchain | None = None) -> dict[str, str]:
    """The environment of every go command and helper run for *group* under
    *build* (spec §3.6), built from scratch. Offline, modules come from the
    user's cache through a file:// proxy (or nowhere) and GOSUMDB is off.
    Download mode reads the user's cache first, then GO_PROXY (R34), and
    checks sums against GO_SUMDB, read at call time (default sum.golang.org;
    evaluate --env sets off). Build tags and -modfile are flags of the go
    command line (R13), never GOFLAGS; GOFLAGS always names -mod."""
    if mode not in ("offline", "download"):
        raise ValueError(f"mode must be 'offline' or 'download', not {mode!r}")
    env = base_env(toolchain or find_toolchain(), output_dir)
    env["GOFLAGS"] = " ".join(["-mod=vendor" if group.vendor else "-mod=readonly",
                               "-modcacherw", "-buildvcs=false"])
    env["GOWORK"] = group.gowork or "off"
    if mode == "download":
        proxy = download_proxy()
        if user_modcache and proxy != "off":
            proxy = f"{file_proxy_url(user_modcache)},{proxy}"
        env["GOPROXY"] = proxy
        env["GOSUMDB"] = str(setting("GO_SUMDB", "sum.golang.org") or "sum.golang.org")
    else:
        env["GOPROXY"] = file_proxy_url(user_modcache) if user_modcache else "off"
        env["GOSUMDB"] = "off"
    env["CGO_ENABLED"] = "1" if build.cgo else "0"
    env["GOOS"] = build.goos
    env["GOARCH"] = build.goarch
    return env


# ---------------------------------------------------------------- the helper binary

def _helper_files(src: str) -> list[str]:
    """Repo-relative files that go into the helper binary: every .go file
    except tests and testdata, plus go.mod, go.sum and vendor/modules.txt."""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = sorted(d for d in dirnames
                             if d != "testdata" and not d.startswith((".", "_")))
        rel_dir = os.path.relpath(dirpath, src)
        for name in filenames:
            rel = os.path.normpath(os.path.join(rel_dir, name)).replace(os.sep, "/")
            if (name.endswith(".go") and not name.endswith("_test.go")
                    and not name.startswith((".", "_"))) or rel in (
                        "go.mod", "go.sum", "vendor/modules.txt"):
                out.append(rel)
    return sorted(out)


def helper_source_hash(src: str | None = None) -> str:
    """SHA-256 over the helper's source files (paths and contents)."""
    src = src or HELPER_DIR
    h = hashlib.sha256()
    for rel in _helper_files(src):
        with open(os.path.join(src, rel), "rb") as f:
            h.update(rel.encode() + b"\0" + hashlib.sha256(f.read()).digest())
    return h.hexdigest()


def ensure_helper(toolchain: Toolchain, output_dir: str) -> str:
    """Absolute path of the gohelper binary for *toolchain* and the current
    source, built with `go build -mod=vendor -trimpath` when not cached under
    OUTPUT_DIR/go/bin/gohelper-<go version>-<source hash>."""
    src = HELPER_DIR
    if not os.path.isfile(os.path.join(src, "go.mod")):
        raise HelperBuildError(f"{src}/go.mod is missing; the helper source is incomplete")
    need = minimum_go()
    if compare_go_versions(from_toolchain_name(toolchain.version), need) < 0:
        raise ToolchainError(f"{toolchain.go} is {toolchain.version}, older than go{need}, which "
                             f"gohelper/go.mod needs: {_SETUP}, or set GO_TOOLCHAIN to a go >= "
                             f"{need} binary")
    digest = helper_source_hash(src)[:16]
    bin_dir = os.path.join(go_dir(output_dir), "bin")
    path = os.path.join(bin_dir, f"gohelper-{toolchain.version}-{digest}")
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    env = base_env(toolchain, output_dir)
    env.update({"GOWORK": "off", "GOFLAGS": "", "GOPROXY": "off", "GOSUMDB": "off",
                "CGO_ENABLED": "0"})
    tmp = f"{path}.tmp-{os.getpid()}"
    cmd = [toolchain.go, "build", "-mod=vendor", "-trimpath", "-buildvcs=false", "-o", tmp, "."]
    try:
        proc = subprocess.run(cmd, cwd=src, env=env, capture_output=True, text=True,
                              timeout=int(setting("GO_HELPER_TIMEOUT", 1800) or 1800))
    except subprocess.TimeoutExpired:
        raise HelperBuildError(f"building the helper timed out: {' '.join(cmd)}") from None
    if proc.returncode != 0:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise HelperBuildError(f"building the helper failed (cd {src} && {' '.join(cmd)}):\n"
                               + (proc.stderr or proc.stdout).strip()[-4000:])
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------- setup go

def _fetch(url: str, path: str) -> None:
    with urllib.request.urlopen(url, timeout=300) as resp, open(path, "wb") as f:
        shutil.copyfileobj(resp, f, 1 << 20)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def setup_go(dest: str | None = None) -> Toolchain:
    """Install the pinned toolchain (go1.27.2 linux-amd64, SHA-256 pinned) as
    GOROOT *dest* (default codedoc_go/.tools/go). The archive is verified
    before anything is extracted; an existing install of the same version is
    kept. Returns the installed toolchain."""
    dest = os.path.abspath(dest or os.path.join(TOOLS_DIR, "go"))
    go_bin = os.path.join(dest, "bin", "go")
    if _read_version_file(dest) == PINNED_VERSION and os.access(go_bin, os.X_OK):
        return probe_toolchain(go_bin)
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)
    work = tempfile.mkdtemp(prefix=".setup-go-", dir=parent)
    try:
        archive = os.path.join(work, "go.tar.gz")
        try:
            _fetch(PINNED_URL, archive)
        except (OSError, ValueError) as exc:
            raise ToolchainError(f"downloading {PINNED_URL} failed: {exc}; download it yourself, "
                                 f"unpack it and set GO_TOOLCHAIN to its bin/go") from None
        digest = _sha256_file(archive)
        if digest != PINNED_SHA256:
            raise ToolchainError(f"checksum mismatch for {PINNED_URL}: got {digest}, want "
                                 f"{PINNED_SHA256}; nothing was installed")
        extract = os.path.join(work, "x")
        os.mkdir(extract)
        try:
            with tarfile.open(archive, "r:gz") as tf:
                tf.extractall(extract, filter="data")
        except (tarfile.TarError, OSError) as exc:
            raise ToolchainError(f"unpacking {PINNED_URL} failed: {exc}; nothing was "
                                 f"installed") from None
        new_root = os.path.join(extract, "go")
        if (_read_version_file(new_root) != PINNED_VERSION
                or not os.path.isfile(os.path.join(new_root, "bin", "go"))):
            raise ToolchainError(f"{PINNED_URL} does not contain go/bin/go of {PINNED_VERSION}")
        if os.path.lexists(dest):
            os.rename(dest, os.path.join(work, "previous"))
        os.rename(new_root, dest)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    _probe_cache.pop(os.path.realpath(go_bin), None)
    return probe_toolchain(go_bin)
