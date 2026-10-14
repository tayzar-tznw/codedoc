"""Phase 1.6 gates (spec §5.6, R1, R5).

apply_gates runs on every classified record in a fixed order; the first gate
that settles a record (unresolved, or ambiguous by variant) ends the run:

  G9 syntax-error   the site lies at or after its file's first error line
  no answer         G6 toolchain / R5 load failure / G5 excluded-by-build /
                    no-module / load-error: the helper has no record
  G2 missing-module the target is in a stub package
  G3 incomplete-type an embedded field on the promotion path whose members are
                    unknown, or a package declaring a type on the embedding
                    path (the receiver's included) that may have lost a
                    declaration (R43; the helper's embed_invalid)
  G1 variant        package variants disagree: ambiguous, the targets as candidates
  G7 no-declaration an in-repo target with no node at its name range
  G9 (packages)     a promoted selection or a method value bound by func-alias,
                    when the site's or the target's package directory has a
                    compiled file with syntax errors: a declaration swallowed
                    by the error could be the shallower method that wins at
                    runtime; and any func-alias answer in a file with syntax
                    errors, where a swallowed reassignment breaks the proof
  G10 build-variant the target has build twins: listed, not edged
  G4 iface-offline  candidate search suppressed: no candidates, the target stays,
                    and the record says candidates_partial "suppressed"

G8 (stale) runs once every group has finished, on the whole artifact
(mark_stale), because a file can change at any point of the run. It covers
calls, imports, embeds and implements (C5 ruling 2026-10-14).
"""

from __future__ import annotations

import posixpath
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from . import go_classify


@dataclass
class SiteContext:
    helper: dict | None = None            # the helper's site record; None when there is none
    ctx: Any = None                       # go_classify.ClassifyContext
    line: int = 0                         # the entity site's line
    syntax_errors: Sequence[int] = ()     # error lines of the site's file
    no_answer: str | None = None          # the file-level reason when helper is None
    relpath: str = ""                     # the site's file
    broken_dirs: frozenset = frozenset()  # directories with a file that has syntax errors


def unresolved(record: dict, reason: str) -> dict:
    """The record, unresolved with *reason* and no targets."""
    out = dict(record)
    out.update(status="unresolved", via="none", target=None, static_target=None, external=None,
               candidates=[], open_world=False, build_variants=[], reason=reason)
    out.pop("candidates_partial", None)
    return out


def no_answer_record(build: str | None) -> dict:
    """The record of a site the helper gave no record for; the no-answer gate settles it."""
    return go_classify.blank_record(build)


def no_answer_reason(*, unit: str | None, group_status: str | None, group_reason: str | None,
                     build_included: bool) -> str:
    """Why a non-range site has no helper record: its file was in no load."""
    if unit is None:
        return "no-module"
    if group_status == "toolchain":
        return "toolchain"                          # G6
    if group_reason:
        return group_reason                         # R5
    if not build_included:
        return "excluded-by-build"                  # G5
    return "load-error"


def load_failure_reason(diagnostics: str) -> str:
    """R5: a failed group load is a toolchain problem when the go command says
    a newer go is required, else a load error."""
    return "toolchain" if "requires go >=" in (diagnostics or "") else "load-error"


# ---------------------------------------------------------------- the gates

def _settle(record: dict, reason: str) -> dict:
    out = unresolved(record, reason)
    out["_final"] = True
    return out


def _g9_syntax_error(record, sc: SiteContext):
    if sc.syntax_errors and sc.line >= min(sc.syntax_errors):
        return _settle(record, "syntax-error")
    return record


def _no_answer(record, sc: SiteContext):
    if sc.helper is None:
        return _settle(record, sc.no_answer or "load-error")
    return record


def _g2_missing_module(record, sc: SiteContext):
    return _settle(record, "missing-module") if sc.helper.get("stub") else record


def _g3_incomplete_type(record, sc: SiteContext):
    return _settle(record, "incomplete-type") if sc.helper.get("embed_invalid") else record


def _g1_variant(record, sc: SiteContext):
    variants = sc.helper.get("variants") or []
    if not variants:
        return record
    out = go_classify.blank_record(record.get("build"))
    out.update(status="ambiguous", via="types", reason="variant",
               candidates=go_classify.variant_candidates(variants, sc.ctx), _final=True)
    return out


