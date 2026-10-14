"""Phase 1.5: `gohelper extract` and the entities store (spec §2.6, §4).

One helper call extracts every scanned file, because receiver owners and the
choice between build twins read sibling files (master plan C3). A record is
reused only while its file's sha256 and its reuse stamp match: the stamp
folds in EXTRACTOR_REVISION, the helper's source hash, the primary build
configuration and the owning go.mod and module path (spec R29). Anything else
re-extracts every file, which takes well under a second on the trial repos.

Phase 1.6 starts with refresh_entities: every scanned file is hashed again,
changed files are re-extracted, deleted ones dropped, and a file that loses
its record on re-extraction raises StaleEntitiesError (codedoc_python §2.6).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from typing import Any, Callable

from . import config

ENTITIES_VERSION = 1
# Bump whenever the records this module stores change for the same helper
# output; the value is folded into every record's stamp.
EXTRACTOR_REVISION = 2  # 2: calls of a type literal are file conversions, not sites (R53)

Printer = Callable[[str], None]
# (relpaths) -> (records by relpath, error messages by relpath, summary line)
ExtractFn = Callable[[list[str]], tuple[dict[str, dict], dict[str, str], dict]]


class ExtractError(RuntimeError):
    """`gohelper extract` failed as a whole: no records at all."""


class StaleEntitiesError(RuntimeError):
    """Entities whose sha256 no longer matches the file on disk (spec §2.6 rule 4)."""

    def __init__(self, relpaths: list[str]) -> None:
        shown = ", ".join(relpaths[:10]) + (" ..." if len(relpaths) > 10 else "")
        super().__init__(f"entities out of date for {len(relpaths)} file(s): {shown}")
        self.relpaths = list(relpaths)


# ---------------------------------------------------------------- the stamp

def entity_stamp(*, helper_revision: str, build, gomod: str | None, module: str | None) -> str:
    """The reuse stamp of one file's record (spec §2.6, R29)."""
    payload = json.dumps({"extractor": EXTRACTOR_REVISION, "helper": helper_revision,
                          "goos": build.goos, "goarch": build.goarch, "tags": list(build.tags),
                          "cgo": bool(build.cgo), "gomod": gomod, "module": module},
                         sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def file_stamps(relpaths: list[str], discovery, build, helper_rev: str) -> dict[str, str]:
    """Stamps of the scanned files: a file's owning module comes from discovery."""
    owner = discovery.unit_of_file if discovery is not None else {}
    roots = {u.key: u.root for u in (discovery.units if discovery is not None else [])}
    out = {}
    for rel in relpaths:
        key = owner.get(rel)
        gomod = None
        if key is not None:
            root = roots.get(key, "")
            gomod = f"{root}/go.mod" if root else "go.mod"
        out[rel] = entity_stamp(helper_revision=helper_rev, build=build, gomod=gomod, module=key)
    return out


_HELPER_REVISION: str | None = None


def helper_revision() -> str:
    """The helper's source hash (go_toolchain), once per process."""
    global _HELPER_REVISION
    if _HELPER_REVISION is None:
        from .go_toolchain import helper_source_hash
        _HELPER_REVISION = helper_source_hash()
    return _HELPER_REVISION


# ---------------------------------------------------------------- reuse or extract

def update_entities(entities: dict[str, dict], *, file_hashes: dict[str, str],
                    stamps: dict[str, str], extract: ExtractFn) -> tuple[dict[str, dict], dict]:
    """The new store for the scanned files (the keys of *file_hashes*).

    Every record is reused when every scanned file has a record with its
    sha256 and stamp and no other record is left; otherwise *extract* runs
    once over every scanned file. The helper's sha256 stays on each record:
    it is the hash of the bytes the positions refer to. report["changed"]
    lists files whose bytes differ from *file_hashes* (changed since the scan).
    """
    rels = sorted(file_hashes)
    fresh = set(entities) == set(rels) and all(
        entities[r].get("sha256") == file_hashes[r] and entities[r].get("stamp") == stamps[r]
        for r in rels)
    if fresh:
        return dict(entities), {"extracted": 0, "reused": len(rels), "failed": {}, "changed": {}}
    if not rels:
        return {}, {"extracted": 0, "reused": 0, "failed": {}, "changed": {}}
    records, errors, _summary = extract(rels)
    store: dict[str, dict] = {}
    changed: dict[str, str] = {}
    for rel in rels:
        rec = records.get(rel)
        if rec is None:
            continue
        rec["stamp"] = stamps[rel]
        store[rel] = rec
        if rec.get("sha256") != file_hashes[rel]:
            changed[rel] = rec.get("sha256", "")
    failed = {rel: errors.get(rel, "no record from the helper") for rel in rels if rel not in store}
    return store, {"extracted": len(store), "reused": 0, "failed": failed, "changed": changed}


# ---------------------------------------------------------------- the helper process

def run_extract(helper: str, repo_root: str, relpaths: list[str], build, *, env: dict[str, str],
                timeout: float, scratch: str) -> tuple[dict[str, dict], dict[str, str], dict]:
    """Run `gohelper extract` over *relpaths* (C2). Returns (records by
    relpath, error messages by relpath, summary). Raises ExtractError when the
    helper fails, times out, or its output is not complete JSON Lines."""
    os.makedirs(scratch, exist_ok=True)
    fd, listing = tempfile.mkstemp(prefix="extract-files-", suffix=".txt", dir=scratch)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("".join(rel + "\n" for rel in relpaths))
        cmd = [helper, "extract", "--root", repo_root, "--files-from", listing,
               "--goos", build.goos, "--goarch", build.goarch, "--tags", ",".join(build.tags),
               "--cgo", "1" if build.cgo else "0"]
        try:
            proc = subprocess.run(cmd, cwd=repo_root, env=env, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ExtractError(f"gohelper extract timed out after {timeout:.0f}s "
                               f"(GO_HELPER_TIMEOUT)") from None
    finally:
        os.remove(listing)
    stderr = proc.stderr.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        raise ExtractError(f"gohelper extract failed (exit {proc.returncode}): {stderr[-2000:]}")
    records: dict[str, dict] = {}
    errors: dict[str, str] = {}
    summary: dict | None = None
    for n, line in enumerate(proc.stdout.splitlines(), 1):
        try:
            obj = json.loads(line)
        except ValueError:
            raise ExtractError(f"gohelper extract output line {n} is not JSON: {line[:200]!r}") from None
        if obj.get("kind") == "file":
            if "record" in obj:
                records[obj["relpath"]] = obj["record"]
            else:
                errors[obj["relpath"]] = obj.get("error") or "unreadable"
        elif obj.get("kind") == "summary":
            summary = obj
    if summary is None:
        raise ExtractError("gohelper extract output has no summary line (truncated output); "
                           + (stderr[-2000:] or "no diagnostics"))
    return records, errors, summary


def _toolchain(data):
    if data.toolchain is None:
        from .go_toolchain import find_toolchain
        data.toolchain = find_toolchain()
    return data.toolchain


def _helper(data, output_dir: str) -> str:
    """The gohelper binary, built and cached by go_toolchain."""
    from .go_toolchain import ensure_helper
    return ensure_helper(_toolchain(data), output_dir)


def _helper_env(data, output_dir: str, build) -> dict[str, str]:
    """The allowlisted go environment (spec §3.6) for the primary build."""
    from .go_toolchain import base_env
    env = base_env(_toolchain(data), output_dir)
    env.update({"GOFLAGS": "-buildvcs=false", "GOWORK": "off", "GOPROXY": "off",
                "GOSUMDB": "off", "CGO_ENABLED": "1" if build.cgo else "0",
                "GOOS": build.goos, "GOARCH": build.goarch})
    return env


def _primary_build(data):
    disc = data.discovery
    builds = list(disc.builds) if disc is not None else []
    for b in builds:
        if b.primary:
            return b
    if builds:
        return builds[0]
    from .go_model import BuildConfig
    return BuildConfig("linux-amd64", "linux", "amd64", (), False, True)


# ---------------------------------------------------------------- the store

def load_entities(path: str, repo: str) -> dict[str, dict]:
    """The records of entities.json, or {} when it is missing, unreadable, of
    another version or of another repo."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {}
    if (not isinstance(doc, dict) or doc.get("version") != ENTITIES_VERSION
            or doc.get("repo") != repo or not isinstance(doc.get("entities"), dict)):
        return {}
    return doc["entities"]


def save_entities(path: str, repo: str, entities: dict[str, dict], *, helper_revision: str) -> None:
    """Write entities.json atomically (C5): version, repo, extractor and
    helper revisions, and the C3 records keyed by relpath."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}.tmp")
    try:
        # json.dumps uses the C encoder; json.dump streams through the pure-Python
        # one, several times slower. The bytes are the same.
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"version": ENTITIES_VERSION, "repo": repo,
                                "extractor_revision": EXTRACTOR_REVISION,
                                "helper_revision": helper_revision,
                                "entities": entities}, ensure_ascii=True))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


