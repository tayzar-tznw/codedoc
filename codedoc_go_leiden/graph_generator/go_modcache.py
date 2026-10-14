"""Module sources: offline from vendor/ or the user's module cache, or download mode (spec §3.7).

Offline, a group with a consistent vendor/ needs nothing. Otherwise modules come
from the user's module cache, read through GOPROXY=file://<cache>/cache/download
into the isolated cache OUTPUT_DIR/go/modcache, so the user's cache is never
written (go-modules R8). Download mode fetches exactly what the loads need with
`go list -deps -test`, 1.6-12x less than `go mod download` (R7), reading the
user's cache before GO_PROXY (R34).

Beyond C4: user_modcache() takes an optional base OUTPUT_DIR for its telemetry
settings; module_available(), mod_sources() and read_mod() serve discovery's
missing-module and dependency-go-line checks.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from urllib.parse import unquote, urlparse

from .go_model import Discovery, Toolchain
from .go_toolchain import ensure_go_dirs, env_for, setting


def escape_path(path: str) -> str:
    """module.EscapePath / EscapeVersion case encoding: "A" -> "!a"."""
    return "".join("!" + c.lower() if "A" <= c <= "Z" else c for c in path)


def module_available(path: str, version: str, caches: list[str], *, mod_only: bool = False) -> bool:
    """True when one of the module caches has path@version's .mod file and
    (unless *mod_only*) its zip or extracted tree, so an offline load can use it."""
    ep, ev = escape_path(path), escape_path(version)
    for cache in caches:
        dl = os.path.join(cache, "cache", "download", ep, "@v")
        if os.path.isfile(os.path.join(dl, ev + ".mod")) and (
                mod_only or os.path.isfile(os.path.join(dl, ev + ".zip"))
                or os.path.isdir(os.path.join(cache, f"{ep}@{ev}"))):
            return True
    return False


def _user_config_dir(env: dict[str, str]) -> str:
    """os.UserConfigDir for linux and darwin."""
    if os.uname().sysname == "Darwin":
        return os.path.join(env.get("HOME", ""), "Library", "Application Support")
    xdg = env.get("XDG_CONFIG_HOME", "")
    return xdg if os.path.isabs(xdg) else os.path.join(env.get("HOME", ""), ".config")


def user_modcache(toolchain: Toolchain | None, output_dir: str | None = None) -> str | None:
    """The user's module cache, read through a file:// proxy and never written:
    GO_USER_MODCACHE, else `go env GOMODCACHE` run from a neutral directory with
    GOTOOLCHAIN=local and only the user's HOME, GOPATH and GOMODCACHE (R14),
    plus the location of their go env file, which may set GOMODCACHE.
    Telemetry is off (XDG_CONFIG_HOME under *output_dir*, default OUTPUT_DIR).
    None when there is no cache with a cache/download tree."""
    override = setting("GO_USER_MODCACHE")
    if override:
        path = os.path.realpath(os.path.expanduser(str(override)))
    else:
        if toolchain is None:
            return None
        env = {"PATH": os.pathsep.join([os.path.join(toolchain.goroot, "bin"), "/usr/bin", "/bin"]),
               "GOTOOLCHAIN": "local",
               "GOENV": os.environ.get("GOENV") or os.path.join(_user_config_dir(os.environ), "go", "env")}
        for key in ("HOME", "GOPATH", "GOMODCACHE"):
            if os.environ.get(key):
                env[key] = os.environ[key]
        root = ensure_go_dirs(output_dir or str(setting("OUTPUT_DIR", "output_docs_pipeline") or "."))
        env["XDG_CONFIG_HOME"] = os.path.join(root, "xdg")
        try:
            proc = subprocess.run([toolchain.go, "env", "GOMODCACHE"], env=env,
                                  cwd=os.path.join(root, "tmp"), capture_output=True,
                                  text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        path = os.path.realpath(proc.stdout.strip())
    return path if os.path.isdir(os.path.join(path, "cache", "download")) else None


def mod_sources(user: str | None, output_dir: str, mode: str) -> list[str]:
    """Local directories laid out like a GOPROXY (`<escaped path>/@v/<v>.mod`)
    that a load can read: the isolated cache, the user's cache, and in download
    mode the file:// entries of GO_PROXY."""
    dirs = [os.path.join(os.path.realpath(os.path.abspath(output_dir)), "go", "modcache", "cache",
                         "download")]
    if user:
        dirs.append(os.path.join(user, "cache", "download"))
    if mode == "download":
        for entry in re.split(r"[,|]", str(setting("GO_PROXY", "") or "")):
            entry = entry.strip()
            if entry.startswith("file://"):
                dirs.append(unquote(urlparse(entry).path))
    return dirs