def _g7_no_declaration(record, sc: SiteContext):
    if record.get(go_classify._INTERNAL_UNMAPPED):
        return _settle(record, "no-declaration")
    return record


def _g9_broken_package(record, sc: SiteContext):
    h = sc.helper
    if h.get("class") == "value" and h.get("alias") and sc.syntax_errors:
        return _settle(record, "syntax-error")
    if not sc.broken_dirs or record.get("status") not in ("resolved", "external"):
        return record
    alias = h.get("alias") or {}
    if not (h.get("promoted_depth") or (h.get("class") == "value" and alias.get("obj") == "method")):
        return record
    t = record.get("target") or {}
    dirs = {posixpath.dirname(sc.relpath)}
    if t and "repo" not in t:
        dirs.add(posixpath.dirname(t.get("relpath") or ""))
    if dirs & set(sc.broken_dirs):
        return _settle(record, "syntax-error")
    return record


def _g10_build_variant(record, sc: SiteContext):
    t = record.get("target")
    if record.get("status") != "resolved" or not t or "repo" in t or sc.ctx is None:
        return record
    node = sc.ctx.decls.named(t["relpath"], t["qualname"], t["kind"])
    if node is not None:
        record["build_variants"] = [n.target() for n in sc.ctx.decls.twins(node)]
    return record


def _g4_iface_offline(record, sc: SiteContext):
    info = sc.helper.get("iface") or {}
    if info.get("suppressed"):
        record["candidates"] = []
        if record.get("status") in ("resolved", "external", "ambiguous"):
            record["candidates_partial"] = go_classify.PARTIAL_SUPPRESSED
    return record


_GATES = (_g9_syntax_error, _no_answer, _g2_missing_module, _g3_incomplete_type, _g1_variant,
          _g7_no_declaration, _g9_broken_package, _g10_build_variant, _g4_iface_offline)


def apply_gates(record: dict, site_ctx: SiteContext) -> dict:
    """G9, no answer, G2, G3, G1, G7, G9 (packages), G10, G4 in order (see the
    module docstring)."""
    out = dict(record)
    for gate in _GATES:
        out = gate(out, site_ctx)
        if out.get("_final"):
            break
    return {k: v for k, v in out.items() if not k.startswith("_")}


# ---------------------------------------------------------------- G8

def _points_into(record: dict, stale: set[str]) -> bool:
    refs = [record.get("target"), record.get("static_target"), *record.get("candidates", []),
            *record.get("build_variants", [])]
    return any(t and "repo" not in t and t.get("relpath") in stale for t in refs)


def _in_repo_end(t: dict | None, stale: set[str]) -> bool:
    return bool(t) and "repo" not in t and t.get("relpath") in stale


def _stale_end(record: dict, end: str) -> dict:
    out = dict(record)
    out.update(status="unresolved", external=None, reason="stale")
    out[end] = None
    return out


def mark_stale(files: dict[str, dict], stale: set[str]) -> int:
    """G8 over the artifact's files. Every call, import, embed and implements
    record of a file in *stale* becomes unresolved (stale); so does every call
    pointing into one (target, candidates, build variants), and every embed or
    implements record whose other end lies in one (peer targets excluded).
    Returns how many records changed."""
    if not stale:
        return 0
    n = 0
    for rel, entry in files.items():
        own = rel in stale
        calls = entry.get("calls", [])
        for i, rec in enumerate(calls):
            if own or _points_into(rec, stale):
                calls[i] = unresolved(rec, "stale")
                n += 1
        imports = entry.get("imports", [])
        for i, rec in enumerate(imports):
            if own:
                imports[i] = _stale_end(rec, "target")
                n += 1
        for key, end in (("embeds", "embedded"), ("implements", "iface")):
            recs = entry.get(key, [])
            for i, rec in enumerate(recs):
                if own or _in_repo_end(rec.get(end), stale) or _in_repo_end(rec.get("type"), stale):
                    recs[i] = _stale_end(rec, end)
                    n += 1
    return n