# ---------------------------------------------------------------- the phase

def _extract(data, output_dir: str, printer: Printer) -> tuple[dict, dict]:
    """update_entities over the scanned files with the real helper."""
    build = _primary_build(data)
    rev = helper_revision()
    stamps = file_stamps(sorted(data.file_hashes), data.discovery, build, rev)
    scratch = os.path.join(os.path.realpath(os.path.abspath(output_dir)), "go", "tmp")

    def extract(relpaths: list[str]):
        records, errors, summary = run_extract(
            _helper(data, output_dir), data.target_dir, relpaths, build,
            env=_helper_env(data, output_dir, build), timeout=float(config.GO_HELPER_TIMEOUT),
            scratch=scratch)
        return records, errors, summary

    store, report = update_entities(data.extracted_entities, file_hashes=data.file_hashes,
                                    stamps=stamps, extract=extract)
    for rel, digest in report["changed"].items():
        data.file_hashes[rel] = digest
    if report["changed"]:
        printer(f"  [Phase 1.5] {len(report['changed'])} file(s) changed after the scan; "
                "their records follow the current bytes")
    for rel, msg in sorted(report["failed"].items())[:20]:
        printer(f"  [Phase 1.5] cannot extract {rel}: {msg}")
    if len(report["failed"]) > 20:
        printer(f"  [Phase 1.5] ... and {len(report['failed']) - 20} more files without entities")
    data.extracted_entities = store
    return report, {"helper_revision": rev, "build": build.id}


