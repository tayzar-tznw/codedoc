"""Phase 2: node summaries with Gemini (spec §7.2, R11, R20, R30).

One batched call per file carries its source and its declarations (types,
functions, methods and interface methods), each with its graph context read
from graph_rows/*.jsonl: up to 10 callees, 5 callers and the interface
candidates of a function; embeds, implements and methods of a type. A file
above WIKI_FILE_MAX_CHARS or WIKI_FILE_MAX_NODES goes node by node, and its
file summary is then written from the node summaries; so does a file whose
batched answer is cut at MAX_TOKENS (R49). Nodes missing from a batched answer
are asked one by one; a key may carry a package prefix (R49).

Closures and `<init>` get no call. Neither do generated files unless
WIKI_SUMMARIZE_GENERATED is set: their summary is the doc comment plus
"生成コード". The `<package>` pseudo-type takes the file summary in Phase 10.

Results are cached per file in summaries/nodes/<relpath>.json (C7), keyed by
the file's SHA-256, the hash of its graph context, PROMPT_VERSION and the
model (R30). The cache directory is WIKI_SUMMARY_CACHE/<repo>, shareable by
codedoc_go and codedoc_go_leiden, else OUTPUT_DIR/repos/<repo>/summaries; the
per-repo directory always receives this run's records too.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from . import config, pipeline
from .prompts import FILE_FROM_NODES_PROMPT, NODE_BATCH_PROMPT, NODE_SINGLE_PROMPT, PROMPT_VERSION

MAX_CALLEES = 10
MAX_CALLERS = 5
MAX_CANDIDATES = 10
MAX_TYPE_ITEMS = 20       # embeds, implements, implementers and methods listed per type
GENERATED = "生成コード"

_GENERATOR = re.compile(r"^// Code generated (.*?)\s*DO NOT EDIT\.\s*$", re.MULTILINE)
_CACHED_MODES = ("batched", "per-node")


@dataclass
class Decl:
    """A declaration Gemini summarizes."""
    qualname: str
    kind: str
    start_line: int
    end_line: int
    doc: str
    context: list[str]


@dataclass
class FileJob:
    """One scanned file: the declarations Gemini summarizes, the summaries
    made without a call, the cache key parts, and the answers so far."""
    relpath: str
    sha256: str
    package: str
    generated: bool
    decls: list[Decl]
    fixed: dict[str, str]
    fixed_file: str | None = None      # the whole file needs no call (generated)
    context_hash: str = ""
    source: str | None = None
    mode: str | None = None
    file_summary: str | None = None
    nodes: dict[str, str] = field(default_factory=dict)
    package_name: str = ""             # the package clause, a key prefix _answer accepts

    def declaration_lines(self) -> str:
        return "\n".join(f"- {d.qualname}（{d.kind}）" + (f": {'; '.join(d.context)}" if d.context else "")
                         for d in self.decls)

    def large(self) -> bool:
        return (len(self.source or "") > config.WIKI_FILE_MAX_CHARS
                or len(self.decls) > config.WIKI_FILE_MAX_NODES)

    def complete(self) -> bool:
        return bool(self.file_summary) and all(d.qualname in self.nodes for d in self.decls)


class GraphContext:
    """Declarations and graph context of every file, from the graph rows (R20)."""

    def __init__(self, rows: dict[str, list[dict]]):
        self.files = {r.get("path", ""): r for r in rows.get("Files", [])}
        path_of = {r.get("file_id"): r.get("path", "") for r in rows.get("Files", [])}
        self.classes = {r["class_id"]: r for r in rows.get("Classes", [])}
        self.methods = {r["method_id"]: r for r in rows.get("Methods", [])}
        self.method_at = {(r.get("file_id"), r.get("qualname")): r["method_id"]
                          for r in rows.get("Methods", [])}
        self.decls_of: defaultdict[str, list[tuple[str, dict]]] = defaultdict(list)
        for r in rows.get("Classes", []):
            self.decls_of[path_of.get(r.get("file_id"), "")].append(("class", r))
        for r in rows.get("Methods", []):
            self.decls_of[path_of.get(r.get("file_id"), "")].append(("method", r))
        self.callees, self.callers, self.candidates = (defaultdict(set) for _ in range(3))
        for e in rows.get("MethodCalls", []):
            self.callees[e["caller_method"]].add(e["callee_method"])
            self.callers[e["callee_method"]].add(e["caller_method"])
        for e in rows.get("PossiblyCalls", []):
            self.candidates[e["caller_method"]].add(e["callee_method"])
        self.embeds, self.implements, self.implementers, self.class_methods = (
            defaultdict(set) for _ in range(4))
        for e in rows.get("ClassEmbeds", []):
            self.embeds[e["child_class"]].add(e["embedded_class"])
        for e in rows.get("ClassImplements", []):
            self.implements[e["class_id"]].add(e["interface_id"])
            self.implementers[e["interface_id"]].add(e["class_id"])
        for r in rows.get("Methods", []):  # closures belong to their parent's owner, but are no method
            if r.get("class_id") and r.get("member_kind") not in ("closure", "var_init"):
                self.class_methods[r["class_id"]].add(r["method_id"])

    def _method_name(self, mid: str) -> str | None:
        r = self.methods.get(mid)
        return (r.get("fqmn") or r.get("qualname")) if r else None

    def _class_name(self, cid: str) -> str | None:
        r = self.classes.get(cid)
        return (r.get("fqcn") or r.get("qualname")) if r else None

    @staticmethod
    def _listed(names, limit: int) -> str:
        names = sorted({n for n in names if n})
        shown = ", ".join(names[:limit])
        return shown + (f" ほか {len(names) - limit} 件" if len(names) > limit else "")

    def _closure(self, mid: str) -> bool:
        return self.methods.get(mid, {}).get("member_kind") == "closure"

    def _enclosing(self, mid: str) -> str:
        """The function around a closure (`F.func1.func2` → `F`); others themselves."""
        seen: set[str] = set()
        while self._closure(mid) and mid not in seen:
            seen.add(mid)
            row = self.methods[mid]
            parent = self.method_at.get((row.get("file_id"), row.get("qualname", "").rpartition(".")[0]))
            if parent is None:
                break
            mid = parent
        return mid

    def _calls_of(self, mid: str) -> tuple[set[str], set[str]]:
        """Callees and interface candidates of a function, its closures' folded in."""
        callees, candidates, stack, seen = set(), set(), [mid], {mid}
        while stack:
            current = stack.pop()
            candidates |= self.candidates[current]
            for callee in self.callees[current]:
                if self._closure(callee):
                    if callee not in seen:
                        seen.add(callee)
                        stack.append(callee)
                else:
                    callees.add(callee)
        return callees, candidates

    def method_context(self, row: dict) -> list[str]:
        """Callees (≤10), callers (≤5) and interface candidates. Closures get no
        summary, so a call made in a closure counts for the function around it."""
        mid, lines = row["method_id"], []
        callees, candidates = self._calls_of(mid)
        callers = {self._enclosing(c) for c in self.callers[mid]}
        for label, ids, limit in (("呼び出し先", callees, MAX_CALLEES),
                                  ("呼び出し元", callers, MAX_CALLERS),
                                  ("インターフェース経由の候補", candidates, MAX_CANDIDATES)):
            if ids:
                lines.append(f"{label}: {self._listed(map(self._method_name, ids), limit)}")
        if row.get("member_kind") == "interface_method":
            impls = []
            for cid in self.implementers.get(row.get("class_id"), ()):
                same = [self._method_name(m) for m in self.class_methods.get(cid, ())
                        if self.methods[m].get("name") == row.get("name")]
                impls += same or [self._class_name(cid)]
            if impls:
                lines.append(f"実装: {self._listed(impls, MAX_CANDIDATES)}")
        return lines

    def class_context(self, row: dict) -> list[str]:
        cid, lines = row["class_id"], []
        pairs = [("埋め込み", self.embeds[cid]), ("実装するインターフェース", self.implements[cid])]
        if row.get("kind") == "interface":
            pairs.append(("実装する型", self.implementers[cid]))
        for label, ids in pairs:
            if ids:
                lines.append(f"{label}: {self._listed(map(self._class_name, ids), MAX_TYPE_ITEMS)}")
        methods = [self.methods[m].get("name") for m in self.class_methods.get(cid, ())]
        if methods:
            lines.append(f"メソッド: {self._listed(methods, MAX_TYPE_ITEMS)}")
        return lines


