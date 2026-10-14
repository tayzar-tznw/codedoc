"""Real-repo trial (spec §9.6 with amendments R6, R7 and R50), GCP-free.

`python -m graph_generator trial <dir> --repo-name N [--packages P...]
[--download-modules] [--skip-oracle]`:

1. **Offline arm** (R7): the graph track with --no-spanner in-process (Phases
   1 → 1.6 plus the rows, as evaluate runs them) with a dedicated, empty
   module cache and GOPROXY=off; no user cache.
2. **Download arm** (only with --download-modules or GO_MODULES_DOWNLOAD=1):
   a separate cache filled from GOPROXY=file://<user cache>/cache/download,
   then GO_PROXY. The trial fails on a changed answer (R50): an offline
   resolved call whose download record is a definite other answer (another
   target, external, builtin, dynamic, ambiguous), and offline candidates,
   imports, embeds or implements the download arm does not have
   (offline ⊆ download). Offline answers against an unresolved or missing
   download record are listed for investigation; external module or
   version changes and the offline recall loss are reported.
   Each arm fails when a group is not `ok` after the load or it wrote no
   MethodCalls edge.
3. **The repo stays untouched**: the tree is snapshotted (modes, sizes,
   mtimes and ctimes, directories and .git included, plus the gitdir a
   `.git` file names) before and after; any change fails the trial.
4. **Precision check** (spec §9.4): goracle (test_codes/_gt/oracle) instruments
   a copy, runs the chosen packages' unit tests (both with GO_BUILD_TAGS; a
   failing package gets one retry and is reported flaky when it passes) and
   scores every MethodCalls edge of the download arm (of the offline arm when
   it ran alone) with `goracle match`, closed-world interface edges against
   their candidates. Every call record of that arm also gets the name and caller
   check against goracle's site table (R50 6b/c, evaluate.record_problems).
   Any WRONG-* verdict, a failing package, a package at none of whose own
   sites goracle observed execution (the execution floor), an oracle error,
   no edge scored, a verdict count other than the edge count, an unscored
   verdict (NO-SITE, NO-TARGET) outside goracle's documented skip classes or
   a record that fails the name or caller check fails the trial; without
   default packages it fails unless --skip-oracle is given. PossiblyCalls
   recall = observed callees at sites with candidates that are among the
   candidates.

Outputs go to OUTPUT_DIR/trial/<repo>/: offline/, download/ and oracle/ (each
recreated per run), summary.json (read by scripts.verification_table) and
summary.md. The repo itself is only read; OUTPUT_DIR must lie outside it.
"""

from __future__ import annotations

import contextlib
import json
import os
import posixpath
import resource
import subprocess
import sys
import time
from collections import Counter
from datetime import date
from typing import Callable, Iterator

from . import config, evaluate, go_toolchain

DEFAULT_PACKAGES: dict[str, tuple[str, ...]] = {   # runtime-oracle report §4
    "etcd": ("pkg/adt", "pkg/wait", "server/etcdserver/api/v2store", "server/lease",
             "server/storage/mvcc", "client/v3"),
    "prometheus": ("model/labels", "model/relabel", "tsdb/chunkenc", "storage", "promql"),
}
TEST_FLAGS: tuple[str, ...] = ("-count=1", "-vet=off", "-gcflags=all=-l", "-ldflags=-checklinkname=0",
                               "-buildvcs=false")
ORACLE_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "test_codes", "_gt", "oracle")
WRONG_VERDICTS: tuple[str, ...] = ("WRONG-OTHER", "WRONG-MULTI", "IFACE-OTHER", "DEFER-GROUP-OTHER")
# goracle's documented skip classes (its rewrite.go): files with //line directives,
# cgo files, files that do not parse, files under no module
DOCUMENTED_SKIPS: tuple[str, ...] = ("skipped:line-directives", "skipped:cgo", "skipped:parse",
                                     "skipped:no-module")
ARMS: tuple[str, ...] = ("offline", "download")
PASSED: tuple[str, ...] = ("ok", "flaky")       # a test package's statuses that pass (flaky: on its retry)


# ═══════════════════════════════════════════════════════════════════
# The repo stays untouched
# ═══════════════════════════════════════════════════════════════════