def extract_phase(data, *, out_root: str, output_dir: str, printer: Printer = print) -> dict[str, Any]:
    """Phase 1.5 on PipelineData: reuse or extract, write entities.json,
    set data.extracted_entities and data.timings["phase1b_extract"]."""
    t0 = time.perf_counter()
    path = os.path.join(out_root, "entities.json")
    if not data.extracted_entities:
        data.extracted_entities = load_entities(path, data.repo)
    report, meta = _extract(data, output_dir, printer)
    save_entities(path, data.repo, data.extracted_entities, helper_revision=meta["helper_revision"])
    elapsed = time.perf_counter() - t0
    data.timings["phase1b_extract"] = elapsed
    printer(f"[Phase 1.5] Extracted {report['extracted']} files, reused {report['reused']}, "
            f"{len(report['failed'])} without entities, in {elapsed:.1f}s")
    return {"extracted": report["extracted"], "reused": report["reused"],
            "failed": report["failed"], "seconds": elapsed}


def _hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def refresh_entities(data, *, out_root: str, output_dir: str, printer: Printer = print) -> list[str]:
    """Spec §2.6 rule 4 at the start of Phase 1.6: hash every scanned file
    again, drop deleted files, re-extract when anything changed (updating
    data.file_hashes), and save the store. Raises StaleEntitiesError when a
    file that had a record lost it. Returns the relpaths that changed or had
    no record."""
    had = set(data.extracted_entities)
    kept = []
    for path in data.file_list:
        rel = os.path.relpath(path, data.target_dir).replace(os.sep, "/")
        try:
            data.file_hashes[rel] = _hash_file(path)
        except OSError:
            data.file_hashes.pop(rel, None)
            continue
        kept.append(path)
    data.file_list = kept
    pending = sorted(rel for rel in data.file_hashes
                     if data.extracted_entities.get(rel, {}).get("sha256") != data.file_hashes[rel])
    gone = sorted(set(data.extracted_entities) - set(data.file_hashes))
    if not pending and not gone:
        return []
    printer(f"[Phase 1.6] {len(pending)} file(s) changed since Phase 1 or have no entities, "
            f"{len(gone)} deleted; extracting again")
    report, meta = _extract(data, output_dir, printer)
    save_entities(os.path.join(out_root, "entities.json"), data.repo, data.extracted_entities,
                  helper_revision=meta["helper_revision"])
    failed = sorted(rel for rel in report["failed"] if rel in had)
    if failed:
        raise StaleEntitiesError(failed)
    return pending


# ---------------------------------------------------------------- chunker support

def declaration_spans(record: dict) -> list[dict]:
    """Top-level declaration spans of a FileRecord, sorted by line, for the
    Gemini chunker (spec §4.4): types and functions, without the <package>
    and <init> pseudo-entries, closures and interface methods."""
    spans = [{"qualname": t["qualname"], "kind": t["kind"], "start_line": t["start_line"],
              "end_line": t["end_line"]}
             for t in record.get("types", []) if t.get("kind") != "package"]
    spans += [{"qualname": f["qualname"], "kind": f["member_kind"], "start_line": f["start_line"],
               "end_line": f["end_line"]}
              for f in record.get("funcs", []) if f.get("member_kind") not in ("closure", "var_init")]
    return sorted(spans, key=lambda s: (s["start_line"], s["end_line"], s["qualname"]))