def read_mod(path: str, version: str, dirs: list[str]) -> str | None:
    """The go.mod text of path@version from the first directory that has it."""
    rel = os.path.join(escape_path(path), "@v", escape_path(version) + ".mod")
    for d in dirs:
        try:
            with open(os.path.join(d, rel), encoding="utf-8", errors="surrogateescape") as f:
                return f.read()
        except OSError:
            continue
    return None


def _json_stream(text: str) -> list[dict]:
    dec, i, out = json.JSONDecoder(), 0, []
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return out
        try:
            obj, i = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            return out
        out.append(obj)


_FETCH_ERR = re.compile(r"([^\s@:\"']+)@(v[^\s:\"']+): ")


def _owner(import_path: str, modules: set[str]) -> str | None:
    best = None
    for m in modules:
        if (import_path == m or import_path.startswith(m + "/")) and (best is None or len(m) > len(best)):
            best = m
    return best


def download(discovery: Discovery, toolchain: Toolchain, output_dir: str) -> dict:
    """Download mode (spec §3.7): for each loadable, non-vendored group and
    build, `go list -e -deps -test -compiled=false -buildvcs=false
    -json=ImportPath,Error,Imports` with the download environment (the user's
    cache first, then GO_PROXY: R34) fills the isolated module cache.

    A module group downloads in module mode on its control copy, with
    `-mod=mod -modfile=<control>/go.mod` (R12 as refined 2026-10-14): lazy
    loading costs a missing module only its own packages, the checksum database
    fills the control go.sum, and only the control files are written. The go
    command may also add requirements the repo's go.mod lacks. The copy is then
    put back byte for byte (the lead's ruling of 2026-10-14), keeping the filled
    go.sum, so the readonly load sees the developer's module graph and an import
    that the developer's build cannot resolve fails there too. Each requirement
    the download added or raised is reported with the packages that import it,
    as a warning of the group. A workspace group downloads through its
    generated go.work, which fills go.work.sum. Failures are reported per module
    (also as a warning) and never stop the run.

    Returns {group id: {"status": "ok"|"partial"|"failed"|"skipped", "reason",
    "packages", "package_errors", "failed_modules": {module: first error},
    "missing_requirements": {"path@version": [importing packages]}, "seconds",
    "builds"}}."""
    from .go_units import group_modules, prepare_control_dirs
    if any(not os.path.isfile(g.gowork or g.modfile or "") for g in discovery.groups):
        prepare_control_dirs(discovery, output_dir)
    timeout = int(setting("GO_HELPER_TIMEOUT", 1800) or 1800)
    user = user_modcache(toolchain, output_dir)
    report: dict[str, dict] = {}
    for g in discovery.groups:
        entry: dict = {"status": "skipped", "reason": None, "packages": 0, "package_errors": 0,
                       "failed_modules": {}, "missing_requirements": {}, "seconds": 0.0,
                       "builds": []}
        report[g.id] = entry
        if g.status != "ok" or g.vendor:
            entry["reason"] = g.status if g.status != "ok" else "vendor"
            continue
        mains, requires = group_modules(g)
        main_paths = set(mains)
        known = main_paths | set(requires)
        original = _read_bytes(g.modfile) if g.modfile else None
        importers: dict[str, set[str]] = {}        # imported path -> importing packages
        t0 = time.monotonic()
        try:
            failed = _fill(g, discovery.builds, entry, importers, main_paths, known,
                           toolchain=toolchain, output_dir=output_dir, user=user, timeout=timeout)
        finally:
            filled = _read_bytes(g.modfile) if g.modfile else None
            if original is not None and filled != original:
                _write_bytes(g.modfile, original)
        if original is not None and filled is not None:
            missing = _missing_requirements(original, filled, importers)
            if missing:
                entry["missing_requirements"] = missing
                _warn(discovery, g, _missing_warning(missing, _versions(original)))
        if entry["failed_modules"]:
            _warn(discovery, g, _failed_warning(entry["failed_modules"]))
        entry["seconds"] = round(time.monotonic() - t0, 3)
        entry["status"] = "failed" if failed else ("partial" if entry["failed_modules"] else "ok")
    return report


