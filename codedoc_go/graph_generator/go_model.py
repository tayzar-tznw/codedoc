"""Shared types and vocabularies for codedoc_go (master plan C4).

go_units, go_toolchain and go_modcache take their data shapes from here, and
the pipeline, resolution, surfaces and evaluate read them. The on-disk shape of
units.json is in the spec (§3.8); the vocabularies are those of spec §5.7
(statuses, provenance, reasons) and §6.1-6.2 (site, file, class and member kinds).

Beyond C4: Group.source, Group.uses and Group.replaces, Discovery.unit() and
group_of(), and Toolchain, which find_toolchain() returns.
"""
from __future__ import annotations

from dataclasses import dataclass, field

UNITS_VERSION = 1

STATUSES = ("resolved", "external", "builtin", "ambiguous", "dynamic", "unresolved")
VIA = ("types", "convention:funclit", "convention:func-alias", "convention:linkname",
       "convention:range", "none")
POSSIBLY_REASONS = ("implementation", "variant")
UNRESOLVED_REASONS = ("missing-module", "incomplete-type", "excluded-by-build", "toolchain",
                      "no-declaration", "stale", "syntax-error", "unscanned", "outside-repo",
                      "no-module", "load-error")
SITE_KINDS = ("call", "go", "defer", "range")
FILE_ORIGINS = ("package", "command", "test", "xtest")
CLASS_KINDS = ("struct", "interface", "named", "alias", "package")
MEMBER_KINDS = ("func", "method", "interface_method", "init", "closure", "var_init")


@dataclass(frozen=True)
class BuildConfig:
    id: str                                    # "linux-amd64", "windows-amd64+integration"
    goos: str
    goarch: str
    tags: tuple[str, ...]                      # user build tags (-tags)
    cgo: bool
    primary: bool


@dataclass(frozen=True)
class Replace:
    old: str                                   # module path
    old_version: str                           # "" = every version
    new: str                                   # absolute real path when local, else a module path
    new_version: str                           # "" when local
    local: bool


@dataclass(frozen=True)
class GoUnit:
    key: str                                   # module path
    root: str                                  # repo-relative POSIX dir; "" = repo root
    go: str | None                             # go line, e.g. "1.26"
    toolchain: str | None                      # toolchain line, e.g. "go1.26.8"
    requires: tuple[tuple[str, str, bool], ...]   # (path, version, indirect)
    replaces: tuple[Replace, ...]
    vendor: bool                               # vendor/ with a modules.txt consistent with go.mod
    in_repo_deps: tuple[str, ...]              # sorted unit keys whose packages this unit's files
                                               # import (primary build, tests included), when its
                                               # group builds them from in-repo source (workspace
                                               # module or local replace)


@dataclass
class Group:
    id: str                                    # "ws0", "ws1", ... / "m0", "m1", ...
    kind: str                                  # "workspace" | "module"
    dir: str                                   # absolute load directory (cwd of go and the helper)
    modules: list[str]                         # this repo's unit keys loaded by the group
    gowork: str | None                         # absolute generated go.work, or None (GOWORK=off)
    modfile: str | None                        # absolute -modfile copy (pass as a flag, never in
                                               # GOFLAGS: R13), or None
    vendor: bool
    degraded: bool                             # the generated go.work differs from the repo's
    status: str                                # "ok" | "toolchain" (go line too new) | "error"
                                               # (e.g. inconsistent vendor/; the load sets it too)
    pattern: str                               # "work" | "./..."
    missing_modules: list[str] = field(default_factory=list)   # "path@version", offline only
    warnings: list[str] = field(default_factory=list)
    source: str | None = None                  # absolute repo go.work (workspace) or go.mod (module)
    uses: list[str] = field(default_factory=list)   # absolute module dirs in the generated go.work
    replaces: list[Replace] = field(default_factory=list)   # the repo go.work's replaces (workspace
                                                            # only; members' are on their units)


@dataclass(frozen=True)
class Peer:
    name: str                                  # repo name used at ingest
    root: str                                  # absolute real path
    units: tuple[GoUnit, ...]


@dataclass
class Discovery:
    repo: str
    repo_root: str                             # absolute real path
    units: list[GoUnit]
    unit_of_file: dict[str, str]               # relpath -> unit key; files under no module are absent
    groups: list[Group]
    builds: list[BuildConfig]
    toolchain: str                             # "go1.27.2"; "" when no toolchain was found
    mode: str                                  # "offline" | "download"
    module_source: str                         # "vendor" | "file-proxy" | "none" | "proxy"
    peers: list[Peer]
    skipped: dict[str, int]                    # scan skip counts by reason
    warnings: list[str]                        # every warning, group ones prefixed "group <id>: "

    def unit(self, key: str) -> GoUnit:
        for u in self.units:
            if u.key == key:
                return u
        raise KeyError(key)

    def group_of(self, key: str) -> Group | None:
        """The group that loads unit *key*, or None."""
        for g in self.groups:
            if key in g.modules:
                return g
        return None


@dataclass(frozen=True)
class Toolchain:
    go: str                                    # absolute path of the go binary
    version: str                               # "go1.27.2"
    goroot: str                                # absolute real path
