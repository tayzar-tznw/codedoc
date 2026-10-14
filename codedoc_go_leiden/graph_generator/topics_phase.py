"""The leiden-only phases of codedoc_go_leiden (leiden spec §2–§6).

The modules shared with codedoc_go call these functions from their
LEIDEN-HOOK lines (scripts/leiden_hooks.py):

| Function | Called from | Does |
|---|---|---|
| `preflight_checks` | `pipeline.preflight` | the Leiden backend row (an error in every mode when it is missing, never a fallback) and the topic settings row |
| `phase4a_communities` | `run_graph_track`, after Phase 1.6 (L-R24) | communities from the builder rows: `topics/communities.json`, `topics/projection.jsonl` |
| `communities_hash` | the graph checkpoint | the hash of communities.json without its carry-over bookkeeping |
| `write_graph_topics` | `run_graph_track`, after Phase 9 | the five topic tables: graph_rows/ always, Spanner delete then batched insert |
| `wiki_problems` | `prepare_wiki` | --wiki needs the communities of the finished graph |
| `estimate_calls` | `estimate_wiki_calls` | 4b batches and Phase 5 pages |
| `phase4b_naming` | `run_wiki_track`, instead of Phase 4 | `topics/names.json` |
| `phase5_reports` | `run_wiki_track`, instead of Phase 5 | `topics/<id>.md`, `pages.json`, `metrics.json`, the topic tree |
| `page_names` | `phase6_index` | keeps the subtopic pages |
| `wiki_topic_rows`, `replace_topic_tables` | `phase10_write_back` | names and summaries into the topic tables |
| `add_compare_parser`, `cmd_compare_topics` | `__main__` | `compare-topics` (§6) |
| `phase_labels` | `__main__` | the timing report's labels |

The topics package (graspologic-native, networkx) is imported inside the
functions, so the CLI and the preflight row work without it.

A topic write is not atomic: ClassBelongsToTopic can reach ~20k rows of 4
cells, above Spanner's 80,000 mutations per commit, so the repo's rows are
deleted and the new ones inserted in batches of `_TOPIC_BATCH` rows. Topics
are derived data; an interrupted write is repaired by the next run (R10).
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import math
import os
import random
import re
import sys
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from . import config, pipeline
from .lazy_import import LazyModule

spanner = LazyModule("google.cloud.spanner")  # imported on first use, as in pipeline.py

_TOPIC_BATCH = 2000  # rows per Spanner commit for the topic tables
PAGES_VERSION = 1
_INTRO = "## はじめに"
_INSTALL_FIX = ("uv pip install --python .venv/bin/python -r requirements.txt "
                "(graspologic-native==1.3.1, networkx==3.7)")
_SETTINGS_FIX = ("set TOPIC_MIN, TOPIC_MAX, SUBTOPIC_MIN_ATOMS, TOPIC_PROFILE and TOPIC_SEEDS "
                 "in .env, or unset them for the defaults")


# ===================================================================
# State of the current repo
# ===================================================================


@dataclass
class _Topics:
    """One repo's communities and projection, for the later phases of a run."""
    out_root: str
    communities: Any = None                    # topics.community.Communities; None when 4a stopped
    projection: Any = None                     # topics.graph_builder.Projection
    nodes: dict = field(default_factory=dict)  # node rows: file_topics joins files to topics
    problem: str = ""                          # why 4a stopped
    rows: dict | None = None                   # the topic table rows of the current write


_state: _Topics | None = None  # one repo at a time: the CLI runs repos one after another


def _out() -> str:
    return os.path.abspath(pipeline._out_root())


def _topics_dir() -> str:
    return os.path.join(pipeline._out_root(), "topics")


def _current() -> _Topics | None:
    return _state if _state is not None and _state.out_root == _out() else None