def _fill(g, builds, entry: dict, importers: dict[str, set[str]], main_paths: set[str],
          known: set[str], *, toolchain: Toolchain, output_dir: str, user: str | None,
          timeout: int) -> bool:
    """Run the download's `go list` for every build of group *g*; True when a
    run failed as a whole (the reason is in entry["reason"])."""
    failed = False
    seen: set[tuple[str, str]] = set()
    for build in builds:
        env = env_for(g, build, mode="download", output_dir=output_dir, user_modcache=user,
                      toolchain=toolchain)
        cmd = [toolchain.go, "list", "-e", "-deps", "-test", "-compiled=false",
               "-buildvcs=false", "-json=ImportPath,Error,Imports"]
        if build.tags:
            cmd.append("-tags=" + ",".join(build.tags))
        if g.modfile:
            cmd += ["-mod=mod", "-modfile=" + g.modfile]
        cmd.append(g.pattern)
        try:
            proc = subprocess.run(cmd, cwd=g.dir, env=env, capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            entry["reason"] = f"timeout after {timeout} s ({build.id})"
            return True
        entry["builds"].append(build.id)
        pkgs = _json_stream(proc.stdout)
        if proc.returncode != 0 and not pkgs:
            failed = True
            entry["reason"] = f"{build.id}: " + (proc.stderr.strip()[-2000:] or f"exit {proc.returncode}")
            continue
        for pkg in pkgs:
            path = pkg.get("ImportPath", "")
            base = _package_of(path)
            for imp in pkg.get("Imports") or []:
                importers.setdefault(imp, set()).add(base)
            if (path, build.id) in seen:
                continue
            seen.add((path, build.id))
            if build.primary or build is builds[0]:
                entry["packages"] += 1
            err = pkg.get("Error")
            if not err:
                continue
            msg = ((err.get("Err") if isinstance(err, dict) else str(err)) or "").strip()
            fetch = _FETCH_ERR.match(msg)               # "mod@v: reading …" names the failed module
            if fetch:
                owner = fetch.group(1)
            elif _owner(base, main_paths):
                entry["package_errors"] += 1
                continue
            else:
                owner = _owner(base, known) or base
            entry["failed_modules"].setdefault(owner, msg[:500])
    return failed


def _package_of(import_path: str) -> str:
    """The package directory's import path of a `go list -test` entry: "p",
    "p [p.test]", "p_test [p.test]" and "p.test" all give "p"."""
    base = import_path.split(" ", 1)[0]
    for suffix in (".test", "_test"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
    return base


def _missing_requirements(original: bytes, filled: bytes,
                          importers: dict[str, set[str]]) -> dict[str, list[str]]:
    """{"path@version": sorted importing packages} for every requirement the
    download added to the go.mod copy, or raised to another version."""
    added = sorted(_requirement_set(filled) - _requirement_set(original))
    by_path = {req.rsplit("@", 1)[0]: req for req in added}
    found: dict[str, set[str]] = {req: set() for req in added}
    for imported, packages in importers.items():
        owner = _owner(imported, set(by_path))
        if owner:
            found[by_path[owner]].update(packages)
    return {req: sorted(found[req]) for req in added}


def _missing_warning(missing: dict[str, list[str]], listed: dict[str, str]) -> str:
    def one(req: str) -> str:
        path, version = req.rsplit("@", 1)
        shown = f"{path} {listed[path]} -> {version}" if path in listed else req
        imps = missing[req]
        if imps:
            shown += f" (imported by {', '.join(imps[:3])}{' …' if len(imps) > 3 else ''})"
        return shown
    reqs = list(missing)
    listing = "; ".join(one(r) for r in reqs[:5]) + (f"; and {len(reqs) - 5} more" if len(reqs) > 5 else "")
    return (f"go.mod lacks requirements for: {listing}. The download added them to the go.mod "
            f"copy and put the copy back, so the load stays readonly like the developer's build: "
            f"imports from these modules are unresolved, or the go command refuses the module "
            f"(updates to go.mod needed). Fix: go mod tidy in the repo")


def _failed_warning(failed: dict[str, str]) -> str:
    mods = sorted(failed)
    shown = "; ".join(f"{m} ({failed[m][:200]})" for m in mods[:3]) + (f"; and {len(mods) - 3} more" if len(mods) > 3 else "")
    return (f"{len(mods)} module(s) could not be downloaded, so calls into them are "
            f"unresolved (missing-module): {shown}")


def _warn(discovery: Discovery, g, warning: str) -> None:
    g.warnings.append(warning)
    discovery.warnings.append(f"group {g.id}: {warning}")


def _read_bytes(path: str | None) -> bytes | None:
    try:
        with open(path or "", "rb") as f:
            return f.read()
    except OSError:
        return None


def _write_bytes(path: str, data: bytes) -> None:
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _parse_requires(data: bytes | None) -> list[dict]:
    from .go_units import ModError, parse_go_mod
    if data is None:
        return []
    try:
        gomod = parse_go_mod(data.decode("utf-8", errors="surrogateescape"))
    except ModError:
        return []
    return gomod.get("Require") or []


def _requirement_set(data: bytes | None) -> set[str]:
    """{"path@version"} required by go.mod bytes (empty when unparsable)."""
    return {f"{r['Path']}@{r['Version']}" for r in _parse_requires(data)}


def _versions(data: bytes | None) -> dict[str, str]:
    """path -> version of the requirements in go.mod bytes."""
    return {r["Path"]: r["Version"] for r in _parse_requires(data)}