def _generator(source: str) -> str:
    match = _GENERATOR.search(source)
    if not match:
        return ""
    text = match.group(1).strip().rstrip(".;").strip()
    return text[3:] if text.startswith("by ") else text


def _generated_summary(doc: str) -> str:
    return f"{doc.strip()}（{GENERATED}）" if doc.strip() else GENERATED


def plan(data, rows: dict[str, list[dict]]) -> list[FileJob]:
    """One job per scanned file, from the graph rows (R20)."""
    ctx = GraphContext(rows)
    summarize_generated = config.WIKI_SUMMARIZE_GENERATED
    jobs = []
    for rel in sorted(data.file_hashes):
        frow = ctx.files.get(rel, {})
        generated = bool(frow.get("generated"))
        deterministic = generated and not summarize_generated
        decls, fixed = [], {}
        entries = sorted(ctx.decls_of.get(rel, []),
                         key=lambda e: (e[1].get("start_line") or 0, e[1].get("qualname") or ""))
        for table, r in entries:
            qualname = r.get("qualname") or r.get("name") or ""
            doc = (r.get("summary") or "").strip()   # Phase 8 writes the doc comment here
            if table == "class":
                if r.get("kind") == "package":
                    continue  # the per-file <package> takes the file summary in Phase 10
                kind, context = r.get("kind") or "type", ctx.class_context(r)
            else:
                kind = r.get("member_kind") or "func"
                if kind == "closure":
                    parent = qualname.rpartition(".")[0] or qualname
                    fixed[qualname] = doc or f"{parent} 内の関数リテラル。"
                    continue
                if kind == "var_init":
                    fixed[qualname] = doc or "このファイルのパッケージレベル変数の初期化式。"
                    continue
                context = ctx.method_context(r)
            if deterministic:
                fixed[qualname] = _generated_summary(doc)
                continue
            decls.append(Decl(qualname, kind, r.get("start_line") or 0, r.get("end_line") or 0,
                              doc, context))
        job = FileJob(rel, data.file_hashes[rel], frow.get("package") or "", generated, decls, fixed,
                      package_name=frow.get("package_name") or "")
        if deterministic:
            path = os.path.join(data.target_dir, rel)
            try:
                gen = _generator(pipeline._read_source_file(path)[:4096])
            except OSError:
                gen = ""
            job.fixed_file = f"{GENERATED}（{gen}）。" if gen else f"{GENERATED}。"
        job.context_hash = hashlib.sha256(job.declaration_lines().encode("utf-8")).hexdigest()
        jobs.append(job)
    return jobs