def _write_doc(path: str, doc: Any) -> None:
    """UTF-8 JSON, so the Japanese names stay readable; written atomically."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _split_rows(rows: Mapping[str, list[dict]]) -> tuple[dict, dict]:
    """(node tables, edge tables) of graph rows."""
    from .setup_spanner_graph import NODE_SPEC
    return ({t: r for t, r in rows.items() if t in NODE_SPEC},
            {t: r for t, r in rows.items() if t not in NODE_SPEC})


# ===================================================================
# Settings and preflight
# ===================================================================


def _raw(name: str) -> str:
    return str(getattr(config, name)).strip()


def _int_setting(name: str) -> int:
    try:
        return int(_raw(name))
    except ValueError:
        raise ValueError(f"{name} must be an integer, not {_raw(name)!r}") from None


def topic_params(repo: str):
    """community.Params from the topic settings. A bad setting raises
    ValueError naming the key, its value and the allowed form."""
    from .topics import community, graph_builder
    try:
        graph_builder.parse_profile(_raw("TOPIC_PROFILE"))
    except ValueError:
        raise ValueError(f"TOPIC_PROFILE must be strict or a '+' combination of "
                         f"{', '.join(graph_builder.PROFILE_OPTIONS)}, not {_raw('TOPIC_PROFILE')!r}") from None
    try:
        seeds = tuple(int(part) for part in _raw("TOPIC_SEEDS").replace(" ", "").split(",") if part)
    except ValueError:
        seeds = ()
    # graspologic-native takes a seed as an unsigned 64-bit integer
    if not seeds or len(set(seeds)) != len(seeds) or not all(0 <= s < 2 ** 64 for s in seeds):
        raise ValueError("TOPIC_SEEDS must be distinct comma-separated integers from 0 to 2^64-1, "
                         f"not {_raw('TOPIC_SEEDS')!r}")
    low, high = _int_setting("TOPIC_MIN"), _int_setting("TOPIC_MAX")
    if not 1 <= low <= high:
        raise ValueError("TOPIC_MIN and TOPIC_MAX must be integers with 1 <= TOPIC_MIN <= TOPIC_MAX, "
                         f"not {_raw('TOPIC_MIN')!r} and {_raw('TOPIC_MAX')!r}")
    subtopic = _int_setting("SUBTOPIC_MIN_ATOMS")
    if subtopic < 1:
        raise ValueError(f"SUBTOPIC_MIN_ATOMS must be an integer >= 1, not {_raw('SUBTOPIC_MIN_ATOMS')!r}")
    return community.Params.of({"repo": repo, "id_prefix": config.ID_PREFIX, "seeds": seeds,
                                "band": (low, high), "subtopic_min_atoms": subtopic})


def preflight_checks(mode: str) -> list:
    """The Leiden rows of the preflight table, the same in every mode, doctor
    included: a backend that cannot be imported is an error (Phase 4a never
    falls back to another algorithm), and so is a bad topic setting, named
    with its value and the allowed form."""
    Check = pipeline.Check
    try:
        for module in ("graspologic_native", "networkx"):
            importlib.import_module(module)
        from .topics import community, graph_builder
    except ImportError as exc:
        missing = exc.name or str(exc)
        return [Check("leiden backend", "error",
                      f"{missing} cannot be imported ({exc}); Phase 4a needs it and never falls back "
                      "to another algorithm", _INSTALL_FIX)]
    networkx = sys.modules["networkx"]
    version = community.backend_version()
    if version == community.TESTED_BACKEND_VERSION:
        rows = [Check("leiden backend", "ok", f"graspologic-native {version}, networkx {networkx.__version__}")]
    else:
        rows = [Check("leiden backend", "warn",
                      f"graspologic-native {version}; this variant is tested with "
                      f"{community.TESTED_BACKEND_VERSION}", _INSTALL_FIX)]
    try:
        p = topic_params("")
        profile, _options = graph_builder.parse_profile(_raw("TOPIC_PROFILE"))
        rows.append(Check("topic settings", "ok",
                          f"band {p.band[0]}-{p.band[1]} topics, subtopics above "
                          f"{p.subtopic_min_atoms} primary atoms, profile {profile}, "
                          f"seeds {','.join(map(str, p.seeds))}"))
    except ValueError as exc:
        rows.append(Check("topic settings", "error", str(exc), _SETTINGS_FIX))
    return rows


# ===================================================================
# Phase 4a: communities (graph track, after Phase 1.6, L-R24)
# ===================================================================


def _groups(data) -> dict | None:
    """L-R39: each resolution group's status and files (topics touching a
    failed group are marked partial); None without discovery."""
    disc = data.discovery
    if disc is None or not hasattr(disc, "unit_of_file"):
        return None
    status = (data.resolutions or {}).get("groups") or {}
    return {g.id: {"status": (status.get(g.id) or {}).get("status", g.status),
                   "files": sorted(rel for rel, key in disc.unit_of_file.items() if key in g.modules)}
            for g in disc.groups}


def phase4a_communities(data) -> None:
    """Phase 4a (leiden spec §3): Leiden communities on the builder rows,
    before Phase 8 (L-R24). A bad setting, a backend failure or a broken
    invariant stops 4a, never the graph track (L-R8): the graph is written
    without topics, the checkpoint stores no communities hash, and --wiki
    refuses until a --graph run produces communities."""
    global _state
    from .topics import community, graph_builder
    t0 = time.perf_counter()
    topics_dir = _topics_dir()
    nodes = pipeline.build_node_rows(data)
    edges = pipeline.derive_edge_rows(data, pipeline._node_index(data))
    _state = state = _Topics(out_root=_out(), nodes=dict(nodes))

    def stop(exc: BaseException) -> None:
        state.problem = f"{type(exc).__name__}: {exc}"
        data.timings["phase4a_communities"] = time.perf_counter() - t0
        pipeline._log_error(f"[Phase 4a] {state.problem}")
        pipeline._print(f"[Phase 4a] Stopped: {state.problem}. The graph is written without topics, and "
                        "--wiki refuses until a --graph run produces communities")

    try:
        params = topic_params(data.repo)
    except ValueError as exc:
        return stop(exc)
    projection = graph_builder.build_projection(nodes, edges, _raw("TOPIC_PROFILE"))
    previous = pipeline._read_json(os.path.join(topics_dir, "communities.json"))
    try:
        communities = community.detect(projection, params, groups=_groups(data),
                                       previous=previous if isinstance(previous, dict) else None)
        problems = community.check_integrity(communities, projection)
        if problems:
            raise community.CommunityError(f"{len(problems)} integrity problems: " + "; ".join(problems[:5])
                                           + (" ..." if len(problems) > 5 else ""))
    except (community.CommunityError, community.BackendMissingError) as exc:
        return stop(exc)
    communities.write(os.path.join(topics_dir, "communities.json"))
    graph_builder.write_ledger(projection, os.path.join(topics_dir, "projection.jsonl"))
    state.communities, state.projection = communities, projection
    elapsed = time.perf_counter() - t0
    data.timings["phase4a_communities"] = elapsed
    pipeline._print(f"[Phase 4a] {community.summary_line(communities)}; {len(projection.atoms)} atoms, "
                    f"{len(projection.edges)} edges, profile {projection.profile}, in {elapsed:.1f}s")
    partial = (communities.data.get("coverage") or {}).get("partial_topics") or []
    if partial:
        pipeline._print(f"[Phase 4a] warning: {len(partial)} topics hold files of a group whose "
                        "resolution failed; they are marked partial")
    for note in communities.data.get("warnings") or []:
        pipeline._print(f"[Phase 4a] warning: {note}")


def document_hash(doc: Mapping) -> str:
    """SHA-256 of a communities.json document without its carry-over
    bookkeeping (`carry_over`, each topic's `inherited_from` and `jaccard`),
    which differs between a first and a second run on the same input.
    Everything the topic tables are built from is covered."""
    kept = {k: v for k, v in doc.items() if k not in ("carry_over", "levels")}
    kept["levels"] = {lv: {tid: {k: v for k, v in t.items() if k not in ("inherited_from", "jaccard")}
                           for tid, t in topics.items()}
                      for lv, topics in (doc.get("levels") or {}).items()}
    return hashlib.sha256(json.dumps(kept, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()


def communities_hash(data) -> str | None:
    """The graph checkpoint's `communities_hash` (C7): this run's communities,
    or None when 4a stopped or did not run."""
    state = _current()
    return document_hash(state.communities.data) if state and state.communities is not None else None


# ===================================================================
# The topic tables (after Phase 9, and Phase 10)
# ===================================================================


def _carried(communities) -> tuple[dict, dict]:
    """Names and page summaries of the topics whose ID, and so whose
    membership, is unchanged since the last --wiki: every names.json entry,
    the deduplicated hub label 4b fell back to included, so a --graph run
    writes the name Phase 10 embedded and the cached embedding still matches.
    4b asks again for a hub-labeled topic, whatever --graph carried."""
    from .topics import community
    current = set(community.all_topics(communities))
    doc = pipeline._read_json(os.path.join(_topics_dir(), "names.json"))
    entries = doc.get("names", {}) if isinstance(doc, dict) else {}
    names = {tid: e for tid, e in entries.items()
             if tid in current and isinstance(e, dict) and e.get("name")}
    summaries = {}
    for tid in sorted(current):
        text = _read_text(os.path.join(_topics_dir(), f"{tid}.md"))
        if text and text.strip():
            summaries[tid] = text[:pipeline._SUMMARY_LIMIT]
    return names, summaries


def _restore_embeddings(db, data, topic_rows: list[dict]) -> int:
    """Embeddings of carried topics from the local cache (no API call)."""
    path = os.path.join(pipeline._out_root(), "embeddings", "cache.sqlite")
    items = pipeline._embedding_items(data, {}, {}, topic_rows)["Topics"]
    if not items or not os.path.isfile(path):
        return 0
    cache = pipeline._EmbeddingCache(path)
    try:
        keys = {tid: pipeline._EmbeddingCache.key(text) for tid, text in items}
        hits = cache.get_many(list(keys.values()))
    finally:
        cache.close()
    rows = [[tid, hits[key]] for tid, key in keys.items() if key in hits]
    if rows:
        pipeline._batch_update(db, "Topics", ["topic_id", "embedding"], rows,
                               batch_size=pipeline._VECTOR_BATCH)
    return len(rows)


def write_graph_topics(data) -> None:
    """The five topic tables after Phase 9 (leiden spec §4, R10, R20), with
    hub labels except the names, summaries and cached embeddings of
    unchanged topics: graph_rows/<Table>.jsonl always and, unless
    --no-spanner, the repo's rows replaced in Spanner. When 4a stopped, the
    tables are emptied for this repo."""
    global _state
    from .topics import community
    t0 = time.perf_counter()
    state = _current()
    if state is None:
        _state = state = _Topics(out_root=_out(), problem="Phase 4a did not run")
    carried = restored = 0
    if state.communities is None:
        rows = {table: [] for table in community.TOPIC_TABLES}
    else:
        names, summaries = _carried(state.communities)
        rows = community.topic_rows(state.communities, state.nodes, names=names, summaries=summaries)
        carried = len(names)
    state.rows = rows
    for table in community.TOPIC_TABLES:
        pipeline._write_jsonl(table, rows[table])
    if not data.no_spanner:
        db = pipeline._get_spanner_db()
        replace_topic_tables(db, data, "Phase 9")
        restored = _restore_embeddings(db, data, rows["Topics"])
    counts = {table: len(rows[table]) for table in community.TOPIC_TABLES}
    data.graph_counts.update(counts)
    elapsed = time.perf_counter() - t0
    data.timings["phase9_write_topics"] = elapsed
    kept = [f"{carried} topics keep their names from the last --wiki"] if carried else []
    kept += [f"{restored} cached embeddings restored"] if restored else []
    pipeline._print(f"[Phase 9] Wrote {sum(counts.values())} topic rows to {pipeline._destination(data)} "
                    f"in {elapsed:.1f}s: {pipeline._counts_text(counts)}"
                    + (f"; {', '.join(kept)}" if kept else ""))


_REPO_TOPICS = "SELECT topic_id FROM Topics WHERE repo = @repo"
# Edge tables first: their queries find the rows through the repo's Topics.
_KEY_QUERIES = (
    ("TopicContainsTopic", f"parent_topic IN ({_REPO_TOPICS}) OR child_topic IN ({_REPO_TOPICS})"),
    ("TopicDependsOn", f"source_topic IN ({_REPO_TOPICS}) OR target_topic IN ({_REPO_TOPICS})"),
    ("FileBelongsToTopic", "file_id IN (SELECT file_id FROM Files WHERE repo = @repo) "
                           f"OR topic_id IN ({_REPO_TOPICS})"),
    ("ClassBelongsToTopic", "class_id IN (SELECT class_id FROM Classes WHERE repo = @repo) "
                            f"OR topic_id IN ({_REPO_TOPICS})"),
)


def _batch_delete(db, table: str, keys: list[str]) -> None:
    """Delete rows by key, `_TOPIC_BATCH` keys per commit, with the retries
    of pipeline._batch_mutate."""
    from google.api_core.exceptions import Aborted, DeadlineExceeded, ServiceUnavailable
    for i in range(0, len(keys), _TOPIC_BATCH):
        keyset = spanner.KeySet(keys=[[k] for k in keys[i:i + _TOPIC_BATCH]])
        for attempt in range(5):
            try:
                with db.batch() as batch:
                    batch.delete(table, keyset)
                break
            except (Aborted, ServiceUnavailable, DeadlineExceeded) as exc:
                if attempt == 4:
                    pipeline._log_error(f"[Spanner] {table} delete batch {i} failed after 5 attempts: {exc}")
                    raise
                pipeline._sync_sleep(2 ** attempt + random.uniform(0, 1))


def _own_key_shape() -> str:
    """The exact shape of this graph's keys, `<ID_PREFIX>_<16 hex>`, as both
    RE2 (Spanner's REGEXP_CONTAINS) and Python read it. A prefix match would
    also take the keys of a side-by-side graph whose ID_PREFIX extends this
    one ("a" and "a_x"). Same pattern as pipeline._own_key_pattern."""
    return f"^{re.escape(config.ID_PREFIX)}_[0-9a-f]{{16}}$"


def _delete_topic_rows(db, repo: str) -> dict[str, int]:
    """Delete this repo's rows of the five topic tables for the current
    ID_PREFIX (R10), edges first; returns the count per table."""
    params = {"repo": repo, "shape": _own_key_shape()}
    param_types = {"repo": spanner.param_types.STRING, "shape": spanner.param_types.STRING}
    queries = [(table, f"SELECT edge_id FROM {table} WHERE REGEXP_CONTAINS(edge_id, @shape) AND ({where})")
               for table, where in _KEY_QUERIES]
    queries.append(("Topics", "SELECT topic_id FROM Topics WHERE repo = @repo AND REGEXP_CONTAINS(topic_id, @shape)"))
    deleted = {}
    for table, sql in queries:
        with db.snapshot() as snapshot:  # keys are STRING(MAX); anything else is not a row key
            keys = [row[0] for row in snapshot.execute_sql(sql, params=params, param_types=param_types)
                    if isinstance(row[0], str)]
        _batch_delete(db, table, keys)
        deleted[table] = len(keys)
    return deleted


def replace_topic_tables(db, data, label: str) -> None:
    """Replace this repo's topic rows in Spanner with the rows of the current
    write (`write_graph_topics` or `wiki_topic_rows`): delete, then insert
    Topics before their edges, `_TOPIC_BATCH` rows per commit. Not atomic."""
    from .topics import community
    state = _current()
    rows = state.rows if state is not None and state.rows is not None else {}
    try:
        deleted = _delete_topic_rows(db, data.repo)
        for table in community.TOPIC_TABLES:
            if rows.get(table):
                columns = pipeline._columns(table)
                pipeline._batch_mutate(db, "insert_or_update", table, columns,
                                       [pipeline._row(columns, r) for r in rows[table]], 5, _TOPIC_BATCH)
    except Exception as exc:
        pipeline._log_error(f"[{label}] the topic tables of repo {data.repo} are partly written ({exc}); "
                            "the next run repairs them")
        raise
    pipeline._print(f"  [{label}] Replaced the topic tables of repo {data.repo}: {sum(deleted.values())} rows "
                    f"deleted, {sum(len(r) for r in rows.values())} inserted, up to {_TOPIC_BATCH} rows per "
                    "commit (not atomic: an interrupted run leaves them partial until the next run)")


# ===================================================================
# Wiki track: precondition, 4b names, Phase 5 reports, Phases 6 and 10
# ===================================================================


def wiki_problems(data, cp: Mapping) -> list[str]:
    """The --wiki precondition of leiden spec §2: the finished graph produced
    communities, topics/communities.json is the one the checkpoint recorded,
    and graph_rows/ give the projection it was built from. Loads them for
    the wiki phases."""
    global _state
    from .topics import community, graph_builder
    command = pipeline._graph_command(data.target_dir)
    path = os.path.join(_topics_dir(), "communities.json")
    expected = cp.get("communities_hash")
    if not expected:
        return [f"The last --graph run of repo {data.repo!r} has no Leiden communities (Phase 4a stopped; "
                f"the reason is in {os.path.join(pipeline._out_root(), 'error_log.txt')}), so there are "
                f"no topics to name. Fix the cause and run {command} again."]
    doc = pipeline._read_json(path)
    if not isinstance(doc, dict):
        return [f"{path} is missing or unreadable: run {command} again."]
    if document_hash(doc) != expected:
        return [f"{path} does not match graph_checkpoint.json: run {command} again."]
    nodes, edges = _split_rows(pipeline._graph_rows(data))
    projection = graph_builder.build_projection(nodes, edges, doc.get("profile") or "strict")
    if projection.hash != doc.get("projection_hash"):
        return [f"graph_rows/ do not give the projection {path} was built from: run {command} again."]
    _state = _Topics(out_root=_out(), communities=community.Communities(data=doc), projection=projection,
                     nodes=nodes)
    return []


def _wiki_state(data) -> _Topics:
    """This repo's communities and projection: those prepare_wiki loaded, or
    loaded again when another repo's run replaced them."""
    state = _current()
    if state is not None and state.communities is not None and state.projection is not None:
        return state
    cp = pipeline._read_json(os.path.join(pipeline._out_root(), pipeline._CHECKPOINT))
    problems = wiki_problems(data, cp if isinstance(cp, dict) else {})
    if problems:
        raise pipeline.WikiPreconditionError("\n\n".join(problems))
    return _state


def estimate_calls(data) -> dict[str, int]:
    """Upper bounds on the calls the phases make: 4b one per naming batch of a
    level plus at most one re-ask (a cut answer has had its one retry with a
    larger budget and falls back to hub labels without a re-ask, R49), Phase 5
    one per topic page; 「未分類」 is never sent. As in codedoc_go's estimate,
    retries inside pipeline._generate and Phase 5's retry rounds are extra."""
    from .topics import community, naming
    try:
        state = _wiki_state(data)
    except pipeline.WikiPreconditionError:
        return {}
    topics = community.all_topics(state.communities)
    per_level = Counter(t["level"] for t in topics.values())
    batches = sum(math.ceil(n / naming.BATCH_SIZE) for n in per_level.values())
    return {"phase4": 2 * batches, "phase5": len(topics)}


def _class_summaries(data) -> dict[str, str]:
    """class_id → its Phase 2 summary (the `<package>` pseudo-type: its file's)."""
    return dict(pipeline._summary_updates(data, pipeline._graph_rows(data))["Classes"])


def _named_matches(communities, named: Mapping) -> dict[str, str]:
    """topic → the topic of the communities the last --wiki named that it
    matches at Jaccard >= the carry-over threshold (community.carry_over,
    one to one per level)."""
    from .topics import community
    doc = communities.data
    levels = {lv: {tid: set(community.primary_members(t)) for tid, t in ts.items()
                   if not t.get("carried") and not t.get("unassigned")}
              for lv, ts in (doc.get("levels") or {}).items()}
    matches, _summary = community.carry_over(levels, named, doc["params"]["jaccard"])
    return {tid: old for tid, (old, _j) in matches.items()}


async def phase4b_naming(data, client) -> None:
    """Phase 4b (leiden spec §5.1): names for the communities, batched, from
    their structural evidence. naming.name_topics calls the model
    synchronously, so it runs in a worker thread and each call is scheduled
    on this event loop (pipeline._generate keeps its throttle, retries and
    token counts). A topic whose ID is in the previous names.json keeps its
    name (same ID, same members); one that matches a topic of the communities
    that names.json named (named_communities.json) inherits that name, however
    many --graph runs came between (L-R38)."""
    from .topics import naming
    t0 = time.perf_counter()
    pipeline._throttle.reset()
    state = _wiki_state(data)
    entries = naming.naming_inputs(state.communities, state.projection, _class_summaries(data))
    path = os.path.join(_topics_dir(), "names.json")
    named_path = os.path.join(_topics_dir(), "named_communities.json")
    previous = pipeline._read_json(path)
    previous = previous if isinstance(previous, dict) else {}
    known = previous.get("names") or {}
    named = pipeline._read_json(named_path)
    matches = _named_matches(state.communities, named) if isinstance(named, dict) and known else None
    for e in entries:
        if isinstance(known.get(e["topic_id"]), dict):
            e["inherited_from"] = e["topic_id"]
        elif matches is not None:  # else: an older run's names.json, with communities.json's carry-over
            e["inherited_from"] = matches.get(e["topic_id"])
    stats: dict[str, int] = {}
    loop = asyncio.get_running_loop()

    def call(prompt: str) -> str:  # the answer is a JSON object: bounded thinking (R49)
        try:
            return asyncio.run_coroutine_threadsafe(
                pipeline._generate(client, prompt, stats=stats, json_answer=True), loop).result()
        except pipeline.TruncatedAnswer as exc:  # already retried with a larger budget
            raise naming.AnswerCut(str(exc)) from exc

    doc = await asyncio.to_thread(naming.name_topics, entries, call, previous)
    _write_doc(path, doc)
    _write_doc(named_path, state.communities.data)  # the topics these names belong to
    elapsed = time.perf_counter() - t0
    data.topic_stats["discovery"] = pipeline._step_stats(stats, elapsed)
    data.timings["phase4b_naming"] = elapsed
    pipeline._record_usage(data, "phase4b_naming", stats)
    s = doc["stats"]
    for error in s["errors"]:
        pipeline._log_error(f"[Phase 4b] naming call failed: {error}")
    pipeline._print(f"[Phase 4b] Named {len(doc['names'])} topics in {pipeline._fmt_elapsed(elapsed)}: "
                    f"{s['llm']} by Gemini, {s['inherited']} inherited, {s['fallbacks']} hub labels; "
                    f"{stats.get('calls', 0)} Gemini calls"
                    + (f"; {len(s['errors'])} calls failed (see error_log.txt)" if s["errors"] else ""))


def _intro(page: str) -> str:
    """The first prose line under 「はじめに」 (else the page's first): a
    child's summary in its parent's evidence."""
    lines = page.splitlines()
    start = next((i + 1 for i, line in enumerate(lines) if line.strip() == _INTRO), 0)
    for line in lines[start:]:
        text = line.strip()
        if text and not text.startswith(("#", "```", "|", "---")):
            return text
    return ""


async def phase5_reports(data, client) -> None:
    """Phase 5 (leiden spec §5.2): one page per topic from its bounded
    evidence package, level 2 before level 1 (a parent's evidence holds its
    children's summaries). The LLM may annotate the Mermaid scaffolds, never
    add edges (`enforce_scaffolds`); every page ends with its grounding check.
    A page is reused while its prompt and the model are unchanged, so a
    changed membership (a new topic ID) always gets a new page (L-R38).
    Writes pages.json, metrics.json and the topic tree (data.topics)."""
    from .topics import community, compare, reports
    MAX_PHASE_RETRIES = 3
    RETRY_COOLDOWNS = [30, 90, 180]
    t0 = time.perf_counter()
    pipeline._throttle.reset()
    state = _wiki_state(data)
    comm, projection = state.communities, state.projection
    topics = community.all_topics(comm)
    topics_dir = _topics_dir()
    os.makedirs(topics_dir, exist_ok=True)
    names_doc = pipeline._read_json(os.path.join(topics_dir, "names.json"))
    names = {tid: e.get("name") for tid, e in ((names_doc or {}).get("names") or {}).items()
             if isinstance(e, dict) and e.get("name")} if isinstance(names_doc, dict) else {}
    summaries = _class_summaries(data)
    name_set = reports.build_name_set(_split_rows(pipeline._graph_rows(data))[0])
    # pages quote topic names, which are no code identifiers (as compare-topics scores them)
    topic_names = compare.topic_names(None, comm, names_doc if isinstance(names_doc, dict) else None)
    index = pipeline._read_json(os.path.join(topics_dir, "pages.json"))
    old = (index.get("pages") or {}) if isinstance(index, dict) and index.get("version") == PAGES_VERSION else {}
    pages: dict[str, dict] = {}
    data.topic_summaries = {}
    stats: dict[str, int] = {}
    sem = asyncio.Semaphore(config.GEMINI_CONCURRENCY)
    counts = Counter()

    async def one(tid: str) -> bool:
        children = {c: _intro(data.topic_summaries.get(c, "")) for c in topics[tid].get("children") or []}
        evidence = reports.build_evidence(tid, comm, projection, names, summaries, children)
        prompt = reports.build_report_prompt(evidence)
        key = hashlib.sha256(f"{config.MODEL}\n{prompt}".encode("utf-8")).hexdigest()
        path = os.path.join(topics_dir, f"{tid}.md")
        if (old.get(tid) or {}).get("key") == key:
            text = _read_text(path)
            if text and text.strip():
                data.topic_summaries[tid] = text
                pages[tid] = old[tid]
                counts["reused"] += 1
                return True
        try:
            async with sem:
                answer = await pipeline._generate(client, prompt, stats=stats)
        except Exception as e:
            pipeline._log_error(f"[Phase 5] topic page failed: {names.get(tid) or tid}: {e}")
            return False
        body, problems = reports.enforce_scaffolds(answer, evidence)
        grounding = reports.grounding_check(body, name_set, topic_names)
        page = reports.append_grounding(body, grounding)
        with open(path, "w", encoding="utf-8") as f:
            f.write(page)
        data.topic_summaries[tid] = page
        pages[tid] = {"key": key, "grounding": grounding, "scaffold_problems": problems}
        counts["written"] += 1
        return True

    failed: list[str] = []
    for level in (2, 1):
        pending = sorted(tid for tid, t in topics.items() if t["level"] == level)
        results = await asyncio.gather(*(one(tid) for tid in pending))
        retry = [tid for tid, ok in zip(pending, results) if not ok]
        for retry_round in range(1, MAX_PHASE_RETRIES + 1):
            if not retry:
                break
            cooldown = RETRY_COOLDOWNS[min(retry_round - 1, len(RETRY_COOLDOWNS) - 1)]
            pipeline._print(f"  [Phase 5] Waiting {cooldown}s, then retry round {retry_round}: "
                            f"{len(retry)} level-{level} pages")
            await pipeline._sleep(cooldown)
            results = await asyncio.gather(*(one(tid) for tid in retry))
            retry = [tid for tid, ok in zip(retry, results) if not ok]
        failed += retry

    _write_doc(os.path.join(topics_dir, "pages.json"), {"version": PAGES_VERSION, "pages": pages})
    grounding = reports.aggregate_grounding(p["grounding"] for p in pages.values())
    _write_doc(os.path.join(topics_dir, "metrics.json"), {
        "grounding": grounding, "structure": comm.data.get("metrics"),
        "scaffold_problems": sum(len(p.get("scaffold_problems") or []) for p in pages.values())})
    data.topics = community.topic_tree(comm, projection, names,
                                       {tid: page[:500] for tid, page in data.topic_summaries.items()})
    elapsed = time.perf_counter() - t0
    data.timings["phase5_topic_pages"] = elapsed
    pipeline._record_usage(data, "phase5_topic_pages", stats)
    data.topic_stats["pages"] = pipeline._step_stats(stats, elapsed)
    pipeline._write_topic_stats(data)
    rate = grounding.get("rate")
    pipeline._print(f"[Phase 5] {len(data.topic_summaries)}/{len(topics)} topic pages in "
                    f"{pipeline._fmt_elapsed(elapsed)}: {counts['written']} written, {counts['reused']} "
                    f"reused; {stats.get('calls', 0)} Gemini calls; identifiers found in the graph: "
                    + (f"{rate:.0%}" if rate is not None else "-")
                    + (f"; {len(failed)} failed (see error_log.txt)" if failed else ""))


def page_names(data) -> set[str]:
    """Phase 6 keeps the page of every current topic, subtopics included."""
    from .topics import community
    state = _current()
    if state is None or state.communities is None:
        return set()
    return {f"{tid}.md" for tid in community.all_topics(state.communities)}


def wiki_topic_rows(data, rows: Mapping[str, list[dict]]) -> tuple[list[dict], list[dict]]:
    """Phase 10's topic rows: the Leiden topics with their 4b names and page
    summaries. Phase 10 writes Topics.jsonl and FileBelongsToTopic.jsonl;
    the other three tables are written here (R20), and
    replace_topic_tables writes all five to Spanner."""
    from .topics import community
    state = _wiki_state(data)
    names = pipeline._read_json(os.path.join(_topics_dir(), "names.json"))
    summaries = {tid: page[:pipeline._SUMMARY_LIMIT] for tid, page in data.topic_summaries.items()}
    out = community.topic_rows(state.communities, _split_rows(rows)[0],
                               names=names if isinstance(names, dict) else None, summaries=summaries)
    state.rows = out
    for table in ("ClassBelongsToTopic", "TopicDependsOn", "TopicContainsTopic"):
        pipeline._write_jsonl(table, out[table])
    return out["Topics"], out["FileBelongsToTopic"]


# ===================================================================
# compare-topics (leiden spec §6, L-R27)
# ===================================================================


class _Usage(Exception):
    """Bad command-line input: exit 2."""


class _Refusal(Exception):
    """The runs cannot be compared (a missing step or another input): exit 4."""


def add_compare_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "compare-topics", help="Compare codedoc_go's LLM topics with the Leiden topics of one repo (no GCP)",
        description="Compare codedoc_go's LLM topics (topics/topic_tree.json) with this variant's Leiden "
                    "topics (topics/communities.json) of the same repo, commit, build configuration and "
                    "mode (leiden spec §6). Writes topic_comparison.json and topic_comparison.md.",
        epilog="Exit codes: 0 ok, 2 usage, 3 the Leiden backend (graspologic-native, networkx) cannot be "
               "imported, 4 the runs did not see the same input or lack a finished step.")
    p.add_argument("--llm", required=True, metavar="DIR",
                   help="codedoc_go's OUTPUT_DIR/repos/<repo> after analyze --all")
    p.add_argument("--leiden", required=True, metavar="DIR",
                   help="this variant's OUTPUT_DIR/repos/<repo> after analyze --all")
    p.add_argument("--llm2", metavar="DIR", help="a second codedoc_go run of the repo: LLM topic stability")
    p.add_argument("--leiden2", metavar="DIR",
                   help="a second run of this variant: rerun equality (default: Leiden runs again here)")
    p.add_argument("--features", metavar="FILE",
                   help="reviewed features {\"features\": [{\"name\", \"files\"}]}, e.g. "
                        "test_codes/iryo-kanri-system/ground_truth/features.json")
    p.add_argument("--out", metavar="DIR", help="where the report goes (default: the --leiden directory)")


def _need(path: str, kind: type, fix: str):
    doc = pipeline._read_json(path)
    if not isinstance(doc, kind):
        raise _Refusal(f"{path} is missing or unreadable: {fix}")
    return doc


def _run(directory: str, variant: str) -> dict:
    """The L-R27 description of one run: entity hashes, build
    configurations, module mode and the resolutions.json hash."""
    docs = {name: pipeline._read_json(os.path.join(directory, name))
            for name in ("entities.json", "units.json", "resolutions.json")}
    missing = [name for name, doc in docs.items() if not isinstance(doc, dict)]
    if missing:
        raise _Refusal(f"{directory} lacks {', '.join(missing)}: run {variant}'s analyze --graph "
                       "(or --all) for this repo first")
    return {"entities": docs["entities.json"].get("entities") or {},
            "build": docs["units.json"].get("builds"), "mode": docs["units.json"].get("mode"),
            "resolutions_hash": pipeline.resolutions_hash(docs["resolutions.json"])}


def _check_leiden_run(directory: str, communities: Mapping, run: Mapping) -> None:
    """A leiden run's files are those of one finished --graph run: its
    graph_checkpoint.json is complete and holds the hashes of this
    communities.json and resolutions.json and the file hashes entities.json
    records."""
    cp = pipeline._read_json(os.path.join(directory, pipeline._CHECKPOINT))
    if not isinstance(cp, dict) or not cp.get("complete"):
        raise _Refusal(f"{directory} has no finished --graph run (graph_checkpoint.json is missing or "
                       "incomplete): run codedoc_go_leiden's analyze --graph for this repo again")
    entities = {p: e.get("sha256") if isinstance(e, Mapping) else e for p, e in run["entities"].items()}
    stale = [name for name, ok in (
        ("topics/communities.json", cp.get("communities_hash") == document_hash(communities)),
        ("resolutions.json", cp.get("resolutions_hash") == run["resolutions_hash"]),
        ("entities.json", cp.get("file_hashes") == entities)) if not ok]
    if stale:
        raise _Refusal(f"{directory}: {', '.join(stale)} not from the --graph run graph_checkpoint.json "
                       "records: run codedoc_go_leiden's analyze --graph for this repo again")


def _settings_mismatches(doc: Mapping, doc2: Mapping) -> list[str]:
    """Topic settings two Leiden runs must share to be one rerun of the other."""
    keys = ("profile", "id_prefix", "repo")
    return ([f"{k} differs: {doc.get(k)!r} vs {doc2.get(k)!r}" for k in keys if doc.get(k) != doc2.get(k)]
            + (["the topic settings (TOPIC_*, SUBTOPIC_MIN_ATOMS) differ"]
               if doc.get("params") != doc2.get("params") else []))


def _check_second_run(flag: str, directory: str, problems: list[str]) -> None:
    if problems:
        raise _Refusal(f"--{flag} {directory} is not a second run on the same input: " + "; ".join(problems))


def _graph_rows_of(directory: str) -> dict[str, list[dict]]:
    rows_dir = os.path.join(directory, "graph_rows")
    rows: dict[str, list[dict]] = {}
    for name in sorted(os.listdir(rows_dir)) if os.path.isdir(rows_dir) else []:
        if name.endswith(".jsonl"):
            with open(os.path.join(rows_dir, name), encoding="utf-8") as f:
                rows[name[:-len(".jsonl")]] = [json.loads(line) for line in f if line.strip()]
    if not rows.get("Files"):
        raise _Refusal(f"{rows_dir} holds no graph rows: run codedoc_go_leiden's analyze --graph first")
    return rows


def _pages(directory: str) -> dict[str, str]:
    """A run's topic pages. reports.grounding_check skips the grounding
    section Phase 5 appends, so pages are compared as written."""
    topics_dir = os.path.join(directory, "topics")
    return {name: _read_text(os.path.join(topics_dir, name)) or ""
            for name in (sorted(os.listdir(topics_dir)) if os.path.isdir(topics_dir) else [])
            if name.endswith(".md") and not name.startswith("topic_comparison")}


def _compare(args) -> tuple[dict, str]:
    from .topics import community, compare, graph_builder
    for flag in ("llm", "leiden", "llm2", "leiden2"):
        value = getattr(args, flag)
        if value is not None and not os.path.isdir(value):
            raise _Usage(f"--{flag} {value} is not a directory")
    features = None
    if args.features:
        features = pipeline._read_json(args.features)
        if not isinstance(features, dict):
            raise _Usage(f"--features {args.features} is not a readable JSON object")
    llm_tree = _need(os.path.join(args.llm, "topics", "topic_tree.json"), list,
                     "run codedoc_go's analyze --wiki (or --all) for this repo first")
    doc = _need(os.path.join(args.leiden, "topics", "communities.json"), dict,
                "run codedoc_go_leiden's analyze --graph (or --all) for this repo first")
    llm_run, leiden_run = _run(args.llm, "codedoc_go"), _run(args.leiden, "codedoc_go_leiden")
    _check_leiden_run(args.leiden, doc, leiden_run)
    nodes, edges = _split_rows(_graph_rows_of(args.leiden))
    llm_tree2 = None
    if args.llm2:
        llm_tree2 = _need(os.path.join(args.llm2, "topics", "topic_tree.json"), list,
                          "run codedoc_go's analyze --wiki for the second run first")
        _check_second_run("llm2", args.llm2, compare.run_mismatches(llm_run, _run(args.llm2, "codedoc_go")))
    projection = graph_builder.build_projection(nodes, edges, doc.get("profile") or "strict")
    if projection.hash != doc.get("projection_hash"):
        raise _Refusal(f"{args.leiden}/graph_rows do not give the projection topics/communities.json was "
                       "built from: run codedoc_go_leiden's analyze --graph for this repo again")
    if args.leiden2:
        communities2 = _need(os.path.join(args.leiden2, "topics", "communities.json"), dict,
                             "run codedoc_go_leiden's analyze --graph for the second run first")
        leiden2_run = _run(args.leiden2, "codedoc_go_leiden")
        _check_leiden_run(args.leiden2, communities2, leiden2_run)
        _check_second_run("leiden2", args.leiden2, compare.run_mismatches(leiden_run, leiden2_run)
                          + _settings_mismatches(doc, communities2))
    else:
        params = {k: doc["params"][k] for k in compare.ABLATION_PARAMS if k in doc.get("params", {})}
        communities2 = community.detect(projection, {**params, "repo": doc.get("repo") or "",
                                                     "id_prefix": doc.get("id_prefix") or "a"}).data
    names_doc = pipeline._read_json(os.path.join(args.leiden, "topics", "names.json"))
    names = {tid: e["name"] for tid, e in ((names_doc or {}).get("names") or {}).items()
             if isinstance(e, dict) and e.get("name")} if isinstance(names_doc, dict) else {}
    stats = {side: pipeline._read_json(os.path.join(d, "topics", "stats.json"))
             for side, d in (("llm", args.llm), ("leiden", args.leiden))}
    result = compare.compare_topics(
        llm_tree, doc, nodes, edges, features, llm_tree2, leiden_names=names, communities2=communities2,
        llm_stats=stats["llm"] if isinstance(stats["llm"], dict) else None,
        leiden_stats=stats["leiden"] if isinstance(stats["leiden"], dict) else None,
        llm_pages=_pages(args.llm), leiden_pages=_pages(args.leiden),
        projection=projection, llm_run=llm_run, leiden_run=leiden_run)
    return result, compare.render_markdown(result)


def _exit(message: str, code: int):
    sys.stdout.flush()
    print(message, file=sys.stderr)
    sys.exit(code)


def cmd_compare_topics(args) -> None:
    """compare-topics: the report of leiden spec §6 for one repo. The report
    goes to the --leiden directory by default, not to topics/, where Phase 6
    deletes every .md that is not a topic page."""
    try:
        from .topics import community, compare
    except ImportError as exc:
        _exit(f"compare-topics needs the Leiden backend: {exc.name or exc} cannot be imported. "
              f"Install it: {_INSTALL_FIX}", 3)
    try:
        result, markdown = _compare(args)
    except _Usage as exc:
        _exit(f"Error: {exc}", 2)
    except (_Refusal, compare.CompareInputError) as exc:
        _exit(f"compare-topics refused: {exc}", 4)
    except community.BackendMissingError as exc:
        _exit(f"compare-topics needs the Leiden backend: {exc}", 3)
    out = args.out or args.leiden
    os.makedirs(out, exist_ok=True)
    json_path, md_path = os.path.join(out, "topic_comparison.json"), os.path.join(out, "topic_comparison.md")
    _write_doc(json_path, result)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(markdown)
    i, top = result["inputs"], result["agreement"]["top"]
    grounding = {side: (g or {}).get("rate") for side, g in result["grounding"].items()}
    shown = {side: f"{rate:.0%}" if rate is not None else "-" for side, rate in grounding.items()}
    print(f"[compare-topics] {i.get('repo')}: LLM {i['llm_topics']} topics, Leiden {i['leiden_topics']} "
          f"topics ({i['leiden_leaves']} leaves), {i['eligible_atoms']} atoms")
    print(f"  Agreement on {top['files']} files with one topic each: AMI {top['ami']}, ARI {top['ari']}")
    print(f"  Identifiers found on the topic pages: LLM {shown['llm']}, Leiden {shown['leiden']}")
    print(f"  Wrote {json_path} and {md_path}")


def phase_labels(labels: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """codedoc_go's timing labels with Phase 4a, the topic tables, 4b and the
    Phase 5 reports."""
    out = []
    for key, label in labels:
        if key == "phase4_topics":
            out.append(("phase4b_naming", "Phase 4b: Topic names"))
            continue
        out.append((key, "Phase 5: Topic reports" if key == "phase5_topic_pages" else label))
        if key == "phase1c_resolve":
            out.append(("phase4a_communities", "Phase 4a: Leiden communities"))
        elif key == "phase9_write_edges":
            out.append(("phase9_write_topics", "Phase 9: Write topic tables"))
    return out