def _entry(path: str, kind: str) -> tuple:
    st = os.lstat(path)
    if kind == "link":
        return ("link", os.readlink(path), st.st_ctime_ns)
    if kind == "dir":
        return ("dir", st.st_mode, st.st_mtime_ns, st.st_ctime_ns)
    return ("file", st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _tree(top: str, prefix: str, out: dict[str, tuple]) -> None:
    out[prefix + "./"] = _entry(top, "dir")
    for dirpath, dirnames, filenames in os.walk(top):
        rel_dir = os.path.relpath(dirpath, top)
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            rel = prefix + os.path.normpath(os.path.join(rel_dir, name)).replace(os.sep, "/")
            if os.path.islink(path):
                out[rel] = _entry(path, "link")
            elif name in dirnames:
                out[rel + "/"] = _entry(path, "dir")
            else:
                out[rel] = _entry(path, "file")


def _git_file_dir(root: str) -> str | None:
    """The gitdir a `.git` FILE names (a worktree or a submodule keeps its index there)."""
    path = os.path.join(root, ".git")
    if os.path.islink(path) or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        if line.startswith("gitdir:"):
            gitdir = line[len("gitdir:"):].strip()
            gitdir = os.path.normpath(gitdir if os.path.isabs(gitdir) else os.path.join(root, gitdir))
            return gitdir if os.path.isdir(gitdir) else None
    return None


def _after_newest(out: dict[str, tuple]) -> None:
    """Return once the clock that stamps file times is past the newest ctime
    of the snapshot (at most one tick), so a write right after it cannot
    repeat a recorded time and hide. Linux stamps from its coarse clock
    (CLOCK_REALTIME_COARSE, one tick = 4 ms at HZ=250)."""
    clock = 5 if sys.platform.startswith("linux") else time.CLOCK_REALTIME
    newest = min(max((e[-1] for e in out.values()), default=0), time.clock_gettime_ns(clock))
    while time.clock_gettime_ns(clock) <= newest:
        time.sleep(0.001)


def snapshot(root: str) -> dict[str, tuple]:
    """Every entry under root, .git included, plus the gitdir a `.git` file names
    (keys "gitdir:<path>"): files by (mode, size, mtime, ctime), directories by
    (mode, mtime, ctime), symlinks by (target, ctime); the root itself is "./".
    Writes through the go command (go.work.sum, go.sum) and git (.git/index)
    change one of them; a directory's mtime also shows a file created and
    removed again, and a ctime shows a rewrite whose mtime was restored."""
    out: dict[str, tuple] = {}
    _tree(root, "", out)
    gitdir = _git_file_dir(root)
    if gitdir is not None:
        _tree(gitdir, "gitdir:", out)
    _after_newest(out)
    return out


def tree_changes(before: dict[str, tuple], after: dict[str, tuple], limit: int = 50) -> list[str]:
    changes = [f"added: {k}" for k in sorted(set(after) - set(before))]
    changes += [f"removed: {k}" for k in sorted(set(before) - set(after))]
    changes += [f"changed: {k}" for k in sorted(set(before) & set(after)) if before[k] != after[k]]
    return changes[:limit]


# ═══════════════════════════════════════════════════════════════════
# Arms and their comparison
# ═══════════════════════════════════════════════════════════════════


def _calls(res: dict) -> Iterator[tuple[str, dict]]:
    for rel, frec in sorted((res or {}).get("files", {}).items()):
        for rec in frec.get("calls", []):
            yield rel, rec


def _site_key(rel: str, rec: dict) -> str:
    site = rec["site"]
    return f"{rel}:{site['line']}:{site['col']}:{site['kind']}"


def _target_label(rec: dict) -> str:
    t = rec.get("target") or {}
    return f"{t.get('relpath')}:{t.get('qualname')}"


def _describe(rec: dict | None) -> str:
    if rec is None:
        return "no record"
    if rec["status"] == "external":
        return f"external {(rec.get('external') or {}).get('module')}"
    if rec.get("reason"):
        return f"{rec['status']} ({rec['reason']})"
    return rec["status"]


def _answer(rec: dict) -> str:
    """A call record's single answer: the target of a resolved record, the module
    of an external one (versions are compared apart), else its status."""
    if rec["status"] == "resolved" and rec.get("target"):
        return _target_label(rec)
    if rec["status"] == "external":
        return f"external {(rec.get('external') or {}).get('module')}"
    return rec["status"]


def _candidate_labels(rec: dict | None) -> set[str]:
    return {f"{c.get('relpath')}:{c.get('qualname')}" for c in (rec or {}).get("candidates") or []}


def _external_label(rec: dict) -> str:
    """module@version of an external record; any other record's answer."""
    if rec["status"] != "external":
        return _answer(rec)
    e = rec.get("external") or {}
    return f"{e.get('module')}@{e['version']}" if e.get("version") else str(e.get("module"))


def _import_label(rec: dict) -> str:
    t = rec.get("target") or {}
    return f"{t.get('dir')}|{t.get('clause')}"


# A definite download answer: anything but unresolved. Against it, an offline
# answer that differs is a changed answer (R50); against "unresolved" or no
# record it is listed for investigation (the download arm knows no better).
DEFINITE: tuple[str, ...] = ("resolved", "external", "builtin", "dynamic", "ambiguous")
# An answered site: a target in the repo (edged) or in a module (counted).
ANSWERED: tuple[str, ...] = ("resolved", "external")


def _relation_pairs(res: dict, key: str, end: str, pointer: str) -> tuple[dict, dict]:
    """(type -> resolved (label, pointer) pairs, type -> statuses) of embeds or implements."""
    pairs: dict[tuple[str, str], set] = {}
    statuses: dict[tuple[str, str], set] = {}
    for rel, frec in sorted((res or {}).get("files", {}).items()):
        for r in frec.get(key, []):
            tkey = (rel, (r.get("type") or {}).get("qualname"))
            statuses.setdefault(tkey, set()).add(r.get("status"))
            e = r.get(end)
            if r.get("status") == "resolved" and e:
                label = ("*" if r.get(pointer) else "") + f"{e.get('relpath')}:{e.get('qualname')}"
                pairs.setdefault(tkey, set()).add(label)
    return pairs, statuses


def compare_arms(offline: dict, download: dict, limit: int = 200) -> dict:
    """The arms per site (R7, R50). A changed answer fails the trial: an offline
    resolved call whose download record is a definite other answer (another
    target, external, builtin, dynamic, ambiguous), an offline candidate the
    download record does not offer, or a resolved import, embed or implements
    record the download arm does not resolve the same way (offline ⊆ download).
    Offline answers (resolved, external) whose download record is unresolved
    or missing are listed for investigation; an offline external answer that
    the download arm gives another module, version or answer is reported.
    The offline recall loss is the share of download-answered sites that
    offline left unanswered."""
    changed: list[dict] = []
    investigate: list[dict] = []
    external_changes: list[dict] = []
    dl = {_site_key(rel, rec): rec for rel, rec in _calls(download)}
    answered = {"offline": 0, "download": sum(r["status"] in ANSWERED for r in dl.values())}
    for rel, rec in _calls(offline):
        answered["offline"] += rec["status"] in ANSWERED
        key = _site_key(rel, rec)
        other = dl.get(key)
        definite = other is not None and other["status"] in DEFINITE
        if rec["status"] in ANSWERED and not definite:
            investigate.append({"kind": "call", "site": key, "offline": _answer(rec), "download": _describe(other)})
        elif rec["status"] == "resolved" and _answer(other) != _answer(rec):
            changed.append({"kind": "call", "site": key, "offline": _answer(rec), "download": _answer(other)})
        elif rec["status"] == "external" and _external_label(other) != _external_label(rec):
            external_changes.append({"kind": "call", "site": key, "offline": _external_label(rec),
                                     "download": _external_label(other)})
        extra = sorted(_candidate_labels(rec) - _candidate_labels(other))
        if extra and definite:
            changed += [{"kind": "candidates", "site": key, "offline": c, "download": "not a candidate"}
                        for c in extra]
        elif extra and rec["status"] not in ANSWERED:
            investigate += [{"kind": "candidates", "site": key, "offline": c, "download": _describe(other)}
                            for c in extra]
    dl_imports = {(rel, r["line"], r["col"]): r for rel, frec in sorted((download or {}).get("files", {}).items())
                  for r in frec.get("imports", [])}
    for rel, frec in sorted((offline or {}).get("files", {}).items()):
        for r in frec.get("imports", []):
            key, other = f"{rel}:{r['line']}:{r['col']}", dl_imports.get((rel, r["line"], r["col"]))
            if r["status"] == "resolved" and r.get("target"):
                entry = {"kind": "import", "site": key, "offline": _import_label(r)}
                if other is None or other["status"] == "unresolved":
                    investigate.append({**entry, "download": _describe(other)})
                elif other["status"] != "resolved" or _import_label(other) != _import_label(r):
                    changed.append({**entry, "download": _import_label(other)
                                    if other["status"] == "resolved" and other.get("target") else _describe(other)})
            elif (r["status"] == "external" and other is not None and other["status"] == "external"
                  and _external_label(r) != _external_label(other)):
                external_changes.append({"kind": "import", "site": key, "offline": _external_label(r),
                                         "download": _external_label(other)})
    for kind, key, end, pointer, absent in (("embed", "embeds", "embedded", "pointer", "not embedded"),
                                            ("implements", "implements", "iface", "pointer_receiver",
                                             "not implemented")):
        off_pairs, _ = _relation_pairs(offline, key, end, pointer)
        dl_pairs, dl_statuses = _relation_pairs(download, key, end, pointer)
        for tkey in sorted(off_pairs):
            for label in sorted(off_pairs[tkey] - dl_pairs.get(tkey, set())):
                entry = {"kind": kind, "site": f"{tkey[0]}:{tkey[1]}", "offline": label}
                if "unresolved" in dl_statuses.get(tkey, set()):
                    investigate.append({**entry, "download": "unresolved"})
                else:
                    changed.append({**entry, "download": ", ".join(sorted(dl_pairs.get(tkey, ()))) or absent})
    lost = max(answered["download"] - answered["offline"], 0)
    return {"changed_answers": len(changed), "changed": changed[:limit],
            "resolved_only_offline": investigate[:limit], "resolved_only_offline_count": len(investigate),
            "external_changes": external_changes[:limit], "external_changes_count": len(external_changes),
            "answered": answered,
            "recall_loss": round(lost / answered["download"], 4) if answered["download"] else 0.0}


def arm_summary(run: evaluate.RepoRun, *, seconds: float, peak_rss: int) -> dict:
    """One arm's counts (summary.json "modes"): files, packages, sites by status,
    class and reason, edges by table, time and peak RSS, and the in-repo
    import records without a PackageImports row (evaluate.imports_check,
    reported, not gated). The peak is the helper's own when resolutions.json
    reports it per group, else the largest child process of the trial."""
    recs = [rec for _rel, rec in _calls(run.resolutions)]
    stats = (run.resolutions or {}).get("stats") or {}
    group_peaks = [g.get("peak_rss_bytes") or 0 for g in ((run.resolutions or {}).get("groups") or {}).values()]
    imports = evaluate.imports_check({run.name: run}, limit=10)
    return {"files": len(run.scanned), "packages": len(run.nodes.get("Packages", [])), "sites": len(recs),
            "by_status": dict(sorted(Counter(r["status"] for r in recs).items())),
            "by_class": dict(sorted((stats.get("by_class") or {}).items())),
            "by_reason": dict(sorted(Counter(r["reason"] for r in recs if r.get("reason")).items())),
            "edges": {table: len(rows) for table, rows in run.edges.items()},
            "seconds": round(seconds, 2),
            "phases": {k: round(v, 2) for k, v in run.timings.items()},
            "peak_rss_bytes": max(group_peaks, default=0) or peak_rss,
            "imports": {k: imports[k] for k in ("records", "mapped", "unmapped", "ambiguous",
                                                "unmapped_examples", "ambiguous_examples")}}


# ═══════════════════════════════════════════════════════════════════
# Oracle records and results
# ═══════════════════════════════════════════════════════════════════


def match_records(resolutions: dict, entities: dict) -> list[dict]:
    """The MethodCalls edges as `goracle match` input, built by evaluate's
    oracle_edges so that the trial and evaluate send the same records: every
    resolved record whose target is a method (resolved targets are in-repo,
    C5), interface methods marked. A closed-world interface edge (open_world
    false, no `candidates_partial`) carries the record's candidates, an empty
    list included, so goracle answers IFACE-OTHER for an observed in-repo
    callee outside them; goracle allows a method of a generic type and, behind
    a candidate marked via_embedded_iface, any method of that name. Every
    other interface edge carries none and goracle judges it by the method
    name: on an open world an out-of-repo implementer may forward to any
    in-repo method (R50 as amended)."""
    return evaluate.oracle_edges(resolutions, entities)


def possibly_calls_recall(obs: list[dict], resolutions: dict) -> dict:
    """Observed in-repo callees at sites with PossiblyCalls candidates, and how
    many of them were among the candidates (spec §9.6 item 2.4)."""
    cands = {(rel, rec["site"]["line"], rec["site"]["col"]):
             {(c["relpath"], c["qualname"]) for c in rec["candidates"]}
             for rel, rec in _calls(resolutions) if rec.get("candidates")}
    observed = covered = 0
    for o in obs:
        want = cands.get((o.get("file"), o.get("line"), o.get("col"))) if o.get("kind") == "site" else None
        if want is None:
            continue
        for c in o.get("callees") or []:
            observed += 1
            covered += (c.get("file"), c.get("qualname")) in want
    return {"observed": observed, "covered": covered}


def count_verdicts(verdicts: list[dict]) -> dict[str, int]:
    return dict(sorted(Counter(v["verdict"] for v in verdicts).items()))


# goracle's detail note for an observed method of a generic type at an
# interface edge: allowed outside the candidates and reported (R50 as amended)
GENERIC_NOTE = " is a generic implementer"


def exercised_at(observations: list[dict], packages) -> dict[str, int]:
    """The execution floor (R50): per tested package, the sites in its own
    directory at which goracle observed execution (a completed statement,
    exec > 0, or a callee seen), whether or not a scored edge sits there. A
    package whose tests ran has at least one, else goracle's runtime recorded
    nothing for it."""
    dirs = {pkg: posixpath.normpath(pkg) for pkg in packages}
    ran = Counter(posixpath.dirname(o.get("file") or "") or "." for o in observations
                  if o.get("kind") == "site" and ((o.get("exec") or 0) > 0 or o.get("callees")))
    return {pkg: ran.get(d, 0) for pkg, d in dirs.items()}


def generic_observations(verdicts: list[dict]) -> int:
    """Observed methods of generic types that goracle allowed outside an
    interface edge's candidates (never wrong; reported next to the verdicts)."""
    return sum((v.get("detail") or "").count(GENERIC_NOTE) for v in verdicts)


def wrong_count(counts: dict[str, int]) -> int:
    return sum(n for v, n in counts.items() if v in WRONG_VERDICTS)


def unscored(verdicts: list[dict], files: dict[str, str], limit: int = 20) -> dict:
    """NO-SITE and NO-TARGET verdicts score nothing (R50). They are documented when
    the site's file (NO-SITE) or the target's file (NO-TARGET) is one goracle
    skips on purpose (DOCUMENTED_SKIPS); any other one fails the trial.
    `files` maps a repo-relative path to its status in goracle's site table."""
    out: dict = {"documented": 0, "undocumented": 0, "documented_examples": [], "undocumented_examples": []}
    for v in verdicts:
        if v.get("verdict") == "NO-SITE":
            which, path = "site", v.get("file")
        elif v.get("verdict") == "NO-TARGET":
            which, path = "target", (v.get("target") or {}).get("file")
        else:
            continue
        status = files.get(path)
        where = f"{v['verdict']} {v.get('file')}:{v.get('line')}:{v.get('col')}: {which} file {path}"
        side = "documented" if status in DOCUMENTED_SKIPS else "undocumented"
        out[side] += 1
        if len(out[f"{side}_examples"]) < limit:
            out[f"{side}_examples"].append(f"{where} is {status}" if status
                                           else f"{where} is not in goracle's site table")
    return out


def report_warnings(log: str) -> list[str]:
    """The "WARNING:" lines of `goracle report` (e.g. entered functions under a
    runtime name its table did not predict): shown in the summary, never a
    failure."""
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            return [line.strip() for line in f if line.startswith("WARNING:")]
    except OSError:
        return []


def site_table_files(path: str) -> dict[str, str]:
    """Repo-relative path -> status of every file in goracle's site table."""
    return {r["file"]: r.get("status", "") for r in _read_jsonl(path) if r.get("kind") == "file"}


def record_checks(resolutions: dict, sites_jsonl: str, limit: int = 20) -> dict:
    """R50 6(b)(c) on every call record of the scored arm, by evaluate's shared
    record_problems: a via=types record names targets and candidates by the
    site's name, and each record's caller is the enclosing function goracle's
    site table gives. goracle keeps no sites for a file it skips on purpose,
    so records there get the name check only."""
    table = evaluate.site_table(sites_jsonl)
    problems = evaluate.record_problems(resolutions, table)
    recs = [rel for rel, _rec in _calls(resolutions)]
    return {"records": len(recs), "name_only": sum(rel in table.skipped for rel in recs),
            "problems": len(problems), "examples": problems[:limit]}


def instrument_argv(goracle: str, src: str, out: str, tags) -> list[str]:
    argv = [goracle, "instrument", "--src", src, "--out", out]
    return argv + ["--tags", ",".join(tags)] if tags else argv


def test_argv(go: str, pattern: str, tags) -> list[str]:
    return [go, "test", *TEST_FLAGS, *([f"-tags={','.join(tags)}"] if tags else []), pattern]


def run_tests(argv: list[str], cwd: str, env: dict, hits: str, log: str, timeout: int,
              printer=print) -> dict:
    """One package's instrumented tests. A failing run gets one retry, with its
    own hits directory and log: "flaky" when the retry passes (its hits are
    the ones reported), else "failed" or "timeout" by the retry's exit.
    Returns {"status", "log", "hits"} plus "retry_log" after a retry."""
    rc = _sh(argv, cwd, {**env, "GORACLE_OUT": hits}, log, timeout)
    if rc == 0:
        return {"status": "ok", "log": log, "hits": hits}
    printer(f"[trial] oracle: go test {argv[-1]} failed (exit {rc}); retrying once")
    retry_hits, retry_log = hits + "-retry", log.removesuffix(".log") + "-retry.log"
    os.makedirs(retry_hits, exist_ok=True)
    rc = _sh(argv, cwd, {**env, "GORACLE_OUT": retry_hits}, retry_log, timeout)
    if rc == 0:
        return {"status": "flaky", "log": log, "retry_log": retry_log, "hits": retry_hits}
    return {"status": "timeout" if rc == 124 else "failed", "log": log, "retry_log": retry_log, "hits": hits}


def packages_for(repo: str, given) -> tuple[str, ...]:
    return tuple(given) if given else DEFAULT_PACKAGES.get(repo.lower(), ())


def test_location(copy: str, package: str) -> tuple[str, str]:
    """(directory to run go test in, package pattern): the nearest module root
    at or above the package inside the copy, so packages of nested modules
    also test without a go.work."""
    pkg_dir = os.path.normpath(os.path.join(copy, package))
    d = pkg_dir
    while True:
        if os.path.isfile(os.path.join(d, "go.mod")) or d == copy or len(d) <= len(copy):
            break
        d = os.path.dirname(d)
    rel = os.path.relpath(pkg_dir, d).replace(os.sep, "/")
    return d, "." if rel == "." else f"./{rel}"


# ═══════════════════════════════════════════════════════════════════
# The real stages (graph arm, oracle)
# ═══════════════════════════════════════════════════════════════════


def _base_output() -> str:
    return os.path.realpath(os.path.abspath(config.OUTPUT_DIR))


@contextlib.contextmanager
def arm_settings(arm_dir: str, mode: str, user_cache: str | None) -> Iterator[None]:
    """Offline: no user cache, so GOPROXY=off over an empty module cache (R7).
    Download: GOPROXY=file://<user cache>/cache/download,<GO_PROXY>. No peers."""
    pinned: dict = {"PEER_REPOS": None, "GO_MODULES_DOWNLOAD": mode == "download"}
    if mode == "offline":
        empty = os.path.join(arm_dir, "no-user-modcache")
        os.makedirs(empty, exist_ok=True)
        pinned["GO_USER_MODCACHE"] = empty
    elif user_cache:
        from pathlib import Path
        pinned["GO_PROXY"] = f"{Path(user_cache, 'cache', 'download').as_uri()},{config.GO_PROXY}"
    saved = {k: getattr(config, k, None) for k in pinned}
    try:
        for k, v in pinned.items():
            setattr(config, k, v)
        yield
    finally:
        for k, v in saved.items():
            setattr(config, k, v)


def graph_arm(repo_root: str, repo: str, mode: str, arm_dir: str,
              printer=print) -> tuple[evaluate.RepoRun, float, int]:
    """One arm of the graph track: (run, seconds, peak RSS of the children)."""
    from . import go_modcache

    toolchain = go_toolchain.find_toolchain()
    user_cache = go_modcache.user_modcache(toolchain) if mode == "download" else None
    evaluate._rmtree(arm_dir)
    os.makedirs(arm_dir)
    evaluate.seed_helper(go_toolchain.ensure_helper(toolchain, _base_output()), arm_dir)
    printer(f"[trial] {mode} arm (module cache {arm_dir}/go/modcache)")
    t0 = time.perf_counter()
    with arm_settings(arm_dir, mode, user_cache):
        run = evaluate.run_graph_track(repo, ".", repo_root, arm_dir, peers=[], mode=mode,
                                       toolchain=toolchain)
    seconds = time.perf_counter() - t0
    return run, seconds, resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024


def _sh(argv: list[str], cwd: str, env: dict, log: str, timeout: int) -> int:
    with open(log, "w", encoding="utf-8") as handle:
        handle.write("$ " + " ".join(argv) + f"\n# cwd {cwd}\n")
        handle.flush()
        try:
            return subprocess.run(argv, cwd=cwd, env=env, stdout=handle, stderr=subprocess.STDOUT,
                                  timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            handle.write(f"\n# timeout after {timeout} s\n")
            return 124


def _read_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def oracle_env(toolchain, work_dir: str, *, download: bool, user_cache: str | None) -> dict[str, str]:
    """The environment of goracle's builds and the instrumented tests: a fresh
    module cache under the oracle dir, the shared build cache, modules from
    the user cache (plus GO_PROXY in download mode), the primary build."""
    from . import go_units

    env = go_toolchain.base_env(toolchain, work_dir)
    env["GOCACHE"] = os.path.join(go_toolchain.ensure_go_dirs(_base_output()), "gocache")
    proxies = []
    if user_cache:
        from pathlib import Path
        proxies.append(Path(user_cache, "cache", "download").as_uri())
    if download:
        proxies.append(go_toolchain.download_proxy())
    build = go_units.builds_from_config()[0]
    env.update({"GOFLAGS": "-modcacherw", "GOPROXY": ",".join(proxies) or "off",
                "GOSUMDB": str(config.GO_SUMDB) if download else "off",
                "CGO_ENABLED": "1" if build.cgo else "0", "GOOS": build.goos, "GOARCH": build.goarch})
    return env


def run_oracle(repo_root: str, repo: str, run: evaluate.RepoRun, packages, work_dir: str,
               printer=print) -> dict:
    """goracle on a copy: instrument, run each package's tests, report, match
    the run's MethodCalls edges. Both goracle and the tests build with the
    primary build's tags (GO_BUILD_TAGS). Returns the summary's "oracle"
    block: verdict counts, WRONG-*, the number of edges sent and of verdicts
    returned, the generic-receiver observations goracle allowed, the unscored
    verdicts by goracle skip class, the name and caller check of every call
    record (R50) and the WARNING lines of `goracle report`."""
    from . import go_modcache, go_units

    result: dict = {"verdicts": {}, "wrong": 0, "possibly_calls_recall": None, "packages": {},
                    "edges": 0, "verdict_total": 0, "generic_observations": 0, "unscored": None,
                    "record_checks": None, "report_warnings": [], "error": None}
    tags = go_units.builds_from_config()[0].tags
    toolchain = go_toolchain.find_toolchain()
    if not toolchain.version.startswith("go1.27"):
        result["error"] = f"goracle is pinned to go1.27, the toolchain is {toolchain.version}"
        return result
    download = bool(run.discovery and run.discovery.mode == "download")
    evaluate._rmtree(work_dir)
    for sub in ("bin", "logs", "hits"):
        os.makedirs(os.path.join(work_dir, sub))
    env = oracle_env(toolchain, work_dir, download=download,
                     user_cache=go_modcache.user_modcache(toolchain))
    timeout = int(config.GO_HELPER_TIMEOUT or 1800)
    logs = os.path.join(work_dir, "logs")
    goracle = os.path.join(work_dir, "bin", "goracle")
    build_env = {**env, "GOWORK": "off", "GOPROXY": "off", "GOSUMDB": "off", "CGO_ENABLED": "0"}
    if _sh([toolchain.go, "build", "-buildvcs=false", "-o", goracle, "."], ORACLE_SRC, build_env,
           os.path.join(logs, "build-goracle.log"), timeout) != 0:
        result["error"] = f"building goracle failed; see {logs}/build-goracle.log"
        return result
    copy = os.path.join(work_dir, "copy")
    if _sh(instrument_argv(goracle, repo_root, copy, tags), work_dir, env,
           os.path.join(logs, "instrument.log"), timeout) != 0:
        result["error"] = f"goracle instrument failed; see {logs}/instrument.log"
        return result
    hit_dirs = []
    for pkg in packages:
        slug = pkg.replace("/", "_") or "root"
        hits = os.path.join(work_dir, "hits", slug)
        os.makedirs(hits, exist_ok=True)
        cwd, pattern = test_location(copy, pkg)
        printer(f"[trial] oracle: go test {pattern} (in {os.path.relpath(cwd, copy)})")
        outcome = run_tests(test_argv(toolchain.go, pattern, tags), cwd, env, hits,
                            os.path.join(logs, f"test-{slug}.log"), timeout, printer)
        hit_dirs.append(outcome.pop("hits"))
        result["packages"][pkg] = outcome
    obs = os.path.join(work_dir, "obs.jsonl")
    if _sh([goracle, "report", "--copy", copy, "--hits", ",".join(hit_dirs), "--out", obs], work_dir, env,
           os.path.join(logs, "report.log"), timeout) != 0:
        result["error"] = f"goracle report failed; see {logs}/report.log"
        return result
    result["report_warnings"] = report_warnings(os.path.join(logs, "report.log"))
    for warning in result["report_warnings"]:
        printer(f"[trial] goracle report: {warning}")
    records = match_records(run.resolutions, run.entities)
    records_path = os.path.join(work_dir, "records.jsonl")
    with open(records_path, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r) + "\n" for r in records)
    verdicts_path = os.path.join(work_dir, "verdicts.jsonl")
    rc = _sh([goracle, "match", "--obs", obs, "--records", records_path, "--out", verdicts_path], work_dir,
             env, os.path.join(logs, "match.log"), timeout)
    if rc not in (0, 1):
        result["error"] = f"goracle match failed; see {logs}/match.log"
        return result
    listed = _read_jsonl(verdicts_path) if os.path.isfile(verdicts_path) else []
    verdicts = count_verdicts(listed)
    observations = _read_jsonl(obs)
    for pkg, n in exercised_at(observations, packages).items():
        result["packages"][pkg]["executed"] = n
    sites = os.path.join(copy, "_goracle", "sites.jsonl")
    result.update(verdicts=verdicts, wrong=wrong_count(verdicts), edges=len(records), verdict_total=len(listed),
                  generic_observations=generic_observations(listed),
                  unscored=unscored(listed, site_table_files(sites)),
                  record_checks=record_checks(run.resolutions, sites),
                  possibly_calls_recall=possibly_calls_recall(observations, run.resolutions),
                  obs=obs, verdicts_path=verdicts_path)
    return result


# ═══════════════════════════════════════════════════════════════════
# The trial
# ═══════════════════════════════════════════════════════════════════


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def oracle_failures(oracle: dict) -> list[str]:
    """Why the precision check fails (R50); empty when it passes or was skipped
    with --skip-oracle: an oracle error, WRONG-* verdicts, a test package that
    failed twice (a flaky one passed its retry) or at none of whose own sites
    goracle observed execution (the execution floor), no edge scored, a
    verdict count other than the record count, an unscored verdict outside
    goracle's documented skip classes, or a record that fails the name or
    caller check."""
    if oracle.get("skipped"):
        return []
    if oracle.get("error"):
        return [f"oracle: {oracle['error']}"]
    out = []
    if oracle.get("wrong", 0):
        out.append(f"oracle: {oracle['wrong']} WRONG-* verdicts")
    out += [f"oracle: test package {p} {r['status']}" for p, r in sorted((oracle.get("packages") or {}).items())
            if r["status"] not in PASSED]
    out += [f"oracle: test package {p} ran but goracle observed no execution at its own sites"
            for p, r in sorted((oracle.get("packages") or {}).items())
            if r["status"] in PASSED and not r.get("executed")]
    edges, total = oracle.get("edges", 0), sum((oracle.get("verdicts") or {}).values())
    if not edges:
        out.append("oracle: no MethodCalls edge was scored")
    elif total != edges:
        out.append(f"oracle: {total} verdicts for {edges} edges")
    undocumented = (oracle.get("unscored") or {}).get("undocumented", 0)
    if undocumented:
        out.append(f"oracle: {undocumented} NO-SITE/NO-TARGET verdicts outside goracle's documented skips")
    problems = (oracle.get("record_checks") or {}).get("problems", 0)
    if problems:
        out.append(f"oracle: {problems} records fail the name or caller check against goracle's site table")
    return out


def _oracle_ok(oracle: dict) -> bool:
    return not oracle_failures(oracle)


def arm_failures(mode: str, run: evaluate.RepoRun) -> list[str]:
    """An arm fails when no group loaded, when a group is not `ok` after the load,
    or when it wrote no MethodCalls edge (R50)."""
    groups = (run.resolutions or {}).get("groups") or {}
    out = [] if groups else [f"{mode} arm: no group was loaded"]
    for gid, g in sorted(groups.items()):
        if g.get("status") != "ok":
            detail = g.get("message") or g.get("reason") or ""
            out.append(f"{mode} arm: group {gid} is {g.get('status')} after the load"
                       + (f" ({detail})" if detail else ""))
    if not run.edges.get("MethodCalls"):
        out.append(f"{mode} arm: 0 MethodCalls edges")
    return out


def run_trial(target_dir: str, repo: str, *, packages=(), download: bool = False,
              skip_oracle: bool = False, printer=print,
              graph: Callable | None = None, oracle: Callable | None = None) -> bool:
    """Run the trial; True when it passes. `graph` and `oracle` replace the
    real stages (tests)."""
    root = os.path.realpath(target_dir)
    base = os.path.join(_base_output(), "trial", repo)
    if _inside(_base_output(), root):
        printer(f"[trial] OUTPUT_DIR ({_base_output()}) lies inside the repo; set OUTPUT_DIR to a "
                "directory outside it, because the trial must not write into the repo")
        return False
    graph = graph or graph_arm
    oracle = oracle or run_oracle
    before = snapshot(root)
    arms = ["offline", "download"] if download else ["offline"]
    runs: dict[str, evaluate.RepoRun] = {}
    modes: dict[str, dict] = {}
    for mode in arms:
        try:
            run, seconds, peak = graph(root, repo, mode, os.path.join(base, mode), printer)
        except (go_toolchain.ToolchainError, go_toolchain.HelperBuildError, NotImplementedError) as exc:
            printer(f"[trial] the {mode} arm cannot run: {exc}")
            return False
        runs[mode] = run
        modes[mode] = arm_summary(run, seconds=seconds, peak_rss=peak)
    cmp = compare_arms(runs["offline"].resolutions, runs["download"].resolutions) if download else None
    scored = arms[-1]
    chosen = packages_for(repo, packages)
    if skip_oracle:
        oracle_block: dict = {"mode": scored, "skipped": "--skip-oracle"}
    elif not chosen:
        oracle_block = {"mode": scored, "error": f"no default packages for {repo}; pass --packages or --skip-oracle"}
    else:
        oracle_block = {**oracle(root, repo, runs[scored], chosen, os.path.join(base, "oracle"), printer),
                        "mode": scored}
    changes = tree_changes(before, snapshot(root))
    first = runs["offline"]
    failures = [f for mode in arms for f in arm_failures(mode, runs[mode])]
    if cmp and cmp["changed_answers"]:
        failures.append(f"{cmp['changed_answers']} changed answers between the arms")
    if changes:
        failures.append("the repo was written: " + ", ".join(changes[:5]))
    failures += oracle_failures(oracle_block)
    summary = {
        "repo": repo, "date": date.today().isoformat(), "repo_root": root,
        "toolchain": (first.discovery.toolchain if first.discovery else ""),
        "modes": modes,
        "changed_answers": cmp["changed_answers"] if cmp else None,
        "changed": cmp["changed"] if cmp else [],
        "resolved_only_offline": cmp["resolved_only_offline"] if cmp else [],
        "resolved_only_offline_count": cmp["resolved_only_offline_count"] if cmp else None,
        "external_changes": cmp["external_changes"] if cmp else [],
        "external_changes_count": cmp["external_changes_count"] if cmp else None,
        "recall_loss": cmp["recall_loss"] if cmp else None,
        "answered": cmp["answered"] if cmp else None,
        "untouched": {"pass": not changes, "changes": changes},
        "oracle": oracle_block,
        "failures": failures,
    }
    summary["pass"] = not failures
    os.makedirs(base, exist_ok=True)
    with open(os.path.join(base, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    with open(os.path.join(base, "summary.md"), "w", encoding="utf-8") as f:
        f.write(summary_markdown(summary))
    for line in summary_markdown(summary).splitlines():
        printer(line)
    return summary["pass"]


def summary_markdown(s: dict) -> str:
    """A short human-readable view of summary.json."""
    lines = [f"# Trial: {s['repo']} ({s['date']}, {s['toolchain'] or 'toolchain ?'})", "",
             "| Arm | Files | Packages | Sites | resolved | external | unresolved | MethodCalls | "
             "PossiblyCalls | Seconds | Peak RSS |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for mode in ARMS:
        m = s["modes"].get(mode)
        if m:
            st, ed = m["by_status"], m["edges"]
            lines.append(f"| {mode} | {m['files']} | {m['packages']} | {m['sites']} | {st.get('resolved', 0)} | "
                         f"{st.get('external', 0)} | {st.get('unresolved', 0)} | {ed.get('MethodCalls', 0)} | "
                         f"{ed.get('PossiblyCalls', 0)} | {m['seconds']} | "
                         f"{m['peak_rss_bytes'] / 1e9:.2f} GB |")
    lines.append("")
    for mode in ARMS:
        m = s["modes"].get(mode)
        if m and m.get("imports"):
            ed, im = m["edges"], m["imports"]
            lines.append(f"- Rows ({mode}): " + ", ".join(f"{t} {ed.get(t, 0)}" for t in evaluate.ROW_TABLES)
                         + f"; in-repo import records mapped {im['mapped']} of {im['records']} "
                           f"({im['unmapped']} unmapped, {im['ambiguous']} ambiguous)")
    if s["changed_answers"] is None:
        lines.append("- Download arm: not run (pass --download-modules).")
    else:
        lines.append(f"- Changed answers between the arms: {s['changed_answers']}"
                     + ("" if not s["changed_answers"] else " (FAIL)"))
        for c in s.get("changed", [])[:5]:
            lines.append(f"  - {c.get('kind', 'call')} {c['site']}: offline {c['offline']}, download {c['download']}")
        only = s.get("resolved_only_offline_count")
        lines.append(f"- Offline answers to investigate (download unresolved or no record): "
                     f"{len(s['resolved_only_offline']) if only is None else only}; offline recall loss "
                     f"{s['recall_loss']:.2%}")
        if s.get("external_changes_count"):
            lines.append(f"- External module or version changes: {s['external_changes_count']}")
    lines.append("- Repo untouched: " + ("yes" if s["untouched"]["pass"]
                                         else "NO: " + ", ".join(s["untouched"]["changes"][:5])))
    o = s["oracle"]
    if o.get("skipped"):
        lines.append(f"- Oracle: skipped ({o['skipped']})")
    elif o.get("error"):
        lines.append(f"- Oracle: ERROR: {o['error']}")
    else:
        verdicts = ", ".join(f"{k} {v}" for k, v in o.get("verdicts", {}).items()) or "none"
        recall = o.get("possibly_calls_recall") or {}
        generic = o.get("generic_observations") or 0
        lines.append(f"- Oracle ({o['mode']} records): WRONG {o.get('wrong', 0)}; {verdicts}"
                     + (f"; generic-receiver observations {generic} (allowed, not wrong)" if generic else ""))
        if recall:
            lines.append(f"- PossiblyCalls recall: {recall.get('covered')}/{recall.get('observed')} observed "
                         "callees were candidates")
        failed = [p for p, r in (o.get("packages") or {}).items() if r["status"] not in PASSED]
        if failed:
            lines.append(f"- Test packages that failed instrumented: {', '.join(failed)}")
        flaky = [p for p, r in (o.get("packages") or {}).items() if r["status"] == "flaky"]
        if flaky:
            lines.append(f"- Test packages flaky (passed on retry): {', '.join(flaky)}")
        un = o.get("unscored") or {}
        if un.get("documented") or un.get("undocumented"):
            lines.append(f"- Unscored verdicts (NO-SITE, NO-TARGET): {un.get('documented', 0)} in files goracle "
                         f"skips on purpose, {un.get('undocumented', 0)} elsewhere")
        rc = o.get("record_checks")
        if rc:
            lines.append(f"- Name and caller check: {rc['problems']} problems in {rc['records']} records "
                         f"({rc['name_only']} in files goracle skips, name check only)")
            lines += [f"  - {p}" for p in rc["examples"][:5]]
        lines += [f"- goracle report: {w}" for w in o.get("report_warnings") or []]
    for failure in s.get("failures", []):
        lines.append(f"- FAIL: {failure}")
    lines += ["", f"Result: {'PASS' if s['pass'] else 'FAIL'}", ""]
    return "\n".join(lines)