# ── the cache (C7, R30) ──────────────────────────────────────────

def summary_dirs(data) -> list[str]:
    """Where summaries are read and written: the cache directory first, then
    the per-repo summaries directory the webapp reads (one entry when equal)."""
    dirs = [pipeline.summary_cache_dir(data.repo), os.path.join(pipeline._out_root(), "summaries")]
    return list(dict.fromkeys(os.path.realpath(os.path.abspath(d)) for d in dirs))


def write_json(path: str, payload: Any) -> None:
    """Atomic, readable UTF-8 JSON for the summary artifacts."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _record_path(directory: str, relpath: str) -> str:
    return os.path.join(directory, "nodes", f"{relpath}.json")


def _valid(record: Any, job: FileJob) -> bool:
    return (isinstance(record, dict) and record.get("sha256") == job.sha256
            and record.get("context_hash") == job.context_hash
            and record.get("prompt_version") == PROMPT_VERSION
            and record.get("model") == config.MODEL and record.get("mode") in _CACHED_MODES
            and isinstance(record.get("file"), str) and bool(record["file"])
            and isinstance(record.get("nodes"), dict))


def cached(job: FileJob, dirs: list[str]) -> dict | None:
    for directory in dirs:
        record = pipeline._read_json(_record_path(directory, job.relpath))
        if _valid(record, job):
            return record
    return None


def _store(job: FileJob, dirs: list[str], mode: str, file_summary: str, nodes: dict[str, str]):
    record = {"sha256": job.sha256, "context_hash": job.context_hash, "mode": mode,
              "model": config.MODEL, "prompt_version": PROMPT_VERSION,
              "file": file_summary, "nodes": nodes}
    for directory in dirs:
        write_json(_record_path(directory, job.relpath), record)


# ── calls ────────────────────────────────────────────────────────

def _too_large(exc: BaseException) -> bool:
    text = str(exc)
    return "400" in text and "INVALID_ARGUMENT" in text


def _span(job: FileJob, decl: Decl) -> str:
    lines = (job.source or "").splitlines()
    if 1 <= decl.start_line <= decl.end_line:
        text = "\n".join(lines[decl.start_line - 1:decl.end_line])
    else:
        text = job.source or ""
    return pipeline._truncate(text, pipeline._CHUNK_CHARS)


# (*T), (T), (*example.com/x.T), at the start of a key or after a dot
_METHOD_EXPR = re.compile(r"(?:^|(?<=\.))\(\*?([^()\s]+)\)")


def _package_prefixes(job: FileJob) -> set[str]:
    """What a key may put before a qualname: the import path, each of its
    trailing parts (`iryo/billing`, `billing`) and the package clause."""
    parts = job.package.split("/") if job.package else []
    return {"/".join(parts[i:]) for i in range(len(parts))} | ({job.package_name} - {""})


def _declared(key: str, names: set[str], prefixes: set[str]) -> tuple[str | None, bool]:
    """The declaration a key names that is not a bare qualname (R49), and
    whether only another package's import path names it: the qualname behind
    a package prefix of this file (`gopkg.in/yaml.v3` included), else behind
    any import path, which ends at the first dot after the key's last "/".
    Method expressions `(*T).M` read as `T.M`. No other split counts, so
    `billing.FeeCalculator.Calculate` never names a function `Calculate`."""
    key = _METHOD_EXPR.sub(r"\1", key.strip("`"))
    if key in names:
        return key, False
    slash = key.rfind("/")
    dots = [i for i, c in enumerate(key) if c == "." and i > slash]
    for dot in dots:
        if key[:dot] in prefixes and key[dot + 1:] in names:
            return key[dot + 1:], False
    if slash >= 0 and dots and key[dots[0] + 1:] in names:
        return key[dots[0] + 1:], True
    return None, False


def _answer(parsed: Any, job: FileJob) -> tuple[str | None, dict[str, str]]:
    """The file summary and the node summaries of a batched JSON answer. A key
    is a declaration's qualname or names one as _declared reads it: the exact
    key wins, then this file's package prefix, then another import path, for a
    name still missing. Keys that name no declaration of the file are ignored."""
    if not isinstance(parsed, dict):
        return None, {}
    names = {d.qualname for d in job.decls}
    nodes = parsed.get("nodes") if isinstance(parsed.get("nodes"), dict) else {}
    answers = [(q.strip(), s.strip()) for q, s in nodes.items()
               if isinstance(q, str) and isinstance(s, str) and s.strip()]
    found = {q: s for q, s in answers if q in names}
    prefixes = _package_prefixes(job)
    elsewhere: list[tuple[str, str]] = []
    for key, summary in answers:
        name, other_path = (None, False) if key in names else _declared(key, names, prefixes)
        if name and name not in found:
            if other_path:
                elsewhere.append((name, summary))
            else:
                found[name] = summary
    for name, summary in elsewhere:
        found.setdefault(name, summary)
    file_summary = parsed.get("file")
    return (file_summary.strip() if isinstance(file_summary, str) and file_summary.strip() else None,
            found)


class Summarizer:
    """Runs the jobs with the shared concurrency limit and counts calls."""

    def __init__(self, data, client):
        self.data, self.client = data, client
        self.sem = asyncio.Semaphore(config.GEMINI_CONCURRENCY)
        self.stats = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0}

    async def _call(self, prompt: str, json_answer: bool = False) -> str:
        async with self.sem:
            return (await pipeline._generate(self.client, prompt, stats=self.stats,
                                             json_answer=json_answer)).strip()

    def _package(self, job: FileJob) -> str:
        return job.package or "-"

    async def _batched(self, job: FileJob) -> None:
        source = job.source or ""
        if job.large() and not job.decls:  # nothing to split by: the head of the file
            source = pipeline._truncate(source, config.WIKI_FILE_MAX_CHARS)
        prompt = NODE_BATCH_PROMPT.format(relpath=job.relpath, package=self._package(job),
                                          source=source,
                                          declarations=job.declaration_lines() or "（宣言なし）")
        try:
            answer = await self._call(prompt, json_answer=True)
        except Exception as exc:
            # too large to ask, or an answer cut at MAX_TOKENS even with the larger budget (R49)
            if (_too_large(exc) or isinstance(exc, pipeline.TruncatedAnswer)) and job.decls:
                job.mode = "per-node"
                return
            raise
        try:
            parsed = await pipeline._parse_json_response(answer, client=self.client, stats=self.stats)
        except json.JSONDecodeError:
            parsed = None
        file_summary, nodes = _answer(parsed, job)
        job.file_summary = job.file_summary or file_summary
        job.nodes.update(nodes)
        if parsed is None and job.decls:
            job.mode = "per-node"

    async def _node(self, job: FileJob, decl: Decl) -> str:
        context = ([f"ドキュメントコメント: {decl.doc}"] if decl.doc else []) + decl.context
        return await self._call(NODE_SINGLE_PROMPT.format(
            relpath=job.relpath, package=self._package(job), qualname=decl.qualname,
            kind=decl.kind, context="\n".join(context) or "（グラフ上の関係なし）",
            source=_span(job, decl)))

    async def _file_from_nodes(self, job: FileJob) -> str:
        order = [d.qualname for d in job.decls if d.qualname in job.nodes]
        return await self._call(FILE_FROM_NODES_PROMPT.format(
            relpath=job.relpath, package=self._package(job),
            node_summaries="\n".join(f"- {q}: {job.nodes[q]}" for q in order)))

    async def run(self, job: FileJob) -> None:
        """Fill job.file_summary and job.nodes. Raises when the batched call
        fails, so the job is tried again after a cooldown; a node or file call
        that fails leaves a gap that the next round retries."""
        if job.source is None:
            job.source = pipeline._read_source_file(os.path.join(self.data.target_dir, job.relpath))
        if not job.source.strip():
            job.mode, job.file_summary = job.mode or "batched", job.file_summary or ""
            return
        if job.mode is None:
            job.mode = "per-node" if (job.large() and job.decls) else "batched"
            if job.mode == "batched":
                try:
                    await self._batched(job)
                except BaseException:
                    job.mode = None
                    raise
        elif job.mode == "batched" and not job.file_summary and not job.decls:
            await self._batched(job)
        missing = [d for d in job.decls if d.qualname not in job.nodes]
        answers = await asyncio.gather(*(self._node(job, d) for d in missing),
                                       return_exceptions=True)
        for decl, answer in zip(missing, answers):
            if isinstance(answer, str) and answer:
                job.nodes[decl.qualname] = answer
            else:
                pipeline._log_error(f"[Phase 2] node summary failed: {job.relpath} "
                                    f"{decl.qualname}: {answer!r}")
        if not job.file_summary and job.nodes:
            try:
                job.file_summary = await self._file_from_nodes(job)
            except Exception as exc:
                pipeline._log_error(f"[Phase 2] file summary failed: {job.relpath}: {exc}")


def _apply(data, job: FileJob, file_summary: str, nodes: dict[str, str]) -> None:
    if file_summary:
        data.file_summaries[job.relpath] = file_summary
    data.node_summaries[job.relpath] = {**nodes, **job.fixed}


async def summarize(data, client, rows: dict[str, list[dict]]) -> dict[str, Any]:
    """Phase 2 over every scanned file; returns the counts and the call stats."""
    MAX_PHASE_RETRIES = 3
    RETRY_COOLDOWNS = [30, 90, 180]
    t0 = time.perf_counter()
    jobs = plan(data, rows)
    dirs = summary_dirs(data)
    counts: Counter[str] = Counter()
    pending: list[FileJob] = []
    for job in jobs:
        if job.fixed_file is not None:
            _apply(data, job, job.fixed_file, {})
            write_json(_record_path(dirs[-1], job.relpath), {
                "sha256": job.sha256, "context_hash": job.context_hash, "mode": "generated",
                "model": "", "prompt_version": PROMPT_VERSION, "file": job.fixed_file,
                "nodes": dict(job.fixed)})
            counts["generated"] += 1
            continue
        record = cached(job, dirs)
        if record is not None:
            _apply(data, job, record["file"], record["nodes"])
            if not _valid(pipeline._read_json(_record_path(dirs[-1], job.relpath)), job):
                write_json(_record_path(dirs[-1], job.relpath), record)
            counts["cached"] += 1
            continue
        pending.append(job)

    summarizer = Summarizer(data, client)
    total = len(pending)
    done = 0

    async def run_one(job: FileJob) -> FileJob | None:
        """None when finished; the job when it should be tried again."""
        nonlocal done
        try:
            await summarizer.run(job)
        except OSError as exc:  # the file went away or became unreadable: no retry
            pipeline._log_error(f"[Phase 2] cannot read {job.relpath}: {exc}")
            job.source = ""
        except Exception as exc:
            pipeline._log_error(f"[Phase 2] summary failed: {job.relpath}: {exc}")
        done += 1
        print(f"\r  [Phase 2] {done}/{total} files, {summarizer.stats['calls']} calls"
              f" | {job.relpath[-55:]}          ", end="", flush=True)
        if job.complete() or (job.source is not None and not job.source.strip()):
            return None
        return job

    if pending:
        pipeline._print(f"[Phase 2] Summarizing {total} files ({counts['cached']} cached, "
                        f"{counts['generated']} generated; concurrency {config.GEMINI_CONCURRENCY})")
    retry = [j for j in await asyncio.gather(*(run_one(j) for j in pending)) if j]
    for round_no in range(1, MAX_PHASE_RETRIES + 1):
        if not retry:
            break
        cooldown = RETRY_COOLDOWNS[min(round_no - 1, len(RETRY_COOLDOWNS) - 1)]
        print()
        pipeline._print(f"  [Phase 2] Waiting {cooldown}s, then retry round {round_no}: "
                        f"{len(retry)} files")
        await pipeline._sleep(cooldown)
        done, total = 0, len(retry)
        retry = [j for j in await asyncio.gather(*(run_one(j) for j in retry)) if j]
    if pending:
        print()

    for job in pending:
        if job.source is not None and not job.source.strip():
            counts["empty"] += 1
            continue
        _apply(data, job, job.file_summary or "", job.nodes)
        if job.complete():  # the record also keeps the summaries made without a call
            _store(job, dirs, job.mode or "batched", job.file_summary, {**job.nodes, **job.fixed})
            counts[job.mode or "batched"] += 1
        else:
            counts["incomplete"] += 1
    seconds = time.perf_counter() - t0
    return {**counts, **summarizer.stats, "seconds": seconds}


def estimate(data, rows: dict[str, list[dict]]) -> dict[str, int]:
    """Phase 2 calls this run needs: one per file not cached and not
    generated, or one per declaration plus one for a large file (R11)."""
    dirs = summary_dirs(data)
    calls = files = cached_files = generated = 0
    for job in plan(data, rows):
        if job.fixed_file is not None:
            generated += 1
            continue
        if cached(job, dirs) is not None:
            cached_files += 1
            continue
        try:
            job.source = pipeline._read_source_file(os.path.join(data.target_dir, job.relpath))
        except OSError:
            continue
        if not job.source.strip():
            continue
        files += 1
        calls += len(job.decls) + 1 if (job.large() and job.decls) else 1
    return {"calls": calls, "files": files, "cached": cached_files, "generated": generated}
